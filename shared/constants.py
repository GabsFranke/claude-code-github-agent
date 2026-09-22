"""Shared constants used across services.

Centralises values that were previously duplicated or hardcoded, so TTLs,
queue names, and limits are consistent and come from a single source.

Environment variable overrides:
    STREAMING_SESSION_TTL_HOURS  — default streaming session TTL (default: 720)
    HISTORY_MAX                  — max messages in Redis history list (default: 2000)
    JOB_TTL_SECONDS              — job data TTL in Redis (default: 3600)
    MAX_AUTO_CONTINUES           — max auto-continue iterations (default: 10)
    WEBHOOK_DEDUP_TTL_SECONDS    — webhook delivery dedup window (default: 86400)
    MEMORY_WORKER_REPLICAS       — memory_worker instances to run (default: 0)
    MEMORY_WORKER_MODEL          — tier for the memory worker (default: haiku)
    RETROSPECTOR_REPLICAS        — retrospector_worker instances to run (default: 0)
    RETROSPECTOR_MODEL           — tier for the retrospector worker (default: sonnet)
"""

import logging
import os
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

# CLI aliases, never dated model ids: the Claude Code CLI maps each of these
# through ANTHROPIC_DEFAULT_<TIER>_MODEL, so ids stay in one place (the env)
# and cannot silently expire in code. See tests/shared/test_model_resolution.py.
MODEL_TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku")


def resolve_model_tier(env_var: str, default: str) -> str:
    """Read a model tier from the environment, falling back to ``default``.

    An unset or blank value yields the default; an unrecognised one logs a
    warning and yields the default, so a typo degrades rather than failing a
    worker at startup.
    """
    raw = (os.getenv(env_var) or "").strip().lower()
    if not raw:
        return default
    if raw not in MODEL_TIERS:
        logger.warning(
            "%s=%r is not one of %s; using %s",
            env_var,
            raw,
            ", ".join(MODEL_TIERS),
            default,
        )
        return default
    return raw


def replica_count(env_var: str, default: int = 0) -> int:
    """How many instances of an optional worker are configured; 0 means off.

    The same number is both the compose ``scale`` for the service and the
    switch the rest of the system reads, so a worker that has no container
    also has no work queued for it. Anything unparseable degrades to the
    default rather than failing a service at startup.
    """
    raw = (os.getenv(env_var) or "").strip()
    if not raw:
        return default
    try:
        count = int(raw)
    except ValueError:
        logger.warning(
            "%s=%r is not a whole number of replicas; using %d",
            env_var,
            raw,
            default,
        )
        return default
    return max(count, 0)


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

# Webhook delivery dedup window (24 hours).
#
# GitHub retries failed deliveries and lets maintainers redeliver manually,
# so the same ``X-GitHub-Delivery`` id can arrive more than once.  Delivery
# ids are remembered for this long to drop replays.  A manual redelivery
# after the window expires is processed again by design.
WEBHOOK_DEDUP_TTL_SECONDS = int(os.getenv("WEBHOOK_DEDUP_TTL_SECONDS", "86400"))

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

# A streaming session is considered orphaned when it is stuck at
# status="running" with no worktree_path/session_id ever set (its job never
# started executing) for longer than this. This happens when the job that
# was meant to service it expired from the pending queue before any sandbox
# worker picked it up — e.g. across a worker shutdown/restart gap — and gets
# dead-lettered instead of reaching the "processing" set that reclaim_stale_jobs
# scans. Without recovery, the session silently swallows every future event
# for that repo/issue/workflow forever. See request_processor.py and
# agent_worker/worker.py's recovery sweep.
STREAMING_SESSION_STALE_SECONDS = int(
    os.getenv("STREAMING_SESSION_STALE_SECONDS", "300")
)

# How often the agent_worker recovery sweep scans for orphaned streaming
# sessions (see above) and requeues fresh jobs for them.
SESSION_RECOVERY_SWEEP_INTERVAL_SECONDS = int(
    os.getenv("SESSION_RECOVERY_SWEEP_INTERVAL_SECONDS", "300")
)
SESSION_RECOVERY_LOCK_KEY = "lock:session_recovery_sweep"

# Transcript archival — durable storage for transcripts before
# worktree deletion so that transcripts survive TTL expiry.
TRANSCRIPT_ARCHIVE_DIR = "/var/transcripts"

# Streaming session Redis key prefixes
SESSION_KEY = "session:stream:{}"
SESSION_LOOKUP_KEY = "session:stream:lookup:{}"
SESSION_INBOX_KEY = "session:inbox:{}"
SESSION_SUBSCRIBERS_KEY = "session:subscribers:{}"
SESSION_HISTORY_KEY = "session:history:{}"

# Tracks the JobQueue job_id currently servicing a streaming session. Lets
# the orphan-recovery sweep (see STREAMING_SESSION_STALE_SECONDS above) check
# the job's REAL status (pending/processing/gone) instead of guessing from
# elapsed time + WorktreeLock presence alone — the lock can expire
# (DEFAULT_LOCK_TTL) well before a long-running job finishes, which caused
# the sweep to duplicate still-running jobs. A live job_id here always wins
# over a time-based staleness guess.
SESSION_JOB_KEY = "session:stream:job:{}"

# Streaming channels
MSG_CHANNEL = "session:msg:{}"
CTL_CHANNEL = "session:ctl:{}"

# Webhook delivery deduplication
WEBHOOK_DEDUP_KEY = "agent:webhook:delivery:{}"

# Session-aware job deduplication lock
SESSION_DEDUP_KEY = "agent:session:lock:{}"
SESSION_DEDUP_LOCK_TTL = 30  # seconds — safety net, cleared by worker on completion


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

    Centralises the key format so all store modules cannot drift apart.

    When *thread_type* is provided the key includes it for precise
    matching; otherwise a legacy (pre-thread_type) key is returned
    for backwards compatibility.
    """
    safe_repo = sanitize_repo_key(repo)
    tid = str(thread_id)
    if thread_type:
        return SESSION_LOOKUP_KEY.format(f"{safe_repo}:{thread_type}:{tid}:{workflow}")
    return SESSION_LOOKUP_KEY.format(f"{safe_repo}:{tid}:{workflow}")


def streaming_session_key(token: str) -> str:
    """Build the Redis key for a streaming session hash.

    Redis type: Hash
    TTL: ``DEFAULT_SESSION_TTL_SECONDS`` (set on creation)
    Written by: ``SessionStore.create_session()``
    Read by: ``SessionStore.get_streaming_session()``, ``set_completed()``,
             ``set_running()``, ``update_session_id()``, etc.

    Returns a key like ``session:stream:{token}``.
    """
    return SESSION_KEY.format(token)


def streaming_job_key(token: str) -> str:
    """Build the Redis key tracking the JobQueue job_id currently servicing
    a streaming session.

    Redis type: String
    TTL: ``DEFAULT_SESSION_TTL_SECONDS``
    Written by: ``SessionStore.set_job_id()``
    Read by: ``SessionStore.get_job_id()``

    Returns a key like ``session:stream:job:{token}``.
    """
    return SESSION_JOB_KEY.format(token)


def inbox_key(token: str) -> str:
    """Build the Redis key for a session's message inbox.

    Redis type: List
    TTL: ``DEFAULT_SESSION_TTL_SECONDS``
    Written by: ``ControlChannel._dispatch()``,
                ``SessionStore.push_inbox_message()``
    Read by: ``SessionStore.pop_inbox_messages()``

    Returns a key like ``session:inbox:{token}``.
    """
    return SESSION_INBOX_KEY.format(token)


def subscribers_key(token: str) -> str:
    """Build the Redis key for a session's active subscriber count.

    Redis type: Integer (String internally)
    TTL: ``DEFAULT_SESSION_TTL_SECONDS`` (set on first INCR via Lua script)
    Written by: ``SessionStore.increment_subscribers()``,
                ``SessionStore.decrement_subscribers()``
    Read by: ``SessionStore.has_subscribers()``

    Returns a key like ``session:subscribers:{token}``.
    """
    return SESSION_SUBSCRIBERS_KEY.format(token)


def history_key(token: str) -> str:
    """Build the Redis key for a session's short-lived message history.

    Redis type: List
    TTL: ``HISTORY_TTL_SECONDS``
    Written by: ``SessionStreamBridge.publish()``
    Read by: ``SessionStore.get_history()``

    Returns a key like ``session:history:{token}``.
    """
    return SESSION_HISTORY_KEY.format(token)


def session_key(repo: str, thread_type: str, thread_id: str, workflow: str) -> str:
    """Build the Redis key for a persistent session mapping.

    Redis type: Hash
    TTL: ``ConversationConfig.ttl_hours`` (default ``DEFAULT_SESSION_TTL_HOURS``)
    Written by: ``SessionStore.save_session()``
    Read by: ``SessionStore.get_session()``, ``close_session()``,
             ``expire_session()``, etc.

    Returns a key like ``session:map:{safe_repo}:{thread_type}:{thread_id}:{workflow}``.
    """
    safe_repo = sanitize_repo_key(repo)
    return f"session:map:{safe_repo}:{thread_type}:{thread_id}:{workflow}"


def session_pattern(repo: str) -> str:
    """Build a Redis SCAN pattern for all session mappings of a repository.

    Redis type: Pattern (used with SCAN)
    Used by: ``SessionStore.list_sessions()``

    Returns a pattern like ``session:map:{safe_repo}:*``.
    """
    safe_repo = sanitize_repo_key(repo)
    return f"session:map:{safe_repo}:*"


def session_cleanup_key(repo: str, thread_type: str, thread_id: str) -> str:
    """Build the Redis key for worktree cleanup coordination.

    This key prevents multiple workers from attempting to clean up
    the same session worktree simultaneously.

    Redis type: String (with TTL)
    Written by: ``SessionStore.close_session()`` / cleanup orchestrators
    Read by: Worktree cleanup workers

    Returns a key like ``session:cleanup:{safe_repo}:{thread_type}:{thread_id}``.
    """
    safe_repo = sanitize_repo_key(repo)
    return f"session:cleanup:{safe_repo}:{thread_type}:{thread_id}"


def session_dedup_key(
    repo: str, thread_type: str, thread_id: str, workflow: str
) -> str:
    """Build the Redis key for session-aware job deduplication.

    Prevents duplicate jobs when multiple requests arrive for the same
    session within a short window.  Uses SETNX for atomicity so exactly
    one request wins the race and creates a job; the others inject
    their message into the running session instead.

    Redis type: String (with short TTL — safety net)
    TTL: ``SESSION_DEDUP_LOCK_TTL`` (30 s, cleared proactively by worker)
    Written by: ``RequestProcessor._execute()`` (SETNX before job creation)
    Cleared by: ``JobProcessor._cleanup()`` (finally block)

    Returns a key like ``agent:session:lock:{safe_repo}:{thread_type}:{thread_id}:{workflow}``.
    """
    safe_repo = sanitize_repo_key(repo)
    return SESSION_DEDUP_KEY.format(f"{safe_repo}:{thread_type}:{thread_id}:{workflow}")


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


def webhook_dedup_key(delivery_id: str) -> str:
    """Build the Redis key that records a processed GitHub webhook delivery.

    GitHub assigns every delivery a unique ``X-GitHub-Delivery`` UUID and
    reuses it for automatic retries and manual redeliveries.  Claiming the
    key with SETNX makes the receiver idempotent: exactly one delivery with
    a given id gets queued, replays are dropped.

    Redis type: String (with TTL)
    TTL: ``WEBHOOK_DEDUP_TTL_SECONDS`` (24 h)
    Written by: ``WebhookDeduplicator.claim()`` (SETNX on receipt)
    Cleared by: ``WebhookDeduplicator.release()`` (only when handling failed
                unexpectedly, so GitHub's retry can be processed)

    Returns a key like ``agent:webhook:delivery:{delivery_id}``.
    """
    return WEBHOOK_DEDUP_KEY.format(delivery_id)
