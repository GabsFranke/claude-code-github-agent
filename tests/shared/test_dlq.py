"""Tests for shared DLQ utilities."""

import json
from unittest.mock import AsyncMock

import pytest

from shared.dlq import enqueue_for_retry, get_dlq_count, inspect_dlq, is_transient_error


class TestIsTransientError:
    def test_timeout_is_transient(self):
        assert is_transient_error(TimeoutError("connection timeout")) is True

    def test_429_is_transient(self):
        assert is_transient_error(Exception("HTTP 429 Too Many Requests")) is True

    def test_503_is_transient(self):
        assert is_transient_error(Exception("503 Service Unavailable")) is True

    def test_502_is_transient(self):
        assert is_transient_error(Exception("502 Bad Gateway")) is True

    def test_econnrefused_is_transient(self):
        assert is_transient_error(OSError("ECONNREFUSED")) is True

    def test_resource_exhausted_is_transient(self):
        assert is_transient_error(Exception("RESOURCE_EXHAUSTED")) is True

    def test_value_error_not_transient(self):
        assert is_transient_error(ValueError("bad input")) is False

    def test_key_error_not_transient(self):
        assert is_transient_error(KeyError("missing")) is False

    def test_config_error_not_transient(self):
        assert is_transient_error(Exception("invalid configuration")) is False

    def test_empty_message_not_transient(self):
        assert is_transient_error(Exception("")) is False


class TestEnqueueForRetry:
    @pytest.mark.asyncio
    async def test_reenqueue_on_first_failure(self):
        redis = AsyncMock()
        message = {"repo": "owner/repo", "ref": "main"}

        await enqueue_for_retry(
            redis, "queue", "dlq", message, Exception("timeout"), max_retries=3
        )

        redis.rpush.assert_called_once()
        args = redis.rpush.call_args
        assert args[0][0] == "queue"
        payload = json.loads(args[0][1])
        assert payload["attempts"] == 1
        assert payload["last_error"] == "Exception: timeout"

    @pytest.mark.asyncio
    async def test_sends_to_dlq_after_max_retries(self):
        redis = AsyncMock()
        message = {"repo": "owner/repo", "attempts": 2}

        await enqueue_for_retry(
            redis, "queue", "dlq", message, Exception("timeout"), max_retries=3
        )

        redis.rpush.assert_called_once()
        args = redis.rpush.call_args
        assert args[0][0] == "dlq"
        dlq_entry = json.loads(args[0][1])
        assert dlq_entry["reason"] == "max_retries_exceeded"
        assert dlq_entry["attempts"] == 3

    @pytest.mark.asyncio
    async def test_force_immediate_dlq(self):
        redis = AsyncMock()
        message = {"repo": "owner/repo"}

        await enqueue_for_retry(
            redis, "queue", "dlq", message, Exception("bad config"), max_retries=0
        )

        redis.rpush.assert_called_once()
        args = redis.rpush.call_args
        assert args[0][0] == "dlq"

    @pytest.mark.asyncio
    async def test_preserves_original_message_in_dlq(self):
        redis = AsyncMock()
        message = {"repo": "owner/repo", "ref": "abc123", "trigger": "push"}

        await enqueue_for_retry(
            redis, "queue", "dlq", message, Exception("fail"), max_retries=0
        )

        args = redis.rpush.call_args
        dlq_entry = json.loads(args[0][1])
        assert dlq_entry["original_message"]["repo"] == "owner/repo"
        assert dlq_entry["original_message"]["ref"] == "abc123"


class TestGetDlqCount:
    @pytest.mark.asyncio
    async def test_returns_count(self):
        redis = AsyncMock()
        redis.llen = AsyncMock(return_value=5)
        count = await get_dlq_count(redis, "test:dlq")
        assert count == 5

    @pytest.mark.asyncio
    async def test_returns_zero_on_error(self):
        redis = AsyncMock()
        redis.llen = AsyncMock(side_effect=Exception("redis down"))
        count = await get_dlq_count(redis, "test:dlq")
        assert count == 0


class TestInspectDlq:
    @pytest.mark.asyncio
    async def test_returns_entries(self):
        redis = AsyncMock()
        entries = [json.dumps({"reason": "test", "attempts": 1})]
        redis.lrange = AsyncMock(return_value=entries)
        result = await inspect_dlq(redis, "test:dlq", limit=10)
        assert len(result) == 1
        assert result[0]["reason"] == "test"

    @pytest.mark.asyncio
    async def test_returns_empty_on_error(self):
        redis = AsyncMock()
        redis.lrange = AsyncMock(side_effect=Exception("redis down"))
        result = await inspect_dlq(redis, "test:dlq")
        assert result == []
