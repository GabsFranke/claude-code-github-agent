"""Redis pub/sub bridge for real-time session streaming.

This module provides the glue between the Claude Agent SDK message loop
(running inside sandbox_worker) and the session_proxy WebSocket service
that delivers those messages to browsers.

Architecture:
    sandbox_worker          Redis                  session_proxy
        |                     |                         |
        |  PUBLISH msg:{tok}->|                         |
        |                     |-- push subscribers ---->|
        |                     |                         |-- WS push --> Browser
        |                     |<-- SUBSCRIBE ctl:{tok}--|
        |<-- resolve Future --|<-- WS message ----------|<-- user click

Two Redis channels per session:
    session:msg:{token}  — SDK messages → browser (pub/sub)
    session:ctl:{token}  — Control messages → worker (pub/sub)

Usage inside sdk_executor._execute_sdk_once():
    bridge = SessionStreamBridge(token, redis)
    ctl = ControlChannel(token, redis)
    await ctl.start()

    async for message in client.receive_messages():
        await bridge.publish_message(message)
        ...

    await ctl.stop()
    await bridge.close()
"""

import asyncio
import json
import logging
from typing import Any

from shared.constants import (
    CTL_CHANNEL,
    DEFAULT_SESSION_TTL_SECONDS,
    HISTORY_MAX,
    HISTORY_TTL_SECONDS,
    MSG_CHANNEL,
    _now_iso,
)
from shared.streaming_session import _history_key, _inbox_key

logger = logging.getLogger(__name__)


class SessionStreamBridge:
    """Publishes SDK messages to Redis pub/sub for cross-process streaming.

    Called inside the message loop in sdk_executor. Each SDK message is
    serialized to JSON and published on session:msg:{token}.

    A short-lived Redis history (capped at HISTORY_MAX, TTL HISTORY_TTL_SECONDS)
    stores recent messages as a fallback before the transcript file is available.

    Example:
        bridge = SessionStreamBridge(token="abc123", redis=redis_client)
        async for message in client.receive_messages():
            await bridge.publish_message(message)
        await bridge.close()
    """

    def __init__(self, token: str, redis: Any) -> None:
        self._token = token
        self._redis = redis
        self._channel = MSG_CHANNEL.format(token)
        self._history_key = _history_key(token)

    async def publish(self, msg_type: str, data: dict) -> None:
        """Publish a typed message to the session channel.

        Also appends to a short-lived Redis history so browsers can load
        recent messages on connect (fallback when transcript isn't available
        yet for a running session). Transcripts are the primary history source.
        """
        payload = json.dumps({"type": msg_type, "data": data, "ts": _now_iso()})
        try:
            await self._redis.publish(self._channel, payload)
            # Short-lived Redis history (fallback for running sessions
            # before transcript is written). Transcripts are primary.
            await self._redis.rpush(self._history_key, payload)
            await self._redis.ltrim(self._history_key, -HISTORY_MAX, -1)
            await self._redis.expire(self._history_key, HISTORY_TTL_SECONDS)
        except Exception as e:
            logger.warning(f"[StreamBridge] Failed to publish to {self._channel}: {e}")

    async def publish_message(self, message: Any) -> None:
        """Publish an SDK message object, routing to the correct type.

        Handles StreamEvent, AssistantMessage, ResultMessage, and any
        other message types generically.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage
        from claude_agent_sdk.types import StreamEvent

        try:
            if isinstance(message, StreamEvent):
                await self.publish(
                    "stream_event",
                    {
                        "event": message.event,
                        "session_id": message.session_id,
                    },
                )
            elif isinstance(message, AssistantMessage):
                # Convert content blocks to serializable dicts
                content: list[dict[str, Any]] = []
                for block in message.content:
                    if hasattr(block, "__dict__"):
                        content.append(block.__dict__)
                    else:
                        # Wrap non-dict content in a dict
                        content.append({"text": str(block)})
                await self.publish("assistant_message", {"content": content})
            elif isinstance(message, ResultMessage):
                await self.publish(
                    "result",
                    {
                        "num_turns": message.num_turns,
                        "duration_ms": message.duration_ms,
                        "is_error": message.is_error,
                        "session_id": getattr(message, "session_id", None),
                        "subtype": getattr(message, "subtype", None),
                    },
                )
            else:
                # Generic fallback — pass message type name and repr
                msg_type = type(message).__name__.lower()
                data: dict[str, Any] = {}
                if hasattr(message, "__dict__"):
                    # Attempt basic serialization
                    for k, v in message.__dict__.items():
                        try:
                            json.dumps(v)
                            data[k] = v
                        except (TypeError, ValueError):
                            data[k] = repr(v)
                await self.publish(msg_type, data)
        except Exception as e:
            logger.warning(
                f"[StreamBridge] Failed to publish message {type(message).__name__}: {e}"
            )

    async def publish_init(self, repo: str, issue_number: int, workflow: str) -> None:
        """Publish session start event with metadata."""
        await self.publish(
            "session_init",
            {
                "repo": repo,
                "issue_number": issue_number,
                "workflow": workflow,
            },
        )

    async def publish_user_message(self, content: str) -> None:
        """Publish a user message event so the browser displays it."""
        await self.publish("user_message", {"content": content})

    async def publish_error(self, error: str) -> None:
        """Publish a session error."""
        await self.publish("session_error", {"error": error})

    async def close(self) -> None:
        """Publish session close event."""
        try:
            await self.publish("session_closed", {})
        except Exception as e:
            logger.warning(f"[StreamBridge] Failed to publish session_closed: {e}")


class ControlChannel:
    """Subscribes to control messages from session_proxy.

    The ControlChannel runs a background task that listens on
    session:ctl:{token} for user messages injected from the browser.

    Example:
        ctl = ControlChannel(token="abc123", redis=redis_client)
        await ctl.start()
        await ctl.stop()
    """

    def __init__(
        self,
        token: str,
        redis: Any,
        interrupt_event: asyncio.Event | None = None,
    ) -> None:
        self._token = token
        self._redis = redis
        self._channel = CTL_CHANNEL.format(token)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self._interrupt_event = interrupt_event
        self._pubsub: Any | None = None

    async def start(self) -> None:
        """Start background listener task."""
        self._task = asyncio.get_running_loop().create_task(
            self._listen(), name=f"ctl-listener-{self._token[:8]}"
        )

    async def _listen(self) -> None:
        """Background task: subscribe and dispatch control messages."""
        try:
            pubsub = self._redis.pubsub()
            self._pubsub = pubsub
            await pubsub.subscribe(self._channel)
            logger.info(f"[ControlChannel] Subscribed to {self._channel}")

            async for raw in pubsub.listen():
                if self._stopped:
                    break
                if raw["type"] != "message":
                    continue
                try:
                    msg = json.loads(raw["data"])
                    await self._dispatch(msg)
                except Exception as e:
                    logger.warning(
                        f"[ControlChannel] Failed to parse control message: {e}"
                    )

        except Exception as e:
            logger.error(f"[ControlChannel] Listener error: {e}")
        finally:
            logger.debug(f"[ControlChannel] Listener exiting for {self._token[:8]}")

    async def _dispatch(self, msg: dict) -> None:
        """Dispatch an incoming control message."""
        msg_type = msg.get("type")

        if msg_type == "inject_message":
            content = msg.get("content", "")
            if not content or not content.strip():
                logger.debug("[ControlChannel] Empty inject_message, ignoring")
                return
            # Store in Redis inbox for the sandbox worker to pick up

            inbox_key = _inbox_key(self._token)
            message_data = json.dumps(
                {
                    "type": "user_message",
                    "content": content,
                    "ts": _now_iso(),
                }
            )
            await self._redis.rpush(inbox_key, message_data)
            await self._redis.expire(inbox_key, DEFAULT_SESSION_TTL_SECONDS)
            logger.info(
                f"[ControlChannel] Stored user message in inbox for {self._token[:8]}..."
            )
            # Signal the sandbox worker to interrupt the current execution
            if self._interrupt_event:
                self._interrupt_event.set()

        elif msg_type == "stop_agent":
            logger.info(
                f"[ControlChannel] Received stop_agent command for {self._token[:8]}..."
            )
            if self._interrupt_event:
                self._interrupt_event.set()

        else:
            logger.debug(f"[ControlChannel] Unknown control message type: {msg_type}")

    async def stop(self) -> None:
        """Stop the listener."""
        self._stopped = True
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(self._channel)
                await self._pubsub.aclose()
            except Exception:
                pass
            self._pubsub = None
