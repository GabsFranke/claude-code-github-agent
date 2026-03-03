"""Rate limiting utilities for API calls."""

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class RateLimiterBackend(ABC):
    """Abstract backend for rate limiting."""

    @abstractmethod
    async def acquire(
        self, key: str, max_requests: int, time_window: float, timeout: float | None
    ) -> bool:
        """Acquire permission to make a request."""

    @abstractmethod
    async def cleanup(self) -> None:
        """Cleanup resources."""


class InMemoryRateLimiterBackend(RateLimiterBackend):
    """In-memory rate limiter backend (single worker only)."""

    def __init__(self):
        self._requests: dict[str, deque] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(self, key: str) -> asyncio.Lock:
        """Get or create lock for key."""
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def _get_requests(self, key: str) -> deque:
        """Get or create request queue for key."""
        if key not in self._requests:
            self._requests[key] = deque()
        return self._requests[key]

    async def acquire(
        self, key: str, max_requests: int, time_window: float, timeout: float | None
    ) -> bool:
        """Acquire permission to make a request."""
        start_time = time.monotonic()
        lock = self._get_lock(key)

        async with lock:
            while True:
                now = time.monotonic()
                requests = self._get_requests(key)

                # Remove expired requests
                while requests and requests[0] <= now - time_window:
                    requests.popleft()

                # Check if we can proceed
                if len(requests) < max_requests:
                    requests.append(now)
                    return True

                # Check timeout
                if timeout is not None:
                    elapsed = now - start_time
                    if elapsed >= timeout:
                        raise TimeoutError(f"Rate limit timeout after {timeout}s")

                # Calculate wait time
                oldest_request = requests[0]
                wait_time = (oldest_request + time_window) - now

                # Release lock while waiting
                lock.release()
                try:
                    await asyncio.sleep(wait_time)
                finally:
                    await lock.acquire()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        self._requests.clear()
        self._locks.clear()


class RedisRateLimiterBackend(RateLimiterBackend):
    """Redis-based distributed rate limiter backend (multi-worker safe)."""

    def __init__(self, redis_client: Any):
        """
        Initialize Redis rate limiter backend.

        Args:
            redis_client: Redis async client instance
        """
        self.redis = redis_client

    async def acquire(
        self, key: str, max_requests: int, time_window: float, timeout: float | None
    ) -> bool:
        """
        Acquire permission using Redis sorted set for distributed rate limiting.

        Uses sliding window algorithm with Redis sorted sets where:
        - Key: rate limiter name
        - Score: timestamp
        - Value: unique request ID
        """
        start_time = time.monotonic()
        redis_key = f"rate_limit:{key}"

        while True:
            now = time.time()  # Use wall clock time for distributed coordination
            window_start = now - time_window

            # Use Redis pipeline for atomic operations
            pipe = self.redis.pipeline()

            # Remove expired entries
            pipe.zremrangebyscore(redis_key, 0, window_start)

            # Count current requests in window
            pipe.zcard(redis_key)

            # Execute pipeline
            results = await pipe.execute()
            current_count = results[1]

            # Check if we can proceed
            if current_count < max_requests:
                # Add new request with unique ID
                request_id = f"{now}:{id(object())}"
                added = await self.redis.zadd(redis_key, {request_id: now})

                if added:
                    # Set expiry on the key to prevent memory leaks
                    await self.redis.expire(redis_key, int(time_window) + 60)
                    return True

            # Check timeout
            if timeout is not None:
                elapsed = time.monotonic() - start_time
                if elapsed >= timeout:
                    raise TimeoutError(f"Rate limit timeout after {timeout}s")

            # Get oldest request timestamp to calculate wait time
            oldest_entries = await self.redis.zrange(redis_key, 0, 0, withscores=True)

            if oldest_entries:
                oldest_timestamp = oldest_entries[0][1]
                wait_time = (oldest_timestamp + time_window) - now
                wait_time = max(0.1, wait_time)  # Minimum 100ms wait
            else:
                wait_time = 0.1  # Default wait if no entries

            await asyncio.sleep(wait_time)

    async def cleanup(self) -> None:
        """Cleanup resources."""
        # Redis connection is managed externally, nothing to cleanup here


class RateLimiter:
    """Token bucket rate limiter for API calls with pluggable backends."""

    def __init__(
        self,
        max_requests: int,
        time_window: float,
        name: str = "rate_limiter",
        backend: RateLimiterBackend | None = None,
    ):
        """
        Initialize rate limiter.

        Args:
            max_requests: Maximum number of requests allowed in time window
            time_window: Time window in seconds
            name: Name for logging purposes
            backend: Rate limiter backend (defaults to in-memory)
        """
        self.max_requests = max_requests
        self.time_window = time_window
        self.name = name
        self.backend = backend or InMemoryRateLimiterBackend()

    async def acquire(self, timeout: float | None = None) -> bool:
        """
        Acquire permission to make a request.

        Args:
            timeout: Maximum time to wait in seconds (None = wait forever)

        Returns:
            True if acquired

        Raises:
            asyncio.TimeoutError: If timeout is exceeded
        """
        try:
            result = await self.backend.acquire(
                self.name, self.max_requests, self.time_window, timeout
            )
            logger.debug(f"{self.name}: Request acquired")
            return result
        except TimeoutError:
            logger.warning(f"{self.name}: Rate limit timeout exceeded")
            raise

    async def cleanup(self) -> None:
        """Cleanup backend resources."""
        await self.backend.cleanup()


class MultiRateLimiter:
    """Manages multiple rate limiters for different services."""

    def __init__(self, backend: RateLimiterBackend | None = None):
        """
        Initialize multi-rate limiter.

        Args:
            backend: Shared backend for all rate limiters (defaults to in-memory)
        """
        self._limiters: dict[str, RateLimiter] = {}
        self._backend = backend or InMemoryRateLimiterBackend()

    def add_limiter(
        self,
        name: str,
        max_requests: int,
        time_window: float,
    ) -> RateLimiter:
        """Add a rate limiter for a service."""
        limiter = RateLimiter(max_requests, time_window, name, self._backend)
        self._limiters[name] = limiter
        logger.info(
            f"Added rate limiter '{name}': {max_requests} requests per {time_window}s"
        )
        return limiter

    def get_limiter(self, name: str) -> RateLimiter | None:
        """Get a rate limiter by name."""
        return self._limiters.get(name)

    async def acquire(self, name: str, timeout: float | None = None) -> bool:
        """Acquire permission from a named rate limiter."""
        limiter = self.get_limiter(name)
        if limiter is None:
            logger.warning(f"Rate limiter '{name}' not found, allowing request")
            return True
        return await limiter.acquire(timeout)

    async def cleanup(self) -> None:
        """Cleanup all rate limiters."""
        await self._backend.cleanup()
        self._limiters.clear()


async def create_redis_rate_limiter_backend(
    redis_url: str | None = None, password: str | None = None
) -> RedisRateLimiterBackend:
    """
    Create a Redis-based rate limiter backend.

    Args:
        redis_url: Redis connection URL
        password: Redis password

    Returns:
        RedisRateLimiterBackend instance

    Raises:
        ImportError: If redis package is not installed
        ConnectionError: If cannot connect to Redis
    """
    try:
        import redis.asyncio as redis
    except ImportError as e:
        raise ImportError(
            "redis package is required for distributed rate limiting. "
            "Install with: pip install redis"
        ) from e

    try:
        url = redis_url or "redis://localhost:6379"
        redis_client = await redis.from_url(
            url, decode_responses=True, password=password
        )
        # Test connection
        await redis_client.ping()
        logger.info(f"Connected to Redis for distributed rate limiting: {url}")
        return RedisRateLimiterBackend(redis_client)
    except Exception as e:
        raise ConnectionError(f"Failed to connect to Redis: {e}") from e
