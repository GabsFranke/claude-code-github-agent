"""Factory for building Claude Agent SDK options with composable configuration.

This module provides a builder pattern for constructing ClaudeAgentOptions
with sensible defaults and flexible customization for different worker types.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from shared.langfuse_hooks import setup_langfuse_hooks
from shared.post_processing import flush_pending_post_jobs as _flush_pending_post_jobs

logger = logging.getLogger(__name__)

# Total system prompt budget in tokens
SYSTEM_PROMPT_BUDGET = 12_000


def _discover_host_mcp_names(cwd: str | None = None) -> list[str]:
    """Read MCP server names from ~/.claude.json for allowed_tools."""
    claude_json = Path.home() / ".claude.json"
    if not claude_json.exists():
        return []
    try:
        data = json.load(open(claude_json, encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to read ~/.claude.json for MCP discovery: {e}")
        return []

    names: set[str] = set(data.get("mcpServers", {}).keys())

    if cwd:
        projects = data.get("projects", {})
        entry = projects.get(cwd) or projects.get(
            next((k for k in projects if k.lower() == (cwd or "").lower()), ""), {}
        )
        if isinstance(entry, dict):
            names.update(entry.get("mcpServers", {}).keys())

    return sorted(names)


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

    @staticmethod
    def _resolve_indexing_config() -> tuple[bool, str, str | None]:
        """Resolve indexing configuration with env-var fallback.

        Returns:
            Tuple of (is_enabled, qdrant_url, gemini_api_key or None).
        """
        try:
            from shared.config import IndexingConfig

            cfg = IndexingConfig()
            return cfg.is_enabled, cfg.qdrant_url, cfg.gemini_api_key
        except Exception as e:
            logger.warning("Failed to resolve indexing config: %s", e)
            return (
                os.getenv("INDEXING_ENABLED", "false").lower() == "true",
                os.getenv("QDRANT_URL") or "",
                os.getenv("GEMINI_API_KEY") or "",
            )

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
        self._repo_context: dict = {}  # Store context for hooks
        self._structural_context: str | None = None  # File tree + repomap
        self._thread_history: str | None = None  # Issue/PR comment history
        self._pending_post_jobs: list[dict] = (
            []
        )  # Buffered during session, flushed after
        self._resume: str | None = None  # Session ID to resume
        self._continue_conversation: bool = False  # Continue most recent session
        self._fork_session: bool = False  # Fork from existing session
        # Streaming fields
        self._include_partial_messages: bool = False  # Enable StreamEvent output
        self._streaming_bridge = None  # SessionStreamBridge (not passed to SDK)
        self._session_signature: str | None = None  # Session URL for comment signature
        self._max_buffer_size: int = int(
            os.getenv("SDK_MAX_BUFFER_SIZE", "4194304")
        )  # 4MB default (was 1MB)

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

    def with_max_buffer_size(self, size: int) -> "SDKOptionsBuilder":
        """Override the default SDK max buffer size.

        Args:
            size: Max buffer size in bytes

        Returns:
            Self for method chaining
        """
        self._max_buffer_size = size
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
        tools = [
            "Task",
            "Skill",
            "Agent",
            "Bash",
            "Read",
            "Write",
            "Edit",
            "List",
            "Search",
            "Grep",
            "Glob",
            "mcp__github__*",
            "mcp__github_actions__*",
            "mcp__memory__memory_read",
            "mcp__codebase_tools__*",
            "mcp__semantic_search__*",
        ]

        if os.getenv("ALLOW_HOST_MCP", "false").lower() == "true":
            for name in _discover_host_mcp_names(self.cwd):
                tools.append(f"mcp__{name}__*")

        return self.with_tools(*tools)

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
        self, repo: str, workflow_name: str | None = None, ref: str | None = None
    ) -> "SDKOptionsBuilder":
        """Add post-session hooks for transcript path capture and job buffering.

        This hook captures the native transcript path from the SDK and buffers
        post-processing jobs (memory, retrospector, indexing) in
        ``_pending_post_jobs``. Jobs are NOT enqueued immediately — the
        SDK may fire Stop/SubagentStop multiple times per session. The
        caller must invoke ``flush_pending_post_jobs()`` after the SDK
        session ends to deduplicate and enqueue the final set of jobs.

        Workers read transcripts directly from the native SDK path via
        the shared ``~/.claude/`` volume — no staging copy is needed.

        Args:
            repo: Repository identifier (e.g., "owner/repo")
            workflow_name: Optional workflow name for retrospection context
            ref: Git ref that was indexed (e.g., "refs/heads/main")

        Returns:
            Self for method chaining
        """
        memory_enabled = os.getenv("MEMORY_WORKER_ENABLED", "true").lower() == "true"
        retrospector_enabled = (
            os.getenv("RETROSPECTOR_ENABLED", "true").lower() == "true"
        )
        indexing_enabled, _, gemini_key = self._resolve_indexing_config()
        indexing_enabled = indexing_enabled and bool(gemini_key)

        # Capture context from builder for hooks to use
        repo_context = self._repo_context
        pending = self._pending_post_jobs

        async def capture_and_buffer(input_data, _tool_use_id, _context):
            """Capture native transcript path and buffer jobs for later flush."""
            event = input_data.get("hook_event_name", "Stop")

            if event == "SubagentStop":
                transcript_path = input_data.get("agent_transcript_path")
            else:
                transcript_path = input_data.get("transcriptPath") or input_data.get(
                    "transcript_path"
                )

            if not transcript_path:
                logger.warning(
                    "Post-session hook: no transcript path in hook input, "
                    "skipping post-processing"
                )
                return {"success": False, "error": "no_transcript_path"}

            logger.debug(f"Post-session hook triggered: {transcript_path} ({event})")

            if memory_enabled:
                pending.append(
                    {
                        "type": "memory",
                        "repo": repo,
                        "transcript_path": transcript_path,
                        "event": event,
                        "claude_md": repo_context.get("claude_md"),
                        "memory_index": repo_context.get("memory_index"),
                    }
                )

            if retrospector_enabled:
                pending.append(
                    {
                        "type": "retrospector",
                        "repo": repo,
                        "transcript_path": transcript_path,
                        "event": event,
                        "workflow_name": workflow_name,
                        "session_meta": {
                            "num_turns": input_data.get("num_turns", 0),
                            "is_error": input_data.get("is_error", False),
                            "duration_ms": input_data.get("duration_ms", 0),
                            "agent_id": input_data.get("agent_id"),
                            "agent_type": input_data.get("agent_type"),
                        },
                    }
                )

            if indexing_enabled:
                pending.append(
                    {
                        "type": "indexing",
                        "repo": repo,
                        "event": event,
                        "ref": ref,
                    }
                )

            return {"success": True}

        if memory_enabled or retrospector_enabled:
            for event in ("Stop", "SubagentStop"):
                if event in self._hooks:
                    self._hooks[event].append(
                        HookMatcher(matcher="*", hooks=[capture_and_buffer])
                    )
                else:
                    self._hooks[event] = [
                        HookMatcher(matcher="*", hooks=[capture_and_buffer])
                    ]

        return self

    async def flush_pending_post_jobs(self) -> None:
        """Flush buffered post-processing jobs after the SDK session ends.

        Deduplicates buffered jobs by (transcript_path, event, type) — keeping
        only the last occurrence — then enqueues them to the respective
        Redis queues. Must be called after ``execute_sdk()`` returns.

        Safe to call even if no jobs were buffered (no-op).
        """
        jobs = self._pending_post_jobs
        self._pending_post_jobs = []
        await _flush_pending_post_jobs(jobs)

    # Session persistence methods

    def with_session_resume(self, session_id: str) -> "SDKOptionsBuilder":
        """Resume an existing SDK session by ID.

        The SDK loads the full conversation history from the session file
        stored under ``~/.claude/projects/<encoded-cwd>/``.

        Args:
            session_id: UUID of the session to resume.

        Returns:
            Self for method chaining
        """
        self._resume = session_id
        return self

    def with_session_continue(self) -> "SDKOptionsBuilder":
        """Continue the most recent session in ``cwd``.

        The SDK finds the latest session file in the working directory
        and resumes it automatically.

        Returns:
            Self for method chaining
        """
        self._continue_conversation = True
        return self

    def with_session_fork(self, session_id: str) -> "SDKOptionsBuilder":
        """Fork from an existing session without modifying the original.

        Starts a new session that inherits the conversation history of
        ``session_id`` but diverges from this point forward.

        Args:
            session_id: UUID of the session to fork from.

        Returns:
            Self for method chaining
        """
        self._resume = session_id
        self._fork_session = True
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

    def with_repository_context(
        self, claude_md: str | None = None, memory_index: str | None = None
    ) -> "SDKOptionsBuilder":
        """Inject repository context (CLAUDE.md and memory) into system prompt.

        This method prepends repository-specific context to the system prompt,
        ensuring it's processed as system-level context rather than user input.

        Args:
            claude_md: Content of CLAUDE.md from repository (optional)
            memory_index: Content of index.md from agent memory (optional)

        Returns:
            Self for method chaining
        """
        # Store context for hooks to use
        self._repo_context = {
            "claude_md": claude_md,
            "memory_index": memory_index,
        }

        context_parts = []

        # Add memory first (most persistent context)
        if memory_index and memory_index.strip():
            context_parts.append(
                f'<memory name="index.md">\n{memory_index.strip()}\n</memory>'
            )

        # Add CLAUDE.md (repository-specific instructions)
        if claude_md and claude_md.strip():
            context_parts.append(
                f"<repository_context>\n{claude_md.strip()}\n</repository_context>"
            )

        # Prepend to existing system prompt if any
        if context_parts:
            repo_context = "\n\n".join(context_parts)
            if self._system_prompt:
                self._system_prompt = f"{repo_context}\n\n{self._system_prompt}"
            else:
                self._system_prompt = repo_context

        return self

    def with_structural_context(
        self, file_tree: str | None = None, repomap: str | None = None
    ) -> "SDKOptionsBuilder":
        """Inject pre-built structural context into system prompt.

        Structural context (file tree + repomap) is the lowest priority
        component and will be truncated first if the total system prompt
        exceeds the budget.

        Args:
            file_tree: Pre-built file tree text.
            repomap: Pre-built repomap text.

        Returns:
            Self for method chaining
        """
        parts = []
        if file_tree and file_tree.strip():
            parts.append(f"<repo_structure>\n{file_tree.strip()}\n</repo_structure>")
        if repomap and repomap.strip():
            parts.append(f"<repo_map>\n{repomap.strip()}\n</repo_map>")

        if parts:
            self._structural_context = "\n\n".join(parts)
        return self

    def with_thread_history(self, history: str | None) -> "SDKOptionsBuilder":
        """Inject issue/PR comment history into system prompt.

        Thread history has medium priority — higher than structural context
        (file tree/repomap) but lower than the workflow prompt. When the
        budget is exceeded, structural is truncated first, then thread
        history (from the top, dropping oldest comments), then the prompt.

        Args:
            history: Formatted thread history string (from
                shared.thread_history.fetch_and_format_thread_history).

        Returns:
            Self for method chaining
        """
        if history and history.strip():
            self._thread_history = history.strip()
        return self

    async def with_repository_context_auto(
        self, repo: str, fetch_claude_md: bool = True, fetch_memory: bool = True
    ) -> "SDKOptionsBuilder":
        """Automatically fetch and inject repository context into system prompt.

        This is a convenience method that fetches CLAUDE.md and memory automatically.
        Use this when you don't have pre-fetched context available.

        Args:
            repo: Repository identifier (e.g., "owner/repo")
            fetch_claude_md: Whether to fetch CLAUDE.md from GitHub (default: True)
            fetch_memory: Whether to fetch memory from local volume (default: True)

        Returns:
            Self for method chaining
        """
        claude_md = None
        memory_index = None

        # Fetch CLAUDE.md from GitHub API
        if fetch_claude_md:
            try:
                from shared.github_auth import get_github_auth_service

                auth_service = await get_github_auth_service()
                if auth_service.is_configured():
                    github_token = await auth_service.get_token()
                    import httpx

                    async with httpx.AsyncClient() as client:
                        url = f"https://api.github.com/repos/{repo}/contents/CLAUDE.md"
                        headers = {
                            "Authorization": f"Bearer {github_token}",
                            "Accept": "application/vnd.github.v3.raw",
                        }
                        response = await client.get(url, headers=headers, timeout=10.0)
                        if response.status_code == 200:
                            claude_md = response.text
                            logger.info(f"Auto-fetched CLAUDE.md for {repo}")
            except Exception as e:
                logger.warning(f"Failed to auto-fetch CLAUDE.md for {repo}: {e}")

        # Fetch memory from local volume
        if fetch_memory:
            try:
                memory_path = str(
                    Path.home() / ".claude" / "memory" / repo / "memory" / "index.md"
                )
                if os.path.exists(memory_path):
                    with open(memory_path, encoding="utf-8") as f:
                        memory_index = f.read()
                    logger.info(f"Auto-loaded memory for {repo}")
            except Exception as e:
                logger.warning(f"Failed to auto-load memory for {repo}: {e}")

        # Inject the fetched context
        return self.with_repository_context(
            claude_md=claude_md, memory_index=memory_index
        )

    # Build method

    # ---------------------------------------------------------------------------
    # Streaming / remote control
    # ---------------------------------------------------------------------------

    def with_streaming(self, bridge: "Any") -> "SDKOptionsBuilder":
        """Enable streaming mode for real-time session observation.

        Sets include_partial_messages=True on the final ClaudeAgentOptions,
        which causes the SDK message loop to emit StreamEvent objects with
        token-level deltas (text chunks, tool call inputs) in addition to
        the normal AssistantMessage / ResultMessage objects.

        The bridge itself is NOT passed to the SDK — it is returned via the
        streaming_bridge property and must be passed separately to execute_sdk().

        Args:
            bridge: SessionStreamBridge instance (imported lazily to avoid
                    circular imports in non-streaming code paths)

        Returns:
            Self for method chaining
        """
        self._streaming_bridge = bridge
        self._include_partial_messages = True
        return self

    @property
    def streaming_bridge(self) -> "Any":
        """The SessionStreamBridge instance (if streaming is configured).

        Pass this to execute_sdk(streaming_bridge=builder.streaming_bridge) to
        enable real-time message publishing.
        """
        return self._streaming_bridge

    def with_session_signature(self, session_url: str) -> "SDKOptionsBuilder":
        """Append a session URL signature instruction to the system prompt.

        When remote control is enabled, the agent should include the session
        URL at the end of every GitHub comment it posts.

        Args:
            session_url: Full URL to the session proxy (human-readable format).

        Returns:
            Self for method chaining
        """
        self._session_signature = session_url
        return self

    # Build method

    def build(self) -> ClaudeAgentOptions:
        """Build the final ClaudeAgentOptions object.

        Enforces a total system prompt budget (~12K tokens). Components
        are truncated by priority if the budget is exceeded:
          1. Prompt content (workflow context, CLAUDE.md, memory index
             -- all combined into a single block; truncated last)
          2. Structural context (file tree + repomap; truncated first)

        Returns:
            Configured ClaudeAgentOptions instance
        """
        # Default to Sonnet if no model specified
        if not self._model:
            self._model = os.getenv(
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
            )

        # Assemble final system prompt with budget enforcement
        self._system_prompt = self._assemble_system_prompt()

        # Log system prompt for debugging (only if SDK_DEBUG is enabled)
        if os.getenv("SDK_DEBUG", "false").lower() == "true" and self._system_prompt:
            logger.debug("=" * 80)
            logger.debug("SYSTEM PROMPT BEING PASSED TO SDK:")
            logger.debug("-" * 80)
            # Print first 1000 chars to see structural context + repomap
            logger.debug(
                self._system_prompt[:1000] + "..."
                if len(self._system_prompt) > 1000
                else self._system_prompt
            )
            logger.debug("=" * 80)

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
            resume=self._resume,
            continue_conversation=self._continue_conversation,
            fork_session=self._fork_session,
            include_partial_messages=self._include_partial_messages,
            max_buffer_size=self._max_buffer_size,
        )

    def _assemble_system_prompt(self) -> str | None:
        """Assemble the final system prompt with budget enforcement.

        Four component tiers (highest priority first):
          1. Prompt content (workflow context, CLAUDE.md, memory index
             -- merged into a single block before this method runs)
          2. Thread history (issue/PR comments -- truncated from top/oldest)
          3. Structural context (file tree + repomap)
          4. Session signature (always appended, never truncated)

        When the budget is exceeded, structural is truncated first,
        then thread history (dropping oldest comments), then prompt.
        Session signature is always included.
        """
        # Collect components with their token costs
        components: list[tuple[str, str]] = []  # (label, text)

        if self._structural_context and self._structural_context.strip():
            components.append(("structural", self._structural_context.strip()))

        if self._thread_history and self._thread_history.strip():
            components.append(("thread_history", self._thread_history.strip()))

        if self._system_prompt and self._system_prompt.strip():
            components.append(("prompt", self._system_prompt.strip()))

        # Session signature (always appended, never truncated)
        if self._session_signature:
            signature_instruction = (
                "When posting any GitHub comment (via add_issue_comment or "
                "pull_request_review_write), you MUST append this signature "
                "at the very end of your comment:\n\n"
                "---\n"
                f"[Link to remote control this session]({self._session_signature})"
                "\n\nDo not include this signature in PR descriptions, "
                "commit messages, or any other output — only GitHub comments."
            )
            components.append(("signature", signature_instruction))

        if not components:
            return None

        # Calculate total tokens
        def _estimate_tokens(text: str) -> int:
            return max(1, int(len(text.split()) * 1.3))

        total = sum(_estimate_tokens(text) for _, text in components)

        if total <= SYSTEM_PROMPT_BUDGET:
            # Everything fits, join all components
            return "\n\n".join(text for _, text in components)

        # Budget exceeded — truncate lowest priority first
        logger.warning(
            f"System prompt budget exceeded: {total} > {SYSTEM_PROMPT_BUDGET} tokens. "
            "Truncating by priority."
        )

        budget_remaining = SYSTEM_PROMPT_BUDGET
        final_parts: list[str] = []

        # Add prompt and signature first (highest priority — keep intact)
        for label, text in components:
            if label not in ("structural", "thread_history"):
                tokens = _estimate_tokens(text)
                if tokens <= budget_remaining:
                    final_parts.append(text)
                    budget_remaining -= tokens
                else:
                    truncated = _truncate_text(text, budget_remaining)
                    if truncated:
                        final_parts.append(truncated)
                        budget_remaining = 0
                    break

        # Add thread history with remaining budget (medium priority — truncate from top)
        if budget_remaining > 0:
            for label, text in components:
                if label == "thread_history":
                    tokens = _estimate_tokens(text)
                    if tokens <= budget_remaining:
                        final_parts.append(text)
                        budget_remaining -= tokens
                    else:
                        truncated = _truncate_text(
                            text, budget_remaining, from_top=True
                        )
                        if truncated:
                            final_parts.append(truncated)
                            budget_remaining = 0
                    break

        # Add structural context with remaining budget (lowest priority — truncate first)
        if budget_remaining > 0:
            for label, text in components:
                if label == "structural":
                    tokens = _estimate_tokens(text)
                    if tokens <= budget_remaining:
                        final_parts.append(text)
                    else:
                        truncated = _truncate_text(text, budget_remaining)
                        if truncated:
                            final_parts.append(truncated)

        if not final_parts:
            return None

        result = "\n\n".join(final_parts)
        final_tokens = _estimate_tokens(result)
        logger.info(
            f"Assembled system prompt: {final_tokens}/{SYSTEM_PROMPT_BUDGET} tokens"
        )
        return result


def _truncate_text(text: str, max_tokens: int, from_top: bool = False) -> str | None:
    """Truncate text to fit within a token budget.

    Args:
        text: Text to truncate.
        max_tokens: Maximum tokens allowed.
        from_top: If True, remove lines from the beginning (keeping the end).
            Use this for thread history so newest comments are preserved.

    Returns:
        Truncated text, or None if even one line exceeds the budget.
    """

    def _estimate(text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))

    # Check if full text already fits
    if _estimate(text) <= max_tokens:
        return text

    lines = text.split("\n")

    if from_top:
        # Remove lines from the beginning, keeping the end
        result_lines: list[str] = []
        for line in reversed(lines):
            candidate = "\n".join([line] + list(reversed(result_lines)))
            if _estimate(candidate) > max_tokens:
                break
            result_lines.append(line)
        result_lines.reverse()

        if not result_lines:
            return None

        result = "\n".join(result_lines)
        if _estimate(result) > max_tokens:
            return None

        return "... (older comments truncated)\n" + result
    else:
        # Remove lines from the end, keeping the beginning
        result_lines = []
        for line in lines:
            candidate = "\n".join(result_lines + [line])
            if _estimate(candidate) > max_tokens:
                break
            result_lines.append(line)

        if not result_lines:
            return None

        result = "\n".join(result_lines)
        if _estimate(result) > max_tokens:
            return None

        return result + "\n... (truncated)"
