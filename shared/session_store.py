"""Session persistence manager for conversation continuity across GitHub comments.

Stores session metadata in Redis so the bot can resume conversations when
users reply in the same thread.  Sessions are scoped by repo + thread type +
thread ID + workflow, and expire after a configurable TTL.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

try:
    import redis.asyncio as aioredis

    RedisClient = aioredis.Redis
except ImportError:
    RedisClient = Any  # type: ignore[assignment, misc]

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SessionInfo(BaseModel):
    """Metadata for a persisted SDK session."""

    session_id: str
    repo: str
    thread_type: str  # "pr", "issue", "discussion"
    thread_id: str
    workflow_name: str
    ref: str
    worktree_path: str
    created_at: str
    last_run: str
    turn_count: int = 0
    status: str = "active"
    summary: str | None = None


class ConversationConfig(BaseModel):
    """Per-workflow conversation persistence settings."""

    persist: bool = Field(default=False, description="Enable session persistence")
    ttl_hours: int = Field(
        default=168, description="Session TTL in hours (default 7 days)"
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


def resolve_thread_type(event_data: dict) -> str:
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
    """Build the Redis key for a session mapping."""
    safe_repo = repo.replace("/", ":")
    return f"session:map:{safe_repo}:{thread_type}:{thread_id}:{workflow}"


def _session_pattern(repo: str) -> str:
    """Build a Redis SCAN pattern for all sessions of a repo."""
    safe_repo = repo.replace("/", ":")
    return f"session:map:{safe_repo}:*"


class SessionStore:
    """Manages session metadata in Redis for conversation continuity.

    Redis schema::

        session:map:{owner:repo}:{thread_type}:{thread_id}:{workflow} = JSON

    Each value is a JSON blob matching ``SessionInfo`` fields.
    """

    def __init__(self, redis_client: RedisClient):
        self.redis = redis_client

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
        ttl_hours: int = 168,
    ) -> None:
        """Create or update a session mapping in Redis."""
        key = _session_key(repo, thread_type, thread_id, workflow)
        now = datetime.now(UTC).isoformat()

        existing_raw = await self.redis.get(key)
        if existing_raw:
            existing = json.loads(existing_raw)
            created_at = existing.get("created_at", now)
            turn_count = existing.get("turn_count", 0) + turn_count
            if summary is None:
                summary = existing.get("summary")
        else:
            created_at = now

        info = SessionInfo(
            session_id=session_id,
            repo=repo,
            thread_type=thread_type,
            thread_id=str(thread_id),
            workflow_name=workflow,
            ref=ref,
            worktree_path=str(worktree_path),
            created_at=created_at,
            last_run=now,
            turn_count=turn_count,
            status="active",
            summary=summary,
        )

        ttl_seconds = ttl_hours * 3600
        await self.redis.setex(key, ttl_seconds, info.model_dump_json())
        logger.info(
            f"Saved session {session_id[:8]}... for "
            f"{repo}/{thread_type}/{thread_id}/{workflow} "
            f"(turns={info.turn_count}, ttl={ttl_hours}h)"
        )

    async def get_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> SessionInfo | None:
        """Look up an active session, returning None if absent or expired."""
        key = _session_key(repo, thread_type, thread_id, workflow)
        raw = await self.redis.get(key)
        if not raw:
            return None
        try:
            return SessionInfo.model_validate_json(raw)  # type: ignore[no-any-return]
        except Exception as e:
            logger.warning(f"Corrupt session data at {key}: {e}")
            return None

    async def close_session(
        self, repo: str, thread_type: str, thread_id: str, workflow: str
    ) -> None:
        """Mark a session as closed (or delete it)."""
        key = _session_key(repo, thread_type, thread_id, workflow)
        await self.redis.delete(key)
        logger.info(f"Closed session for {repo}/{thread_type}/{thread_id}/{workflow}")

    async def list_sessions(self, repo: str) -> list[SessionInfo]:
        """List all active sessions for a repository."""
        pattern = _session_pattern(repo)
        sessions: list[SessionInfo] = []
        cursor = 0
        while True:
            cursor, keys = await self.redis.scan(
                cursor=cursor, match=pattern, count=100
            )
            for key in keys:
                raw = await self.redis.get(key)
                if raw:
                    try:
                        sessions.append(SessionInfo.model_validate_json(raw))
                    except Exception as e:
                        logger.warning(f"Skipping corrupt session at {key}: {e}")
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
        """Set a new TTL for an existing session (e.g., when an issue is closed)."""
        key = _session_key(repo, thread_type, thread_id, workflow)
        if await self.redis.exists(key):
            await self.redis.expire(key, ttl_hours * 3600)
            logger.info(
                f"Set TTL to {ttl_hours}h for session {repo}/{thread_type}/{thread_id}/{workflow}"
            )

    async def update_summary(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        summary: str,
    ) -> None:
        """Update the conversation summary for a session (atomic via Lua)."""
        key = _session_key(repo, thread_type, thread_id, workflow)

        lua_update_summary = """
        local val = redis.call('GET', KEYS[1])
        if not val then return 0 end
        local data = cjson.decode(val)
        data['summary'] = ARGV[1]
        local ttl = redis.call('TTL', KEYS[1])
        local new_val = cjson.encode(data)
        if ttl > 0 then
            redis.call('SETEX', KEYS[1], ttl, new_val)
        else
            redis.call('SET', KEYS[1], new_val)
        end
        return 1
        """
        try:
            await self.redis.eval(lua_update_summary, 1, key, summary)  # type: ignore[misc]
        except Exception as e:
            logger.warning(f"Failed to update summary for {key}: {e}")

    async def increment_turn_count(
        self,
        repo: str,
        thread_type: str,
        thread_id: str,
        workflow: str,
        additional_turns: int,
    ) -> None:
        """Add to the cumulative turn count after a continuation (atomic via Lua)."""
        key = _session_key(repo, thread_type, thread_id, workflow)
        last_run = datetime.now(UTC).isoformat()

        lua_increment_turns = """
        local val = redis.call('GET', KEYS[1])
        if not val then return 0 end
        local data = cjson.decode(val)
        data['turn_count'] = (data['turn_count'] or 0) + tonumber(ARGV[1])
        data['last_run'] = ARGV[2]
        local ttl = redis.call('TTL', KEYS[1])
        local new_val = cjson.encode(data)
        if ttl > 0 then
            redis.call('SETEX', KEYS[1], ttl, new_val)
        else
            redis.call('SET', KEYS[1], new_val)
        end
        return 1
        """
        try:
            await self.redis.eval(
                lua_increment_turns, 1, key, str(additional_turns), last_run  # type: ignore[misc]
            )
        except Exception as e:
            logger.warning(f"Failed to increment turn count for {key}: {e}")
