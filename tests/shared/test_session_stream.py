"""Tests for shared/session_stream.py — SessionStreamBridge and ControlChannel."""

import asyncio
import json
import sys
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# Set up distinct fake SDK types so isinstance checks work properly
class _FakeAssistantMessage:
    """Fake claude_agent_sdk.AssistantMessage."""

    def __init__(self, content):
        self.content = content


class _FakeResultMessage:
    """Fake claude_agent_sdk.ResultMessage."""

    def __init__(self, num_turns, duration_ms, is_error, session_id=None):
        self.num_turns = num_turns
        self.duration_ms = duration_ms
        self.is_error = is_error
        self.session_id = session_id
        self.subtype = None


class _FakeStreamEvent:
    """Fake claude_agent_sdk.types.StreamEvent."""

    def __init__(self, event, session_id):
        self.event = event
        self.session_id = session_id


# Ensure claude_agent_sdk.types is importable for session_stream.py
mock_sdk_types = MagicMock()
mock_sdk_types.StreamEvent = _FakeStreamEvent
sys.modules.setdefault("claude_agent_sdk.types", mock_sdk_types)

from shared.session_stream import ControlChannel, SessionStreamBridge  # noqa: E402


@pytest.fixture(autouse=True)
def _patch_sdk_types():
    """Patch SDK types so isinstance checks in publish_message work.

    Applied per-test so the global claude_agent_sdk mock is restored for
    other test modules (e.g. test_sdk_executor).
    """
    sdk = sys.modules.get("claude_agent_sdk")
    types_mod = sys.modules.get("claude_agent_sdk.types")
    with (
        patch.object(sdk, "AssistantMessage", _FakeAssistantMessage),
        patch.object(sdk, "ResultMessage", _FakeResultMessage),
        patch.object(types_mod, "StreamEvent", _FakeStreamEvent),
    ):
        yield


def _make_redis():
    """Create a mock Redis client with explicit async methods."""
    redis = MagicMock()
    redis.publish = AsyncMock(return_value=1)
    redis.rpush = AsyncMock(return_value=1)
    redis.ltrim = AsyncMock(return_value=None)
    redis.expire = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=None)
    redis.delete = AsyncMock(return_value=1)
    redis.hset = AsyncMock(return_value=None)
    return redis


class TestSessionStreamBridgeInit:
    def test_init_sets_token_channel_and_history_key(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        assert bridge._token == "test-token-1234"
        assert bridge._redis is redis
        assert bridge._channel == "session:msg:test-token-1234"
        assert bridge._history_key == "session:history:test-token-1234"


class TestSessionStreamBridgePublish:
    @pytest.mark.asyncio
    async def test_publish_calls_redis_in_order(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        with patch(
            "shared.session_stream._now_iso", return_value="2025-01-01T00:00:00+00:00"
        ):
            await bridge.publish("my_type", {"key": "val"})

        assert redis.publish.call_count == 1
        assert redis.rpush.call_count == 1
        assert redis.ltrim.call_count == 1
        assert redis.expire.call_count == 1

        channel = redis.publish.call_args[0][0]
        payload = redis.publish.call_args[0][1]
        assert channel == "session:msg:test-token-1234"
        data = json.loads(payload)
        assert data["type"] == "my_type"
        assert data["data"] == {"key": "val"}
        assert data["ts"] == "2025-01-01T00:00:00+00:00"

    @pytest.mark.asyncio
    async def test_publish_handles_redis_error_gracefully(self):
        redis = _make_redis()
        redis.publish = AsyncMock(side_effect=ConnectionError("redis down"))
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        # Should not raise
        await bridge.publish("my_type", {"key": "val"})

    @pytest.mark.asyncio
    async def test_publish_json_payload_structure(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        with patch(
            "shared.session_stream._now_iso", return_value="2025-01-01T00:00:00+00:00"
        ):
            await bridge.publish("test_type", {"nested": True})

        payload = redis.publish.call_args[0][1]
        parsed = json.loads(payload)
        assert set(parsed.keys()) == {"type", "data", "ts"}
        assert parsed["type"] == "test_type"
        assert parsed["data"] == {"nested": True}


class TestSessionStreamBridgePublishMessage:
    @pytest.mark.asyncio
    async def test_stream_event(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        msg = _FakeStreamEvent(event="text_delta", session_id="sess-123")

        await bridge.publish_message(msg)

        assert redis.publish.call_count == 1
        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "stream_event"
        assert payload["data"]["event"] == "text_delta"
        assert payload["data"]["session_id"] == "sess-123"

    @pytest.mark.asyncio
    async def test_assistant_message(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        class _Block:
            __dict__ = {"text": "hello world"}

        msg = _FakeAssistantMessage(content=[_Block()])
        await bridge.publish_message(msg)

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "assistant_message"
        assert payload["data"]["content"] == [{"text": "hello world"}]

    @pytest.mark.asyncio
    async def test_assistant_message_non_dict_block(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        msg = _FakeAssistantMessage(content=["plain string block"])
        await bridge.publish_message(msg)

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["data"]["content"] == [{"text": "plain string block"}]

    @pytest.mark.asyncio
    async def test_result_message(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        msg = _FakeResultMessage(num_turns=3, duration_ms=5000, is_error=False)
        await bridge.publish_message(msg)

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "result"
        assert payload["data"]["num_turns"] == 3
        assert payload["data"]["duration_ms"] == 5000
        assert payload["data"]["is_error"] is False

    @pytest.mark.asyncio
    async def test_unknown_type(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        class CustomMessage:
            __dict__ = {"custom_field": 42}

        msg = CustomMessage()

        await bridge.publish_message(msg)

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "custommessage"
        assert payload["data"]["custom_field"] == 42

    @pytest.mark.asyncio
    async def test_handles_exception_without_propagation(self):
        redis = _make_redis()
        redis.publish = AsyncMock(side_effect=RuntimeError("boom"))
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        msg = _FakeAssistantMessage(content=[])

        # Should not raise
        await bridge.publish_message(msg)


class TestSessionStreamBridgeConvenienceMethods:
    @pytest.mark.asyncio
    async def test_publish_init(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        await bridge.publish_init("owner/repo", 42, "review-pr")

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "session_init"
        assert payload["data"]["repo"] == "owner/repo"
        assert payload["data"]["issue_number"] == 42
        assert payload["data"]["workflow"] == "review-pr"

    @pytest.mark.asyncio
    async def test_publish_user_message(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        await bridge.publish_user_message("Hello world")

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "user_message"
        assert payload["data"]["content"] == "Hello world"

    @pytest.mark.asyncio
    async def test_publish_error(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        await bridge.publish_error("Something went wrong")

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "session_error"
        assert payload["data"]["error"] == "Something went wrong"

    @pytest.mark.asyncio
    async def test_close(self):
        redis = _make_redis()
        bridge = SessionStreamBridge(token="test-token-1234", redis=redis)

        await bridge.close()

        payload = json.loads(redis.publish.call_args[0][1])
        assert payload["type"] == "session_closed"
        assert payload["data"] == {}


# ---------------------------------------------------------------------------
# ControlChannel tests
# ---------------------------------------------------------------------------


class TestControlChannelInit:
    def test_init_defaults(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        assert ctl._token == "test-token-1234"
        assert ctl._redis is redis
        assert ctl._channel == "session:ctl:test-token-1234"
        assert ctl._task is None
        assert ctl._stopped is False
        assert ctl._interrupt_event is None

    def test_init_with_interrupt_event(self):
        redis = _make_redis()
        event = asyncio.Event()
        ctl = ControlChannel(
            token="test-token-1234", redis=redis, interrupt_event=event
        )

        assert ctl._interrupt_event is event


class TestControlChannelStart:
    @pytest.mark.asyncio
    async def test_start_creates_background_task(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        await ctl.start()

        assert ctl._task is not None
        assert not ctl._task.done()
        # Clean up
        ctl._task.cancel()
        try:
            await ctl._task
        except asyncio.CancelledError:
            pass


class TestControlChannelDispatch:
    @pytest.mark.asyncio
    async def test_inject_message_stores_in_inbox(self):
        redis = _make_redis()
        event = asyncio.Event()
        ctl = ControlChannel(
            token="test-token-1234", redis=redis, interrupt_event=event
        )

        await ctl._dispatch({"type": "inject_message", "content": "Hello from browser"})

        assert redis.rpush.call_count == 1
        assert redis.expire.call_count == 1
        assert event.is_set()

    @pytest.mark.asyncio
    async def test_inject_message_empty_content_ignored(self):
        redis = _make_redis()
        event = asyncio.Event()
        ctl = ControlChannel(
            token="test-token-1234", redis=redis, interrupt_event=event
        )

        await ctl._dispatch({"type": "inject_message", "content": "   "})

        assert redis.rpush.call_count == 0

    @pytest.mark.asyncio
    async def test_unknown_type_skipped(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        await ctl._dispatch({"type": "unknown_type", "data": "whatever"})

        assert redis.rpush.call_count == 0


class TestControlChannelStop:
    @pytest.mark.asyncio
    async def test_sets_stopped(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        await ctl.stop()

        assert ctl._stopped is True

    @pytest.mark.asyncio
    async def test_cancels_background_task(self):
        redis = _make_redis()

        async def empty_listen() -> AsyncGenerator[dict, None]:
            # Yield nothing, just end so the task is idle
            return
            yield

        pubsub = MagicMock()
        pubsub.listen = MagicMock(return_value=empty_listen())
        pubsub.subscribe = AsyncMock()
        redis.pubsub = MagicMock(return_value=pubsub)

        ctl = ControlChannel(token="test-token-1234", redis=redis)
        await ctl.start()

        await asyncio.sleep(0.01)
        assert ctl._task is not None

        await ctl.stop()

        assert ctl._task.done()


def _create_mock_pubsub(messages):
    """Create a mock pubsub that yields messages then ends."""

    async def _mock_listen() -> AsyncGenerator[dict, None]:
        for msg in messages:
            yield msg

    pubsub = MagicMock()
    pubsub.subscribe = AsyncMock()
    pubsub.unsubscribe = AsyncMock()
    pubsub.close = AsyncMock()
    pubsub.listen = MagicMock(return_value=_mock_listen())
    return pubsub


class TestControlChannelListen:
    @pytest.mark.asyncio
    async def test_skips_non_message_types(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        pubsub = _create_mock_pubsub(
            [
                {"type": "subscribe", "channel": "test"},
                {
                    "type": "message",
                    "data": json.dumps({"type": "inject_message", "content": "hello"}),
                },
            ]
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        await ctl._listen()

        assert redis.rpush.call_count == 1

    @pytest.mark.asyncio
    async def test_handles_json_parse_error(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        pubsub = _create_mock_pubsub([{"type": "message", "data": "not valid json{{"}])
        redis.pubsub = MagicMock(return_value=pubsub)

        await ctl._listen()  # Should not raise

    @pytest.mark.asyncio
    async def test_exits_on_stopped(self):
        redis = _make_redis()
        ctl = ControlChannel(token="test-token-1234", redis=redis)

        pubsub = _create_mock_pubsub([{"type": "message", "data": "{}"}])
        redis.pubsub = MagicMock(return_value=pubsub)

        # _stopped is True, so it should break after first iteration
        ctl._stopped = True
        await asyncio.wait_for(ctl._listen(), timeout=0.5)
