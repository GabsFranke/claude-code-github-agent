"""Tests for shared GitHub authentication service."""

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from shared.exceptions import AuthenticationError
from shared.github_auth import GitHubAuthService


@pytest.fixture(scope="session")
def valid_private_key():
    """A throwaway RSA private key, generated per test session.

    Generated rather than hardcoded: a literal PEM in the repository trips
    secret scanners and is indistinguishable from a real leaked key to anyone
    reading the diff, even when it is only a test fixture.
    """
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def mock_http_client():
    """Mock HTTP client."""
    return AsyncMock(spec=httpx.AsyncClient)


@pytest.fixture
def auth_service(valid_private_key, mock_http_client):
    """Create auth service instance."""
    return GitHubAuthService(
        app_id="123456",
        private_key=valid_private_key,
        installation_id="789012",
        http_client=mock_http_client,
    )


class TestGitHubAuthService:
    """Test GitHubAuthService class."""

    def test_init(self, auth_service):
        """Test initialization."""
        assert auth_service._app_id == "123456"
        assert auth_service._installation_id == "789012"
        assert auth_service._token is None
        assert auth_service._expires_at == 0

    def test_is_configured_valid(self, auth_service):
        """Test is_configured with valid credentials."""
        assert auth_service.is_configured() is True

    def test_is_configured_missing_credentials(self, mock_http_client):
        """Test is_configured with missing credentials."""
        with patch.dict(os.environ, {}, clear=True):
            service = GitHubAuthService(
                app_id="",
                private_key="",
                installation_id="",
                http_client=mock_http_client,
            )
            assert service.is_configured() is False

    def test_is_configured_partial_credentials(self, mock_http_client):
        """Test is_configured with partial credentials."""
        with patch.dict(os.environ, {}, clear=True):
            service = GitHubAuthService(
                app_id="123",
                private_key="",
                installation_id="456",
                http_client=mock_http_client,
            )
            # Missing private key
            assert service.is_configured() is False

    def test_validate_private_key_invalid_empty(self, mock_http_client):
        """Test private key validation with empty key."""
        with patch.dict(os.environ, {}, clear=True):
            service = GitHubAuthService(
                app_id="123",
                private_key="",
                installation_id="456",
                http_client=mock_http_client,
            )
            assert service._validate_private_key() is False

    def test_validate_private_key_invalid_format(self, mock_http_client):
        """Test private key validation with invalid format."""
        with patch.dict(os.environ, {}, clear=True):
            service = GitHubAuthService(
                app_id="123",
                private_key="not a valid key",
                installation_id="456",
                http_client=mock_http_client,
            )
            assert service._validate_private_key() is False

    def test_validate_private_key_missing_markers(self, mock_http_client):
        """Test private key validation with missing BEGIN/END markers."""
        with patch.dict(os.environ, {}, clear=True):
            service = GitHubAuthService(
                app_id="123",
                private_key="RSA PRIVATE KEY without markers",
                installation_id="456",
                http_client=mock_http_client,
            )
            assert service._validate_private_key() is False

    @pytest.mark.asyncio
    async def test_get_token_success(self, auth_service, mock_http_client):
        """Test successful token retrieval."""
        # Mock successful API response
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "ghs_test_token"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        token = await auth_service.get_token()

        assert token == "ghs_test_token"
        assert auth_service._token == "ghs_test_token"
        assert auth_service._expires_at > time.time()
        mock_http_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_token_cached(self, auth_service):
        """Test token caching."""
        # Set a valid cached token
        auth_service._token = "cached_token"
        auth_service._expires_at = time.time() + 300  # 5 minutes from now

        token = await auth_service.get_token()

        assert token == "cached_token"
        # HTTP client should not be called for cached token
        auth_service._http_client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_token_refresh_expired(self, auth_service, mock_http_client):
        """Test token refresh when expired."""
        # Set an expired token
        auth_service._token = "old_token"
        auth_service._expires_at = time.time() - 100  # Expired

        # Mock successful refresh
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"token": "new_token"}
        mock_http_client.post = AsyncMock(return_value=mock_response)

        token = await auth_service.get_token()

        assert token == "new_token"
        assert auth_service._token == "new_token"
        mock_http_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_token_invalid_private_key(self, mock_http_client):
        """Test getting token with invalid private key."""
        service = GitHubAuthService(
            app_id="123",
            private_key="invalid",
            installation_id="456",
            http_client=mock_http_client,
        )

        with pytest.raises(AuthenticationError, match="GitHub App credentials"):
            await service.get_token()

    @pytest.mark.asyncio
    async def test_get_token_api_error(self, auth_service, mock_http_client):
        """Test handling of API errors."""
        # Mock API error response
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_http_client.post = AsyncMock(return_value=mock_response)

        with pytest.raises(Exception):  # noqa: B017 - Will raise GitHubAPIError
            await auth_service.get_token()

    @pytest.mark.asyncio
    async def test_context_manager(self, valid_private_key):
        """Test async context manager."""
        async with GitHubAuthService(
            app_id="123",
            private_key=valid_private_key,
            installation_id="456",
        ) as service:
            assert service._http_client is not None
            assert service._owns_client is True

    def test_jwt_generation(self, auth_service):
        """Test JWT token generation."""
        # This is tested indirectly through _refresh_token
        # Just verify the private key is valid for JWT
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": auth_service._app_id}

        # Should not raise an exception
        jwt_token = jwt.encode(payload, auth_service._private_key, algorithm="RS256")
        assert jwt_token is not None
