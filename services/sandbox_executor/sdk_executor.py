import asyncio
import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
)

from shared import SDKError, SDKTimeoutError
from shared.langfuse_hooks import setup_langfuse_hooks as _setup_langfuse_hooks
from subagents import AGENTS

logger = logging.getLogger(__name__)

# Only enable SDK debug logging if SDK_DEBUG is set
sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
if sdk_debug:
    logging.getLogger("claude_agent_sdk").setLevel(logging.DEBUG)
else:
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


def setup_langfuse_hooks() -> dict:
    return _setup_langfuse_hooks(parent_span_id=os.getenv("CURRENT_SPAN_ID"))


async def _enqueue_retrospector_job(
    repo: str,
    transcript_path: str,
    hook_event: str,
    workflow_name: str | None,
    session_meta: dict,
) -> None:
    """Enqueue a retrospection job — fires after Stop/SubagentStop hooks.

    Only the Stop event (main agent) is relevant for instruction improvement.
    SubagentStop events are skipped — the main transcript captures everything.
    """
    if hook_event != "Stop":
        return

    try:
        import redis.asyncio as aioredis

        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        redis_password = os.getenv("REDIS_PASSWORD")
        rc = await aioredis.from_url(
            redis_url, decode_responses=True, password=redis_password
        )
        try:
            payload = json.dumps(
                {
                    "repo": repo,
                    "transcript_path": transcript_path,
                    "hook_event": hook_event,
                    "workflow_name": workflow_name,
                    "session_meta": session_meta,
                }
            )
            await rc.rpush("agent:retrospector:requests", payload)
            logger.info(
                f"Enqueued retrospector job for {repo} "
                f"[{workflow_name or 'unknown'}] [{hook_event}]"
            )
        finally:
            await rc.close()
    except Exception as e:
        logger.warning(f"Failed to enqueue retrospector job for {repo}: {e}")


async def _stage_transcript(
    repo: str,
    transcript_path: str,
    consumers: int,
    redis_url: str,
    redis_password: str | None,
) -> str | None:
    """Move transcript to the shared transcripts volume and set a ref counter.

    The file is moved (not copied) so there is only ever one copy.
    Each consumer decrements the counter when done; the last one deletes the file.
    The counter has a 24 h safety TTL in case a worker crashes before cleanup.

    Returns the staged path on success, None on failure.
    """
    import shutil

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        logger.warning(f"Transcript not found, cannot stage: {transcript_path}")
        return None

    staged_dir = f"/home/bot/transcripts/{repo}"
    os.makedirs(staged_dir, exist_ok=True)
    staged_path = os.path.join(staged_dir, transcript_file.name)
    try:
        shutil.copy2(transcript_path, staged_path)
        logger.info(f"Transcript staged: {staged_path}")
    except Exception as e:
        logger.warning(f"Failed to stage transcript for {repo}: {e}")
        return None

    # Set ref counter so workers can coordinate deletion
    try:
        import redis.asyncio as aioredis

        rc = await aioredis.from_url(
            redis_url, decode_responses=True, password=redis_password
        )
        try:
            ref_key = f"agent:transcript:ref:{transcript_file.stem}"
            await rc.set(ref_key, consumers, ex=86400)
        finally:
            await rc.close()
    except Exception as e:
        logger.warning(f"Failed to set transcript ref counter: {e}")

    return staged_path


async def _enqueue_memory_job(repo: str, transcript_path: str, hook_event: str) -> None:
    """Enqueue a memory extraction job for an already-persisted transcript."""
    try:
        import redis.asyncio as aioredis

        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        redis_password = os.getenv("REDIS_PASSWORD")
        rc = await aioredis.from_url(
            redis_url, decode_responses=True, password=redis_password
        )
        try:
            payload = json.dumps(
                {
                    "repo": repo,
                    "transcript_path": transcript_path,
                    "hook_event": hook_event,
                }
            )
            await rc.rpush("agent:memory:requests", payload)
            logger.info(f"Enqueued memory job for {repo} [{hook_event}]")
        finally:
            await rc.close()
    except Exception as e:
        logger.warning(f"Failed to enqueue memory job for {repo}: {e}")


async def execute_sandbox_request(
    prompt: str,
    github_token: str,
    repo: str,
    issue_number: int,
    user: str,
    auto_review: bool,
    auto_triage: bool,
    workspace: str,
    system_context: str | None = None,
    workflow_name: str | None = None,
) -> str:
    """Execute the Claude Agent SDK inside the sandbox"""

    # 1. Setup Environment
    # The sandbox executor container is given ANTHROPIC_API_KEY globally in docker-compose.
    # We don't need to manually configure it as the SDK picks it up.

    # Use provided workspace as working directory
    working_dir = workspace
    os.environ["CLAUDE_TEMP_DIR"] = working_dir
    os.environ["TMPDIR"] = working_dir

    # Inject GitHub token for tools/plugins to use
    # The token is passed via execute_sandbox_request parameter and set here
    # It's also needed by MCP servers defined in plugins
    os.environ["GITHUB_TOKEN"] = github_token

    # Log token availability for debugging (first/last chars only)
    if github_token:
        logger.info(
            f"GitHub token available: {github_token[:10]}...{github_token[-10:]}"
        )
    else:
        logger.warning("No GitHub token provided to sandbox executor")

    # 2. Build Options (Equivalent of MCPConfigurationBuilder)
    mcp_servers = {
        "github": {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": f"Bearer {github_token}"},
        },
        "github-actions": {
            "type": "stdio",
            "command": "python",
            "args": [
                os.path.expanduser(
                    "~/.claude/plugins/ci-failure-toolkit/servers/github_actions_server.py"
                )
            ],
            "env": {
                "PYTHONPATH": os.path.expanduser(
                    "~/.claude/plugins/ci-failure-toolkit"
                ),
                "GITHUB_TOKEN": github_token,
            },
        },
        "memory": {
            "type": "stdio",
            "command": "python3",
            "args": ["/app/mcp_servers/memory/server.py"],
            "env": {
                "GITHUB_REPOSITORY": repo,
                "PYTHONPATH": "/app",
            },
        },
    }

    # Plugin MCP servers are auto-discovered from ~/.claude/plugins/*/.mcp.json
    # via setting_sources=["user", "project", "local"]

    hooks = setup_langfuse_hooks()

    memory_enabled = os.getenv("MEMORY_WORKER_ENABLED", "true").lower() == "true"
    retrospector_enabled = os.getenv("RETROSPECTOR_ENABLED", "true").lower() == "true"
    consumers = (1 if memory_enabled else 0) + (1 if retrospector_enabled else 0)

    _redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    _redis_password = os.getenv("REDIS_PASSWORD")

    # On Stop/SubagentStop: stage transcript and enqueue enabled post-processing jobs.
    # Runs inside the hook so it fires even on failures/timeouts.
    async def capture_and_enqueue(input_data, _tool_use_id, _context):
        transcript = (
            input_data.get("agent_transcript_path")
            or input_data.get("transcriptPath")
            or input_data.get("transcript_path")
        )
        if transcript:
            event = input_data.get("hook_event_name", "Stop")
            logger.debug(f"Post-session hook triggered: {transcript} ({event})")
            # Move to shared volume once — both workers get the same path.
            staged_path = await _stage_transcript(
                repo, transcript, consumers, _redis_url, _redis_password
            )
            if not staged_path:
                return {"success": True}
            if memory_enabled:
                asyncio.create_task(_enqueue_memory_job(repo, staged_path, event))
            if retrospector_enabled:
                asyncio.create_task(
                    _enqueue_retrospector_job(
                        repo,
                        staged_path,
                        event,
                        workflow_name,
                        {
                            "num_turns": input_data.get("num_turns", 0),
                            "is_error": input_data.get("is_error", False),
                            "duration_ms": input_data.get("duration_ms", 0),
                        },
                    )
                )
        return {"success": True}

    if memory_enabled or retrospector_enabled:
        for event in ("Stop", "SubagentStop"):
            if event in hooks:
                hooks[event].append(
                    HookMatcher(matcher="*", hooks=[capture_and_enqueue])
                )
            else:
                hooks[event] = [HookMatcher(matcher="*", hooks=[capture_and_enqueue])]

    # Load plugins and skills from ~/.claude/ using setting_sources
    # Get model from environment variable
    model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514")

    # Callback to capture stderr from SDK CLI
    def stderr_callback(message: str) -> None:
        """Log stderr output from Claude CLI."""
        logger.warning(f"SDK CLI stderr: {message}")

    # Explicitly load plugins from ~/.claude/plugins/
    # setting_sources alone doesn't load plugins, we need to pass them explicitly
    plugins_dir = os.path.expanduser("~/.claude/plugins")
    plugins = []
    if os.path.exists(plugins_dir):
        for plugin_name in os.listdir(plugins_dir):
            plugin_path = os.path.join(plugins_dir, plugin_name)
            if os.path.isdir(plugin_path) and not plugin_name.startswith("."):
                plugins.append({"type": "local", "path": plugin_path})
                logger.info(f"Loading plugin: {plugin_name} from {plugin_path}")

    options = ClaudeAgentOptions(
        model=model,  # Specify the model to use
        allowed_tools=[
            "Task",
            "Skill",
            "Bash",
            "Read",
            "Write",
            "Edit",
            "List",
            "Search",
            "Grep",
            "Glob",
            "mcp__github__*",
            "mcp__github-actions__*",  # Allow GitHub Actions tools
            "mcp__memory__memory_read",  # Read-only memory access (writes handled by memory_worker)
        ],
        permission_mode="acceptEdits",
        mcp_servers=mcp_servers,  # type: ignore[arg-type]
        agents=AGENTS,
        setting_sources=[
            "user",
            "project",
            "local",
        ],  # Load skills and config from ~/.claude/
        plugins=plugins,  # type: ignore[arg-type]
        hooks=hooks,
        cwd=working_dir,  # Set working directory for SDK operations
        add_dirs=[
            f"/home/bot/agent-memory/{repo}/memory"
        ],  # Allow writes to memory directory
        stderr=stderr_callback,  # Capture stderr output for debugging
        system_prompt=system_context,  # Add system context as system prompt
    )

    # 3. Execute
    logger.info("Starting sandbox SDK execution...")
    logger.info(f"Model: {model}")
    logger.info(f"Prompt length: {len(prompt)} characters")

    # Only show detailed info if SDK_DEBUG is enabled
    sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
    if sdk_debug:
        logger.debug(f"Prompt preview: {prompt[:200]}...")
        logger.debug(f"Working directory: {working_dir}")
        logger.debug(f"Setting sources: {options.setting_sources}")
        logger.debug(f"Allowed tools: {options.allowed_tools}")
        logger.debug(f"MCP servers configured: {list(mcp_servers.keys())}")

        # Verify we can access the working directory
        try:
            files = os.listdir(working_dir)
            logger.debug(f"Working directory contains {len(files)} items")
            logger.debug(f"First 10 items: {files[:10]}")
        except Exception as e:
            logger.error(f"Cannot access working directory: {e}")

    response_parts = []

    try:
        # Get SDK timeout from environment (default: 30 minutes)
        sdk_timeout = int(os.getenv("SDK_EXECUTION_TIMEOUT", "1800"))
        async with asyncio.timeout(sdk_timeout):
            async with ClaudeSDKClient(options=options) as client:
                logger.info("SDK client created, sending query...")
                await client.query(prompt)
                logger.info("Waiting for SDK response...")

                async for message in client.receive_messages():
                    if sdk_debug:
                        logger.debug(f"Received message type: {type(message).__name__}")

                    if isinstance(message, AssistantMessage):
                        logger.info(
                            f"Received response with {len(message.content)} blocks"
                        )
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                                if sdk_debug:
                                    logger.debug(
                                        f"Text block content: {block.text[:200]}..."
                                    )
                    elif isinstance(message, ResultMessage):
                        logger.info(
                            f"SDK completed - {message.num_turns} turns, {message.duration_ms}ms"
                        )
                        if sdk_debug:
                            logger.debug(
                                f"ResultMessage details: is_error={message.is_error}, subtype={message.subtype}"
                            )
                        break
                    elif sdk_debug:
                        # Log any other message types only in debug mode
                        logger.debug(
                            f"Received other message type: {type(message).__name__}"
                        )
                        if hasattr(message, "__dict__"):
                            logger.debug(f"Message content: {message.__dict__}")

    except TimeoutError as e:
        raise SDKTimeoutError(
            "Claude Agent SDK execution timed out after 30 minutes in sandbox"
        ) from e
    except Exception as e:
        raise SDKError(f"Failed to execute Claude Agent SDK in sandbox: {e}") from e

    response = "\n".join(response_parts)
    logger.info(
        f"Collected {len(response_parts)} response parts, total length: {len(response)} chars"
    )

    if not response or not response.strip():
        raise SDKError("Claude Agent SDK returned empty response in sandbox")

    logger.info("Sandbox SDK completed successfully")
    return response
