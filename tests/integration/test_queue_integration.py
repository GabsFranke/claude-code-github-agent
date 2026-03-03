"""Integration tests for message queue with real Redis."""

import asyncio

import pytest

from shared.queue import RedisQueue


@pytest.mark.integration
@pytest.mark.slow
class TestQueueIntegration:
    """Test message queue with real Redis instance."""

    @pytest.mark.asyncio
    async def test_publish_and_subscribe(self, redis_client):
        """Test publishing and subscribing with real Redis."""
        queue = RedisQueue(
            redis_url="redis://localhost:6379",
            queue_name="test_integration",
            password="S5e_V7kdhPOI9DNJfBvYodxJgeQCG8Xup2mG3rBPwDU",
        )

        # Publish a message
        test_message = {"event": "test", "data": "integration"}
        await queue.publish(test_message)

        # Subscribe and receive with timeout
        received = []

        async def callback(msg):
            received.append(msg)

        # Start subscriber in background
        subscriber_task = asyncio.create_task(queue.subscribe(callback))

        # Wait for message with timeout
        try:
            for _ in range(50):  # 5 seconds total
                if len(received) >= 1:
                    break
                await asyncio.sleep(0.1)
        finally:
            await queue.close()
            subscriber_task.cancel()
            try:
                await subscriber_task
            except asyncio.CancelledError:
                pass

        assert len(received) == 1
        assert received[0]["event"] == "test"

    @pytest.mark.asyncio
    async def test_multiple_messages(self, redis_client):
        """Test handling multiple messages."""
        queue = RedisQueue(
            redis_url="redis://localhost:6379",
            queue_name="test_multi",
            password="S5e_V7kdhPOI9DNJfBvYodxJgeQCG8Xup2mG3rBPwDU",
        )

        # Publish multiple messages
        messages = [{"id": i, "data": f"message_{i}"} for i in range(5)]

        for msg in messages:
            await queue.publish(msg)

        # Receive messages with timeout
        received = []

        async def callback(msg):
            received.append(msg)

        # Start subscriber in background
        subscriber_task = asyncio.create_task(queue.subscribe(callback))

        # Wait for all messages with timeout
        try:
            for _ in range(100):  # 10 seconds total
                if len(received) >= 5:
                    break
                await asyncio.sleep(0.1)
        finally:
            await queue.close()
            subscriber_task.cancel()
            try:
                await subscriber_task
            except asyncio.CancelledError:
                pass

        assert len(received) == 5
        assert all(msg["id"] in range(5) for msg in received)
