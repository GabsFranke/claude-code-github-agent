"""Unit tests for message queue module."""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.exceptions import RepositorySyncError
from shared.queue import PubSubQueue, RedisQueue, get_queue, wait_for_repo_sync


class TestRedisQueue:
    """Test RedisQueue class."""

    @pytest.mark.asyncio
    async def test_redis_queue_initialization(self):
        """Test RedisQueue initialization."""
        queue = RedisQueue(redis_url="redis://localhost:6379", queue_name="test-queue")
        assert queue.redis_url == "redis://localhost:6379"
        assert queue.queue_name == "test-queue"
        assert queue.redis is None

    @pytest.mark.asyncio
    async def test_redis_queue_publish(self):
        """Test publishing a message to Redis queue."""
        queue = RedisQueue(queue_name="test-queue")

        # Mock Redis client
        mock_redis = AsyncMock()
        mock_redis.rpush = AsyncMock()
        queue.redis = mock_redis

        message = {"event": "test", "data": "value"}
        await queue.publish(message)

        mock_redis.rpush.assert_called_once()

    @pytest.mark.asyncio
    async def test_redis_queue_close(self):
        """Test closing Redis queue."""
        queue = RedisQueue()
        mock_redis = AsyncMock()
        queue.redis = mock_redis

        await queue.close()

        assert queue._running is False
        mock_redis.aclose.assert_called_once()


class TestPubSubQueue:
    """Test PubSubQueue class."""

    def test_pubsub_queue_initialization(self):
        """Test PubSubQueue initialization."""
        queue = PubSubQueue(
            project_id="test-project",
            topic_name="test-topic",
            subscription_name="test-sub",
        )
        assert queue.project_id == "test-project"
        assert queue.topic_name == "test-topic"
        assert queue.subscription_name == "test-sub"

    @pytest.mark.asyncio
    async def test_pubsub_queue_close(self):
        """Test closing PubSub queue."""
        queue = PubSubQueue(project_id="test-project")
        queue._running = True

        await queue.close()

        assert queue._running is False


class TestGetQueue:
    """Test get_queue factory function."""

    @patch.dict("os.environ", {"QUEUE_TYPE": "redis"})
    def test_get_queue_redis(self):
        """Test get_queue returns RedisQueue."""
        queue = get_queue()
        assert isinstance(queue, RedisQueue)

    @patch.dict("os.environ", {"QUEUE_TYPE": "pubsub"})
    def test_get_queue_pubsub(self):
        """Test get_queue returns PubSubQueue."""
        queue = get_queue()
        assert isinstance(queue, PubSubQueue)

    @patch.dict("os.environ", {}, clear=True)
    def test_get_queue_default(self):
        """Test get_queue defaults to Redis."""
        queue = get_queue()
        assert isinstance(queue, RedisQueue)


class TestWaitForRepoSync:
    """Test wait_for_repo_sync deduplication logic."""

    @pytest.mark.asyncio
    async def test_fast_path_cached(self):
        """Return immediately when completion key and repo dir exist."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value="1")
        expected_dir = os.path.join("/var/cache/repos", "owner/repo.git")

        with patch("os.path.exists", return_value=True):
            result = await wait_for_repo_sync("owner/repo", "main", mock_redis)

        assert result == expected_dir
        mock_redis.get.assert_called_once_with("agent:sync:complete:owner/repo:main")

    @pytest.mark.asyncio
    async def test_publishes_sync_when_lock_not_held(self):
        """Publish sync request when no lock is held."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=False)

        completion_msg = {
            "type": "message",
            "data": json.dumps(
                {"repo": "owner/repo", "ref": "main", "status": "complete"}
            ),
        }
        mock_pubsub = _make_pubsub_mock([completion_msg])
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with patch("os.path.exists", return_value=True):
            with patch(
                "shared.queue._request_repo_sync", new_callable=AsyncMock
            ) as mock_req:
                await wait_for_repo_sync("owner/repo", "main", mock_redis)

        mock_req.assert_called_once_with("owner/repo", "main", mock_redis)

    @pytest.mark.asyncio
    async def test_skips_publish_when_lock_held(self):
        """Skip sync request when lock key exists (sync already in-flight)."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=True)

        completion_msg = {
            "type": "message",
            "data": json.dumps(
                {"repo": "owner/repo", "ref": "main", "status": "complete"}
            ),
        }
        mock_pubsub = _make_pubsub_mock([completion_msg])
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with patch("os.path.exists", return_value=True):
            with patch(
                "shared.queue._request_repo_sync", new_callable=AsyncMock
            ) as mock_req:
                await wait_for_repo_sync("owner/repo", "main", mock_redis)

        mock_req.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_raises_error(self):
        """Raise RepositorySyncError on timeout."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=True)

        # Infinite stream of non-matching messages so timeout check fires
        mock_pubsub = _make_infinite_pubsub_mock()
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with patch("os.path.exists", return_value=True):
            with patch("shared.queue._request_repo_sync", new_callable=AsyncMock):
                with pytest.raises(RepositorySyncError, match="Sync timeout"):
                    await wait_for_repo_sync(
                        "owner/repo", "main", mock_redis, timeout=0
                    )

    @pytest.mark.asyncio
    async def test_error_event_raises(self):
        """Raise RepositorySyncError when sync error event received."""
        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.exists = AsyncMock(return_value=False)

        error_msg = {
            "type": "message",
            "data": json.dumps(
                {
                    "repo": "owner/repo",
                    "ref": "main",
                    "status": "error",
                    "error": "clone failed",
                }
            ),
        }
        mock_pubsub = _make_pubsub_mock([error_msg])
        mock_redis.pubsub = MagicMock(return_value=mock_pubsub)

        with patch("os.path.exists", return_value=True):
            with patch("shared.queue._request_repo_sync", new_callable=AsyncMock):
                with pytest.raises(RepositorySyncError, match="clone failed"):
                    await wait_for_repo_sync("owner/repo", "main", mock_redis)


def _make_pubsub_mock(messages: list[dict]) -> MagicMock:
    """Create a pubsub mock that yields the given messages via listen()."""
    mock_pubsub = MagicMock()

    async def _listen():
        for msg in messages:
            yield msg

    mock_pubsub.listen = MagicMock(return_value=_listen())
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.unsubscribe = AsyncMock()
    mock_pubsub.close = AsyncMock()
    return mock_pubsub


def _make_infinite_pubsub_mock() -> MagicMock:
    """Create a pubsub mock with an infinite stream of subscribe messages."""
    import asyncio

    mock_pubsub = MagicMock()

    async def _listen():
        while True:
            yield {"type": "subscribe", "data": None}
            await asyncio.sleep(0.01)

    mock_pubsub.listen = MagicMock(return_value=_listen())
    mock_pubsub.subscribe = AsyncMock()
    mock_pubsub.unsubscribe = AsyncMock()
    mock_pubsub.close = AsyncMock()
    return mock_pubsub
