"""Message queue abstraction that works with Redis or Google Pub/Sub."""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from .exceptions import QueueError

logger = logging.getLogger(__name__)


class MessageQueue(ABC):
    """Abstract message queue interface."""

    @abstractmethod
    async def publish(self, message: dict[str, Any]) -> None:
        """Publish a message to the queue."""

    @abstractmethod
    async def subscribe(
        self,
        callback: (
            Callable[[dict[str, Any]], None]
            | Callable[[dict[str, Any]], Awaitable[None]]
        ),
    ) -> None:
        """Subscribe to messages and process them with callback.

        Callback can be either sync or async.
        """

    @abstractmethod
    async def close(self) -> None:
        """Close the queue connection."""


class RedisQueue(MessageQueue):
    """Redis-based message queue (for self-hosted)."""

    def __init__(
        self,
        redis_url: str | None = None,
        queue_name: str = "agent-requests",
        password: str | None = None,
    ):
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379")
        self.password = password or os.getenv("REDIS_PASSWORD")
        self.queue_name = queue_name
        self.redis: Any = None  # Redis client, typed as Any due to dynamic import
        self._running = False

    async def _connect(self) -> None:
        """Connect to Redis."""
        if self.redis is None:
            try:
                import redis.asyncio as redis

                # redis_url is guaranteed to be a string from __init__
                url = self.redis_url if self.redis_url else "redis://localhost:6379"
                self.redis = await redis.from_url(
                    url, decode_responses=True, password=self.password
                )
            except ImportError as e:
                raise QueueError("redis package is required for RedisQueue") from e
            except OSError as e:
                raise QueueError(f"Failed to connect to Redis: {e}") from e

    async def publish(self, message: dict[str, Any]) -> None:
        """Publish a message to Redis list."""
        try:
            await self._connect()
            message_json = json.dumps(message)
            await self.redis.rpush(self.queue_name, message_json)
            logger.info(f"Published message to Redis queue: {self.queue_name}")
        except OSError as e:
            raise QueueError(f"Failed to publish message to Redis: {e}") from e
        except (TypeError, ValueError) as e:
            raise QueueError(f"Failed to serialize message: {e}") from e

    async def subscribe(
        self,
        callback: (
            Callable[[dict[str, Any]], None]
            | Callable[[dict[str, Any]], Awaitable[None]]
        ),
    ) -> None:
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
                    # Callback can be async
                    if asyncio.iscoroutinefunction(callback):
                        await callback(message)
                    else:
                        callback(message)
            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode message: {e}", exc_info=True)
            except OSError as e:
                logger.error(f"Redis connection error: {e}", exc_info=True)
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Error processing Redis message: {e}", exc_info=True)
                await asyncio.sleep(1)

    async def close(self) -> None:
        """Close Redis connection."""
        self._running = False
        if self.redis:
            await self.redis.aclose()


class PubSubQueue(MessageQueue):
    """Google Pub/Sub message queue (for cloud)."""

    def __init__(
        self,
        project_id: str | None = None,
        topic_name: str = "agent-requests",
        subscription_name: str = "agent-requests-sub",
    ):
        self.project_id = project_id or os.getenv("GCP_PROJECT_ID")
        self.topic_name = topic_name
        self.subscription_name = subscription_name
        self.publisher: Any = None  # PubSub publisher, typed as Any
        self.subscriber: Any = None  # PubSub subscriber, typed as Any
        self._running = False

    async def publish(self, message: dict[str, Any]) -> None:
        """Publish a message to Pub/Sub."""
        try:
            from google.cloud import pubsub_v1  # type: ignore[attr-defined]
        except ImportError as e:
            raise QueueError("google-cloud-pubsub is required for PubSubQueue") from e

        if self.publisher is None:
            self.publisher = pubsub_v1.PublisherClient()

        try:
            topic_path = self.publisher.topic_path(self.project_id, self.topic_name)
            message_json = json.dumps(message).encode("utf-8")

            future = self.publisher.publish(topic_path, message_json)
            future.result()  # Wait for publish to complete
            logger.info(f"Published message to Pub/Sub topic: {self.topic_name}")
        except (TypeError, ValueError) as e:
            raise QueueError(f"Failed to serialize message: {e}") from e
        except Exception as e:
            raise QueueError(f"Failed to publish to Pub/Sub: {e}") from e

    async def subscribe(
        self,
        callback: (
            Callable[[dict[str, Any]], None]
            | Callable[[dict[str, Any]], Awaitable[None]]
        ),
    ) -> None:
        """Subscribe to Pub/Sub and process messages."""
        try:
            from google.cloud import pubsub_v1  # type: ignore[attr-defined]
        except ImportError as e:
            raise QueueError("google-cloud-pubsub is required for PubSubQueue") from e

        if self.subscriber is None:
            self.subscriber = pubsub_v1.SubscriberClient()

        subscription_path = self.subscriber.subscription_path(
            self.project_id, self.subscription_name
        )

        # Get event loop for running async callbacks
        loop = asyncio.get_event_loop()

        def _callback(message: Any) -> None:
            try:
                data = json.loads(message.data.decode("utf-8"))
                logger.info(f"Received message from Pub/Sub: {data}")

                # Handle both sync and async callbacks
                if asyncio.iscoroutinefunction(callback):
                    task = loop.create_task(callback(data))

                    def _ack_on_complete(future: Any) -> None:
                        try:
                            future.result()
                            message.ack()
                            logger.debug("Message processed and acknowledged")
                        except Exception as e:
                            logger.error(
                                f"Callback failed, nacking message: {e}", exc_info=True
                            )
                            message.nack()

                    task.add_done_callback(_ack_on_complete)
                else:
                    callback(data)
                    message.ack()

            except json.JSONDecodeError as e:
                logger.error(f"Failed to decode Pub/Sub message: {e}", exc_info=True)
                message.nack()
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

    logger.info("Using Redis message queue")
    redis_password = os.getenv("REDIS_PASSWORD")
    return RedisQueue(password=redis_password)
