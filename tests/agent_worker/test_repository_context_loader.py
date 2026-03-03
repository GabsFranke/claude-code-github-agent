"""Unit tests for RepositoryContextLoader."""

from unittest.mock import AsyncMock, Mock

import pytest

from services.agent_worker.processors.repository_context_loader import (
    RepositoryContextLoader,
)


class TestRepositoryContextLoader:
    """Test RepositoryContextLoader class."""

    def test_initialization(self):
        """Test RepositoryContextLoader initialization."""
        token_manager = Mock()
        http_client = Mock()

        loader = RepositoryContextLoader(token_manager, http_client)

        assert loader.token_manager == token_manager
        assert loader.http_client == http_client
        assert loader.rate_limiters is None

    def test_initialization_with_rate_limiters(self):
        """Test initialization with rate limiters."""
        token_manager = Mock()
        http_client = Mock()
        rate_limiters = Mock()

        loader = RepositoryContextLoader(token_manager, http_client, rate_limiters)

        assert loader.rate_limiters == rate_limiters

    @pytest.mark.asyncio
    async def test_fetch_claude_md_success(self):
        """Test fetching CLAUDE.md successfully."""
        token_manager = AsyncMock()
        token_manager.get_token.return_value = "test-token"

        http_client = AsyncMock()
        response = Mock()
        response.status_code = 200
        response.text = "# CLAUDE.md content"
        http_client.get.return_value = response

        loader = RepositoryContextLoader(token_manager, http_client)
        content = await loader.fetch_claude_md("owner/repo")

        assert content == "# CLAUDE.md content"
        http_client.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_fetch_claude_md_not_found(self):
        """Test fetching CLAUDE.md when file doesn't exist."""
        token_manager = AsyncMock()
        token_manager.get_token.return_value = "test-token"

        http_client = AsyncMock()
        response = Mock()
        response.status_code = 404
        http_client.get.return_value = response

        loader = RepositoryContextLoader(token_manager, http_client)
        content = await loader.fetch_claude_md("owner/repo")

        assert content == ""

    @pytest.mark.asyncio
    async def test_fetch_claude_md_api_error(self):
        """Test fetching CLAUDE.md with API error."""
        from shared.exceptions import RetryExhaustedError

        token_manager = AsyncMock()
        token_manager.get_token.return_value = "test-token"

        http_client = AsyncMock()
        response = Mock()
        response.status_code = 500
        http_client.get.return_value = response

        loader = RepositoryContextLoader(token_manager, http_client)

        # Should raise RetryExhaustedError after 3 attempts
        with pytest.raises(RetryExhaustedError):
            await loader.fetch_claude_md("owner/repo")

    @pytest.mark.asyncio
    async def test_fetch_claude_md_with_rate_limiting(self):
        """Test fetching CLAUDE.md with rate limiting."""
        token_manager = AsyncMock()
        token_manager.get_token.return_value = "test-token"

        http_client = AsyncMock()
        response = Mock()
        response.status_code = 200
        response.text = "# Content"
        http_client.get.return_value = response

        rate_limiters = AsyncMock()
        rate_limiters.acquire = AsyncMock()

        loader = RepositoryContextLoader(token_manager, http_client, rate_limiters)
        content = await loader.fetch_claude_md("owner/repo")

        assert content == "# Content"
        rate_limiters.acquire.assert_called_once_with("github", timeout=30.0)
