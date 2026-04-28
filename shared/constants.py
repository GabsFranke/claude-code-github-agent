"""Shared constants used across services.

Centralises values that were previously duplicated or hardcoded, so TTLs,
queue names, and limits are consistent and come from a single source.

Environment variable overrides:
    STREAMING_SESSION_TTL_HOURS  — default streaming session TTL (default: 720)
    HISTORY_MAX                  — max messages in Redis history list (default: 2000)
    JOB_TTL_SECONDS              — job data TTL in Redis (default: 3600)
    AUTO_APPROVE_TIMEOUT         — tool approval wait in seconds (default: 30)
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

# Tool approval auto-approve timeout (seconds).
AUTO_APPROVE_TIMEOUT = int(os.getenv("AUTO_APPROVE_TIMEOUT", "30"))

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
