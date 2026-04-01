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
from subagents import AGENTS

logger = logging.getLogger(__name__)

# Only enable SDK debug logging if SDK_DEBUG is set
sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
if sdk_debug:
    logging.getLogger("claude_agent_sdk").setLevel(logging.DEBUG)
else:
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


# We recreate the ObservabilityManager logic here inside the sandbox
def setup_langfuse_hooks() -> dict:
    span_id = os.getenv("CURRENT_SPAN_ID")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
        process = None
        try:
            hook_payload = json.dumps(input_data)
            process = await asyncio.create_subprocess_exec(
                "python3",
                "/app/hooks/langfuse_hook.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "TRACE_TO_LANGFUSE": "true",
                    "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                    "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                    "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                    "LANGFUSE_BASE_URL": os.getenv(
                        "LANGFUSE_HOST", "http://langfuse:3000"
                    ),
                    "CC_LANGFUSE_DEBUG": "true",
                    "PARENT_SPAN_ID": span_id or "",
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/home/bot"),
                },
            )

            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=hook_payload.encode()),
                    timeout=float(os.getenv("LANGFUSE_HOOK_TIMEOUT", "30.0")),
                )
                if process.returncode != 0:
                    logger.warning(f"Langfuse hook failed: {stderr.decode()}")
                else:
                    return {"success": True}
            except TimeoutError:
                logger.warning("Langfuse hook timed out after 30s")
                process.kill()
                await process.wait()

        except Exception as e:
            logger.warning(f"Error running Langfuse hook: {e}")
            error_msg = str(e)
        finally:
            # Ensure process is cleaned up if it exists and hasn't been waited on
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass  # Expected - process already terminated
                except OSError as e:
                    logger.warning(f"Failed to cleanup Langfuse hook process: {e}")
                except Exception as e:
                    logger.error(
                        f"Unexpected error cleaning up Langfuse hook process: {e}",
                        exc_info=True,
                    )

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def _enqueue_memory_job(repo: str, transcript_path: str, hook_event: str) -> None:
    """Copy transcript to persistent volume and enqueue a memory extraction job.

    Runs inside the Stop/SubagentStop hook — fires even if the main agent fails,
    so memory is captured from crashed sessions too.
    """
    import shutil

    transcript_file = Path(transcript_path)
    if not transcript_file.exists():
        logger.warning(f"Memory hook: transcript not found: {transcript_path}")
        return

    # Persist transcript to the agent-memory volume so memory_worker can read it
    # after the ephemeral workspace is cleaned up.
    transcripts_dir = f"/home/bot/agent-memory/{repo}/transcripts"
    os.makedirs(transcripts_dir, exist_ok=True)
    persistent_path = os.path.join(transcripts_dir, transcript_file.name)
    try:
        shutil.copy2(transcript_path, persistent_path)
        logger.info(f"Transcript persisted: {persistent_path}")
    except Exception as e:
        logger.warning(f"Failed to persist transcript for {repo}: {e}")
        return  # Don't enqueue if we can't persist the file

    # Publish to memory extraction queue — memory_worker will pick this up
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
                    "transcript_path": persistent_path,
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
    workspace: str,  # Add workspace parameter
    system_context: str | None = None,  # Add system context parameter
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

    # On Stop/SubagentStop: persist transcript to the agent-memory volume and enqueue
    # a memory extraction job. Runs inside the hook so it fires even on failures/timeouts.
    async def capture_and_enqueue_memory(input_data, _tool_use_id, _context):
        transcript = (
            input_data.get("agent_transcript_path")
            or input_data.get("transcriptPath")
            or input_data.get("transcript_path")
        )
        if transcript:
            event = input_data.get("hook_event_name", "Stop")
            logger.debug(f"Memory hook triggered: {transcript} ({event})")
            # Fire-and-forget — don't block the hook response
            asyncio.create_task(_enqueue_memory_job(repo, transcript, event))
        return {"success": True}

    for event in ("Stop", "SubagentStop"):
        if event in hooks:
            hooks[event].append(
                HookMatcher(matcher="*", hooks=[capture_and_enqueue_memory])
            )
        else:
            hooks[event] = [
                HookMatcher(matcher="*", hooks=[capture_and_enqueue_memory])
            ]

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
        plugins=plugins,  # Explicitly pass plugins
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
