"""Unit tests for retry logic module."""

import pytest

from shared.exceptions import RetryExhaustedError
from shared.retry import async_retry


class TestAsyncRetry:
    """Test async_retry decorator."""

    @pytest.mark.asyncio
    async def test_retry_succeeds_first_try(self):
        """Test function succeeds on first try."""
        call_count = 0

        @async_retry(max_attempts=3)
        async def test_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = await test_func()

        assert result == "success"
        assert call_count == 1

    @pytest.mark.asyncio
    async def test_retry_succeeds_after_failures(self):
        """Test function succeeds after some failures."""
        call_count = 0

        @async_retry(max_attempts=3, base_delay=0.01)
        async def test_func():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("fail")
            return "success"

        result = await test_func()

        assert result == "success"
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_retry_exhausts_attempts(self):
        """Test function fails after max retries."""

        @async_retry(max_attempts=2, base_delay=0.01)
        async def test_func():
            raise ValueError("persistent failure")

        with pytest.raises(RetryExhaustedError):
            await test_func()

    @pytest.mark.asyncio
    async def test_retry_with_specific_exceptions(self):
        """Test retry only catches specified exceptions."""

        @async_retry(max_attempts=3, base_delay=0.01, exceptions=(ValueError,))
        async def test_func():
            raise TypeError("wrong exception type")

        # Should not retry TypeError
        with pytest.raises(TypeError, match="wrong exception type"):
            await test_func()
