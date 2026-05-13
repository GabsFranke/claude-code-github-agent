"""Tests for shared/post_processing.py — enqueue and flush logic."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import shared.post_processing as pp
from shared.post_processing import (
    enqueue_indexing_job,
    enqueue_memory_job,
    enqueue_retrospector_job,
    flush_pending_post_jobs,
    get_redis,
)


def _make_redis():
    """Create a mock Redis client for testing."""
    redis = MagicMock()
    redis.rpush = AsyncMock(return_value=1)
    return redis


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    """Reset the module-level Redis singleton between tests."""
    pp._redis = None
    yield
    pp._redis = None


class TestEnqueueMemoryJob:
    @pytest.mark.asyncio
    async def test_pushes_to_correct_key(self):
        redis = _make_redis()
        pp._redis = redis

        await enqueue_memory_job(
            repo="owner/repo",
            transcript_path="/path/to/transcript.jsonl",
            hook_event="Stop",
        )

        call_args = redis.rpush.call_args
        assert call_args[0][0] == "agent:memory:requests"
        payload = json.loads(call_args[0][1])
        assert payload["repo"] == "owner/repo"
        assert payload["hook_event"] == "Stop"
        assert payload["transcript_path"] == "/path/to/transcript.jsonl"

    @pytest.mark.asyncio
    async def test_includes_optional_fields(self):
        redis = _make_redis()
        pp._redis = redis

        await enqueue_memory_job(
            repo="owner/repo",
            transcript_path="/path/to/transcript.jsonl",
            hook_event="Stop",
            claude_md="some claude content",
            memory_index="owner--repo",
        )

        payload = json.loads(redis.rpush.call_args[0][1])
        assert payload["claude_md"] == "some claude content"
        assert payload["memory_index"] == "owner--repo"


class TestEnqueueRetrospectorJob:
    @pytest.mark.asyncio
    async def test_pushes_to_correct_key(self):
        redis = _make_redis()
        pp._redis = redis

        await enqueue_retrospector_job(
            repo="owner/repo",
            transcript_path="/path/to/transcript.jsonl",
            hook_event="Stop",
            workflow_name="review-pr",
            session_meta={"test": True},
        )

        call_args = redis.rpush.call_args
        assert call_args[0][0] == "agent:retrospector:requests"
        payload = json.loads(call_args[0][1])
        assert payload["repo"] == "owner/repo"
        assert payload["workflow_name"] == "review-pr"
        assert payload["session_meta"] == {"test": True}


class TestEnqueueIndexingJob:
    @pytest.mark.asyncio
    async def test_pushes_to_correct_key(self):
        redis = _make_redis()
        pp._redis = redis

        await enqueue_indexing_job(
            repo="owner/repo",
            hook_event="Stop",
            ref="refs/heads/main",
        )

        call_args = redis.rpush.call_args
        assert call_args[0][0] == "agent:indexing:requests"
        payload = json.loads(call_args[0][1])
        assert payload["repo"] == "owner/repo"
        assert payload["ref"] == "refs/heads/main"
        assert payload["trigger"] == "job_stop"

    @pytest.mark.asyncio
    async def test_default_ref_is_main(self):
        redis = _make_redis()
        pp._redis = redis

        await enqueue_indexing_job(
            repo="owner/repo",
            hook_event="Stop",
        )

        payload = json.loads(redis.rpush.call_args[0][1])
        assert payload["ref"] == "main"


class TestFlushPendingPostJobs:
    @pytest.mark.asyncio
    async def test_empty_list_is_noop(self):
        """Flushing an empty list should not call Redis at all."""
        redis = _make_redis()
        pp._redis = redis

        await flush_pending_post_jobs([])
        redis.rpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_deduplication_by_transcript_event_type(self):
        """Duplicate (transcript_path, event, type) entries should keep only the last."""
        redis = _make_redis()
        pp._redis = redis

        jobs = [
            {
                "type": "memory",
                "repo": "owner/repo",
                "transcript_path": "/path/a.jsonl",
                "event": "Stop",
                "claude_md": "old",
            },
            {
                "type": "memory",
                "repo": "owner/repo",
                "transcript_path": "/path/a.jsonl",
                "event": "Stop",
                "claude_md": "new",
            },
        ]

        await flush_pending_post_jobs(jobs)

        # Only one rpush call (deduped)
        assert redis.rpush.call_count == 1
        payload = json.loads(redis.rpush.call_args[0][1])
        assert payload["claude_md"] == "new"

    @pytest.mark.asyncio
    async def test_mixed_types_enqueue_to_correct_queues(self):
        """Different job types go to different queues."""
        redis = _make_redis()
        pp._redis = redis

        jobs = [
            {
                "type": "memory",
                "repo": "owner/repo",
                "transcript_path": "/a.jsonl",
                "event": "Stop",
            },
            {
                "type": "retrospector",
                "repo": "owner/repo",
                "transcript_path": "/a.jsonl",
                "event": "Stop",
                "workflow_name": "review-pr",
                "session_meta": {},
            },
            {
                "type": "indexing",
                "repo": "owner/repo",
                "event": "Stop",
                "ref": "main",
            },
        ]

        await flush_pending_post_jobs(jobs)

        keys = [call[0][0] for call in redis.rpush.call_args_list]
        assert "agent:memory:requests" in keys
        assert "agent:retrospector:requests" in keys
        assert "agent:indexing:requests" in keys

    @pytest.mark.asyncio
    async def test_retry_on_first_failure(self):
        """First failure triggers retry; second attempt succeeds."""
        redis = _make_redis()
        redis.rpush = AsyncMock(side_effect=[Exception("connection error"), 1])
        pp._redis = redis

        # Should succeed on second attempt
        await enqueue_memory_job(
            repo="owner/repo",
            transcript_path="/path.jsonl",
            hook_event="Stop",
        )

        assert redis.rpush.call_count == 2

    @pytest.mark.asyncio
    async def test_final_failure_logs_error(self):
        """After max retries, should log error and return without raising."""
        redis = _make_redis()
        redis.rpush = AsyncMock(side_effect=Exception("persistent error"))
        pp._redis = redis

        # Should not raise, just log error
        await enqueue_memory_job(
            repo="owner/repo",
            transcript_path="/path.jsonl",
            hook_event="Stop",
        )

        assert redis.rpush.call_count == 2  # Initial attempt + 1 retry


class TestGetRedis:
    def test_returns_client_when_initialized(self):
        """get_redis() should return the client after initialization."""
        mock_redis = MagicMock()
        pp._redis = mock_redis
        assert get_redis() is mock_redis

    def test_returns_same_client_on_subsequent_calls(self):
        """get_redis() should return the same client on subsequent calls."""
        mock_redis = MagicMock()
        pp._redis = mock_redis
        assert get_redis() is get_redis()
