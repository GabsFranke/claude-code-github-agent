"""Request processor that creates jobs for sandbox execution."""

import asyncio
import logging
from typing import TYPE_CHECKING, Literal, Optional

import httpx
from langfuse import Langfuse

from shared import GitHubAuthService, JobQueue
from workflows import WorkflowEngine

from .repository_context_loader import RepositoryContextLoader

if TYPE_CHECKING:
    from shared import HealthChecker, MultiRateLimiter

logger = logging.getLogger(__name__)


# Type alias for process return value
ProcessResult = str | Literal["ignored"]


class RequestProcessor:
    """Processes agent requests by creating jobs for sandbox execution."""

    def __init__(
        self,
        token_manager: GitHubAuthService,
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

        # Initialize workflow engine
        self.workflow_engine = WorkflowEngine()

        # Initialize focused components
        self.context_loader = RepositoryContextLoader(
            token_manager, http_client, rate_limiters
        )

    async def process(
        self,
        repo: str,
        issue_number: int | None,
        event_data: dict,
        user_query: str,
        user: str,
        ref: str | None = None,
    ) -> ProcessResult:
        """Process a single agent request by creating a job.

        Args:
            repo: Repository full name (owner/repo)
            issue_number: Issue or PR number (optional)
            event_data: Raw event data (event_type, action, command if present)
            user_query: User-provided query/context
            user: User who triggered the request
            ref: Git ref to use (if None, defaults to main)

        Returns:
            Job ID string if job was created, or "ignored" if no workflow matched
        """
        logger.info(f"Processing request for {repo} issue #{issue_number} by {user}")
        logger.info(
            f"Event: {event_data}, Query: {user_query[:100] if user_query else '(none)'}"
        )

        if self.langfuse:
            with self.langfuse.start_as_current_span(
                name="github_agent_request"
            ) as trace:
                trace.update(
                    input={
                        "repo": repo,
                        "issue_number": issue_number,
                        "event_data": event_data,
                        "user_query": user_query,
                        "user": user,
                    },
                    metadata={
                        "repo": repo,
                        "issue_number": issue_number,
                        "user": user,
                    },
                )

                try:
                    job_id = await self._execute(
                        repo, issue_number, event_data, user_query, user, ref
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
                repo, issue_number, event_data, user_query, user, ref
            )

    async def _execute(
        self,
        repo: str,
        issue_number: int | None,
        event_data: dict,
        user_query: str,
        user: str,
        ref: str | None = None,
    ) -> ProcessResult:
        """Create a job for sandbox execution.

        Args:
            repo: Repository full name
            issue_number: Issue or PR number (optional)
            event_data: Raw event data (event_type, action, command if present)
            user_query: User-provided query/context
            user: User who triggered the request
            ref: Git ref to use (if None, defaults to main)

        Returns:
            Job ID string if job was created, or "ignored" if no workflow matched
        """
        # Route event/command to workflow
        event_type = event_data.get("event_type", "")
        action = event_data.get("action", "")
        command = event_data.get("command")

        workflow_name = None
        if command:
            # User command
            workflow_name = self.workflow_engine.get_workflow_for_command(command)
            logger.info(f"Command '{command}' -> workflow '{workflow_name}'")
        elif event_type:
            # Event trigger
            workflow_name = self.workflow_engine.get_workflow_for_event(
                event_type, action
            )
            logger.info(f"Event {event_type}.{action} -> workflow '{workflow_name}'")

        if not workflow_name:
            logger.info(
                f"No workflow configured for event={event_type}.{action} command={command} - ignoring"
            )
            return "ignored"

        # Workflow found - trigger repo sync before processing
        logger.info(f"Triggering sync for {repo} ref {ref or 'main'}")
        from shared import get_queue

        sync_queue = get_queue(queue_name="agent:sync:requests")
        await sync_queue.publish({"repo": repo, "ref": ref or "main"})

        # Build prompt using workflow engine
        prompt = self.workflow_engine.build_prompt(
            workflow_name=workflow_name,
            repo=repo,
            issue_number=issue_number,
            user_query=user_query,
        )

        logger.info(f"Built prompt: {prompt[:150]}...")

        # Fetch CLAUDE.md if exists
        try:
            claude_md = await self.context_loader.fetch_claude_md(repo)
            if claude_md:
                prompt = f"{claude_md}\n\n{prompt}"
                logger.info("Prepended CLAUDE.md context to prompt")
        except Exception as e:
            logger.warning(
                f"Failed to fetch CLAUDE.md from {repo}, continuing without repository context: {e}"
            )

        # Use provided ref or default to main
        final_ref = ref or "main"
        logger.info(f"Using ref: {final_ref}")

        # Get GitHub token
        github_token = await self.token_manager.get_token()

        # Create job in queue
        logger.info(f"Creating job with ref: {final_ref}")
        job_id = await self.job_queue.create_job(
            {
                "repo": repo,
                "issue_number": issue_number,
                "ref": final_ref,
                "prompt": prompt,
                "github_token": github_token,
                "user": user,
                "workflow_name": workflow_name,
                "user_query": user_query,
            }
        )

        logger.info(
            f"Created job {job_id} for {repo}#{issue_number} - worker is now free"
        )
        return job_id

    async def cleanup(self):
        """Cleanup resources."""
        await self.job_queue.close()
