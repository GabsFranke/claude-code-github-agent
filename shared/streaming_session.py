"""Streaming session metadata store.

IMPORTANT: Do NOT rename this to session_store.py — that filename is
already taken by shared/session_store.py which manages persistent
conversation sessions (a separate system).

This module manages the lifecycle metadata for streaming sessions:
- Created when sandbox_worker picks up a streaming-enabled job
- Read by session_proxy to validate WebSocket connections
- Subscriber count tracks how many browsers are watching

Redis keys:
    session:stream:{token}       Hash — session metadata (TTL from session config)
    session:stream:lookup:{repo}:{thread_id}:{workflow}  String — token lookup (TTL from session config)
    session:inbox:{token}        List — user messages from browser (TTL from session config)
    session:subscribers:{token}  Integer — active WebSocket count
"""

import json
import logging
from typing import Any

from shared.constants import (
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_HISTORY_KEY,
    SESSION_INBOX_KEY,
    SESSION_KEY,
    SESSION_SUBSCRIBERS_KEY,
    decode_redis_hash,
    streaming_lookup_key,
)

logger = logging.getLogger(__name__)


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


def _session_key(token: str) -> str:
    return SESSION_KEY.format(token)


def _inbox_key(token: str) -> str:
    return SESSION_INBOX_KEY.format(token)


def _subscribers_key(token: str) -> str:
    return SESSION_SUBSCRIBERS_KEY.format(token)


def _history_key(token: str) -> str:
    return SESSION_HISTORY_KEY.format(token)


class StreamingSessionStore:
    """Manages streaming session metadata in Redis.

    Lifecycle:
        1. sandbox_worker calls create_session() when a streaming job starts
        2. session_proxy calls get_session() to validate WebSocket tokens
        3. session_proxy calls increment/decrement_subscribers() on connect/disconnect
        4. sandbox_worker calls has_subscribers() before waiting for tool approval
        5. sandbox_worker calls set_completed() when the SDK session ends
    """

    def __init__(self, redis: Any) -> None:
        self._redis = redis

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
        thread_type: str = "issue",
        ref: str = "main",
        user: str = "",
        conversation_config: str = "",
        session_id: str = "",
    ) -> None:
        """Create a new streaming session record.

        Args:
            token: UUID session token (also the URL path)
            repo: GitHub repository (owner/repo)
            issue_number: GitHub issue/PR number
            workflow: Workflow name (e.g. "review-pr")
            session_proxy_url: Public URL of session_proxy (for GitHub comment)
            ttl_seconds: TTL in seconds — should match the persistent session TTL
            installation_id: GitHub App installation ID (for re-invoke token generation)
            initial_query: The GitHub comment that triggered this session
            thread_type: Thread type (pr/issue/discussion)
            ref: Git reference
            user: GitHub username who triggered the session
            conversation_config: JSON-encoded conversation persistence settings
            session_id: SDK session ID (empty on first creation, updated after each run)
        """
        key = _session_key(token)
        data = {
            "token": token,
            "repo": repo,
            "issue_number": str(issue_number),
            "workflow": workflow,
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
        }
        pipeline = self._redis.pipeline()
        pipeline.hset(key, mapping=data)
        pipeline.expire(key, ttl_seconds)
        # Register lookup so the token can be found by repo/issue/workflow
        lk = streaming_lookup_key(
            repo, str(issue_number), workflow, thread_type=thread_type
        )
        pipeline.setex(lk, ttl_seconds, token)
        await pipeline.execute()
        logger.info(
            f"[StreamingSessionStore] Created session {token[:8]}... "
            f"for {repo}#{issue_number} (ttl={ttl_seconds}s)"
        )

    async def find_session(
        self,
        repo: str,
        issue_number: int,
        workflow: str,
        thread_type: str = "",
    ) -> str | None:
        """Find a streaming token for the given repo/issue/workflow.

        Returns the token if the session exists (regardless of status),
        otherwise None. Stale entries (missing hash) are cleaned up.

        When thread_type is provided, the lookup key includes it for
        precise matching. When empty, falls back to the legacy key
        format (without thread_type) for backwards compatibility.
        """
        lk = streaming_lookup_key(
            repo, str(issue_number), workflow, thread_type=thread_type
        )
        raw = await self._redis.get(lk)
        if not raw:
            # If thread_type was given but no match, try legacy key
            if thread_type:
                lk_legacy = streaming_lookup_key(
                    repo, str(issue_number), workflow, thread_type=""
                )
                raw = await self._redis.get(lk_legacy)
                if not raw:
                    return None
            else:
                return None
        if isinstance(raw, bytes):
            token: str = raw.decode()
        else:
            token = raw  # type: ignore[assignment]
        # Verify the session hash still exists
        session = await self.get_session(token)
        if session:
            return token
        # Hash expired but lookup didn't — clean up
        await self._redis.delete(lk)
        return None

    async def find_active_session(
        self,
        repo: str,
        issue_number: int,
        workflow: str,
        thread_type: str = "",
    ) -> str | None:
        """Find an active (running) streaming token for the given repo/issue/workflow.

        Returns the token if found and status is 'running', otherwise None.
        Stale entries are cleaned up automatically.
        """
        token = await self.find_session(
            repo, issue_number, workflow, thread_type=thread_type
        )
        if not token:
            return None
        session = await self.get_session(token)
        if session and session.get("status") == "running":
            return token
        return None

    async def get_session(self, token: str) -> dict | None:
        """Get session metadata.

        Returns:
            Session dict, or None if the token doesn't exist / is expired.
        """
        key = _session_key(token)
        data = await self._redis.hgetall(key)
        if not data:
            return None
        decoded = decode_redis_hash(data)
        return decoded

    async def set_completed(
        self,
        token: str,
        is_error: bool = False,
        repo: str | None = None,
        issue_number: int | None = None,
        workflow: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """Mark session as completed or errored.

        The session stays alive with its full TTL so the browser can
        display history and the user can re-invoke the agent.
        The lookup key is preserved so find_session() can still find it.

        If session_id is provided, it is updated atomically with the
        status change to avoid a race where a resume job reads an
        empty session_id between set_completed and update_session_id.
        """
        key = _session_key(token)
        status = "error" if is_error else "completed"
        if session_id:
            await self._redis.hset(
                key, mapping={"status": status, "session_id": session_id}
            )
        else:
            await self._redis.hset(key, "status", status)
        logger.info(f"[StreamingSessionStore] Session {token[:8]}... -> {status}")

    async def set_running(
        self, token: str, ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS
    ) -> None:
        """Reset session status to running (for auto-continue)."""
        key = _session_key(token)
        await self._redis.hset(key, "status", "running")
        await self._redis.expire(key, ttl_seconds)
        logger.info(
            f"[StreamingSessionStore] Session {token[:8]}... -> running (auto-continue)"
        )

    async def delete_session(self, token: str) -> None:
        """Delete a streaming session entirely."""
        key = _session_key(token)
        await self._redis.delete(key)
        inbox = _inbox_key(token)
        await self._redis.delete(inbox)
        subscribers = _subscribers_key(token)
        await self._redis.delete(subscribers)
        logger.info(f"[StreamingSessionStore] Deleted session {token[:8]}...")

    async def set_ttl(self, token: str, ttl_seconds: int) -> None:
        """Set TTL on a streaming session (for cascading from SessionStore)."""
        key = _session_key(token)
        await self._redis.expire(key, ttl_seconds)
        inbox = _inbox_key(token)
        await self._redis.expire(inbox, ttl_seconds)
        subscribers = _subscribers_key(token)
        await self._redis.expire(subscribers, ttl_seconds)
        logger.debug(
            f"[StreamingSessionStore] Set TTL {ttl_seconds}s on session {token[:8]}..."
        )

    async def increment_subscribers(self, token: str) -> int:
        """Increment subscriber count atomically. Returns new count."""
        key = _subscribers_key(token)
        count = await self._redis.eval(
            _INCR_SUBSCRIBERS_LUA, 1, key, str(DEFAULT_SESSION_TTL_SECONDS)
        )
        return int(count)

    async def decrement_subscribers(self, token: str) -> int:
        """Decrement subscriber count atomically (floor 0). Returns new count."""
        key = _subscribers_key(token)
        count = await self._redis.eval(_DECR_SUBSCRIBERS_LUA, 1, key)
        return int(count)

    async def has_subscribers(self, token: str) -> bool:
        """Return True if at least one browser is connected."""
        key = _subscribers_key(token)
        raw = await self._redis.get(key)
        if raw is None:
            return False
        count = int(raw.decode() if isinstance(raw, bytes) else raw)
        return count > 0

    async def get_replay_buffer(self, token: str) -> list[dict]:
        """Fetch recent messages for a new browser connection.

        Delegates to get_history() — the replay buffer has been replaced
        by transcript-based history with Redis as a short-lived fallback.

        Returns:
            List of message dicts (already parsed JSON), oldest first.
        """
        return await self.get_history(token)

    async def get_history(self, token: str) -> list[dict]:
        """Fetch the full persistent message history for a session.

        Returns:
            List of message dicts (already parsed JSON), oldest first.
        """
        key = _history_key(token)
        raw_messages = await self._redis.lrange(key, 0, -1)
        result = []
        for raw in raw_messages:
            try:
                text = raw.decode() if isinstance(raw, bytes) else raw
                result.append(json.loads(text))
            except Exception:
                pass
        return result

    async def update_session_id(self, token: str, session_id: str) -> None:
        """Update the SDK session_id in the streaming session metadata.

        Called by sandbox_worker after each SDK run so the session_proxy
        can include the correct session_id when creating a resume job.
        """
        key = _session_key(token)
        await self._redis.hset(key, "session_id", session_id)
        logger.debug(f"[StreamingSessionStore] Updated session_id for {token[:8]}...")

    async def update_transcript_path(self, token: str, path: str) -> None:
        """Update the transcript_path in the streaming session metadata.

        Called by sandbox_worker after each SDK run so the session_proxy
        can load history from the transcript file.
        """
        key = _session_key(token)
        await self._redis.hset(key, "transcript_path", path)
        logger.debug(
            f"[StreamingSessionStore] Updated transcript_path for {token[:8]}..."
        )

    async def increment_run_count(self, token: str) -> int:
        """Increment the run count. Returns new count."""
        key = _session_key(token)
        count = await self._redis.hincrby(key, "run_count", 1)
        return int(count)

    # -----------------------------------------------------------------------
    # Inbox: user messages from the browser
    # -----------------------------------------------------------------------

    async def push_inbox_message(self, token: str, content: str) -> None:
        """Push a user message into the session inbox."""
        inbox = _inbox_key(token)
        message_data = json.dumps({"type": "user_message", "content": content})
        await self._redis.rpush(inbox, message_data)
        await self._redis.expire(inbox, DEFAULT_SESSION_TTL_SECONDS)

    async def pop_inbox_messages(self, token: str) -> list[str]:
        """Atomically drain all messages from the inbox.

        Returns:
            List of message content strings, oldest first.
        """
        inbox = _inbox_key(token)

        lua_drain = """
        local items = redis.call('LRANGE', KEYS[1], 0, -1)
        redis.call('DEL', KEYS[1])
        return items
        """
        try:
            raw_items = await self._redis.eval(lua_drain, 1, inbox)  # type: ignore[misc]
        except Exception as e:
            logger.error(
                f"[StreamingSessionStore] Failed to drain inbox for {token}: {e}"
            )
            raise  # Don't silently drop messages

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
