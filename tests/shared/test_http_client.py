"""Unit tests for HTTP client utilities."""

import pytest

from shared.http_client import AsyncHTTPClient, close_http_client, get_http_client


class TestAsyncHTTPClient:
    """Test AsyncHTTPClient class."""

    @pytest.mark.asyncio
    async def test_client_initialization(self):
        """Test HTTP client initialization."""
        client = AsyncHTTPClient(timeout=30.0)
        await client.connect()

        assert client._client is not None
        assert client._timeout == 30.0

        await client.close()

    @pytest.mark.asyncio
    async def test_context_manager(self):
        """Test async context manager."""
        async with AsyncHTTPClient() as client:
            assert client._client is not None

        # Should be closed after context exit
        assert client._client is None

    @pytest.mark.asyncio
    async def test_client_property_before_connect(self):
        """Test accessing client property before connect raises error."""
        http_client = AsyncHTTPClient()

        with pytest.raises(RuntimeError, match="not initialized"):
            _ = http_client.client

    @pytest.mark.asyncio
    async def test_client_property_after_connect(self):
        """Test accessing client property after connect."""
        http_client = AsyncHTTPClient()
        await http_client.connect()

        client = http_client.client
        assert client is not None

        await http_client.close()


class TestGlobalHTTPClient:
    """Test global HTTP client functions."""

    @pytest.mark.asyncio
    async def test_get_http_client(self):
        """Test getting global HTTP client."""
        client = get_http_client()
        assert isinstance(client, AsyncHTTPClient)

        # Clean up
        await close_http_client()

    @pytest.mark.asyncio
    async def test_close_http_client(self):
        """Test closing global HTTP client."""
        client = get_http_client()
        await client.connect()

        await close_http_client()

        # Should be able to get a new client
        new_client = get_http_client()
        assert isinstance(new_client, AsyncHTTPClient)
