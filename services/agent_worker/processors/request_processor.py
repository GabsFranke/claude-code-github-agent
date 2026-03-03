"""Request processor that orchestrates Claude SDK execution."""

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Optional

import httpx
from langfuse import Langfuse

from ..auth import GitHubTokenManager
from ..commands import CommandContext, get_command_registry
from .claude_sdk_executor import ClaudeSDKExecutor
from .mcp_configuration_builder import MCPConfigurationBuilder
from .observability_manager import ObservabilityManager
from .repository_context_loader import RepositoryContextLoader

if TYPE_CHECKING:
    from shared import HealthChecker, MultiRateLimiter

logger = logging.getLogger(__name__)


class RequestProcessor:
    """Processes agent requests by orchestrating Claude SDK execution."""

    def __init__(
        self,
        token_manager: GitHubTokenManager,
        http_client: httpx.AsyncClient,
        langfuse_client: Langfuse | None = None,
        shutdown_event: asyncio.Event | None = None,
        rate_limiters: Optional["MultiRateLimiter"] = None,
        health_checker: Optional["HealthChecker"] = None,
    ):
        self.token_manager = token_manager
        self.http_client = http_client
        self.langfuse = langfuse_client
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.rate_limiters = rate_limiters
        self.health_checker = health_checker

        # Initialize focused components
        self.context_loader = RepositoryContextLoader(
            token_manager, http_client, rate_limiters
        )
        self.mcp_builder = MCPConfigurationBuilder(token_manager)
        self.observability = ObservabilityManager()
        self.sdk_executor = ClaudeSDKExecutor(self.shutdown_event, rate_limiters)

    async def process(
        self,
        repo: str,
        issue_number: int,
        command: str,
        user: str,
        auto_review: bool = False,
        auto_triage: bool = False,
    ) -> str:
        """Process a single agent request."""
        logger.info(f"Processing request for {repo} issue #{issue_number} by {user}")
        logger.info(f"Command: {command}")

        if self.langfuse:
            with self.langfuse.start_as_current_span(
                name="github_agent_request"
            ) as trace:
                trace.update(
                    input={
                        "repo": repo,
                        "issue_number": issue_number,
                        "command": command,
                        "user": user,
                        "auto_review": auto_review,
                        "auto_triage": auto_triage,
                    },
                    metadata={
                        "repo": repo,
                        "issue_number": issue_number,
                        "user": user,
                    },
                )

                try:
                    response = await self._execute(
                        repo, issue_number, command, user, auto_review, auto_triage
                    )

                    trace.update(
                        output={"response": response[:500]},
                        metadata={
                            "status": "success",
                            "response_length": len(response),
                        },
                    )
                    return response

                except Exception as e:
                    logger.error(f"Error processing request: {e}", exc_info=True)
                    trace.update(
                        output={"error": str(e)},
                        metadata={"status": "error"},
                        level="ERROR",
                    )
                    raise
                finally:
                    self.langfuse.flush()
        else:
            return await self._execute(
                repo, issue_number, command, user, auto_review, auto_triage
            )

    async def _execute(
        self,
        repo: str,
        issue_number: int,
        command: str,
        user: str,
        auto_review: bool,
        auto_triage: bool,
    ) -> str:
        """Execute the agent request."""
        # Determine event type
        if auto_review:
            event_type = "auto_review"
        elif auto_triage:
            event_type = "auto_triage"
        else:
            event_type = "manual"

        # Build command context
        context = CommandContext(
            repo=repo,
            issue_number=issue_number,
            command_text=command,
            user=user,
            event_type=event_type,
            raw_data={},
        )

        # Execute command via registry
        registry = get_command_registry()
        result = await registry.execute(context)
        prompt = result.prompt

        # Fetch CLAUDE.md if exists
        # Note: 404 returns empty string (expected), but API errors are raised
        try:
            claude_md = await self.context_loader.fetch_claude_md(repo)
            if claude_md:
                prompt = f"{claude_md}\n\n{prompt}"
        except Exception as e:
            # Log error but continue - missing CLAUDE.md shouldn't block execution
            logger.warning(
                f"Failed to fetch CLAUDE.md from {repo}, continuing without repository context: {e}"
            )

        # Execute Claude SDK with the prompt
        if self.langfuse:
            model_name = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514")
            with self.langfuse.start_as_current_observation(
                name="claude_sdk_execution",
                as_type="generation",
                model=model_name,
                model_parameters={"sdk_version": "agent-sdk"},
            ) as generation:
                generation.update(
                    input=prompt,
                    metadata={
                        "repo": repo,
                        "issue_number": issue_number,
                        "user": user,
                        "auto_review": auto_review,
                        "auto_triage": auto_triage,
                        "command": command,
                    },
                )

                try:
                    response = await self._execute_claude_sdk(prompt)
                    generation.update(output=response, level="DEFAULT")
                    return response
                except Exception as e:
                    generation.update(level="ERROR", status_message=str(e))
                    raise
        else:
            return await self._execute_claude_sdk(prompt)

    async def _execute_claude_sdk(self, prompt: str) -> str:
        """Execute Claude Agent SDK using focused components."""
        # Get MCP configuration
        mcp_servers = await self.mcp_builder.create_mcp_config()

        # Setup Langfuse hooks if configured
        hooks = self.observability.setup_langfuse_hooks()

        # Configure agent options
        options = self.mcp_builder.create_agent_options(mcp_servers, hooks)

        # Execute SDK
        return await self.sdk_executor.execute_sdk(prompt, options)

    async def cleanup(self):
        """Cleanup resources."""
        await self.sdk_executor.cleanup()
