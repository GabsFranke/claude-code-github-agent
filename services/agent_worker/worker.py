"""Claude Code worker that processes GitHub requests from message queue."""

import asyncio
import logging
import sys

import httpx
from langfuse import Langfuse

# Import shared utilities
from shared import JobQueue, MultiRateLimiter, get_queue, setup_graceful_shutdown
from shared.config import get_worker_config, handle_config_error
from shared.constants import (
    SESSION_RECOVERY_LOCK_KEY,
    SESSION_RECOVERY_SWEEP_INTERVAL_SECONDS,
    STREAMING_SESSION_STALE_SECONDS,
)
from shared.health import HealthChecker
from shared.logging_utils import setup_logging
from shared.session_store import SessionStore, UnifiedSessionInfo
from shared.worktree_lock import WorktreeKey, WorktreeLock

# Import modularized components
from .processors import RequestProcessor

# Load configuration first with detailed error reporting
try:
    config = get_worker_config()
except Exception as e:
    handle_config_error(e, "worker")

# Configure logging
setup_logging(level=config.log_level, silence_noisy=True)
logger = logging.getLogger(__name__)

logger.info(f"Logging configured at {config.log_level} level")
logger.info(f"Configuration loaded: GitHub App ID={config.github.github_app_id}")

# Initialize Langfuse client (module-level, never shutdown)
langfuse = None
if config.langfuse.is_enabled:
    langfuse = Langfuse(
        public_key=config.langfuse.langfuse_public_key,
        secret_key=config.langfuse.langfuse_secret_key,
        host=config.langfuse.langfuse_host,
    )
    logger.info("Langfuse observability enabled")
else:
    logger.info("Langfuse not configured - skipping observability")

# Global state
http_client = None
shutdown_event = asyncio.Event()
processor = None
health_checker = None
rate_limiters = None
job_queue = None


async def _recover_orphaned_sessions_loop(
    job_queue: JobQueue, request_processor: RequestProcessor
) -> None:
    """Periodically recreate jobs for streaming sessions stuck at
    status="running" whose job died before it ever started.

    A job that expires from the pending queue before any sandbox worker
    pops it (e.g. across a worker shutdown/restart gap) gets dead-lettered
    directly from ``JobQueue.get_next_job()`` without ever reaching the
    "processing" set — so ``reclaim_stale_jobs()`` never sees it and never
    re-queues it. The streaming session created for it is left at
    status="running" forever, and every subsequent webhook event for that
    repo/issue/workflow just gets pushed into its inbox, which nothing will
    ever read. This sweep finds those orphans and recreates their jobs
    without needing a new GitHub event to trigger recovery.
    """
    await job_queue.ensure_connected()
    redis = job_queue.redis
    store = SessionStore(redis)

    while not shutdown_event.is_set():
        acquired = await redis.set(
            SESSION_RECOVERY_LOCK_KEY,
            "locked",
            nx=True,
            ex=SESSION_RECOVERY_SWEEP_INTERVAL_SECONDS,
        )
        if acquired:
            try:
                stale = await store.list_stale_running_sessions(
                    STREAMING_SESSION_STALE_SECONDS
                )
                for info in stale:
                    await _recover_session(info, store, job_queue, request_processor)
            except Exception as e:
                logger.error(
                    f"[Recovery] Error scanning for orphaned sessions: {e}",
                    exc_info=True,
                )

        for _ in range(SESSION_RECOVERY_SWEEP_INTERVAL_SECONDS):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)


async def _recover_session(
    info: UnifiedSessionInfo,
    store: SessionStore,
    job_queue: JobQueue,
    request_processor: RequestProcessor,
) -> None:
    """Recreate the job for one stale-looking session, unless it's alive.

    A recorded job_id is authoritative: if the JobQueue still shows it
    pending/processing, the job is genuinely in flight no matter how long
    it's taking. WorktreeLock alone can't rule that out — its TTL
    (DEFAULT_LOCK_TTL) can expire well before a long-running job finishes,
    which previously made this sweep duplicate still-running jobs every
    cycle (last_run never advanced on its own, so the same session kept
    re-qualifying as "stale").
    """
    if info.streaming_token:
        job_id = await store.get_job_id(info.streaming_token)
        if job_id:
            job_status = await job_queue.get_job_status(job_id)
            if job_status in ("pending", "processing"):
                return

    lock = WorktreeLock(
        job_queue.redis,
        WorktreeKey(
            repo=info.repo,
            thread_type=info.thread_type,
            thread_id=info.thread_id,
            workflow=info.workflow_name,
        ),
    )
    if await lock.get_lock_info():
        return  # actually being worked on right now

    logger.warning(
        f"[Recovery] Streaming session for {info.repo}#{info.issue_number} "
        f"({info.workflow_name}) stuck at 'running' with no worker for "
        f"over {STREAMING_SESSION_STALE_SECONDS}s - recreating its job"
    )
    if info.streaming_token:
        await store.set_completed(info.streaming_token, is_error=True)

    try:
        await request_processor.process(
            repo=info.repo,
            issue_number=int(info.issue_number) if info.issue_number else None,
            event_data={
                "event_type": "recovery",
                "action": "requeue",
                "installation_id": info.installation_id,
                "is_pr": info.thread_type == "pr",
            },
            user_query=info.initial_query,
            user=info.user or "unknown",
            ref=info.ref or None,
            workflow_name=info.workflow_name,
        )
    except Exception as e:
        logger.error(
            f"[Recovery] Failed to recreate job for "
            f"{info.repo}#{info.issue_number}: {e}",
            exc_info=True,
        )


async def main():
    """Main worker loop - subscribes to queue and processes messages."""
    global http_client, processor, health_checker, rate_limiters, job_queue  # pylint: disable=global-statement

    logger.info("Starting Claude Agent SDK worker (job queue mode)")

    # Setup signal handlers
    setup_graceful_shutdown(shutdown_event, logger)

    # Initialize HTTP client
    http_client = httpx.AsyncClient(
        timeout=30.0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    # Initialize health checker
    health_checker = HealthChecker(
        health_file=config.health_check_file,
        update_interval=config.health_check_interval,
        max_idle_time=config.health_check_max_idle,
    )
    health_checker.start()
    logger.info(f"Health checker started: {config.health_check_file}")

    # Initialize rate limiters with Redis backend for distributed rate limiting
    try:
        from shared.rate_limiter import create_redis_rate_limiter_backend

        logger.info("Initializing distributed rate limiting with Redis...")
        redis_backend = await create_redis_rate_limiter_backend(
            redis_url=config.queue.redis_url, password=config.queue.redis_password
        )
        rate_limiters = MultiRateLimiter(backend=redis_backend)
        logger.info("Using Redis-based distributed rate limiting (multi-worker safe)")
    except (ImportError, ConnectionError, TimeoutError) as e:
        logger.warning(
            f"Failed to initialize Redis rate limiting: {e}. "
            "Falling back to in-memory rate limiting (single worker only)"
        )
        rate_limiters = MultiRateLimiter()  # Falls back to in-memory backend
    except Exception as e:
        # Catch Redis-specific errors (AuthenticationError, ResponseError, etc.)
        logger.warning(
            f"Redis error during rate limiter initialization: {e}. "
            "Falling back to in-memory rate limiting (single worker only)"
        )
        rate_limiters = MultiRateLimiter()  # Falls back to in-memory backend

    rate_limiters.add_limiter(
        "github",
        max_requests=config.github_rate_limit,
        time_window=3600,  # 1 hour
    )
    rate_limiters.add_limiter(
        "anthropic",
        max_requests=config.anthropic_rate_limit,
        time_window=60,  # 1 minute
    )
    logger.info(
        f"Rate limiters configured: GitHub={config.github_rate_limit}/hour, "
        f"Anthropic={config.anthropic_rate_limit}/min"
    )

    # Initialize job queue
    job_queue = JobQueue(
        redis_url=config.queue.redis_url,
        password=config.queue.redis_password,
        job_ttl=3600,  # 1 hour
    )
    logger.info("Job queue initialized")

    recovery_task: asyncio.Task | None = None

    try:
        # Initialize shared GitHub auth service
        from shared import GitHubAuthService

        token_manager = GitHubAuthService(
            app_id=config.github.github_app_id,
            private_key=config.github.github_private_key,
            installation_id=config.github.github_installation_id,
            http_client=http_client,
        )

        # Initialize request processor
        processor = RequestProcessor(
            token_manager=token_manager,
            http_client=http_client,
            job_queue=job_queue,
            langfuse_client=langfuse,
            shutdown_event=shutdown_event,
            rate_limiters=rate_limiters,
            health_checker=health_checker,
        )

        logger.info("Worker initialized successfully")

        # Initialize queue
        queue = get_queue()

        # Background sweep to recover streaming sessions orphaned by jobs
        # that expired from the pending queue before any sandbox worker
        # ever picked them up (see _recover_orphaned_sessions_loop).
        recovery_task = asyncio.create_task(
            _recover_orphaned_sessions_loop(job_queue, processor)
        )

        # Subscribe and process messages
        async def callback(message: dict):
            if shutdown_event.is_set():
                logger.info("Shutdown in progress, skipping message")
                return

            try:
                repo = message.get("repository")
                issue_number = message.get("issue_number")
                event_data = message.get("event_data", {})
                user_query = message.get("user_query", "")
                user = message.get("user", "unknown")
                ref = message.get("ref")
                workflow_name = message.get("workflow_name")

                logger.info(
                    f"Received message with ref: {ref}, workflow: {workflow_name}"
                )
                logger.info(f"Message keys: {list(message.keys())}")
                logger.info(f"Event data: {event_data}")

                if not all([repo, event_data, workflow_name]):
                    logger.error(f"Invalid message format: {message}")
                    health_checker.record_error()
                    return

                # Type assertions after validation
                assert isinstance(repo, str)
                assert isinstance(event_data, dict)
                assert isinstance(user_query, str)
                assert isinstance(user, str)
                assert isinstance(workflow_name, str)

                job_id = await processor.process(
                    repo,
                    issue_number,
                    event_data,
                    user_query,
                    user,
                    ref,
                    workflow_name,
                )

                # Check if event was ignored
                if job_id == "ignored":
                    logger.debug("Event ignored, no workflow configured")
                    health_checker.record_activity()
                    return

                # Record successful processing
                health_checker.record_activity()

            except AssertionError as e:
                logger.error(f"Message validation failed: {e}", exc_info=True)
                health_checker.record_error()
            except Exception as e:
                logger.error(f"Error in callback: {e}", exc_info=True)
                health_checker.record_error()

        # Start listening
        logger.info("Worker ready, waiting for messages...")
        await queue.subscribe(callback)

    finally:
        # Cleanup
        logger.info("Cleaning up resources...")
        if recovery_task and not recovery_task.done():
            recovery_task.cancel()
        if health_checker:
            await health_checker.stop()
        if processor:
            await processor.cleanup()
        if rate_limiters:
            await rate_limiters.cleanup()
        if job_queue:
            await job_queue.close()
        await http_client.aclose()
        if langfuse:
            langfuse.flush()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
