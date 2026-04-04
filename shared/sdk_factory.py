"""Factory for building Claude Agent SDK options with composable configuration.

This module provides a builder pattern for constructing ClaudeAgentOptions
with sensible defaults and flexible customization for different worker types.
"""

import json
import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from shared.langfuse_hooks import setup_langfuse_hooks

logger = logging.getLogger(__name__)


class SDKOptionsBuilder:
    """Composable builder for ClaudeAgentOptions.

    Usage:
        options = (
            SDKOptionsBuilder(cwd="/workspace")
            .with_sonnet()
            .with_github_mcp(token)
            .with_memory_mcp(repo)
            .with_full_toolset()
            .with_agents(AGENTS)
            .build()
        )
    """

    def __init__(self, cwd: str):
        """Initialize builder with working directory.

        Args:
            cwd: Working directory for SDK operations
        """
        self.cwd = cwd
        self._model: str | None = None
        self._allowed_tools: list[str] = []
        self._mcp_servers: dict = {}
        self._plugins: list[dict] = []
        self._hooks: dict = {}
        self._add_dirs: list[str] = []
        self._system_prompt: str | None = None
        self._agents: dict | None = None

    # Model selection methods

    def with_model(self, model: str) -> "SDKOptionsBuilder":
        """Set a specific model.

        Args:
            model: Model identifier (e.g., "claude-sonnet-4-20250514")

        Returns:
            Self for method chaining
        """
        self._model = model
        return self

    def with_sonnet(self) -> "SDKOptionsBuilder":
        """Use Sonnet model from environment (default for main agent work).

        Returns:
            Self for method chaining
        """
        self._model = os.getenv(
            "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
        )
        return self

    def with_haiku(self) -> "SDKOptionsBuilder":
        """Use Haiku model from environment (default for lightweight tasks).

        Returns:
            Self for method chaining
        """
        self._model = os.getenv(
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001"
        )
        return self

    # MCP server methods (à la carte)

    def with_github_mcp(self, token: str) -> "SDKOptionsBuilder":
        """Add GitHub MCP server for GitHub API operations.

        Args:
            token: GitHub authentication token

        Returns:
            Self for method chaining
        """
        self._mcp_servers["github"] = {
            "type": "http",
            "url": "https://api.githubcopilot.com/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
        }
        return self

    def with_github_actions_mcp(self, token: str) -> "SDKOptionsBuilder":
        """Add GitHub Actions MCP server for CI/CD operations.

        Args:
            token: GitHub authentication token

        Returns:
            Self for method chaining
        """
        self._mcp_servers["github-actions"] = {
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
                "GITHUB_TOKEN": token,
            },
        }
        return self

    def with_memory_mcp(self, repo: str) -> "SDKOptionsBuilder":
        """Add memory MCP server for repository memory operations.

        Args:
            repo: Repository identifier (e.g., "owner/repo")

        Returns:
            Self for method chaining
        """
        self._mcp_servers["memory"] = {
            "type": "stdio",
            "command": "python3",
            "args": ["/app/mcp_servers/memory/server.py"],
            "env": {
                "GITHUB_REPOSITORY": repo,
                "PYTHONPATH": "/app",
            },
        }
        return self

    # Plugin methods (à la carte)

    def with_auto_discovered_plugins(self) -> "SDKOptionsBuilder":
        """Auto-discover and load all plugins from ~/.claude/plugins/.

        Returns:
            Self for method chaining
        """
        plugins_dir = os.path.expanduser("~/.claude/plugins")
        if os.path.exists(plugins_dir):
            for plugin_name in os.listdir(plugins_dir):
                plugin_path = os.path.join(plugins_dir, plugin_name)
                if os.path.isdir(plugin_path) and not plugin_name.startswith("."):
                    self._plugins.append({"type": "local", "path": plugin_path})
                    logger.info(f"Loading plugin: {plugin_name} from {plugin_path}")
        return self

    def with_plugin(self, path: str) -> "SDKOptionsBuilder":
        """Add a specific plugin by path.

        Args:
            path: Absolute path to plugin directory

        Returns:
            Self for method chaining
        """
        self._plugins.append({"type": "local", "path": path})
        logger.info(f"Loading plugin from {path}")
        return self

    # Tool methods (à la carte or presets)

    def with_tools(self, *tools: str) -> "SDKOptionsBuilder":
        """Add specific tools to the allowed tools list.

        Args:
            *tools: Tool names or patterns (e.g., "Read", "mcp__github__*")

        Returns:
            Self for method chaining
        """
        self._allowed_tools.extend(tools)
        return self

    def with_full_toolset(self) -> "SDKOptionsBuilder":
        """Add full toolset for sandbox executor (main agent work).

        Includes: Task, Skill, Bash, Read, Write, Edit, List, Search, Grep, Glob,
        all GitHub MCP tools, all GitHub Actions MCP tools, memory read-only.

        Returns:
            Self for method chaining
        """
        return self.with_tools(
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
            "mcp__github-actions__*",
            "mcp__memory__memory_read",
        )

    def with_retrospector_toolset(self) -> "SDKOptionsBuilder":
        """Add toolset for retrospector worker (instruction analysis).

        Includes: Skill, Bash, Glob, Grep, Read, Write, Edit, GitHub MCP tools.

        Returns:
            Self for method chaining
        """
        return self.with_tools(
            "Skill",
            "Bash",
            "Glob",
            "Grep",
            "Read",
            "Write",
            "Edit",
            "mcp__github__*",
        )

    def with_memory_toolset(self) -> "SDKOptionsBuilder":
        """Add toolset for memory worker (memory extraction).

        Includes: Read, Write, Edit, List, all memory MCP tools.

        Returns:
            Self for method chaining
        """
        return self.with_tools("Read", "Write", "Edit", "List", "mcp__memory__*")

    # Subagent methods

    def with_agents(self, agents: dict) -> "SDKOptionsBuilder":
        """Add subagent definitions for delegation.

        Args:
            agents: Dictionary of subagent definitions

        Returns:
            Self for method chaining
        """
        self._agents = agents
        return self

    # Hook methods

    def with_langfuse_hooks(
        self, parent_span_id: str | None = None
    ) -> "SDKOptionsBuilder":
        """Add Langfuse observability hooks.

        Args:
            parent_span_id: Optional parent span ID for tracing

        Returns:
            Self for method chaining
        """
        self._hooks.update(setup_langfuse_hooks(parent_span_id=parent_span_id))
        return self

    def with_transcript_staging(
        self, repo: str, workflow_name: str | None = None
    ) -> "SDKOptionsBuilder":
        """Add post-session hooks for transcript staging and job enqueueing.

        This hook stages transcripts to the shared volume and enqueues
        memory extraction and retrospection jobs after agent sessions complete.

        Args:
            repo: Repository identifier (e.g., "owner/repo")
            workflow_name: Optional workflow name for retrospection context

        Returns:
            Self for method chaining
        """
        memory_enabled = os.getenv("MEMORY_WORKER_ENABLED", "true").lower() == "true"
        retrospector_enabled = (
            os.getenv("RETROSPECTOR_ENABLED", "true").lower() == "true"
        )

        async def capture_and_enqueue(input_data, _tool_use_id, _context):
            """Stage transcript and enqueue post-processing jobs."""
            transcript = (
                input_data.get("agent_transcript_path")
                or input_data.get("transcriptPath")
                or input_data.get("transcript_path")
            )
            if transcript:
                event = input_data.get("hook_event_name", "Stop")
                logger.debug(f"Post-session hook triggered: {transcript} ({event})")

                # Copy to shared volume for post-processing workers
                staged_path = await _stage_transcript(repo, transcript)
                if not staged_path:
                    logger.error(
                        f"Failed to stage transcript {transcript}, "
                        "skipping post-processing"
                    )
                    return {"success": True}

                # Enqueue jobs with error handling
                if memory_enabled:
                    try:
                        await _enqueue_memory_job(repo, staged_path, event)
                    except Exception as e:
                        logger.error(
                            f"Failed to enqueue memory job: {e}", exc_info=True
                        )

                if retrospector_enabled:
                    try:
                        await _enqueue_retrospector_job(
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
                    except Exception as e:
                        logger.error(
                            f"Failed to enqueue retrospector job: {e}", exc_info=True
                        )
            return {"success": True}

        if memory_enabled or retrospector_enabled:
            for event in ("Stop", "SubagentStop"):
                if event in self._hooks:
                    self._hooks[event].append(
                        HookMatcher(matcher="*", hooks=[capture_and_enqueue])
                    )
                else:
                    self._hooks[event] = [
                        HookMatcher(matcher="*", hooks=[capture_and_enqueue])
                    ]

        return self

    # Directory methods

    def with_writable_dir(self, path: str) -> "SDKOptionsBuilder":
        """Allow SDK to write to a specific directory.

        Args:
            path: Absolute path to directory

        Returns:
            Self for method chaining
        """
        self._add_dirs.append(path)
        return self

    # System prompt methods

    def with_system_prompt(self, prompt: str | None) -> "SDKOptionsBuilder":
        """Set system context/prompt for the agent.

        Args:
            prompt: System prompt text (None to skip)

        Returns:
            Self for method chaining
        """
        if prompt:
            self._system_prompt = prompt
        return self

    # Build method

    def build(self) -> ClaudeAgentOptions:
        """Build the final ClaudeAgentOptions object.

        Returns:
            Configured ClaudeAgentOptions instance
        """
        # Default to Sonnet if no model specified
        if not self._model:
            self._model = os.getenv(
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
            )

        return ClaudeAgentOptions(
            model=self._model,
            allowed_tools=self._allowed_tools,
            permission_mode="acceptEdits",
            mcp_servers=self._mcp_servers,  # type: ignore[arg-type]
            agents=self._agents,
            setting_sources=["user", "project", "local"],
            plugins=self._plugins,  # type: ignore[arg-type]
            hooks=self._hooks,
            cwd=self.cwd,
            add_dirs=self._add_dirs,  # type: ignore[arg-type]
            stderr=lambda msg: logger.warning(f"SDK stderr: {msg}"),
            system_prompt=self._system_prompt,
        )


# Helper functions for transcript staging (used by with_transcript_staging)


async def _stage_transcript(repo: str, transcript_path: str) -> str | None:
    """Copy transcript to the shared transcripts volume for post-processing workers.

    The transcript is persisted permanently for future analysis and debugging.

    Args:
        repo: Repository identifier
        transcript_path: Path to transcript file

    Returns:
        Staged path on success, None on failure
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

    return staged_path


async def _enqueue_memory_job(repo: str, transcript_path: str, hook_event: str) -> None:
    """Enqueue a memory extraction job for an already-persisted transcript.

    Args:
        repo: Repository identifier
        transcript_path: Path to staged transcript
        hook_event: Hook event name (Stop or SubagentStop)
    """
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


async def _enqueue_retrospector_job(
    repo: str,
    transcript_path: str,
    hook_event: str,
    workflow_name: str | None,
    session_meta: dict,
) -> None:
    """Enqueue a retrospection job — fires after Stop/SubagentStop hooks.

    Both Stop (main agent) and SubagentStop events trigger retrospection.
    Each subagent session gets its own analysis to improve subagent instructions.

    Args:
        repo: Repository identifier
        transcript_path: Path to staged transcript
        hook_event: Hook event name (Stop or SubagentStop)
        workflow_name: Workflow name for context
        session_meta: Session metadata (num_turns, is_error, duration_ms)
    """
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
