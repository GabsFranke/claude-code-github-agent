"""Request processor that creates jobs for sandbox execution."""

import asyncio
import logging
from typing import TYPE_CHECKING, Optional

import httpx
from langfuse import Langfuse

from shared import JobQueue

from ..auth import GitHubTokenManager
from ..commands import CommandContext, get_command_registry
from .repository_context_loader import RepositoryContextLoader

if TYPE_CHECKING:
    from shared import HealthChecker, MultiRateLimiter

logger = logging.getLogger(__name__)


class RequestProcessor:
    """Processes agent requests by creating jobs for sandbox execution."""

    def __init__(
        self,
        token_manager: GitHubTokenManager,
        http_client: httpx.AsyncClient,
        job_queue: JobQueue,
        langfuse_client: Langfuse | None = None,
        shutdown_event: asyncio.Event | None = None,
        rate_limiters: Optional["MultiRateLimiter"] = None,
        health_checker: Optional["HealthChecker"] = None,
    ):
        self.token_manager = token_manager
        self.http_client = http_client
        self.job_queue = job_queue
        self.langfuse = langfuse_client
        self.shutdown_event = shutdown_event or asyncio.Event()
        self.rate_limiters = rate_limiters
        self.health_checker = health_checker

        # Initialize focused components
        self.context_loader = RepositoryContextLoader(
            token_manager, http_client, rate_limiters
        )

    async def process(
        self,
        repo: str,
        issue_number: int,
        command: str,
        user: str,
        auto_review: bool = False,
        auto_triage: bool = False,
    ) -> str:
        """Process a single agent request by creating a job."""
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
                    job_id = await self._execute(
                        repo, issue_number, command, user, auto_review, auto_triage
                    )

                    trace.update(
                        output={"job_id": job_id},
                        metadata={
                            "status": "job_created",
                            "job_id": job_id,
                        },
                    )
                    return job_id

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
        """Create a job for sandbox execution."""
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

        # Execute command via registry to generate prompt
        registry = get_command_registry()
        result = await registry.execute(context)
        prompt = result.prompt

        # Fetch CLAUDE.md if exists
        try:
            claude_md = await self.context_loader.fetch_claude_md(repo)
            if claude_md:
                prompt = f"{claude_md}\n\n{prompt}"
        except Exception as e:
            logger.warning(
                f"Failed to fetch CLAUDE.md from {repo}, continuing without repository context: {e}"
            )

        # Get GitHub token
        github_token = await self.token_manager.get_token()

        # Create job in queue
        job_id = await self.job_queue.create_job(
            {
                "repo": repo,
                "issue_number": issue_number,
                "prompt": prompt,
                "github_token": github_token,
                "user": user,
                "auto_review": auto_review,
                "auto_triage": auto_triage,
                "command": command,
            }
        )

        logger.info(
            f"Created job {job_id} for {repo}#{issue_number} - worker is now free"
        )
        return job_id

    async def cleanup(self):
        """Cleanup resources."""
        await self.job_queue.close()
