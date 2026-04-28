"""Request processor that creates jobs for sandbox execution."""

import asyncio
import json
import logging
import os
import re
import uuid
from typing import TYPE_CHECKING, Literal, Optional

import httpx
from langfuse import Langfuse

from shared import GitHubAuthService, JobQueue
from shared.session_store import SessionStore, resolve_thread_type
from shared.streaming_session import StreamingSessionStore
from shared.utils import build_session_url
from workflows import WorkflowEngine

from .repository_context_loader import RepositoryContextLoader

if TYPE_CHECKING:
    from shared import HealthChecker, MultiRateLimiter

logger = logging.getLogger(__name__)

_SESSION_FLAG_RE = re.compile(
    r"^\s*/\S+\s+(-c|-f|--continue|--fork|--new)?\s*(.*)",
)


def _parse_session_flag(user_query: str) -> str:
    """Extract session continuation flag from user query.

    Returns:
        ``"resume"`` for ``-c`` / ``--continue``,
        ``"fork"`` for ``-f`` / ``--fork``,
        ``"new"`` for ``--new``,
        ``""`` if no flag found.
    """
    if not user_query:
        return ""
    m = _SESSION_FLAG_RE.match(user_query)
    if not m:
        return ""  # type: ignore[unreachable]
    flag = (m.group(1) or "").strip()
    if flag in ("-c", "--continue"):
        return "resume"
    if flag in ("-f", "--fork"):
        return "fork"
    if flag == "--new":
        return "new"
    return ""


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
        workflow_name: str | None = None,
    ) -> ProcessResult:
        """Process a single agent request by creating a job.

        Args:
            repo: Repository full name (owner/repo)
            issue_number: Issue or PR number (optional)
            event_data: Raw event data (event_type, action, command if present)
            user_query: User-provided query/context
            user: User who triggered the request
            ref: Git ref to use (if None, defaults to main)
            workflow_name: Workflow name (pre-determined by webhook)

        Returns:
            Job ID string if job was created, or "ignored" if no workflow matched
        """
        logger.info(f"Processing request for {repo} issue #{issue_number} by {user}")
        logger.info(
            f"Event: {event_data}, Query: {user_query[:100] if user_query else '(none)'}"
        )

        if self.langfuse:
            with self.langfuse.start_as_current_observation(  # type: ignore[attr-defined]
                name="github_agent_request"
            ) as trace:
                trace.update(
                    input={
                        "repo": repo,
                        "issue_number": issue_number,
                        "event_data": event_data,
                        "user_query": user_query,
                        "user": user,
                        "workflow_name": workflow_name,
                    },
                    metadata={
                        "repo": repo,
                        "issue_number": issue_number,
                        "user": user,
                        "workflow_name": workflow_name,
                    },
                )

                try:
                    job_id = await self._execute(
                        repo,
                        issue_number,
                        event_data,
                        user_query,
                        user,
                        ref,
                        workflow_name,
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
                repo, issue_number, event_data, user_query, user, ref, workflow_name
            )

    async def _execute(
        self,
        repo: str,
        issue_number: int | None,
        event_data: dict,
        user_query: str,
        user: str,
        ref: str | None = None,
        workflow_name: str | None = None,
    ) -> ProcessResult:
        """Create a job for sandbox execution.

        Args:
            repo: Repository full name
            issue_number: Issue or PR number (optional)
            event_data: Raw event data (event_type, action, command if present)
            user_query: User-provided query/context
            user: User who triggered the request
            ref: Git ref to use (if None, defaults to main)
            workflow_name: Workflow name (pre-determined by webhook)

        Returns:
            Job ID string if job was created, or "ignored" if no workflow matched
        """
        # Workflow name should be provided by webhook
        if not workflow_name:
            logger.error("No workflow_name provided - webhook should filter events")
            return "ignored"

        # Validate workflow exists before triggering sync
        if workflow_name not in self.workflow_engine.workflows:
            logger.error(
                f"Unknown workflow '{workflow_name}' - ignoring request for {repo}"
            )
            return "ignored"

        logger.info(f"Processing workflow '{workflow_name}' for {repo}")

        # Workflow validated - trigger repo sync
        logger.info(f"Triggering sync for {repo} ref {ref or 'main'}")
        from shared import get_queue

        sync_queue = get_queue(queue_name="agent:sync:requests")
        await sync_queue.publish({"repo": repo, "ref": ref or "main"})

        # Build prompt using workflow engine
        prompt, system_context = self.workflow_engine.build_prompt(
            workflow_name=workflow_name,
            repo=repo,
            issue_number=issue_number,
            user_query=user_query,
        )

        # Inject workflow context if available (for CI failures)
        if event_data.get("run_id"):
            workflow_context = "\n\n## Workflow Failure Context\n\n"
            workflow_context += f"- Run ID: {event_data['run_id']}\n"
            if event_data.get("workflow_name_gh"):
                workflow_context += (
                    f"- Workflow Name: {event_data['workflow_name_gh']}\n"
                )
            if event_data.get("job_name"):
                workflow_context += f"- Failed Job: {event_data['job_name']}\n"
            if event_data.get("conclusion"):
                workflow_context += f"- Conclusion: {event_data['conclusion']}\n"
            if event_data.get("head_branch"):
                workflow_context += f"- Head Branch: {event_data['head_branch']}\n"
                workflow_context += (
                    f"- Target Branch for PR: {event_data['head_branch']}\n"
                )
            workflow_context += "\nUse the GitHub MCP tools to investigate this specific workflow run and job.\n"
            prompt = prompt + workflow_context

        logger.info(f"Built prompt: {prompt[:150]}...")

        # Get context profile for structural context generation
        context_profile = self.workflow_engine.get_context_profile(workflow_name)

        # Get conversation config and resolve session
        conversation_config = self.workflow_engine.get_conversation_config(
            workflow_name
        )
        session_mode = "new"
        session_id = None
        conversation_summary = None
        thread_type = resolve_thread_type(event_data)

        if conversation_config.persist and issue_number:
            try:
                await self.job_queue.ensure_connected()
                session_store = SessionStore(self.job_queue.redis)
                existing_session = await session_store.get_session(
                    repo=repo,
                    thread_type=thread_type,
                    thread_id=str(issue_number),
                    workflow=workflow_name,
                )
                if existing_session:
                    # Check if user explicitly requested continuation
                    session_flag = _parse_session_flag(user_query)
                    if session_flag == "resume":
                        session_mode = "resume"
                        session_id = existing_session.session_id
                    elif session_flag == "fork":
                        session_mode = "fork"
                        session_id = existing_session.session_id
                    elif session_flag == "new":
                        session_mode = "new"
                    elif conversation_config.auto_continue:
                        # Auto-continue for follow-up replies
                        session_mode = "resume"
                        session_id = existing_session.session_id

                    # Check turn limit
                    if (
                        session_mode != "new"
                        and existing_session.turn_count >= conversation_config.max_turns
                    ):
                        logger.info(
                            f"Session reached turn limit "
                            f"({existing_session.turn_count}/{conversation_config.max_turns}), "
                            f"starting fresh"
                        )
                        session_mode = "new"
                        session_id = None

                    # Provide summary as fallback
                    if (
                        session_mode in ("resume", "continue")
                        and conversation_config.summary_fallback
                        and existing_session.summary
                    ):
                        conversation_summary = existing_session.summary

                    if session_mode != "new":
                        logger.info(
                            f"Session: mode={session_mode}, "
                            f"id={session_id[:8] if session_id else 'N/A'}..."
                        )
            except Exception as e:
                logger.warning(f"Session lookup failed, starting fresh: {e}")

        # Fetch repository context (CLAUDE.md and memory) for system prompt
        # These will be injected by the SDK factory, not prepended to user prompt
        claude_md = None
        memory_index = None

        try:
            claude_md = await self.context_loader.fetch_claude_md(repo)
            if claude_md:
                logger.info("Fetched CLAUDE.md for system context")
        except Exception as e:
            logger.warning(
                f"Failed to fetch CLAUDE.md from {repo}, continuing without repository context: {e}"
            )

        try:
            memory_index = await self.context_loader.fetch_memory_index(repo)
            if memory_index:
                logger.info("Fetched index.md for system context")
        except Exception as e:
            logger.warning(f"Failed to fetch index.md from {repo}: {e}")

        # Use provided ref or default to main
        final_ref = ref or "main"
        logger.info(f"Using ref: {final_ref}")

        # Get GitHub token
        github_token = await self.token_manager.get_token()

        # Generate parent span ID for trace linking (if enabled)
        parent_span_id = None
        if os.getenv("LANGFUSE_TRACE_LINKING", "true").lower() == "true":
            parent_span_id = str(uuid.uuid4())
            logger.debug(
                f"Generated parent span ID for trace linking: {parent_span_id}"
            )

        # ---------------------------------------------------------------------------
        # Session streaming setup (remote control)
        # ---------------------------------------------------------------------------
        streaming_enabled = False
        session_token = None
        session_proxy_url = None

        workflow_config = self.workflow_engine.workflows.get(workflow_name)
        if workflow_config and workflow_config.streaming.enabled:
            session_proxy_url = os.getenv("SESSION_PROXY_URL", "").strip()
            if session_proxy_url:
                try:
                    await self.job_queue.ensure_connected()
                    streaming_store = StreamingSessionStore(self.job_queue.redis)

                    # Find existing streaming session (any status) for stable URLs
                    existing_token = await streaming_store.find_session(
                        repo=repo,
                        issue_number=issue_number or 0,
                        workflow=workflow_name,
                        thread_type=thread_type,
                    )
                    if existing_token:
                        session_token = existing_token
                        # Set session back to running for the new invocation
                        ttl_hours = conversation_config.ttl_hours
                        await streaming_store.set_running(
                            session_token, ttl_seconds=ttl_hours * 3600
                        )
                        logger.info(
                            f"[Streaming] Reusing session {session_token[:8]}... "
                            f"for {repo}#{issue_number}"
                        )
                    else:
                        # Generate a new session token
                        session_token = str(uuid.uuid4())
                        ttl_hours = conversation_config.ttl_hours
                        installation_id = event_data.get("installation_id", "")
                        conversation_config_json = json.dumps(
                            conversation_config.model_dump()
                        )
                        # Build the full human-readable URL (base + path)
                        owner, _, repo_name = repo.partition("/")
                        full_session_url = build_session_url(
                            session_proxy_url,
                            owner,
                            repo_name,
                            thread_type,
                            issue_number or 0,
                            workflow_name,
                        )
                        await streaming_store.create_session(
                            token=session_token,
                            repo=repo,
                            issue_number=issue_number or 0,
                            workflow=workflow_name,
                            session_proxy_url=full_session_url,
                            ttl_seconds=ttl_hours * 3600,
                            installation_id=installation_id,
                            initial_query=user_query,
                            thread_type=thread_type,
                            ref=final_ref,
                            user=user,
                            conversation_config=conversation_config_json,
                        )
                        logger.info(
                            f"[Streaming] Created session {session_token[:8]}... "
                            f"for {repo}#{issue_number}"
                        )

                    # Publish initial user query to the session history
                    if user_query and session_token:
                        try:
                            from shared.session_stream import SessionStreamBridge

                            bridge = SessionStreamBridge(
                                token=session_token,
                                redis=self.job_queue.redis,
                            )
                            await bridge.publish_user_message(user_query)
                        except Exception as e:
                            logger.warning(
                                f"[Streaming] Failed to publish initial query: {e}"
                            )

                    streaming_enabled = True

                except Exception as e:
                    logger.warning(
                        f"[Streaming] Failed to create streaming session: {e}. "
                        "Continuing without streaming."
                    )
                    session_token = None
                    streaming_enabled = False
            else:
                logger.warning(
                    "[Streaming] SESSION_PROXY_URL not configured, "
                    "streaming disabled for this job"
                )

        # Create job in queue
        logger.info(f"Creating job with ref: {final_ref}")
        job_id: str = await self.job_queue.create_job(
            {
                "repo": repo,
                "issue_number": issue_number,
                "ref": final_ref,
                "prompt": prompt,
                "system_context": system_context,
                "claude_md": claude_md,  # Pass separately for system prompt injection
                "memory_index": memory_index,  # Pass separately for system prompt injection
                "github_token": github_token,
                "user": user,
                "workflow_name": workflow_name,
                "user_query": user_query,
                "event_data": event_data,
                "parent_span_id": parent_span_id,  # For Langfuse trace linking
                "context_profile": context_profile,  # Structural context config
                # Session persistence fields
                "session_mode": session_mode,
                "session_id": session_id,
                "thread_type": thread_type,
                "thread_id": str(issue_number) if issue_number else "0",
                "conversation_config": {
                    "persist": conversation_config.persist,
                    "ttl_hours": conversation_config.ttl_hours,
                    "max_turns": conversation_config.max_turns,
                    "auto_continue": conversation_config.auto_continue,
                    "summary_fallback": conversation_config.summary_fallback,
                },
                "conversation_summary": conversation_summary,
                # Streaming session fields (remote control)
                "streaming_enabled": streaming_enabled,
                "session_token": session_token,
            }
        )

        logger.info(
            f"Created job {job_id} for {repo}#{issue_number} - worker is now free"
        )
        return job_id

    async def cleanup(self):
        """Cleanup resources."""
        await self.job_queue.close()
