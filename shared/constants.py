"""Shared constants used across services.

Centralises values that were previously duplicated or hardcoded, so TTLs,
queue names, and limits are consistent and come from a single source.

Environment variable overrides:
    STREAMING_SESSION_TTL_HOURS  — default streaming session TTL (default: 720)
    HISTORY_MAX                  — max messages in Redis history list (default: 2000)
    JOB_TTL_SECONDS              — job data TTL in Redis (default: 3600)
    MAX_AUTO_CONTINUES           — max auto-continue iterations (default: 10)
"""

import os
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# TTLs — sourced from ConversationConfig.ttl_hours in the normal flow,
# these are fallback defaults for edge cases (missing config, resume, etc.)
# ---------------------------------------------------------------------------

# Default streaming session TTL (30 days). Overridden by conversation_config.ttl_hours.
DEFAULT_SESSION_TTL_HOURS = int(os.getenv("STREAMING_SESSION_TTL_HOURS", "720"))
DEFAULT_SESSION_TTL_SECONDS = DEFAULT_SESSION_TTL_HOURS * 3600

# Session TTL when an issue is closed (3 days).
CLOSED_SESSION_TTL_HOURS = 72

# Session TTL when a closed issue is revived (matches default).
REVIVED_SESSION_TTL_HOURS = DEFAULT_SESSION_TTL_HOURS

# ConversationConfig fallback TTL — used when job data is missing.
# Matches the Pydantic model default so the fallback is consistent.
FALLBACK_CONVERSATION_TTL_HOURS = DEFAULT_SESSION_TTL_HOURS

# Job data TTL in Redis (1 hour).
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "3600"))

# Short-lived Redis history TTL (1 hour) — fallback before transcript is written.
HISTORY_TTL_SECONDS = 3600

# Orphan cleanup lock TTL (1 hour).
ORPHAN_LOCK_TTL_SECONDS = 3600

# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------

# Max messages in Redis history list.
HISTORY_MAX = int(os.getenv("HISTORY_MAX", "2000"))

# Max auto-continue iterations per job.
MAX_AUTO_CONTINUES = int(os.getenv("MAX_AUTO_CONTINUES", "10"))

# ---------------------------------------------------------------------------
# Redis key prefixes and queue names
# ---------------------------------------------------------------------------

# Job queue
JOB_DATA_PREFIX = "agent:job:data:"
JOB_STATUS_PREFIX = "agent:job:status:"
PENDING_JOB_QUEUE = "agent:jobs:pending"

# Cleanup
WORKTREE_CLEANUP_QUEUE = "agent:worktree:cleanup"
ORPHAN_LOCK_KEY = "lock:orphan_cleanup"

# Streaming session Redis key prefixes
SESSION_KEY = "session:stream:{}"
SESSION_LOOKUP_KEY = "session:stream:lookup:{}"
SESSION_INBOX_KEY = "session:inbox:{}"
SESSION_SUBSCRIBERS_KEY = "session:subscribers:{}"
SESSION_HISTORY_KEY = "session:history:{}"

# Streaming channels
MSG_CHANNEL = "session:msg:{}"
CTL_CHANNEL = "session:ctl:{}"


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(UTC).isoformat()


def sanitize_repo_key(repo: str) -> str:
    """Return a Redis-safe key fragment for a repository name.

    Uses double-dash to avoid collisions (e.g. ``owner/repo``
    and ``owner-repo`` would collide with single-dash).
    """
    return repo.replace("/", "--")


def streaming_lookup_key(
    repo: str, thread_id: str | int, workflow: str, thread_type: str = ""
) -> str:
    """Build the Redis lookup key for a streaming session token.

    Centralises the key format used by both SessionStore and
    StreamingSessionStore so the two modules cannot drift apart.

    When *thread_type* is provided the key includes it for precise
    matching; otherwise a legacy (pre-thread_type) key is returned
    for backwards compatibility.
    """
    safe_repo = sanitize_repo_key(repo)
    tid = str(thread_id)
    if thread_type:
        return SESSION_LOOKUP_KEY.format(f"{safe_repo}:{thread_type}:{tid}:{workflow}")
    return SESSION_LOOKUP_KEY.format(f"{safe_repo}:{tid}:{workflow}")


def decode_redis_hash(data: dict[bytes | str, bytes | str]) -> dict[str, str]:
    """Decode all keys and values in a Redis hash result from bytes to str.

    Redis returns bytes keys/values when decode_responses is False (the
    default for async clients).  This helper converts the entire dict so
    callers never need to repeat the isinstance/decode pattern.
    """
    return {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in data.items()
    }
