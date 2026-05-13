"""Tests for shared/streaming_session.py — StreamingSessionStore."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from shared.streaming_session import StreamingSessionStore


def _make_redis():
    """Create a mock Redis client with explicit async methods."""
    redis = MagicMock()
    pipeline = MagicMock()
    pipeline.execute = AsyncMock(return_value=[])
    redis.pipeline = MagicMock(return_value=pipeline)
    redis.hset = AsyncMock(return_value=None)
    redis.expire = AsyncMock(return_value=True)
    redis.set = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.hgetall = AsyncMock(return_value={})
    redis.delete = AsyncMock(return_value=1)
    redis.eval = AsyncMock(return_value=1)
    redis.rpush = AsyncMock(return_value=1)
    redis.lrange = AsyncMock(return_value=[])
    redis.hincrby = AsyncMock(return_value=1)
    return redis


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_hset_expire_and_lookup(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
        )

        pipeline = redis.pipeline.return_value
        assert pipeline.hset.call_count == 1
        args, kwargs = pipeline.hset.call_args
        assert args[0] == "session:stream:test-token"
        mapping = kwargs.get("mapping")
        assert mapping["status"] == "running"
        assert mapping["repo"] == "owner/repo"
        assert mapping["issue_number"] == "42"
        assert mapping["workflow"] == "review-pr"

        assert pipeline.expire.call_count == 1
        assert pipeline.setex.call_count == 1

    @pytest.mark.asyncio
    async def test_create_session_pipeline_execute_awaited(self):
        """Verify pipeline.execute() is awaited during create_session (T7)."""
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
        )

        pipeline = redis.pipeline.return_value
        # Verify that pipeline.execute was called (and awaited, since it's AsyncMock)
        pipeline.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.create_session(
            token="test-token",
            repo="owner/repo",
            issue_number=42,
            workflow="review-pr",
            ttl_seconds=600,
        )

        pipeline = redis.pipeline.return_value
        assert pipeline.expire.call_args[0][1] == 600


class TestFindSession:
    @pytest.mark.asyncio
    async def test_returns_token_when_found(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result == "test-token"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_stale_lookup_cleaned_up(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="stale-token")
        redis.hgetall = AsyncMock(return_value={})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result is None
        assert redis.delete.call_count == 1

    @pytest.mark.asyncio
    async def test_legacy_fallback(self):
        redis = _make_redis()

        async def _get(key):
            if ":issue:" in key:
                return None
            return "legacy-token"

        redis.get = AsyncMock(side_effect=_get)
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_session(
            "owner/repo", 42, "review-pr", thread_type="issue"
        )
        assert result == "legacy-token"

    @pytest.mark.asyncio
    async def test_decodes_bytes_token(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value=b"byte-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_session("owner/repo", 42, "review-pr")
        assert result == "byte-token"


class TestFindActiveSession:
    @pytest.mark.asyncio
    async def test_returns_token_for_running(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "running"})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result == "test-token"

    @pytest.mark.asyncio
    async def test_returns_none_for_completed(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="test-token")
        redis.hgetall = AsyncMock(return_value={"status": "completed"})
        store = StreamingSessionStore(redis=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_session(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        result = await store.find_active_session("owner/repo", 42, "review-pr")
        assert result is None


class TestGetSession:
    @pytest.mark.asyncio
    async def test_decodes_bytes_dict(self):
        redis = _make_redis()
        redis.hgetall = AsyncMock(
            return_value={b"token": b"abc", b"status": b"running"}
        )
        store = StreamingSessionStore(redis=redis)

        result = await store.get_session("abc")
        assert result == {"token": "abc", "status": "running"}

    @pytest.mark.asyncio
    async def test_empty_returns_none(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        result = await store.get_session("abc")
        assert result is None

    @pytest.mark.asyncio
    async def test_mixed_bytes_and_strings(self):
        redis = _make_redis()
        redis.hgetall = AsyncMock(return_value={"token": b"abc", b"status": "running"})
        store = StreamingSessionStore(redis=redis)

        result = await store.get_session("abc")
        assert result == {"token": "abc", "status": "running"}


class TestSetCompleted:
    @pytest.mark.asyncio
    async def test_status_completed(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.set_completed("test-token")

        assert redis.hset.call_count == 1
        key, field, value = redis.hset.call_args[0]
        assert key == "session:stream:test-token"
        assert field == "status"
        assert value == "completed"

    @pytest.mark.asyncio
    async def test_status_error(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.set_completed("test-token", is_error=True)

        key, field, value = redis.hset.call_args[0]
        assert field == "status"
        assert value == "error"

    @pytest.mark.asyncio
    async def test_with_session_id(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.set_completed("test-token", session_id="sess-123")

        key, kwargs = redis.hset.call_args
        mapping = kwargs.get("mapping") or key[1]
        assert mapping["status"] == "completed"
        assert mapping["session_id"] == "sess-123"


class TestSetRunning:
    @pytest.mark.asyncio
    async def test_updates_status_and_ttl(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.set_running("test-token", ttl_seconds=3600)

        assert redis.hset.call_count == 1
        key, field, value = redis.hset.call_args[0]
        assert key == "session:stream:test-token"
        assert field == "status"
        assert value == "running"
        assert redis.expire.call_count == 1


class TestDeleteSession:
    @pytest.mark.asyncio
    async def test_removes_all_keys(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.delete_session("test-token")

        assert redis.delete.call_count == 3


class TestSetTtl:
    @pytest.mark.asyncio
    async def test_applies_to_keys(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.set_ttl("test-token", ttl_seconds=7200)

        assert redis.expire.call_count == 3


class TestSubscribers:
    @pytest.mark.asyncio
    async def test_increment_subscribers(self):
        redis = _make_redis()
        redis.eval = AsyncMock(return_value=3)
        store = StreamingSessionStore(redis=redis)

        result = await store.increment_subscribers("test-token")
        assert result == 3
        assert redis.eval.call_count == 1

    @pytest.mark.asyncio
    async def test_decrement_subscribers(self):
        redis = _make_redis()
        redis.eval = AsyncMock(return_value=1)
        store = StreamingSessionStore(redis=redis)

        result = await store.decrement_subscribers("test-token")
        assert result == 1
        assert redis.eval.call_count == 1

    @pytest.mark.asyncio
    async def test_has_subscribers_true(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="2")
        store = StreamingSessionStore(redis=redis)

        result = await store.has_subscribers("test-token")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_subscribers_false(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value=None)
        store = StreamingSessionStore(redis=redis)

        result = await store.has_subscribers("test-token")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_subscribers_zero(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value="0")
        store = StreamingSessionStore(redis=redis)

        result = await store.has_subscribers("test-token")
        assert result is False

    @pytest.mark.asyncio
    async def test_has_subscribers_decodes_bytes(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value=b"1")
        store = StreamingSessionStore(redis=redis)

        result = await store.has_subscribers("test-token")
        assert result is True


class TestHistory:
    @pytest.mark.asyncio
    async def test_parses_json(self):
        redis = _make_redis()
        redis.lrange = AsyncMock(return_value=[b'{"type": "stream_event", "data": {}}'])
        store = StreamingSessionStore(redis=redis)

        result = await store.get_history("test-token")
        assert len(result) == 1
        assert result[0]["type"] == "stream_event"

    @pytest.mark.asyncio
    async def test_empty_returns_empty_list(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        result = await store.get_history("test-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_skips_invalid_json(self):
        redis = _make_redis()
        redis.lrange = AsyncMock(return_value=[b"invalid json", b'{"type": "ok"}'])
        store = StreamingSessionStore(redis=redis)

        result = await store.get_history("test-token")
        assert len(result) == 1
        assert result[0]["type"] == "ok"


class TestInbox:
    @pytest.mark.asyncio
    async def test_push_inbox_message(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.push_inbox_message("test-token", "Hello")

        assert redis.rpush.call_count == 1
        assert redis.expire.call_count == 1
        payload = json.loads(redis.rpush.call_args[0][1])
        assert payload["type"] == "user_message"
        assert payload["content"] == "Hello"

    @pytest.mark.asyncio
    async def test_pop_inbox_messages(self):
        redis = _make_redis()
        redis.eval = AsyncMock(
            return_value=[b'{"type": "user_message", "content": "Hello"}']
        )
        store = StreamingSessionStore(redis=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == ["Hello"]

    @pytest.mark.asyncio
    async def test_pop_inbox_messages_empty(self):
        redis = _make_redis()
        redis.eval = AsyncMock(return_value=[])
        store = StreamingSessionStore(redis=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == []

    @pytest.mark.asyncio
    async def test_pop_inbox_messages_eval_error(self):
        redis = _make_redis()
        redis.eval = AsyncMock(side_effect=RuntimeError("redis error"))
        store = StreamingSessionStore(redis=redis)

        with pytest.raises(RuntimeError, match="redis error"):
            await store.pop_inbox_messages("test-token")

    @pytest.mark.asyncio
    async def test_pop_filters_non_user_messages(self):
        redis = _make_redis()
        redis.eval = AsyncMock(
            return_value=[
                b'{"type": "system_message", "content": "ignore"}',
                b'{"type": "user_message", "content": "keep"}',
            ]
        )
        store = StreamingSessionStore(redis=redis)

        result = await store.pop_inbox_messages("test-token")
        assert result == ["keep"]


class TestMisc:
    @pytest.mark.asyncio
    async def test_update_session_id(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.update_session_id("test-token", "sess-123")

        assert redis.hset.call_count == 1
        key, field, value = redis.hset.call_args[0]
        assert key == "session:stream:test-token"
        assert field == "session_id"
        assert value == "sess-123"

    @pytest.mark.asyncio
    async def test_update_transcript_path(self):
        redis = _make_redis()
        store = StreamingSessionStore(redis=redis)

        await store.update_transcript_path("test-token", "/path/to/transcript")

        key, field, value = redis.hset.call_args[0]
        assert field == "transcript_path"
        assert value == "/path/to/transcript"

    @pytest.mark.asyncio
    async def test_increment_run_count(self):
        redis = _make_redis()
        redis.hincrby = AsyncMock(return_value=5)
        store = StreamingSessionStore(redis=redis)

        result = await store.increment_run_count("test-token")
        assert result == 5
        key, field, amount = redis.hincrby.call_args[0]
        assert field == "run_count"
        assert amount == 1

    @pytest.mark.asyncio
    async def test_get_replay_buffer_delegates(self):
        redis = _make_redis()
        redis.lrange = AsyncMock(return_value=[])
        store = StreamingSessionStore(redis=redis)

        result = await store.get_replay_buffer("test-token")
        assert result == []
        assert redis.lrange.call_count == 1
