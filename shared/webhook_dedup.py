"""Idempotency guard for GitHub webhook deliveries.

GitHub delivers a webhook more than once in several ordinary situations:
the receiver times out or returns 5xx and GitHub retries, or a maintainer
replays a delivery from the App's *Advanced* tab.  Every one of those
carries the *same* ``X-GitHub-Delivery`` UUID, so the id is the natural
idempotency token.

Without this guard a single retried ``issue_comment`` would queue the agent
job twice and the agent would post its answer twice.

The claim is taken with ``SET key value NX EX ttl``, which is atomic in
Redis, so concurrent replicas of the webhook service cannot both win.

Failure policy
--------------
The guard **fails open**: if Redis is unreachable the delivery is processed.
Dropping real events on an infrastructure blip is worse than the duplicate
work this module exists to prevent, and the downstream session dedup lock
(``shared.constants.session_dedup_key``) still absorbs near-simultaneous
duplicates.
"""

import logging
import os
from typing import Any

from .constants import WEBHOOK_DEDUP_TTL_SECONDS, webhook_dedup_key

logger = logging.getLogger(__name__)


class WebhookDeduplicator:
    """Remembers recently seen GitHub delivery ids in Redis."""

    def __init__(
        self,
        redis_url: str | None = None,
        password: str | None = None,
        ttl_seconds: int = WEBHOOK_DEDUP_TTL_SECONDS,
    ):
        self.redis_url: str = (
            redis_url or os.getenv("REDIS_URL") or "redis://localhost:6379"
        )
        self.password = password or os.getenv("REDIS_PASSWORD")
        self.ttl_seconds = ttl_seconds
        self.redis: Any = None  # Redis client, typed as Any due to dynamic import
        self._degraded = False  # True once we have logged a connection failure

    async def _connect(self) -> None:
        """Connect to Redis on first use."""
        if self.redis is None:
            import redis.asyncio as redis

            self.redis = await redis.from_url(
                self.redis_url,
                decode_responses=True,
                password=self.password,
                socket_timeout=5,
                socket_connect_timeout=5,
                retry_on_timeout=True,
            )

    async def claim(self, delivery_id: str) -> bool:
        """Claim *delivery_id* for processing.

        Returns:
            ``True`` if this is the first time the id has been seen (the
            caller should process the delivery), ``False`` if the id was
            already claimed (the caller should drop it as a replay).

            Also returns ``True`` when *delivery_id* is empty or Redis is
            unavailable — see the module docstring on failing open.
        """
        if not delivery_id:
            logger.debug("No delivery id supplied; skipping dedup check")
            return True

        try:
            await self._connect()
            was_set = await self.redis.set(
                webhook_dedup_key(delivery_id),
                "1",
                nx=True,
                ex=self.ttl_seconds,
            )
        except Exception as e:
            if not self._degraded:
                self._degraded = True
                logger.warning(
                    "Webhook dedup unavailable (%s); processing deliveries "
                    "without replay protection",
                    e,
                )
            self.redis = None
            return True

        self._degraded = False
        return bool(was_set)

    async def release(self, delivery_id: str) -> None:
        """Drop the claim on *delivery_id* so a GitHub retry can be processed.

        Called only when handling failed unexpectedly.  Deterministic
        rejections (bad payload, no matching workflow) keep their claim —
        replaying them would reach the same conclusion.
        """
        if not delivery_id or self.redis is None:
            return

        try:
            await self.redis.delete(webhook_dedup_key(delivery_id))
        except Exception as e:
            # The key expires on its own; a failed release only means a
            # retry of an already-failed delivery gets dropped.
            logger.warning("Failed to release dedup claim %s: %s", delivery_id, e)

    async def close(self) -> None:
        """Close the Redis connection."""
        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None
