"""Session lifecycle state machine — thin business-logic layer over SessionStore.

Wraps ``SessionStore`` and enforces valid session state transitions::

    running ──→ completed
    running ──→ error
    completed ──→ running  (resume)
    error ──→ running      (resume)

Invalid transitions (e.g. completed → active without resume, error → completed,
expired → anything) raise ``InvalidSessionTransition``.

Also provides convenience methods for subscriber tracking, TTL expiry,
and session creation.  Endpoints import ``get_store()`` to obtain the
singleton store instance.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from shared.session_store import (
    DEFAULT_SESSION_TTL_SECONDS,
    SessionStatus,
    SessionStore,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exception types
# ---------------------------------------------------------------------------


class InvalidSessionTransition(Exception):
    """Raised when an invalid session state transition is attempted."""

    def __init__(self, current: str, target: str) -> None:  # noqa: B042
        self.current = current
        self.target = target
        msg = f"Cannot transition from '{current}' to '{target}'"
        super().__init__(msg)


class SessionNotFoundError(Exception):
    """Raised when a session token cannot be resolved to an existing session."""

    def __init__(self, token: str) -> None:
        self.token = token
        super().__init__(f"Session not found for token: {token}")


# ---------------------------------------------------------------------------
# SessionStoreWrapper — state machine + convenience methods
# ---------------------------------------------------------------------------

_store: SessionStoreWrapper | None = None


class SessionStoreWrapper:
    """Service-level wrapper around ``SessionStore``.

    Validates and enforces session lifecycle transitions, tracks
    subscribers, and manages TTL propagation.

    State machine (valid transitions)::

        running ──→ completed   (set_completed)
        running ──→ error       (set_completed with is_error=True)
        completed ──→ running   (resume via transition_to_running)
        error ──→ running       (resume via transition_to_running)
        running ──→ running     (idempotent reset, no run_count bump)
    """

    # Valid transition targets for each current status.
    _VALID_TRANSITIONS: dict[str, frozenset[str]] = {
        "running": frozenset({"completed", "error", "running"}),
        "completed": frozenset({"running"}),
        "error": frozenset({"running"}),
        "expired": frozenset(),  # terminal — no transitions allowed
        "active": frozenset({"running"}),  # persistent → streaming
    }

    def __init__(self, redis_client: Any) -> None:
        self._store = SessionStore(redis_client)

    @property
    def store(self) -> SessionStore:
        """Access the underlying ``SessionStore`` instance."""
        return self._store

    # ------------------------------------------------------------------
    # Transition validation
    # ------------------------------------------------------------------

    def _validate_transition(self, current: str, target: str) -> None:
        """Raise ``InvalidSessionTransition`` if *current → target* is not allowed."""
        valid = self._VALID_TRANSITIONS.get(current)
        if valid is None:
            raise InvalidSessionTransition(current, target)  # unknown source status
        if target not in valid:
            raise InvalidSessionTransition(current, target)

    # ------------------------------------------------------------------
    # Lifecycle methods
    # ------------------------------------------------------------------

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
        """Create a new streaming session with status ``running``.

        Delegates directly to ``SessionStore.create_session()``.
        """
        await self._store.create_session(
            token=token,
            repo=repo,
            issue_number=issue_number,
            workflow=workflow,
            session_proxy_url=session_proxy_url,
            ttl_seconds=ttl_seconds,
            installation_id=installation_id,
            initial_query=initial_query,
            thread_type=thread_type,
            ref=ref,
            user=user,
            conversation_config=conversation_config,
            session_id=session_id,
        )
        logger.info(
            "Created streaming session %s... for %s#%d", token[:8], repo, issue_number
        )

    async def transition_to_running(self, token: str) -> None:
        """Transition session to ``running`` status.

        Valid from: ``completed``, ``error`` (resume — run_count incremented),
        ``running`` (idempotent reset — run_count NOT incremented).

        Raises ``SessionNotFoundError`` if the session does not exist.
        Raises ``InvalidSessionTransition`` if the current status does not
        allow a transition to ``running``.
        """
        session = await self._store.get_streaming_session(token)
        if session is None:
            raise SessionNotFoundError(token)

        current = str(session.status)
        self._validate_transition(current, SessionStatus.running)

        await self._store.set_running(token)

        # Only increment run_count on actual resume (completed/error → running),
        # not on idempotent reset (running → running).
        if current in ("completed", "error"):
            await self._store.increment_run_count(token)
            logger.info(
                "Resumed session %s...  %s → running  (run_count bumped)",
                token[:8],
                current,
            )
        else:
            logger.info(
                "Set session %s... to running (was already %s, idempotent)",
                token[:8],
                current,
            )

    async def transition_to_completed(
        self,
        token: str,
        is_error: bool = False,
        session_id: str | None = None,
    ) -> None:
        """Transition session to ``completed`` (or ``error``).

        Valid from: ``running`` only.

        Raises ``SessionNotFoundError`` if the session does not exist.
        Raises ``InvalidSessionTransition`` if the current status is not ``running``.
        """
        session = await self._store.get_streaming_session(token)
        if session is None:
            raise SessionNotFoundError(token)

        current = str(session.status)
        target = SessionStatus.error if is_error else SessionStatus.completed
        self._validate_transition(current, target)

        await self._store.set_completed(
            token=token,
            is_error=is_error,
            session_id=session_id,
        )
        logger.info("Session %s...  %s → %s", token[:8], current, target)

    async def expire_session_by_token(self, token: str, ttl_hours: int = 72) -> None:
        """Shorten TTL on a session and all its sub-keys.

        Propagates TTL to the streaming session hash, inbox, and
        subscribers key.  Does NOT require fetching the session first
        (session may already be expired from Redis hash perspective).

        Args:
            token: Streaming session token.
            ttl_hours: New TTL in hours (default: 72 = 3 days).
        """
        ttl_seconds = ttl_hours * 3600
        await self._store.set_ttl(token, ttl_seconds)
        logger.info(
            "Expired session %s...  TTL shortened to %dh (%ds)",
            token[:8],
            ttl_hours,
            ttl_seconds,
        )

    # ------------------------------------------------------------------
    # Subscriber tracking
    # ------------------------------------------------------------------

    async def connect_subscriber(self, token: str) -> int:
        """Increment the subscriber count for a session (WebSocket connect).

        Returns the new subscriber count after increment.
        """
        count = await self._store.increment_subscribers(token)
        logger.debug("Subscriber connected to %s... (count=%d)", token[:8], count)
        return count

    async def disconnect_subscriber(self, token: str) -> int:
        """Decrement the subscriber count (WebSocket disconnect).

        Returns the new subscriber count.  When the count reaches 0 the
        Redis key is automatically deleted by the atomic Lua script.
        """
        count = await self._store.decrement_subscribers(token)
        logger.debug("Subscriber disconnected from %s... (count=%d)", token[:8], count)
        return count

    async def has_subscribers(self, token: str) -> bool:
        """Return ``True`` if at least one WebSocket client is connected."""
        return await self._store.has_subscribers(token)


# ---------------------------------------------------------------------------
# Singleton management
# ---------------------------------------------------------------------------


def init_store(redis_client: Any) -> SessionStoreWrapper:
    """Initialise the global store singleton (called once at startup)."""
    global _store
    if _store is not None:
        logger.warning("Session store already initialised — re-creating")
    _store = SessionStoreWrapper(redis_client)
    logger.info("Session store initialised")
    return _store


def get_store() -> SessionStoreWrapper:
    """Return the global store singleton.

    Raises ``RuntimeError`` if ``init_store()`` has not been called yet.
    """
    if _store is None:
        raise RuntimeError(
            "Session store not initialised — call init_store(redis_client) first"
        )
    return _store
