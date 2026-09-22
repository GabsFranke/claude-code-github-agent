"""Pub/sub bridge with auto-reconnect for session messaging.

Provides publish + subscribe over Redis pub/sub channels with
automatic reconnection on connection loss.  Multiple subscribers
on the same channel are supported — each receives every message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class SessionBridge:
    """Redis pub/sub bridge with per-channel handlers and auto-reconnect.

    Publish messages to Redis channels and subscribe handlers that
    fire on every incoming message.  When the Redis pub/sub connection
    drops the bridge automatically reconnects with exponential backoff.

    Handlers receive the raw Redis pub/sub message dict::

        {"type": "message", "channel": b"channel-name", "data": "payload"}

    Usage::

        bridge = SessionBridge(redis_client)
        await bridge.subscribe("session:msg:abc", my_handler)
        await bridge.publish("session:msg:abc", json_payload)
        await bridge.unsubscribe("session:msg:abc")
        await bridge.stop()
    """

    _MAX_RETRIES = 5
    _BACKOFF: tuple[float, ...] = (1, 2, 4, 8, 16)

    def __init__(self, redis: Any) -> None:
        self._redis = redis
        self._handlers: dict[str, list[Handler]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish(self, channel: str, message: str | bytes) -> int:
        """Publish *message* to a Redis channel.

        Returns the number of subscribers that received the message.
        """
        return await self._redis.publish(channel, message)  # type: ignore[no-any-return]

    async def subscribe(self, channel: str, handler: Handler) -> None:
        """Register *handler* to be called for every message on *channel*.

        Starts the background listener if it is not already running.
        If channels have changed the listener is transparently restarted
        to include the new subscription.
        """
        if channel not in self._handlers:
            self._handlers[channel] = []
        self._handlers[channel].append(handler)

        if self._listener_task is None or self._listener_task.done():
            await self._start_listener()
        else:
            await self._restart_listener()

    async def unsubscribe(self, channel: str) -> None:
        """Remove all handlers for *channel* and stop listening to it.

        If no handlers remain the background listener task is cancelled.
        """
        self._handlers.pop(channel, None)
        if self._handlers:
            await self._restart_listener()
        else:
            await self._stop_listener()

    async def stop(self) -> None:
        """Shut down the bridge — cancel listener and release resources."""
        self._stopped = True
        await self._stop_listener()
        self._handlers.clear()

    # ------------------------------------------------------------------
    # Internal — listener lifecycle
    # ------------------------------------------------------------------

    async def _start_listener(self) -> None:
        self._listener_task = asyncio.create_task(
            self._listen(), name="session-bridge-listener"
        )

    async def _restart_listener(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        await self._start_listener()

    async def _stop_listener(self) -> None:
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        self._listener_task = None

    # ------------------------------------------------------------------
    # Listener loop with reconnect
    # ------------------------------------------------------------------

    async def _listen(self) -> None:
        """Background task: subscribe to all registered channels and
        dispatch incoming messages to handlers.

        Automatically reconnects with exponential backoff when the
        Redis pub/sub connection drops.
        """
        for attempt in range(self._MAX_RETRIES + 1):
            pubsub = self._redis.pubsub()
            channels = list(self._handlers.keys())

            try:
                for ch in channels:
                    await pubsub.subscribe(ch)

                async for raw in pubsub.listen():
                    if self._stopped:
                        return
                    if raw["type"] != "message":
                        continue
                    await self._dispatch(raw)

                # pubsub.listen() exhausted — connection closed cleanly
                if self._stopped:
                    return
                if attempt >= self._MAX_RETRIES:
                    logger.error(
                        "[SessionBridge] Pub/sub closed, max retries (%d) exceeded",
                        self._MAX_RETRIES,
                    )
                    return
                delay = self._BACKOFF[attempt]
                logger.warning(
                    "[SessionBridge] Pub/sub closed (attempt %d/%d), "
                    "reconnecting in %.0fs...",
                    attempt + 1,
                    self._MAX_RETRIES,
                    delay,
                )

            except Exception as exc:
                if self._stopped:
                    return
                if attempt >= self._MAX_RETRIES:
                    logger.error(
                        "[SessionBridge] Pub/sub failed after %d retries: %s",
                        self._MAX_RETRIES,
                        exc,
                    )
                    return
                delay = self._BACKOFF[attempt]
                logger.warning(
                    "[SessionBridge] Connection lost (attempt %d/%d): %s. "
                    "Reconnecting in %.0fs...",
                    attempt + 1,
                    self._MAX_RETRIES,
                    exc,
                    delay,
                )

            finally:
                # Clean up the current pub/sub object before retrying
                try:
                    await pubsub.unsubscribe()
                    await pubsub.aclose()
                except Exception:
                    pass

            await asyncio.sleep(delay)

    # ------------------------------------------------------------------
    # Message dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, raw: dict[str, Any]) -> None:
        """Route an incoming pub/sub message to matching channel handlers."""
        channel = raw["channel"]
        if isinstance(channel, bytes):
            channel = channel.decode()

        handlers = self._handlers.get(channel, ())
        for handler in handlers:
            try:
                await handler(raw)
            except Exception:
                logger.warning(
                    "[SessionBridge] Handler for channel %s raised an exception",
                    channel,
                    exc_info=True,
                )
