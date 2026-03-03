"""Tests for GitHub token manager."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from services.agent_worker.auth.github_token_manager import GitHubTokenManager
from shared.exceptions import AuthenticationError


@pytest.fixture
def valid_private_key():
    """Valid RSA private key for testing."""
    return """-----BEGIN RSA PRIVATE KEY-----
MIIEpAIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF0K3j8N8K8z8N8K8z8N8K8z8N8K8
-----END RSA PRIVATE KEY-----"""


@pytest.fixture
def mock_http_client():
    """Mock httpx client."""
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
def token_manager(valid_private_key, mock_http_client):
    """Create token manager instance."""
    return GitHubTokenManager(
        app_id="123456",
        private_key=valid_private_key,
        installation_id="789012",
        http_client=mock_http_client,
    )


class TestGitHubTokenManager:
    """Test GitHubTokenManager class."""

    def test_init(self, token_manager):
        """Test initialization."""
        assert token_manager._app_id == "123456"
        assert token_manager._installation_id == "789012"
        assert token_manager._token is None
        assert token_manager._expires_at == 0

    def test_validate_private_key_valid(self, token_manager):
        """Test private key validation with valid key."""
        assert token_manager._validate_private_key() is True

    def test_validate_private_key_invalid_empty(self, mock_http_client):
        """Test private key validation with empty key."""
        manager = GitHubTokenManager(
            app_id="123",
            private_key="",
            installation_id="456",
            http_client=mock_http_client,
        )
        assert manager._validate_private_key() is False

    def test_validate_private_key_invalid_format(self, mock_http_client):
        """Test private key validation with invalid format."""
        manager = GitHubTokenManager(
            app_id="123",
            private_key="not a valid key",
            installation_id="456",
            http_client=mock_http_client,
        )
        assert manager._validate_private_key() is False

    def test_validate_private_key_missing_markers(self, mock_http_client):
        """Test private key validation with missing BEGIN/END markers."""
        manager = GitHubTokenManager(
            app_id="123",
            private_key="RSA PRIVATE KEY without markers",
            installation_id="456",
            http_client=mock_http_client,
        )
        assert manager._validate_private_key() is False

    def test_is_expired_no_token(self, token_manager):
        """Test expiration check with no token."""
        assert token_manager._is_expired() is True

    def test_is_expired_expired_token(self, token_manager):
        """Test expiration check with expired token."""
        token_manager._token = "test_token"
        token_manager._expires_at = time.time() - 100
        assert token_manager._is_expired() is True

    def test_is_expired_valid_token(self, token_manager):
        """Test expiration check with valid token."""
        token_manager._token = "test_token"
        token_manager._expires_at = time.time() + 300
        assert token_manager._is_expired() is False

    def test_is_expired_near_expiry(self, token_manager):
        """Test expiration check near expiry (within 60s buffer)."""
        token_manager._token = "test_token"
        token_manager._expires_at = time.time() + 30  # Within 60s buffer
        assert token_manager._is_expired() is True

    @pytest.mark.asyncio
    async def test_get_token_success(self, token_manager, mock_http_client):
        """Test getting token successfully."""
        # Mock successful API response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "ghs_test_token_123"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with patch("jwt.encode", return_value="mock_jwt_token"):
            token = await token_manager.get_token()

        assert token == "ghs_test_token_123"
        assert token_manager._token == "ghs_test_token_123"
        assert token_manager._expires_at > time.time()

    @pytest.mark.asyncio
    async def test_get_token_cached(self, token_manager):
        """Test getting cached token without refresh."""
        # Set valid cached token
        token_manager._token = "cached_token"
        token_manager._expires_at = time.time() + 300

        token = await token_manager.get_token()

        assert token == "cached_token"
        # HTTP client should not be called
        token_manager._http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_token_invalid_private_key(self, mock_http_client):
        """Test getting token with invalid private key."""
        manager = GitHubTokenManager(
            app_id="123",
            private_key="invalid",
            installation_id="456",
            http_client=mock_http_client,
        )

        with pytest.raises(AuthenticationError, match="Invalid GITHUB_PRIVATE_KEY"):
            await manager.get_token()

    @pytest.mark.asyncio
    async def test_refresh_token_api_error(self, token_manager, mock_http_client):
        """Test refresh token with API error."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with patch("jwt.encode", return_value="mock_jwt_token"):
            # Should raise RetryExhaustedError after 3 attempts
            from shared.exceptions import RetryExhaustedError

            with pytest.raises(RetryExhaustedError, match="failed after 3 attempts"):
                await token_manager.get_token()

    @pytest.mark.asyncio
    async def test_refresh_token_network_error(self, token_manager, mock_http_client):
        """Test refresh token with network error."""
        mock_http_client.post = AsyncMock(
            side_effect=httpx.RequestError("Network error")
        )

        with patch("jwt.encode", return_value="mock_jwt_token"):
            # Should retry 3 times then fail
            from shared.exceptions import RetryExhaustedError

            with pytest.raises(RetryExhaustedError, match="failed after 3 attempts"):
                await token_manager.get_token()

        # Verify retries happened (3 attempts)
        assert mock_http_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_refresh_token_timeout(self, token_manager, mock_http_client):
        """Test refresh token with timeout."""
        mock_http_client.post = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))

        with patch("jwt.encode", return_value="mock_jwt_token"):
            from shared.exceptions import RetryExhaustedError

            with pytest.raises(RetryExhaustedError, match="failed after 3 attempts"):
                await token_manager.get_token()

        # Verify retries happened
        assert mock_http_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_refresh_token_jwt_generation(self, token_manager, mock_http_client):
        """Test JWT generation during token refresh."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "ghs_token"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with patch("jwt.encode") as mock_jwt_encode:
            mock_jwt_encode.return_value = "mock_jwt"
            await token_manager.get_token()

            # Verify JWT was generated with correct parameters
            mock_jwt_encode.assert_called_once()
            call_args = mock_jwt_encode.call_args
            payload = call_args[0][0]
            assert payload["iss"] == "123456"
            assert "iat" in payload
            assert "exp" in payload
            assert call_args[1]["algorithm"] == "RS256"

    @pytest.mark.asyncio
    async def test_concurrent_token_requests(self, token_manager, mock_http_client):
        """Test concurrent token requests use lock properly."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "ghs_token"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with patch("jwt.encode", return_value="mock_jwt"):
            # Make multiple concurrent requests
            import asyncio

            results = await asyncio.gather(
                token_manager.get_token(),
                token_manager.get_token(),
                token_manager.get_token(),
            )

        # All should get the same token
        assert all(r == "ghs_token" for r in results)
        # API should only be called once due to locking
        assert mock_http_client.post.call_count == 1

    @pytest.mark.asyncio
    async def test_token_refresh_updates_expiry(self, token_manager, mock_http_client):
        """Test that token refresh updates expiry time."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "ghs_new_token"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        before_time = time.time()

        with patch("jwt.encode", return_value="mock_jwt"):
            await token_manager.get_token()

        # Expiry should be set to ~9 minutes from now
        assert token_manager._expires_at > before_time + 500
        assert token_manager._expires_at < before_time + 600
