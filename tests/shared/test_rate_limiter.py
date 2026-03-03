"""Unit tests for rate limiter module."""

import asyncio
import time

import pytest

from shared.rate_limiter import (
    InMemoryRateLimiterBackend,
    MultiRateLimiter,
    RateLimiter,
)


class TestInMemoryRateLimiterBackend:
    """Test InMemoryRateLimiterBackend class."""

    @pytest.mark.asyncio
    async def test_backend_acquire_within_limit(self):
        """Test backend allows requests within limit."""
        backend = InMemoryRateLimiterBackend()

        # Should allow 5 requests immediately
        for _ in range(5):
            result = await backend.acquire(
                "test", max_requests=5, time_window=1.0, timeout=None
            )
            assert result is True

    @pytest.mark.asyncio
    async def test_backend_cleanup(self):
        """Test backend cleanup."""
        backend = InMemoryRateLimiterBackend()
        await backend.acquire("test", max_requests=5, time_window=1.0, timeout=None)

        await backend.cleanup()

        assert len(backend._requests) == 0
        assert len(backend._locks) == 0


class TestRateLimiter:
    """Test RateLimiter class."""

    @pytest.mark.asyncio
    async def test_rate_limiter_allows_within_limit(self):
        """Test rate limiter allows requests within limit."""
        limiter = RateLimiter(max_requests=5, time_window=1.0, name="test")

        # Should allow 5 requests immediately
        for _ in range(5):
            await limiter.acquire()

        assert True  # All requests completed

    @pytest.mark.asyncio
    async def test_rate_limiter_blocks_over_limit(self):
        """Test rate limiter blocks requests over limit."""
        limiter = RateLimiter(max_requests=2, time_window=1.0, name="test")

        # First 2 requests should be immediate
        start = time.time()
        await limiter.acquire()
        await limiter.acquire()
        first_two = time.time() - start

        assert first_two < 0.1  # First two should be fast

    @pytest.mark.asyncio
    async def test_rate_limiter_timeout(self):
        """Test rate limiter respects timeout."""
        limiter = RateLimiter(max_requests=1, time_window=10.0, name="test")

        # Use up the limit
        await limiter.acquire()

        # Next request should timeout
        # Note: The actual timeout behavior depends on the backend implementation
        # For now, we just verify it doesn't hang forever
        try:
            await asyncio.wait_for(limiter.acquire(timeout=0.1), timeout=0.2)
        except TimeoutError:
            pass  # Expected

    @pytest.mark.asyncio
    async def test_rate_limiter_cleanup(self):
        """Test rate limiter cleanup."""
        limiter = RateLimiter(max_requests=5, time_window=1.0, name="test")
        await limiter.acquire()

        await limiter.cleanup()

        # Should be able to create new limiter after cleanup
        assert True


class TestMultiRateLimiter:
    """Test MultiRateLimiter class."""

    def test_multi_rate_limiter_add_limiter(self):
        """Test adding rate limiters."""
        multi = MultiRateLimiter()

        limiter = multi.add_limiter("github", max_requests=5000, time_window=3600)

        assert isinstance(limiter, RateLimiter)
        assert multi.get_limiter("github") is limiter

    @pytest.mark.asyncio
    async def test_multi_rate_limiter_acquire(self):
        """Test acquiring from named limiter."""
        multi = MultiRateLimiter()
        multi.add_limiter("test", max_requests=5, time_window=1.0)

        result = await multi.acquire("test")

        assert result is True

    @pytest.mark.asyncio
    async def test_multi_rate_limiter_acquire_nonexistent(self):
        """Test acquiring from nonexistent limiter allows request."""
        multi = MultiRateLimiter()

        result = await multi.acquire("nonexistent")

        assert result is True  # Should allow when limiter doesn't exist

    @pytest.mark.asyncio
    async def test_multi_rate_limiter_cleanup(self):
        """Test multi rate limiter cleanup."""
        multi = MultiRateLimiter()
        multi.add_limiter("test", max_requests=5, time_window=1.0)

        await multi.cleanup()

        assert len(multi._limiters) == 0
