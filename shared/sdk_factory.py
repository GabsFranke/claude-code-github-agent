"""Factory for building Claude Agent SDK options with composable configuration.

This module provides a builder pattern for constructing ClaudeAgentOptions
with sensible defaults and flexible customization for different worker types.
"""

import logging
import os
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher

from shared.langfuse_hooks import setup_langfuse_hooks
from shared.post_processing import flush_pending_post_jobs as _flush_pending_post_jobs
from shared.post_processing import (
    stage_transcript_with_retry as _stage_transcript_with_retry,
)

logger = logging.getLogger(__name__)

# Total system prompt budget in tokens
SYSTEM_PROMPT_BUDGET = 12_000


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
        self._pending_post_jobs: list[dict] = (
            []
        )  # Buffered during session, flushed after

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
        # Determine plugin path (priority order):
        # 1. Docker container: /app/plugins/ci-failure-toolkit
        # 2. Project directory: <project_root>/plugins/ci-failure-toolkit
        # 3. User home: ~/.claude/plugins/ci-failure-toolkit

        plugin_path = None
        server_script = None

        # Check Docker container path
        if os.path.exists("/app/plugins/ci-failure-toolkit"):
            plugin_path = "/app/plugins/ci-failure-toolkit"
            server_script = (
                "/app/plugins/ci-failure-toolkit/servers/github_actions_server.py"
            )
        else:
            # Check project directory (relative to this file)
            project_plugin_path = os.path.join(
                Path(__file__).parent.parent, "plugins/ci-failure-toolkit"
            )
            if os.path.exists(project_plugin_path):
                plugin_path = str(project_plugin_path)
                server_script = os.path.join(
                    plugin_path, "servers/github_actions_server.py"
                )
            else:
                # Fall back to user home directory
                plugin_path = os.path.expanduser("~/.claude/plugins/ci-failure-toolkit")
                server_script = os.path.join(
                    plugin_path, "servers/github_actions_server.py"
                )

        self._mcp_servers["github-actions"] = {
            "type": "stdio",
            "command": "python",
            "args": [server_script],
            "env": {
                "PYTHONPATH": plugin_path,
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

    def with_codebase_tools(self, worktree_path: str) -> "SDKOptionsBuilder":
        """Add codebase tools MCP server for structured code search.

        Provides find_definitions, find_references, search_codebase, and
        read_file_summary tools that reuse the tree-sitter infrastructure.

        Args:
            worktree_path: Absolute path to the git worktree.

        Returns:
            Self for method chaining
        """
        self._mcp_servers["codebase-tools"] = {
            "type": "stdio",
            "command": "python3",
            "args": ["/app/mcp_servers/codebase_tools/server.py"],
            "env": {
                "REPO_PATH": worktree_path,
                "PYTHONPATH": "/app",
            },
        }
        return self

    def with_semantic_search(self, repo: str) -> "SDKOptionsBuilder":
        """Add semantic search MCP server for embedding-based code queries.

        Only registers the server if indexing is enabled and both Qdrant
        and Gemini API are configured. If unavailable, the tool gracefully
        returns empty results.

        Args:
            repo: Repository identifier (e.g., "owner/repo") for collection lookup.

        Returns:
            Self for method chaining
        """
        indexing_enabled, qdrant_url, gemini_key = self._resolve_indexing_config()

        if indexing_enabled and qdrant_url and gemini_key:
            self._mcp_servers["semantic-search"] = {
                "type": "stdio",
                "command": "python3",
                "args": ["/app/mcp_servers/semantic_search/server.py"],
                "env": {
                    "REPO_PATH": self.cwd,
                    "GITHUB_REPOSITORY": repo,
                    "QDRANT_URL": qdrant_url,
                    "GEMINI_API_KEY": gemini_key,
                    "EMBEDDING_DIMENSION": os.getenv("EMBEDDING_DIMENSION", "1024"),
                    "PYTHONPATH": "/app",
                },
            }
            logger.info(f"Semantic search MCP registered for {repo}")
        else:
            logger.debug(
                "Semantic search skipped: INDEXING_ENABLED not set or "
                "Qdrant/Gemini not configured"
            )

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
            "mcp__github-actions__*",
            "mcp__memory__memory_read",
            "mcp__codebase-tools__find_definitions",
            "mcp__codebase-tools__find_references",
            "mcp__codebase-tools__search_codebase",
            "mcp__codebase-tools__read_file_summary",
            "mcp__semantic-search__semantic_search",
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
        self, repo: str, workflow_name: str | None = None, ref: str | None = None
    ) -> "SDKOptionsBuilder":
        """Add post-session hooks for transcript staging and job buffering.

        This hook stages transcripts to the shared volume and buffers
        post-processing jobs (memory, retrospector, indexing) in
        ``_pending_post_jobs``. Jobs are NOT enqueued immediately — the
        SDK may fire Stop/SubagentStop multiple times per session. The
        caller must invoke ``flush_pending_post_jobs()`` after the SDK
        session ends to deduplicate and enqueue the final set of jobs.

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
        # Preserve original env-fallback guard: require GEMINI_API_KEY
        indexing_enabled = indexing_enabled and bool(gemini_key)

        # Capture context from builder for hooks to use
        repo_context = self._repo_context
        pending = self._pending_post_jobs

        async def capture_and_buffer(input_data, _tool_use_id, _context):
            """Stage transcript and buffer post-processing jobs for later flush."""
            event = input_data.get("hook_event_name", "Stop")

            # Select the correct transcript source based on event type.
            if event == "SubagentStop":
                transcript = input_data.get("agent_transcript_path")
            else:
                transcript = input_data.get("transcriptPath") or input_data.get(
                    "transcript_path"
                )

            if not transcript:
                logger.warning(
                    "Post-session hook: no transcript path in hook input, "
                    "skipping post-processing"
                )
                return {"success": False, "error": "no_transcript_path"}

            logger.debug(f"Post-session hook triggered: {transcript} ({event})")

            # Copy to shared volume for post-processing workers
            staged_path = await _stage_transcript_with_retry(
                repo,
                transcript,
                hook_event=event,
                agent_id=input_data.get("agent_id"),
                workflow_name=workflow_name,
            )
            if not staged_path:
                logger.error(
                    f"Failed to stage transcript {transcript} after retries, "
                    "skipping post-processing"
                )
                return {"success": False, "error": "transcript_staging_failed"}

            # Buffer the job — don't enqueue yet
            if memory_enabled:
                pending.append(
                    {
                        "type": "memory",
                        "repo": repo,
                        "staged_path": staged_path,
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
                        "staged_path": staged_path,
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

        Deduplicates buffered jobs by (staged_path, event, type) — keeping
        only the last occurrence — then enqueues them to the respective
        Redis queues. Must be called after ``execute_sdk()`` returns.

        Safe to call even if no jobs were buffered (no-op).
        """
        jobs = self._pending_post_jobs
        self._pending_post_jobs = []
        await _flush_pending_post_jobs(jobs)

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
                memory_path = f"/home/bot/agent-memory/{repo}/memory/index.md"
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
        )

    def _assemble_system_prompt(self) -> str | None:
        """Assemble the final system prompt with budget enforcement.

        Two component tiers (highest priority first):
          1. Prompt content (workflow context, CLAUDE.md, memory index
             -- merged into a single block before this method runs)
          2. Structural context (file tree + repomap)

        Structural context is truncated first if the total exceeds
        SYSTEM_PROMPT_BUDGET tokens.
        """
        # Collect components with their token costs
        components: list[tuple[str, str]] = []  # (label, text)

        if self._structural_context and self._structural_context.strip():
            components.append(("structural", self._structural_context.strip()))

        if self._system_prompt and self._system_prompt.strip():
            components.append(("prompt", self._system_prompt.strip()))

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

        # Add prompt content first (highest priority — keep intact)
        for label, text in components:
            if label != "structural":
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


def _truncate_text(text: str, max_tokens: int) -> str | None:
    """Truncate text to fit within a token budget by removing lines from the end.

    Args:
        text: Text to truncate.
        max_tokens: Maximum tokens allowed.

    Returns:
        Truncated text, or None if even one line exceeds the budget.
    """

    def _estimate(text: str) -> int:
        return max(1, int(len(text.split()) * 1.3))

    # Check if full text already fits
    if _estimate(text) <= max_tokens:
        return text

    lines = text.split("\n")
    result_lines: list[str] = []

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
