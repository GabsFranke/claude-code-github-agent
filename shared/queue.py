"""Message queue abstraction that works with Redis or Google Pub/Sub."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict

logger = logging.getLogger(__name__)


class MessageQueue(ABC):
    """Abstract message queue interface."""

    @abstractmethod
    async def publish(self, message: Dict[str, Any]) -> None:
        """Publish a message to the queue."""
        pass

    @abstractmethod
    async def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Subscribe to messages and process them with callback."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close the queue connection."""
        pass


class RedisQueue(MessageQueue):
    """Redis-based message queue (for self-hosted)."""

    def __init__(
        self,
        redis_url: str = None,
        queue_name: str = "agent-requests",
        password: str = None,
    ):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.password = password or os.getenv("REDIS_PASSWORD")
        self.queue_name = queue_name
        self.redis = None
        self._running = False

    async def _connect(self):
        """Connect to Redis."""
        if self.redis is None:
            import redis.asyncio as redis

            self.redis = await redis.from_url(
                self.redis_url, decode_responses=True, password=self.password
            )

    async def publish(self, message: Dict[str, Any]) -> None:
        """Publish a message to Redis list."""
        await self._connect()
        message_json = json.dumps(message)
        await self.redis.rpush(self.queue_name, message_json)
        logger.info(f"Published message to Redis queue: {self.queue_name}")

    async def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Subscribe to Redis list and process messages."""
        await self._connect()
        self._running = True
        logger.info(f"Subscribed to Redis queue: {self.queue_name}")

        while self._running:
            try:
                # Block for 1 second waiting for messages
                result = await self.redis.blpop(self.queue_name, timeout=1)
                if result:
                    _, message_json = result
                    message = json.loads(message_json)
                    logger.info(f"Received message from Redis: {message}")
                    await callback(message)
            except Exception as e:
                logger.error(f"Error processing Redis message: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def close(self) -> None:
        """Close Redis connection."""
        self._running = False
        if self.redis:
            await self.redis.close()


class PubSubQueue(MessageQueue):
    """Google Pub/Sub message queue (for cloud)."""

    def __init__(
        self,
        project_id: str = None,
        topic_name: str = "agent-requests",
        subscription_name: str = "agent-requests-sub",
    ):
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID")
        self.topic_name = topic_name
        self.subscription_name = subscription_name
        self.publisher = None
        self.subscriber = None
        self._running = False

    async def publish(self, message: Dict[str, Any]) -> None:
        """Publish a message to Pub/Sub."""
        from google.cloud import pubsub_v1

        if self.publisher is None:
            self.publisher = pubsub_v1.PublisherClient()

        topic_path = self.publisher.topic_path(self.project_id, self.topic_name)
        message_json = json.dumps(message).encode("utf-8")

        future = self.publisher.publish(topic_path, message_json)
        future.result()  # Wait for publish to complete
        logger.info(f"Published message to Pub/Sub topic: {self.topic_name}")

    async def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """Subscribe to Pub/Sub and process messages."""
        from google.cloud import pubsub_v1

        if self.subscriber is None:
            self.subscriber = pubsub_v1.SubscriberClient()

        subscription_path = self.subscriber.subscription_path(
            self.project_id, self.subscription_name
        )

        # Get event loop for running async callbacks
        loop = asyncio.get_event_loop()

        def _callback(message):
            try:
                data = json.loads(message.data.decode("utf-8"))
                logger.info(f"Received message from Pub/Sub: {data}")

                # Create task and wait for completion before acking
                task = loop.create_task(callback(data))

                # Use callback to ack after task completes
                def _ack_on_complete(future):
                    try:
                        future.result()  # Raise exception if callback failed
                        message.ack()
                        logger.debug("Message processed and acknowledged")
                    except Exception as e:
                        logger.error(
                            f"Callback failed, nacking message: {e}", exc_info=True
                        )
                        message.nack()

                task.add_done_callback(_ack_on_complete)

            except Exception as e:
                logger.error(f"Error processing Pub/Sub message: {e}", exc_info=True)
                message.nack()

        self._running = True
        logger.info(f"Subscribed to Pub/Sub subscription: {self.subscription_name}")

        streaming_pull_future = self.subscriber.subscribe(
            subscription_path, callback=_callback
        )

        try:
            # Keep the subscriber running
            while self._running:
                await asyncio.sleep(1)
        finally:
            streaming_pull_future.cancel()

    async def close(self) -> None:
        """Close Pub/Sub connections."""
        self._running = False


def get_queue() -> MessageQueue:
    """Get the appropriate message queue based on environment."""
    queue_type = os.getenv("QUEUE_TYPE", "redis").lower()

    if queue_type == "pubsub":
        logger.info("Using Google Pub/Sub message queue")
        return PubSubQueue()
    else:
        logger.info("Using Redis message queue")
        redis_password = os.getenv("REDIS_PASSWORD")
        return RedisQueue(password=redis_password)
