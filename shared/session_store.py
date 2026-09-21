"""Session persistence manager for conversation continuity across GitHub comments.

Stores session metadata in Redis so the bot can resume conversations when
users reply in the same thread.  Sessions are scoped by repo + thread type +
thread ID + workflow, and expire after a configurable TTL.

The merged store combines the old ``SessionInfo`` and ``StreamingSessionData``
schemas into ``UnifiedSessionInfo`` — expect higher method count than either
individual store.
"""  # pylint: disable=too-many-lines

import json
import logging
import re
import shutil
import warnings
from collections.abc import Awaitable
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, cast

try:
    import redis.asyncio as aioredis

    RedisClient = aioredis.Redis
except ImportError:
    RedisClient = Any  # type: ignore[assignment, misc]

# redis-py shares one set of command signatures between its sync and async
# clients, so commands with a concrete return type are annotated
# ``Awaitable[T] | T``.  The async client always returns the awaitable half,
# but mypy cannot know that, so calls below are cast to ``Awaitable[T]``
# before being awaited.  The casts are erased at runtime.

from pydantic import BaseModel, Field

from .constants import (
    DEFAULT_SESSION_TTL_HOURS,
    TRANSCRIPT_ARCHIVE_DIR,
    _now_iso,
    decode_redis_hash,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Re-export public key builders from constants.py
# (Task 2 consolidated them there — we expose them here for convenience)
# ---------------------------------------------------------------------------

from .constants import (  # noqa: E402, F401  (intentional re-exports)
    DEFAULT_SESSION_TTL_SECONDS,
    history_key,
    inbox_key,
    session_cleanup_key,
    session_key,
    session_pattern,
    streaming_job_key,
    streaming_lookup_key,
    streaming_session_key,
    subscribers_key,
)

__all__ = [
    "ConversationConfig",
    "SessionInfo",
    "SessionStatus",
    "SessionStore",
    "SessionStoreConfig",
    "UnifiedSessionInfo",
    "history_key",
    "inbox_key",
    "resolve_thread_type",
    "session_cleanup_key",
    "session_key",
    "session_pattern",
    "streaming_job_key",
    "streaming_lookup_key",
    "streaming_session_key",
    "subscribers_key",
]

# ---------------------------------------------------------------------------
# Session status enum (Track 1 — merged store schema)
# ---------------------------------------------------------------------------


class SessionStatus(StrEnum):
    """Unified session lifecycle status.

    Replaces the ad-hoc ``Literal["active", "completed", "expired"]`` in
    ``SessionInfo`` and the free-form ``str`` status in ``StreamingSessionData``.
    """

    active = "active"
    running = "running"
    completed = "completed"
    error = "error"
    expired = "expired"


class SessionInfo(BaseModel):
    """Metadata for a persisted SDK session."""

    session_id: str
    repo: str
    thread_type: Literal["pr", "issue", "discussion"]
    thread_id: str
    workflow_name: str
    ref: str
    worktree_path: str
    created_at: str
    last_run: str
    turn_count: int = 0
    status: Literal["active", "completed", "expired"] = "active"
    summary: str | None = None
    streaming_token: str | None = None


class UnifiedSessionInfo(BaseModel):
    """Unified session metadata — merges ALL fields from ``SessionInfo`` (13 fields)
    and ``StreamingSessionData`` (8 unique fields) into a single model.

    This is the canonical session schema for the merged SessionStore.
    Every field includes a ``source`` note in its docstring documenting
    which original store it originated from.
    """

    # ---- Fields from SessionInfo (session_store.py) ----
    session_id: str = Field(description="SDK session identifier. Source: SessionInfo")
    repo: str = Field(
        description="GitHub repository (owner/repo). Source: SessionInfo + StreamingSessionData"
    )
    thread_type: Literal["pr", "issue", "discussion"] = Field(
        description="Thread type. Source: SessionInfo + StreamingSessionData"
    )
    thread_id: str = Field(
        description="Thread number (issue/PR/discussion #). Source: SessionInfo"
    )
    workflow_name: str = Field(
        description="Workflow name. Source: SessionInfo (mapped from StreamingSessionData.workflow)"
    )
    ref: str = Field(
        description="Git reference (branch/tag). Source: SessionInfo + StreamingSessionData"
    )
    worktree_path: str = Field(
        description="Local filesystem path to the worktree. Source: SessionInfo"
    )
    created_at: str = Field(
        description="ISO 8601 timestamp of first session creation. Source: SessionInfo"
    )
    last_run: str = Field(
        description="ISO 8601 timestamp of most recent run. Source: SessionInfo"
    )
    turn_count: int = Field(
        default=0,
        description="Cumulative turn count across continuations. Source: SessionInfo",
    )
    status: SessionStatus = Field(
        default=SessionStatus.active,
        description="Session lifecycle status. Source: SessionInfo + StreamingSessionData (unified)",
    )
    summary: str | None = Field(
        default=None,
        description="Conversation summary injected on resume. Source: SessionInfo",
    )
    streaming_token: str | None = Field(
        default=None,
        description="Streaming session token linking to the store. "
        "Maps to StreamingSessionData.token. Source: SessionInfo.streaming_token + StreamingSessionData.token",
    )

    # ---- Fields from StreamingSessionData (streaming_session.py) ----
    installation_id: str = Field(
        default="",
        description="GitHub App installation ID. Source: StreamingSessionData",
    )
    initial_query: str = Field(
        default="",
        description="The GitHub comment that triggered this session. Source: StreamingSessionData",
    )
    conversation_config: str = Field(
        default="",
        description="JSON-encoded conversation persistence settings. Source: StreamingSessionData",
    )
    transcript_path: str = Field(
        default="",
        description="Filesystem path to the session transcript JSONL file. Source: StreamingSessionData",
    )
    run_count: int = Field(
        default=1,
        description="Number of times this session has been run (includes resume). Source: StreamingSessionData",
    )
    session_proxy_url: str = Field(
        default="",
        description="Public URL of session_proxy for GitHub comments. Source: StreamingSessionData",
    )
    issue_number: str = Field(
        default="",
        description="GitHub issue/PR number (string form). Source: StreamingSessionData",
    )
    user: str = Field(
        default="",
        description="GitHub username who triggered the session. Source: StreamingSessionData",
    )


class SessionStoreConfig(BaseModel):
    """Configuration for the merged session store.

    Extends the existing ``ConversationConfig`` pattern with explicit
    streaming-aware settings. Kept separate from ``ConversationConfig``
    so workflow-level conversation settings can evolve independently
    from store-level infrastructure settings.
    """

    persist: bool = Field(default=False, description="Enable session persistence")
    ttl_hours: int = Field(
        default=DEFAULT_SESSION_TTL_HOURS,
        description="Session TTL in hours (default from constants)",
    )
    max_turns: int = Field(
        default=50, description="Max total turns across continuations"
    )
    auto_continue: bool = Field(
        default=False, description="Auto-resume on replies without explicit -c flag"
    )
    summary_fallback: bool = Field(
        default=True, description="Inject conversation summary when full resume fails"
    )


class ConversationConfig(BaseModel):
    """Per-workflow conversation persistence settings."""

    persist: bool = Field(default=False, description="Enable session persistence")
    ttl_hours: int = Field(
        default=DEFAULT_SESSION_TTL_HOURS,
        description="Session TTL in hours (default from constants)",
    )
    max_turns: int = Field(
        default=50, description="Max total turns across continuations"
    )
    auto_continue: bool = Field(
        default=False, description="Auto-resume on replies without explicit -c flag"
    )
    summary_fallback: bool = Field(
        default=True, description="Inject conversation summary when full resume fails"
    )


def resolve_thread_type(event_data: dict) -> Literal["pr", "issue", "discussion"]:
    """Determine thread type from webhook payload.

    Args:
        event_data: Webhook event data containing event_type and payload hints.

    Returns:
        One of "pr", "issue", or "discussion".
    """
    # Check for explicit PR indicators
    event_type = event_data.get("event_type", "")
    if event_type.startswith("pull_request"):
        return "pr"

    # issue_comment on a PR has a pull_request field in the issue
    if event_type == "issue_comment":
        payload = event_data.get("payload", {})
        if isinstance(payload, dict):
            issue = payload.get("issue", {})
            if isinstance(issue, dict) and issue.get("pull_request"):
                return "pr"
        # Some webhook processors embed it differently
        if event_data.get("is_pr"):
            return "pr"

    # Discussion events
    if event_type.startswith("discussion"):
        return "discussion"

    return "issue"


def _session_key(repo: str, thread_type: str, thread_id: str, workflow: str) -> str:
    """Build the Redis key for a session mapping.

    Deprecated: Use ``session_key()`` from ``shared.constants`` instead.
    """
    warnings.warn(
        "_session_key() is deprecated — use shared.constants.session_key() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return session_key(repo, thread_type, thread_id, workflow)


def _session_pattern(repo: str) -> str:
    """Build a Redis SCAN pattern for all sessions of a repo.

    Deprecated: Use ``session_pattern()`` from ``shared.constants`` instead.
    """
    warnings.warn(
        "_session_pattern() is deprecated — use shared.constants.session_pattern() instead",
        DeprecationWarning,
        stacklevel=2,
    )
    return session_pattern(repo)


# ============================================================================
# SessionStore — merged store
# ============================================================================

# Lua scripts for atomic subscriber count operations
_INCR_SUBSCRIBERS_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

_DECR_SUBSCRIBERS_LUA = """
local count = redis.call('DECR', KEYS[1])
if count <= 0 then
    redis.call('DEL', KEYS[1])
end
return count
"""


class SessionStore:  # pylint: disable=too-many-public-methods
    """Unified session store.

    Manages both persistent session mappings (``session:map:*``) and
    streaming session metadata (``session:stream:*``) in a single class.

    Redis schema::

        session:map:{safe_repo}:{type}:{id}:{wf}     Hash  — persistent session
        session:stream:{token}                        Hash  — streaming metadata
        session:stream:lookup:{repo}:{id}:{wf}        String — token lookup
        session:inbox:{token}                         List  — user messages
        session:subscribers:{token}                   Int   — WebSocket count
        session:history:{token}                       List  — short-lived history
    """

    def __init__(self, redis_client: RedisClient):
        self.redis = redis_client

    # ------------------------------------------------------------------
    # Persistence layer (session:map:* keys)
    # ------------------------------------------------------------------

    async def save_session(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        session_id: str,
        worktree_path: str,
        ref: str,
        turn_count: int = 0,
        summary: str | None = None,
        ttl_hours: int = DEFAULT_SESSION_TTL_HOURS,
        streaming_token: str | None = None,
        installation_id: str = "",
        initial_query: str = "",
        conversation_config: str = "",
        transcript_path: str = "",
        run_count: int = 1,
        session_proxy_url: str = "",
        issue_number: str = "",
        user: str = "",
    ) -> None:
        """Create or update a persistent session mapping.

        Writes the base fields atomically via ``HSET mapping=``, then
        conditionally sets *created_at* (HSetNX), *turn_count* (HINCRBY
        when > 0), *summary* and *streaming_token* (only when explicitly
        provided).
        """
        key = session_key(repo, thread_type, thread_id, workflow)
        now = datetime.now(UTC).isoformat()
        redis: Any = self.redis

        mapping: dict[str, str] = {
            "session_id": str(session_id),
            "repo": str(repo),
            "thread_type": str(thread_type),
            "thread_id": str(thread_id),
            "workflow_name": str(workflow),
            "ref": str(ref),
            "worktree_path": str(worktree_path),
            "last_run": str(now),
            "status": "active",
            "turn_count": "0",
            "installation_id": str(installation_id),
            "initial_query": str(initial_query),
            "conversation_config": str(conversation_config),
            "transcript_path": str(transcript_path),
            "run_count": str(run_count),
            "session_proxy_url": str(session_proxy_url),
            "issue_number": str(issue_number),
            "user": str(user),
        }
        await redis.hset(key, mapping=mapping)

        # Preserve created_at across re-saves
        await redis.hsetnx(key, "created_at", now)

        # Accumulate turn_count atomically
        if turn_count > 0:
            await redis.hincrby(key, "turn_count", turn_count)

        # Only update optional fields when explicitly provided
        if summary is not None:
            await redis.hset(key, "summary", summary)
        if streaming_token is not None:
            await redis.hset(key, "streaming_token", streaming_token)

        await redis.expire(key, ttl_hours * 3600)
        logger.info(
            f"[SessionStore] Saved session {session_id[:8]}... for "
            f"{repo}/{thread_type}/{thread_id}/{workflow} (ttl={ttl_hours}h)"
        )

    async def get_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> UnifiedSessionInfo | None:
        """Look up a persistent session, returning ``None`` if absent or corrupt."""
        key = session_key(repo, thread_type, thread_id, workflow)
        redis: Any = self.redis
        data = await redis.hgetall(key)
        if not data:
            return None
        decoded = decode_redis_hash(data)
        try:
            return UnifiedSessionInfo.model_validate(decoded)  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning(f"[SessionStore] Corrupt session data at {key}: {e}")
            return None

    async def close_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> None:
        """Close a session and clean up all associated Redis keys.

        Deletes the persistent hash, and if a *streaming_token* is present,
        also cleans up the streaming session hash, lookup keys (typed + legacy),
        inbox, subscribers, and history.
        """
        key = session_key(repo, thread_type, thread_id, workflow)
        info = await self.get_session(repo, thread_type, thread_id, workflow)
        if info and info.streaming_token:
            try:
                await self._cleanup_streaming(
                    info.streaming_token, repo, thread_id, workflow, thread_type
                )
            except Exception as e:
                logger.warning(
                    f"[SessionStore] Failed to clean up streaming for {key}: {e}"
                )
        await self.redis.delete(key)
        logger.info(
            f"[SessionStore] Closed session {repo}/{thread_type}/{thread_id}/{workflow}"
        )

    async def archive_transcript(
        self, session_id: str, repo: str, worktree_path: str
    ) -> bool:
        """Copy the session transcript JSONL to durable archive storage.

        Called before worktree deletion to preserve transcripts beyond
        Redis TTL expiry.  Ephemeral sessions (worktrees under
        ``.../ephemeral/``) are skipped — their transcripts are transient.

        Returns ``True`` if the transcript was archived successfully,
        ``False`` if it was skipped or the source file was not found.
        """
        if "/ephemeral/" in worktree_path.replace("\\", "/"):
            logger.debug(
                f"[SessionStore] Skipping transcript archival for ephemeral "
                f"session {session_id[:8]}..."
            )
            return False

        if not re.match(r"^[a-zA-Z0-9_-]+$", session_id):
            logger.warning(
                f"[SessionStore] Invalid session_id for archival: {session_id[:8]}..."
            )
            return False

        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            logger.warning(
                f"[SessionStore] Projects dir missing, cannot archive "
                f"transcript for {session_id[:8]}..."
            )
            return False

        source_path: Path | None = None
        sanitized = re.sub(r"[^a-zA-Z0-9]", "-", worktree_path)
        direct = projects_dir / sanitized / f"{session_id}.jsonl"
        if direct.exists():
            source_path = direct
        else:
            count = 0
            for project_dir in projects_dir.iterdir():
                if count >= 200:
                    break
                count += 1
                if not project_dir.is_dir():
                    continue
                candidate = project_dir / f"{session_id}.jsonl"
                if candidate.exists():
                    source_path = candidate
                    break

        if source_path is None:
            logger.warning(
                f"[SessionStore] No transcript found for session {session_id[:8]}..."
            )
            return False

        archive_dir = Path(TRANSCRIPT_ARCHIVE_DIR) / repo
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / f"{session_id}.jsonl"
        shutil.copy2(source_path, dest)
        logger.info(f"[SessionStore] Archived transcript {session_id[:8]}... to {dest}")

        meta_source = source_path.with_suffix(".meta.json")
        if meta_source.exists():
            meta_dest = archive_dir / f"{session_id}.meta.json"
            shutil.copy2(meta_source, meta_dest)

        return True

    async def list_sessions_by_worktree(
        self, worktree_path: str
    ) -> list[UnifiedSessionInfo]:
        """Return sessions whose ``worktree_path`` matches *worktree_path*.

        Uses a Redis SCAN across ALL repositories (``"*"`` wildcard) then
        filters in-process.  Suitable for orphan-cleanup coordination
        where the repo is not known in advance.
        """
        all_sessions = await self.list_sessions("*")
        return [s for s in all_sessions if s.worktree_path == worktree_path]

    async def list_sessions(self, repo: str) -> list[UnifiedSessionInfo]:
        """List all active sessions for a repository via SCAN."""
        pattern = session_pattern(repo)
        sessions: list[UnifiedSessionInfo] = []
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            for key in keys:
                redis: Any = self.redis
                data = await redis.hgetall(key)
                if data:
                    try:
                        decoded = decode_redis_hash(data)
                        sessions.append(UnifiedSessionInfo.model_validate(decoded))
                    except Exception as e:
                        logger.warning(
                            f"[SessionStore] Skipping corrupt session at {key}: {e}"
                        )
            if cursor == 0:
                break
        return sessions

    async def expire_session(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        ttl_hours: int = 72,
    ) -> None:
        """Set a new TTL on a session and propagate to streaming sub-keys."""
        key = session_key(repo, thread_type, thread_id, workflow)
        ttl_seconds = ttl_hours * 3600
        result = await self.redis.expire(key, ttl_seconds)
        if not result:
            logger.debug(
                f"[SessionStore] Session key {key} does not exist, skipping TTL"
            )
            return
        # Propagate TTL to streaming sub-keys
        info = await self.get_session(repo, thread_type, thread_id, workflow)
        if info and info.streaming_token:
            try:
                await self._propagate_streaming_ttl(
                    info.streaming_token,
                    repo,
                    thread_id,
                    workflow,
                    ttl_hours,
                    thread_type,
                )
            except Exception as e:
                logger.warning(
                    f"[SessionStore] Failed to propagate streaming TTL for {key}: {e}"
                )
        logger.info(
            f"[SessionStore] Set TTL {ttl_hours}h on session "
            f"{repo}/{thread_type}/{thread_id}/{workflow}"
        )

    async def update_summary(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        summary: str,
    ) -> None:
        """Update the conversation summary field."""
        key = session_key(repo, thread_type, thread_id, workflow)
        redis: Any = self.redis
        try:
            await redis.hset(key, "summary", summary)
        except Exception as e:
            logger.warning(f"[SessionStore] Failed to update summary for {key}: {e}")

    async def increment_turn_count(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        additional_turns: int,
    ) -> None:
        """Increment turn_count and refresh last_run."""
        key = session_key(repo, thread_type, thread_id, workflow)
        last_run = datetime.now(UTC).isoformat()
        redis: Any = self.redis
        try:
            await redis.hincrby(key, "turn_count", additional_turns)
            await redis.hset(key, "last_run", last_run)
        except Exception as e:
            logger.warning(
                f"[SessionStore] Failed to increment turn count for {key}: {e}"
            )

    # ------------------------------------------------------------------
    # Streaming layer (session:stream:* keys)
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_streaming_data(data: dict[str, str]) -> dict[str, str]:
        """Normalize legacy streaming session fields to UnifiedSessionInfo schema.

        The old ``StreamingSessionData`` used ``issue_number`` / ``workflow``
        and was missing ``worktree_path`` / ``created_at`` / ``last_run``.
        This shim maps those fields so ``model_validate`` succeeds against
        sessions written before the merge.
        """
        data = data.copy()  # don't mutate caller's dict
        # Map old field names to new
        if "issue_number" in data and "thread_id" not in data:
            data["thread_id"] = data["issue_number"]
        if "workflow" in data and "workflow_name" not in data:
            data["workflow_name"] = data["workflow"]
        # Supply defaults for fields missing from old schema
        data.setdefault("worktree_path", "")
        data.setdefault("created_at", _now_iso())
        data.setdefault("last_run", _now_iso())
        return data

    async def create_session(
        self,
        token: str,
        repo: str,
        issue_number: int,
        workflow: str,
        session_proxy_url: str = "",
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        installation_id: str = "",
        initial_query: str = "",
        thread_type: Literal["pr", "issue", "discussion"] = "issue",
        ref: str = "main",
        user: str = "",
        conversation_config: str = "",
        session_id: str = "",
    ) -> None:
        """Create a streaming session record with lookup key in one pipeline."""
        key = streaming_session_key(token)
        data: dict[str, str] = {
            "token": token,
            "repo": repo,
            "issue_number": str(issue_number),
            "thread_id": str(issue_number),
            "workflow": workflow,
            "workflow_name": workflow,
            "session_proxy_url": session_proxy_url,
            "status": "running",
            "installation_id": installation_id,
            "initial_query": initial_query,
            "thread_type": thread_type,
            "ref": ref,
            "user": user,
            "conversation_config": conversation_config,
            "session_id": session_id,
            "transcript_path": "",
            "run_count": "1",
            "worktree_path": "",
            "created_at": _now_iso(),
            "last_run": _now_iso(),
        }
        pipeline = self.redis.pipeline()
        pipeline.hset(key, mapping=data)  # type: ignore[arg-type]
        pipeline.expire(key, ttl_seconds)
        lk = streaming_lookup_key(
            repo, str(issue_number), workflow, thread_type=thread_type
        )
        pipeline.setex(lk, ttl_seconds, token)
        await pipeline.execute()
        logger.info(
            f"[SessionStore] Created streaming session {token[:8]}... "
            f"for {repo}#{issue_number} (ttl={ttl_seconds}s)"
        )

    async def get_streaming_session(self, token: str) -> UnifiedSessionInfo | None:
        """Get streaming session metadata by token.

        Returns ``UnifiedSessionInfo`` or ``None`` if the token doesn't exist
        or the data is corrupt.
        """
        key = streaming_session_key(token)
        data = await cast(Awaitable[dict], self.redis.hgetall(key))
        if not data:
            return None
        decoded = decode_redis_hash(data)
        decoded = self._normalize_streaming_data(decoded)
        try:
            return UnifiedSessionInfo.model_validate(decoded)  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning(
                f"[SessionStore] Corrupt streaming data for {token[:8]}...: {e}"
            )
            return None

    async def find_session(
        self,
        repo: str,
        issue_number: int,
        workflow: str,
        thread_type: str = "",
    ) -> str | None:
        """Find a streaming token by repo/issue/workflow.

        Returns the token regardless of status, or ``None`` if not found.
        Stale lookup entries (hash expired) are cleaned up automatically.
        Falls back to legacy lookup (without thread_type) when the typed
        lookup returns nothing.
        """
        lk = streaming_lookup_key(
            repo, str(issue_number), workflow, thread_type=thread_type
        )
        raw = await self.redis.get(lk)
        if not raw:
            if thread_type:
                lk_legacy = streaming_lookup_key(
                    repo, str(issue_number), workflow, thread_type=""
                )
                raw = await self.redis.get(lk_legacy)
                if not raw:
                    return None
            else:
                return None
        if isinstance(raw, bytes):
            token_val: str = raw.decode()
        else:
            token_val = raw  # type: ignore[assignment]
        stream_key = streaming_session_key(token_val)
        exists = await cast(Awaitable[dict], self.redis.hgetall(stream_key))
        if exists:
            return token_val
        await self.redis.delete(lk)
        return None

    async def find_active_session(
        self,
        repo: str,
        issue_number: int,
        workflow: str,
        thread_type: str = "",
    ) -> str | None:
        """Find an active (running) streaming token.

        Returns the token if found and status is ``"running"``, otherwise ``None``.
        """
        token = await self.find_session(
            repo, issue_number, workflow, thread_type=thread_type
        )
        if not token:
            return None
        stream_key = streaming_session_key(token)
        data = await cast(Awaitable[dict], self.redis.hgetall(stream_key))
        if not data:
            return None
        decoded = decode_redis_hash(data)
        if decoded.get("status") == "running":
            return token
        return None

    async def list_stale_running_sessions(
        self, stale_after_seconds: int
    ) -> list[UnifiedSessionInfo]:
        """Scan all streaming sessions stuck at status="running" whose job
        never actually started (no worktree_path/session_id) and whose
        last_run is older than *stale_after_seconds*.

        These are orphans left behind when a job expires from the pending
        queue before any sandbox worker ever picks it up (e.g. a restart
        gap), which skips the "processing" set entirely and so is invisible
        to ``JobQueue.reclaim_stale_jobs()``. Callers should still confirm no
        ``WorktreeLock`` is held before treating a result as truly dead.
        """
        pattern = streaming_session_key("*")
        cutoff = datetime.now(UTC).timestamp() - stale_after_seconds
        stale: list[UnifiedSessionInfo] = []
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(
                cursor=cursor, match=pattern, count=200
            )
            for key in keys:
                # streaming_session_key("*") also matches lookup keys
                # (session:stream:lookup:...); session hash keys have
                # exactly one segment after "session:stream:".
                if key.count(":") != 2:
                    continue
                token = key.rsplit(":", 1)[-1]
                data = await cast(Awaitable[dict], self.redis.hgetall(key))
                if not data:
                    continue
                try:
                    decoded = decode_redis_hash(data)
                    decoded = self._normalize_streaming_data(decoded)
                    decoded.setdefault("streaming_token", token)
                    info = UnifiedSessionInfo.model_validate(decoded)
                except Exception as e:
                    logger.warning(
                        f"[SessionStore] Skipping corrupt streaming session at {key}: {e}"
                    )
                    continue
                if info.status != SessionStatus.running:
                    continue
                if info.worktree_path or info.session_id:
                    continue
                try:
                    last_run_ts = datetime.fromisoformat(info.last_run).timestamp()
                except ValueError:
                    continue
                if last_run_ts < cutoff:
                    stale.append(info)
            if cursor == 0:
                break
        return stale

    async def set_completed(
        self,
        token: str,
        is_error: bool = False,
        repo: str | None = None,
        issue_number: int | None = None,
        workflow: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Mark a streaming session as completed or errored.

        When *session_id* is provided it is atomically updated with the
        status change to prevent a race between set_completed and
        update_session_id.
        """
        key = streaming_session_key(token)
        status = "error" if is_error else "completed"
        if session_id:
            await cast(
                Awaitable[int],
                self.redis.hset(
                    key, mapping={"status": status, "session_id": session_id}
                ),
            )
        else:
            await cast(Awaitable[int], self.redis.hset(key, "status", status))
        logger.info(f"[SessionStore] Streaming session {token[:8]}... -> {status}")

    async def set_running(
        self, token: str, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    ) -> None:
        """Reset streaming session to running (for auto-continue / resume).

        Clears the stale *session_id* from the previous run so a fresh
        SDK session can be started.  Preserves *transcript_path* so the
        SPA can still load conversation history while the new run begins.
        """
        key = streaming_session_key(token)
        await cast(
            Awaitable[int],
            self.redis.hset(
                key,
                mapping={
                    "status": "running",
                    "session_id": "",
                    "last_run": _now_iso(),
                },
            ),
        )
        await self.redis.expire(key, ttl_seconds)
        logger.info(f"[SessionStore] Streaming session {token[:8]}... -> running")

    async def set_job_id(self, token: str, job_id: str) -> None:
        """Record the JobQueue job_id currently servicing a streaming session.

        Lets the orphan-recovery sweep check the job's real status instead
        of guessing from elapsed time — see ``streaming_job_key``.
        """
        await self.redis.set(
            streaming_job_key(token), job_id, ex=DEFAULT_SESSION_TTL_SECONDS
        )

    async def get_job_id(self, token: str) -> str | None:
        """Return the JobQueue job_id last recorded for a streaming session."""
        raw = await self.redis.get(streaming_job_key(token))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def delete_session(self, token: str) -> None:
        """Delete a streaming session and its sub-keys (inbox, subscribers)."""
        key = streaming_session_key(token)
        await self.redis.delete(key)
        ibx = inbox_key(token)
        await self.redis.delete(ibx)
        sub = subscribers_key(token)
        await self.redis.delete(sub)
        logger.info(f"[SessionStore] Deleted streaming session {token[:8]}...")

    async def set_ttl(self, token: str, ttl_seconds: int) -> None:
        """Set TTL on streaming session + inbox + subscribers keys."""
        key = streaming_session_key(token)
        await self.redis.expire(key, ttl_seconds)
        ibx = inbox_key(token)
        await self.redis.expire(ibx, ttl_seconds)
        sub = subscribers_key(token)
        await self.redis.expire(sub, ttl_seconds)
        logger.debug(
            f"[SessionStore] Set TTL {ttl_seconds}s on streaming {token[:8]}..."
        )

    # ------------------------------------------------------------------
    # Subscriber count (atomic Lua scripts)
    # ------------------------------------------------------------------

    async def increment_subscribers(self, token: str) -> int:
        """Atomically increment subscriber count. Returns new count."""
        key = subscribers_key(token)
        count = await cast(
            Awaitable[Any],
            self.redis.eval(
                _INCR_SUBSCRIBERS_LUA, 1, key, str(DEFAULT_SESSION_TTL_SECONDS)
            ),
        )
        return int(count)

    async def decrement_subscribers(self, token: str) -> int:
        """Atomically decrement subscriber count (floor 0). Returns new count."""
        key = subscribers_key(token)
        count = await cast(
            Awaitable[Any], self.redis.eval(_DECR_SUBSCRIBERS_LUA, 1, key)
        )
        return int(count)

    async def has_subscribers(self, token: str) -> bool:
        """Return ``True`` if at least one browser is connected."""
        key = subscribers_key(token)
        raw = await self.redis.get(key)
        if raw is None:
            return False
        count = int(raw.decode() if isinstance(raw, bytes) else raw)
        return count > 0

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------

    async def get_history(self, token: str) -> list[dict]:
        """Fetch the full persistent message history (LRANGE + JSON parse)."""
        key = history_key(token)
        raw_messages = await cast(Awaitable[list], self.redis.lrange(key, 0, -1))
        result: list[dict] = []
        for raw in raw_messages:
            try:
                text = raw.decode() if isinstance(raw, bytes) else raw
                result.append(json.loads(text))
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # Inbox
    # ------------------------------------------------------------------

    async def push_inbox_message(self, token: str, content: str) -> None:
        """Push a user message into the session inbox."""
        ibx = inbox_key(token)
        message_data = json.dumps({"type": "user_message", "content": content})
        await cast(Awaitable[int], self.redis.rpush(ibx, message_data))
        await self.redis.expire(ibx, DEFAULT_SESSION_TTL_SECONDS)

    async def pop_inbox_messages(self, token: str) -> list[str]:
        """Atomically drain all messages from the inbox.

        Returns list of message content strings, oldest first.
        Raises on Redis eval failures to prevent silent message loss.
        """
        ibx = inbox_key(token)

        lua_drain = """
        local items = redis.call('LRANGE', KEYS[1], 0, -1)
        redis.call('DEL', KEYS[1])
        return items
        """
        try:
            raw_items = await cast(Awaitable[Any], self.redis.eval(lua_drain, 1, ibx))
        except Exception as e:
            logger.error(f"[SessionStore] Failed to drain inbox for {token}: {e}")
            raise

        messages: list[str] = []
        for raw in raw_items or []:
            try:
                text = raw.decode() if isinstance(raw, bytes) else raw
                data = json.loads(text)
                if data.get("type") == "user_message" and data.get("content"):
                    messages.append(data["content"])
            except Exception:
                pass
        return messages

    # ------------------------------------------------------------------
    # Field updates
    # ------------------------------------------------------------------

    async def update_session_id(self, token: str, session_id: str) -> None:
        """Update the SDK session_id in the streaming session metadata."""
        key = streaming_session_key(token)
        await cast(Awaitable[int], self.redis.hset(key, "session_id", session_id))
        logger.debug(f"[SessionStore] Updated session_id for {token[:8]}...")

    async def update_transcript_path(self, token: str, path: str) -> None:
        """Update the transcript_path in the streaming session metadata."""
        key = streaming_session_key(token)
        await cast(Awaitable[int], self.redis.hset(key, "transcript_path", path))
        logger.debug(f"[SessionStore] Updated transcript_path for {token[:8]}...")

    async def increment_run_count(self, token: str) -> int:
        """Increment the run count. Returns new count."""
        key = streaming_session_key(token)
        count = await cast(Awaitable[int], self.redis.hincrby(key, "run_count", 1))
        return int(count)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _cleanup_streaming(
        self,
        token: str,
        repo: str,
        thread_id: str,
        workflow: str,
        thread_type: str = "",
    ) -> None:
        """Delete streaming session data and all sub-keys."""
        # Delete the streaming session hash
        stream_key = streaming_session_key(token)
        await self.redis.delete(stream_key)
        # Delete inbox and subscribers
        await self.redis.delete(inbox_key(token))
        await self.redis.delete(subscribers_key(token))
        # Delete history
        await self.redis.delete(history_key(token))
        # Delete the lookup key
        lookup_key = streaming_lookup_key(
            repo, thread_id, workflow, thread_type=thread_type
        )
        await self.redis.delete(lookup_key)
        # Also try deleting the legacy key (without thread_type) for cleanup
        if thread_type:
            legacy_key = streaming_lookup_key(repo, thread_id, workflow, thread_type="")
            await self.redis.delete(legacy_key)
        logger.info(f"[SessionStore] Cleaned up streaming session {token[:8]}...")

    async def _propagate_streaming_ttl(
        self,
        token: str,
        repo: str,
        thread_id: str,
        workflow: str,
        ttl_hours: int,
        thread_type: str = "",
    ) -> None:
        """Propagate session TTL to streaming session and all sub-keys."""
        ttl_seconds = ttl_hours * 3600
        # Propagate to streaming session hash + inbox + subscribers
        await self.set_ttl(token, ttl_seconds)
        # Also propagate to the lookup key
        lookup_key = streaming_lookup_key(
            repo, thread_id, workflow, thread_type=thread_type
        )
        await self.redis.expire(lookup_key, ttl_seconds)
        # Also propagate to legacy key (without thread_type) if present
        if thread_type:
            legacy_key = streaming_lookup_key(repo, thread_id, workflow, thread_type="")
            await self.redis.expire(legacy_key, ttl_seconds)
        logger.debug(
            f"[SessionStore] Propagated TTL {ttl_hours}h to streaming {token[:8]}..."
        )
