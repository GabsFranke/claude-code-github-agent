"""Dead-letter queue (DLQ) utilities for job retry and failure handling.

Provides a reusable pattern for:
- Classifying errors as transient (retryable) vs permanent
- Re-enqueueing failed jobs with attempt tracking
- Routing exhausted jobs to a dead-letter queue for inspection
"""

import json
import logging
import time

logger = logging.getLogger(__name__)

DEFAULT_MAX_JOB_RETRIES = 3

TRANSIENT_MARKERS = (
    "timeout",
    "connection",
    "reset",
    "429",
    "RESOURCE_EXHAUSTED",
    "503",
    "502",
    "ECONNREFUSED",
    "ECONNRESET",
    "ETIMEDOUT",
)


def is_transient_error(exc: Exception) -> bool:
    """Check if an error is transient and worth retrying.

    Non-transient errors (config issues, missing API keys, validation)
    should go straight to the DLQ without retry.
    """
    msg = str(exc).lower()
    return any(marker.lower() in msg for marker in TRANSIENT_MARKERS)


async def enqueue_for_retry(
    redis_client,
    queue_key: str,
    dlq_key: str,
    message: dict,
    exc: Exception,
    max_retries: int = DEFAULT_MAX_JOB_RETRIES,
) -> None:
    """Re-enqueue a failed job with incremented attempts, or push to DLQ."""
    attempts = message.get("attempts", 0) + 1
    message["attempts"] = attempts
    message["last_error"] = f"{type(exc).__name__}: {exc}"

    if attempts >= max_retries:
        dlq_entry = json.dumps(
            {
                "reason": "max_retries_exceeded",
                "last_error": message["last_error"],
                "attempts": attempts,
                "original_message": message,
                "timestamp": time.time(),
            }
        )
        await redis_client.rpush(dlq_key, dlq_entry)  # type: ignore[misc]
        logger.error(
            "Job for %s exceeded %d retries, sent to DLQ: %s",
            message.get("repo", "unknown"),
            max_retries,
            exc,
        )
    else:
        await redis_client.rpush(queue_key, json.dumps(message))  # type: ignore[misc]
        logger.warning(
            "Re-enqueued job for %s (attempt %d/%d): %s",
            message.get("repo", "unknown"),
            attempts,
            max_retries,
            exc,
        )


async def get_dlq_count(redis_client, dlq_key: str) -> int:
    """Get number of entries in a dead-letter queue."""
    try:
        return int(await redis_client.llen(dlq_key))  # type: ignore[no-any-return]
    except Exception:
        return 0


async def inspect_dlq(redis_client, dlq_key: str, limit: int = 10) -> list[dict]:
    """Inspect dead-letter queue entries."""
    try:
        entries = await redis_client.lrange(dlq_key, 0, limit - 1)
        return [json.loads(e) for e in entries]
    except Exception:
        return []
