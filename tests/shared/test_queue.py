"""Unit tests for message queue module."""

from unittest.mock import AsyncMock, patch

import pytest

from shared.queue import PubSubQueue, RedisQueue, get_queue


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
