"""Post-processing orchestration for completed SDK sessions.

Handles Redis enqueue for memory/retrospector/indexing jobs and
flush/dedup of buffered post-processing work.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

# Module-level Redis connection pool for reuse across hook invocations
_redis_pool = None


async def get_redis_pool():
    """Get or create the module-level Redis connection pool."""
    global _redis_pool
    if _redis_pool is None:
        import redis.asyncio as aioredis

        redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
        redis_password = os.getenv("REDIS_PASSWORD")
        _redis_pool = aioredis.ConnectionPool.from_url(
            redis_url, decode_responses=True, password=redis_password
        )
    return _redis_pool


async def enqueue_memory_job(
    repo: str,
    transcript_path: str,
    hook_event: str,
    claude_md: str | None = None,
    memory_index: str | None = None,
) -> None:
    """Enqueue a memory extraction job for an already-persisted transcript."""
    for attempt in range(2):
        try:
            import redis.asyncio as aioredis

            pool = await get_redis_pool()
            rc = aioredis.Redis(connection_pool=pool)
            try:
                payload = json.dumps(
                    {
                        "repo": repo,
                        "transcript_path": transcript_path,
                        "hook_event": hook_event,
                        "claude_md": claude_md,
                        "memory_index": memory_index,
                    }
                )
                await rc.rpush("agent:memory:requests", payload)  # type: ignore[misc]
                logger.info("Enqueued memory job for %s [%s]", repo, hook_event)
                return
            finally:
                await rc.aclose()  # type: ignore[attr-defined]
        except Exception as e:
            if attempt == 0:
                logger.warning(
                    "Redis enqueue failed for memory job (%s), retrying: %s",
                    repo,
                    e,
                )
                await asyncio.sleep(1)
            else:
                logger.error(
                    "Failed to enqueue memory job for %s after retry: %s",
                    repo,
                    e,
                    exc_info=True,
                )


async def enqueue_retrospector_job(
    repo: str,
    transcript_path: str,
    hook_event: str,
    workflow_name: str | None,
    session_meta: dict,
) -> None:
    """Enqueue a retrospection job for an already-persisted transcript."""
    for attempt in range(2):
        try:
            import redis.asyncio as aioredis

            pool = await get_redis_pool()
            rc = aioredis.Redis(connection_pool=pool)
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
                await rc.rpush("agent:retrospector:requests", payload)  # type: ignore[misc]
                logger.info(
                    "Enqueued retrospector job for %s [%s] [%s]",
                    repo,
                    workflow_name or "unknown",
                    hook_event,
                )
                return
            finally:
                await rc.aclose()  # type: ignore[attr-defined]
        except Exception as e:
            if attempt == 0:
                logger.warning(
                    "Redis enqueue failed for retrospector job (%s), retrying: %s",
                    repo,
                    e,
                )
                await asyncio.sleep(1)
            else:
                logger.error(
                    "Failed to enqueue retrospector job for %s after retry: %s",
                    repo,
                    e,
                    exc_info=True,
                )


async def enqueue_indexing_job(
    repo: str, hook_event: str, ref: str | None = None
) -> None:
    """Enqueue a code indexing job for embedding-based semantic search."""
    for attempt in range(2):
        try:
            import redis.asyncio as aioredis

            pool = await get_redis_pool()
            rc = aioredis.Redis(connection_pool=pool)
            try:
                payload = json.dumps(
                    {
                        "repo": repo,
                        "ref": ref or "main",
                        "trigger": f"job_{hook_event.lower()}",
                    }
                )
                await rc.rpush("agent:indexing:requests", payload)  # type: ignore[misc]
                logger.info(
                    "Enqueued indexing job for %s [%s] ref=%s",
                    repo,
                    hook_event,
                    ref,
                )
                return
            finally:
                await rc.aclose()  # type: ignore[attr-defined]
        except Exception as e:
            if attempt == 0:
                logger.warning(
                    "Redis enqueue failed for indexing job (%s), retrying: %s",
                    repo,
                    e,
                )
                await asyncio.sleep(1)
            else:
                logger.error(
                    "Failed to enqueue indexing job for %s after retry: %s",
                    repo,
                    e,
                    exc_info=True,
                )


async def flush_pending_post_jobs(pending_jobs: list[dict]) -> None:
    """Flush buffered post-processing jobs after the SDK session ends.

    Deduplicates buffered jobs by (transcript_path, event, type) -- keeping
    only the last occurrence -- then enqueues them to the respective
    Redis queues.

    Safe to call with an empty list (no-op).
    """
    if not pending_jobs:
        return

    # Dedup: for the same (transcript_path, event, type), keep only the
    # last entry.
    seen: dict[tuple, dict] = {}
    for job in pending_jobs:
        key = (job.get("transcript_path", ""), job.get("event"), job["type"])
        seen[key] = job

    deduped = list(seen.values())
    total = len(pending_jobs)
    removed = total - len(deduped)
    if removed:
        logger.info(
            "Flush: deduped %d duplicate post-processing jobs (%d -> %d)",
            removed,
            total,
            len(deduped),
        )

    for job in deduped:
        try:
            job_type = job["type"]
            if job_type == "memory":
                await enqueue_memory_job(
                    job["repo"],
                    job["transcript_path"],
                    job["event"],
                    claude_md=job.get("claude_md"),
                    memory_index=job.get("memory_index"),
                )
            elif job_type == "retrospector":
                await enqueue_retrospector_job(
                    job["repo"],
                    job["transcript_path"],
                    job["event"],
                    job.get("workflow_name"),
                    job.get("session_meta", {}),
                )
            elif job_type == "indexing":
                await enqueue_indexing_job(
                    job["repo"], job["event"], ref=job.get("ref")
                )
        except Exception as e:
            logger.error(
                "Failed to enqueue %s job during flush: %s",
                job["type"],
                e,
                exc_info=True,
            )
