"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import json
import logging
import os
import shutil
import sys
from typing import Any

from redis.exceptions import TimeoutError as RedisTimeoutError

from shared import JobQueue, setup_graceful_shutdown
from shared.constants import (
    CLOSED_SESSION_TTL_HOURS,
    JOB_TTL_SECONDS,
    ORPHAN_LOCK_KEY,
    ORPHAN_LOCK_TTL_SECONDS,
    REVIVED_SESSION_TTL_HOURS,
    WORKTREE_CLEANUP_QUEUE,
)
from shared.logging_utils import setup_logging
from shared.session_store import SessionStore
from shared.worktree_manager import (
    cleanup_worktrees,
    cleanup_worktrees_by_branch,
    detect_orphan_worktrees,
    get_project_dir_for_worktree,
)

from .git_setup import sweep_orphaned_credentials
from .processor import JobProcessor

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Configure Claude Agent SDK logger to match our log level
logging.getLogger("claude_agent_sdk").setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Global state
shutdown_event = asyncio.Event()


async def process_job(job_queue: JobQueue, job_id: str, job_data: dict) -> None:
    """Process a single job in an isolated workspace.

    Args:
        job_queue: Job queue instance
        job_id: Job identifier
        job_data: Job data dictionary
    """
    processor = JobProcessor(job_queue, job_id, job_data)
    await processor.process()


async def _process_cleanup_requests(redis: Any) -> None:
    """Process pending worktree cleanup requests from Redis.

    Webhook service queues cleanup events (PR close, issue close,
    branch delete) to the ``agent:worktree:cleanup`` Redis list.
    This function drains all pending requests each cycle.
    """
    # pylint: disable=too-many-nested-blocks
    if redis is None:
        return

    while True:
        raw = await redis.lpop(WORKTREE_CLEANUP_QUEUE)
        if not raw:
            break

        try:
            msg = json.loads(raw)
            action = msg.get("action")
            repo = msg.get("repo", "")

            if action == "cleanup_thread":
                thread_type = msg.get("thread_type", "issue")
                thread_id = msg.get("thread_id", "")
                logger.info(
                    f"Cleaning up worktrees for {repo}/{thread_type}/{thread_id}"
                )

                # Check session state before deletion (coordinate archival)
                try:
                    session_store = SessionStore(redis)
                    sessions = await session_store.list_sessions(repo)
                    matching = [
                        s
                        for s in sessions
                        if s.thread_type == thread_type and s.thread_id == thread_id
                    ]
                    for s in matching:
                        logger.info(
                            f"Session state for {repo}/{thread_type}/{thread_id}/"
                            f"{s.workflow_name}: status={s.status}, "
                            f"last_run={s.last_run}"
                        )
                        await session_store.archive_transcript(
                            session_id=s.session_id,
                            repo=s.repo,
                            worktree_path=s.worktree_path,
                        )
                except Exception as e:
                    logger.warning(f"Failed to check session state before cleanup: {e}")

                bare_repo = os.path.join("/var/cache/repos", f"{repo}.git")
                await cleanup_worktrees(
                    repo, thread_type, thread_id, bare_repo=bare_repo
                )

                # Also clean up session metadata
                try:
                    session_store = SessionStore(redis)
                    # Find and delete sessions for this thread
                    sessions = await session_store.list_sessions(repo)
                    for s in sessions:
                        if s.thread_type == thread_type and s.thread_id == thread_id:
                            await session_store.close_session(
                                repo, thread_type, thread_id, s.workflow_name
                            )
                            logger.info(
                                f"Closed session for {repo}/{thread_type}/{thread_id}/{s.workflow_name}"
                            )
                except Exception as e:
                    logger.warning(f"Failed to cleanup session metadata: {e}")

            elif action == "expire_thread":
                thread_type = msg.get("thread_type", "issue")
                thread_id = msg.get("thread_id", "")
                logger.info(
                    f"Setting 72h expiration for worktrees in {repo}/{thread_type}/{thread_id}"
                )
                try:
                    session_store = SessionStore(redis)
                    sessions = await session_store.list_sessions(repo)
                    for s in sessions:
                        if s.thread_type == thread_type and s.thread_id == thread_id:
                            await session_store.expire_session(
                                repo,
                                thread_type,
                                thread_id,
                                s.workflow_name,
                                ttl_hours=CLOSED_SESSION_TTL_HOURS,
                            )
                except Exception as e:
                    logger.warning(f"Failed to expire session metadata: {e}")

            elif action == "revive_thread":
                thread_type = msg.get("thread_type", "issue")
                thread_id = msg.get("thread_id", "")
                logger.info(
                    f"Setting 30d expiration for revived worktrees in {repo}/{thread_type}/{thread_id}"
                )
                try:
                    session_store = SessionStore(redis)
                    sessions = await session_store.list_sessions(repo)
                    for s in sessions:
                        if s.thread_type == thread_type and s.thread_id == thread_id:
                            await session_store.expire_session(
                                repo,
                                thread_type,
                                thread_id,
                                s.workflow_name,
                                ttl_hours=REVIVED_SESSION_TTL_HOURS,
                            )
                except Exception as e:
                    logger.warning(f"Failed to revive session metadata: {e}")

            elif action == "cleanup_branch":
                branch = msg.get("branch", "")
                logger.info(f"Cleaning up worktrees for branch {branch} in {repo}")
                bare_repo = os.path.join("/var/cache/repos", f"{repo}.git")
                await cleanup_worktrees_by_branch(repo, branch, bare_repo=bare_repo)

        except Exception as e:
            logger.error(f"Failed to process cleanup request: {e}", exc_info=True)


async def _orphan_cleanup_loop(redis: Any) -> None:
    """Periodically scan for and remove orphan worktrees.

    ...
    """
    # pylint: disable=too-many-nested-blocks
    if redis is None:
        return

    session_store = SessionStore(redis)
    lock_key = ORPHAN_LOCK_KEY

    while not shutdown_event.is_set():
        # Try to acquire lock, expires in 1 hour
        acquired = await redis.set(
            lock_key, "locked", nx=True, ex=ORPHAN_LOCK_TTL_SECONDS
        )

        if acquired:
            try:
                # Reap credential files from jobs that died non-gracefully.
                # JobProcessor._cleanup is the normal deleter, but it does not
                # run on SIGKILL, an OOM kill under mem_limit, or a container
                # restart mid-job — which would otherwise strand a live
                # installation token on disk.
                try:
                    sweep_orphaned_credentials()
                except Exception as e:
                    logger.warning(f"Credential sweep failed: {e}")

                # Check overall session state distribution before cleanup
                all_sessions = await session_store.list_sessions("*")
                status_counts: dict[str, int] = {}
                for s in all_sessions:
                    status_counts[s.status] = status_counts.get(s.status, 0) + 1
                if status_counts:
                    logger.info(
                        f"Session state distribution before orphan cleanup: {status_counts}"
                    )

                orphans = await detect_orphan_worktrees(session_store)
                for orphan in orphans:
                    logger.info(f"TTL expired, cleaning up orphan worktree: {orphan}")

                    try:
                        orphan_sessions = await session_store.list_sessions_by_worktree(
                            str(orphan)
                        )
                        for s in orphan_sessions:
                            await session_store.archive_transcript(
                                session_id=s.session_id,
                                repo=s.repo,
                                worktree_path=s.worktree_path,
                            )
                    except Exception as e:
                        logger.warning(
                            f"Failed to archive transcripts for orphan {orphan}: {e}"
                        )

                    shutil.rmtree(orphan, ignore_errors=True)

                    project_dir = get_project_dir_for_worktree(orphan)
                    if project_dir.exists():
                        shutil.rmtree(project_dir, ignore_errors=True)
            except Exception as e:
                logger.error(f"Error in orphan cleanup loop: {e}")
            finally:
                # Release lock so other workers can acquire it promptly
                try:
                    await redis.delete(lock_key)
                except Exception:
                    pass

        # Sleep for 1 hour, checking for shutdown occasionally
        for _ in range(3600):
            if shutdown_event.is_set():
                break
            await asyncio.sleep(1)


async def main():
    """Main sandbox worker loop."""
    logger.info("Starting sandbox worker")

    # Setup signal handlers
    setup_graceful_shutdown(shutdown_event, logger)

    # Initialize job queue
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    job_queue = JobQueue(
        redis_url=redis_url,
        password=redis_password,
        job_ttl=JOB_TTL_SECONDS,
    )

    logger.info("Sandbox worker ready, waiting for jobs...")

    # Start background cleanup task
    cleanup_task = asyncio.create_task(_orphan_cleanup_loop(job_queue.redis))

    try:
        while not shutdown_event.is_set():
            try:
                # Reclaim stale jobs from crashed workers
                reclaimed = await job_queue.reclaim_stale_jobs()
                if reclaimed:
                    logger.info(f"Reclaimed {reclaimed} stale job(s)")

                # Process pending worktree cleanup requests
                await _process_cleanup_requests(job_queue.redis)

                # Pull next job (blocking with timeout)
                result = await job_queue.get_next_job(timeout=5)

                if not result:
                    # Timeout, check shutdown and continue
                    continue

                job_id, job_data = result
                logger.info(
                    f"Processing job {job_id} for {job_data['repo']}#{job_data['issue_number']}"
                )

                # Hold the lease for the whole run. Without it the job data
                # key expires after JOB_TTL_SECONDS and the next worker to
                # sweep reclaims a job that is still executing here — both
                # workers then drive the same shared worktree.
                async with job_queue.job_lease(job_id):
                    await process_job(job_queue, job_id, job_data)

            except (OSError, RedisTimeoutError) as e:
                # Redis connection errors — force reconnection so the next
                # iteration starts from a clean state instead of looping on
                # a stale connection.
                logger.warning(f"Redis connection error, reconnecting: {e}")
                await job_queue._reconnect()
                await asyncio.sleep(2)
            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    finally:
        logger.info("Shutting down sandbox worker...")
        if cleanup_task and not cleanup_task.done():
            cleanup_task.cancel()
        await job_queue.close()
        logger.info("Sandbox worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
