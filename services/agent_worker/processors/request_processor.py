"""Request processor that creates jobs for sandbox execution."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, Optional

import httpx
from langfuse import Langfuse

from shared import GitHubAuthService, JobQueue
from shared.constants import (
    MODEL_TIERS,
    SESSION_DEDUP_LOCK_TTL,
    STREAMING_SESSION_STALE_SECONDS,
    session_dedup_key,
)
from shared.session_store import SessionStore, resolve_thread_type
from shared.utils import build_session_url
from shared.worktree_lock import WorktreeKey, WorktreeLock
from workflows import WorkflowEngine

from .repository_context_loader import RepositoryContextLoader

if TYPE_CHECKING:
    from shared import HealthChecker, MultiRateLimiter

logger = logging.getLogger(__name__)

_SESSION_FLAG_RE = re.compile(
    r"^\s*/\S+(?:\s+(-c|-f|--continue|--fork|--new))?(?:\s+(.*))?$"
)


def _parse_session_flag(user_query: str) -> tuple[str, str]:
    """Extract session flag and remaining query from a slash command."""
    m = _SESSION_FLAG_RE.match(user_query)
    if not m:
        return "", user_query  # type: ignore[unreachable]
    flag = (m.group(1) or "").strip()
    rest = (m.group(2) or "").strip()
    if flag in ("-c", "--continue"):
        return "resume", rest
    if flag in ("-f", "--fork"):
        return "fork", rest
    if flag == "--new":
        return "new", rest
    return "", rest


_MODEL_FLAG_RE = re.compile(r"(?:^|\s)--model(?:=|\s+)(\S+)")


def _parse_model_flag(user_query: str) -> tuple[str | None, str]:
    """Extract a ``--model <tier>`` override from a slash command.

    Accepts ``--model opus`` or ``--model=opus`` anywhere in the comment and
    strips it so it never reaches the prompt. Unknown tiers are dropped with a
    warning; full model ids belong in ``ANTHROPIC_DEFAULT_*_MODEL``.
    """
    m = _MODEL_FLAG_RE.search(user_query)
    if not m:
        return None, user_query
    tier = m.group(1).lower()
    rest = (user_query[: m.start()] + " " + user_query[m.end() :]).strip()
    rest = re.sub(r"[ \t]{2,}", " ", rest)
    if tier not in MODEL_TIERS:
        logger.warning(
            f"Ignoring --model '{tier}': expected one of {', '.join(MODEL_TIERS)}"
        )
        return None, rest
    return tier, rest


# Type alias for process return value
ProcessResult = str | Literal["ignored", "injected", "queued"]


async def _inject_into_running_session(
    token: str,
    user_query: str,
    redis: Any,
    repo: str,
    thread_type: Literal["pr", "issue", "discussion"],
    issue_number: int,
    workflow: str,
) -> None:
    """Push a follow-up message into a running session and interrupt the agent.

    This is the core of the conversation-continuity flow: when a new message
    arrives via a GitHub comment while the agent is already running, instead
    of creating a duplicate job, we inject the message directly into the
    running session's inbox and trigger an interrupt.

    The worker's auto-continue loop will:
      1. Detect the interrupt (via cancel signal / user_interrupt_event)
      2. Cancel the current SDK turn
      3. Pop the follow-up message from the inbox
      4. Start a new SDK turn with the follow-up as prompt
    """
    # 1. Push to inbox → worker drains it in the auto-continue loop
    store = SessionStore(redis)
    await store.push_inbox_message(token, user_query)

    # 2. Send cancel signal → interrupts current SDK turn immediately
    key = WorktreeKey(repo, thread_type, str(issue_number), workflow)
    lock = WorktreeLock(redis, key)
    await lock.send_cancel_signal()

    # 3. Publish to message stream → browser displays + history persists
    from shared.session_stream import SessionStreamBridge

    bridge = SessionStreamBridge(token, redis)
    await bridge.publish_user_message(user_query)


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

    async def _orphaned_session_age(
        self, streaming_store: SessionStore, token: str
    ) -> float | None:
        """Return how long a "running" session has been orphaned, or ``None``.

        A session is orphaned when its job never started (no worktree_path/
        session_id), no live job_id is recorded for it, and last_run is
        older than STREAMING_SESSION_STALE_SECONDS. A recorded job_id that
        the JobQueue still reports as pending/processing is authoritative:
        the job is in flight no matter how long it has taken (queue backlog,
        slow repo sync), so elapsed time alone never declares it dead.
        """
        info = await streaming_store.get_streaming_session(token)
        if not info or info.worktree_path or info.session_id:
            return None

        job_id = await streaming_store.get_job_id(token)
        if job_id:
            status = await self.job_queue.get_job_status(job_id)
            if status in ("pending", "processing"):
                return None

        try:
            age = (
                datetime.now(UTC) - datetime.fromisoformat(info.last_run)
            ).total_seconds()
        except ValueError:
            return None
        return age if age > STREAMING_SESSION_STALE_SECONDS else None

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

        # Per-request model tier: `--model opus` in the comment beats the
        # workflow's `model:` key; neither set leaves resolution to the CLI.
        workflow_config = self.workflow_engine.workflows[workflow_name]
        model_flag, user_query = _parse_model_flag(user_query)
        model_tier = model_flag or workflow_config.model
        if model_flag:
            logger.info(f"Model tier '{model_flag}' requested via --model flag")

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
                    session_flag, _ = _parse_session_flag(user_query)
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
            if event_data.get("trigger_type") == "schedule":
                logger.warning(
                    f"Skipping scheduled job for {repo} because it is not accessible (likely bot is uninstalled): {e}"
                )
                return "ignored"
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
        if (
            os.getenv("LANGFUSE_ENABLED", "false").lower() in ("true", "1", "yes")
            and os.getenv("LANGFUSE_TRACE_LINKING", "true").lower() == "true"
        ):
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

        if workflow_config.streaming.enabled:
            session_proxy_url = os.getenv("SESSION_PROXY_URL", "").strip()
            if session_proxy_url:
                try:
                    await self.job_queue.ensure_connected()
                    redis = self.job_queue.redis

                    # ── Session-aware job dedup (atomic SETNX) ──────────
                    dedup_key = session_dedup_key(
                        repo, thread_type, str(issue_number or 0), workflow_name
                    )
                    acquired = await redis.set(
                        dedup_key, "1", nx=True, ex=SESSION_DEDUP_LOCK_TTL
                    )
                    if not acquired:
                        dedup_store = SessionStore(redis)
                        existing_token = await dedup_store.find_active_session(
                            repo=repo,
                            issue_number=issue_number or 0,
                            workflow=workflow_name,
                            thread_type=thread_type,
                        )
                        if existing_token:
                            await _inject_into_running_session(
                                token=existing_token,
                                user_query=user_query,
                                redis=redis,
                                repo=repo,
                                thread_type=thread_type,
                                issue_number=issue_number or 0,
                                workflow=workflow_name,
                            )
                            logger.info(
                                f"[Streaming] Injected follow-up into running session "
                                f"{existing_token[:8]}... for {repo}#{issue_number}"
                            )
                            return "injected"

                        existing_token = await dedup_store.find_session(
                            repo=repo,
                            issue_number=issue_number or 0,
                            workflow=workflow_name,
                            thread_type=thread_type,
                        )
                        if existing_token:
                            await dedup_store.push_inbox_message(
                                existing_token, user_query
                            )
                            logger.info(
                                f"[Streaming] Queued follow-up for pending session "
                                f"{existing_token[:8]}... for {repo}#{issue_number}"
                            )
                            return "queued"

                        # Stale lock — no session found, clear and fall through
                        await redis.delete(dedup_key)

                    streaming_store = SessionStore(redis)

                    # Find existing streaming session for stable URLs.
                    # Prefer find_active_session (running only) to avoid
                    # racing with session_proxy's resume handler which
                    # independently checks status and creates resume jobs.
                    existing_token = await streaming_store.find_active_session(
                        repo=repo,
                        issue_number=issue_number or 0,
                        workflow=workflow_name,
                        thread_type=thread_type,
                    )
                    if existing_token:
                        # Active session exists — this is a follow-up message
                        # sent while the agent is already working.
                        # Check if a worker is actually processing (lock held).
                        wt_key = WorktreeKey(
                            repo=repo,
                            thread_type=thread_type,
                            thread_id=str(issue_number or 0),
                            workflow=workflow_name,
                        )
                        lock = WorktreeLock(self.job_queue.redis, wt_key)
                        lock_info = await lock.get_lock_info()

                        if lock_info:
                            # Worker is running → inject as follow-up, interrupt
                            await _inject_into_running_session(
                                token=existing_token,
                                user_query=user_query,
                                redis=self.job_queue.redis,
                                repo=repo,
                                thread_type=thread_type,
                                issue_number=issue_number or 0,
                                workflow=workflow_name,
                            )
                            logger.info(
                                f"[Streaming] Injected follow-up into running session "
                                f"{existing_token[:8]}... for {repo}#{issue_number}"
                            )
                            return "injected"

                        # Session is running but no active worker. This
                        # normally means a job is pending (worker hasn't
                        # started yet) or the worker crashed mid-run, in
                        # which case reclaim_stale_jobs() will re-queue it
                        # and it'll pick up the inbox message once it starts.
                        #
                        # But if the job never even started — no
                        # worktree_path/session_id was ever set — and it's
                        # been stuck like this for a while, the original job
                        # most likely expired out of the pending queue before
                        # any worker popped it (e.g. a worker shutdown/
                        # restart gap) and was dead-lettered. Dead-lettered
                        # jobs never reach the "processing" set, so
                        # reclaim_stale_jobs() will never find or re-queue
                        # them — the session would stay a zombie forever,
                        # silently swallowing every future event. Detect that
                        # and fall through to create a fresh job instead.
                        orphaned_age = await self._orphaned_session_age(
                            streaming_store, existing_token
                        )

                        if orphaned_age is not None:
                            logger.warning(
                                f"[Streaming] Session {existing_token[:8]}... for "
                                f"{repo}#{issue_number} has been 'running' with no "
                                "worktree/session_id and no active worker for "
                                f"{orphaned_age:.0f}s — its job was likely lost "
                                "before ever starting; recreating it"
                            )
                            await streaming_store.set_completed(
                                existing_token, is_error=True
                            )
                            # Fall through to the "completed/error session"
                            # reuse path below, which will reuse this token
                            # and enqueue a real job.
                        else:
                            # Push the message to the inbox — the worker will
                            # pick it up after it starts or the reclaim will
                            # re-queue the job and find it later.
                            await streaming_store.push_inbox_message(
                                existing_token, user_query
                            )
                            # Also publish for browser display
                            from shared.session_stream import SessionStreamBridge

                            bridge = SessionStreamBridge(
                                token=existing_token,
                                redis=self.job_queue.redis,
                            )
                            await bridge.publish_user_message(user_query)
                            logger.info(
                                f"[Streaming] Queued follow-up for pending session "
                                f"{existing_token[:8]}... for {repo}#{issue_number}"
                            )
                            return "queued"

                    # Try completed/error sessions as fallback
                    existing_token = await streaming_store.find_session(
                        repo=repo,
                        issue_number=issue_number or 0,
                        workflow=workflow_name,
                        thread_type=thread_type,
                    )
                    if existing_token:
                        session_token = existing_token
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
                "model": model_tier,
                # Session persistence fields
                "session_mode": session_mode,
                "session_id": session_id,
                "thread_type": thread_type,
                "thread_id": str(issue_number) if issue_number else "0",
                "conversation_config": {
                    "persist": conversation_config.persist,
                    "ttl_hours": conversation_config.ttl_hours,
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

        if session_token:
            try:
                await SessionStore(self.job_queue.redis).set_job_id(
                    session_token, job_id
                )
            except Exception as e:
                logger.warning(
                    f"[Streaming] Failed to record job_id on session "
                    f"{session_token[:8]}...: {e}"
                )

        return job_id

    async def cleanup(self):
        """Cleanup resources."""
        await self.job_queue.close()
