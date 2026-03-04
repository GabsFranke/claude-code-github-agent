"""Shared GitHub authentication service."""

import asyncio
import logging
import os
import time

import httpx
import jwt

from .exceptions import AuthenticationError, GitHubAPIError
from .retry import async_retry

logger = logging.getLogger(__name__)


class GitHubAuthService:
    """Shared GitHub App authentication service.

    This service can be used by any component that needs GitHub authentication.
    It manages token lifecycle and caching automatically.
    """

    def __init__(
        self,
        app_id: str | None = None,
        private_key: str | None = None,
        installation_id: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ):
        """Initialize GitHub auth service.

        Args:
            app_id: GitHub App ID (defaults to GITHUB_APP_ID env var)
            private_key: GitHub App private key (defaults to GITHUB_PRIVATE_KEY env var)
            installation_id: Installation ID (defaults to GITHUB_INSTALLATION_ID env var)
            http_client: Optional HTTP client (creates one if not provided)
        """
        self._app_id = app_id or os.getenv("GITHUB_APP_ID", "")
        self._private_key = private_key or os.getenv("GITHUB_PRIVATE_KEY", "")
        self._installation_id = installation_id or os.getenv(
            "GITHUB_INSTALLATION_ID", ""
        )
        self._http_client = http_client
        self._owns_client = http_client is None

        # Thread-safe state
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0
        self._cache_duration = 540  # 9 minutes

    async def __aenter__(self):
        """Async context manager entry."""
        if self._owns_client:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self._owns_client and self._http_client:
            await self._http_client.aclose()

    def _validate_private_key(self) -> bool:
        """Validate PEM format."""
        if not self._private_key:
            return False
        if not ("-----BEGIN" in self._private_key and "-----END" in self._private_key):
            return False
        valid_markers = ["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY"]
        return any(marker in self._private_key for marker in valid_markers)

    def _is_expired(self) -> bool:
        """Check if token is expired (must hold lock)."""
        return not self._token or time.time() >= self._expires_at - 60

    def is_configured(self) -> bool:
        """Check if GitHub App credentials are configured."""
        return bool(
            self._app_id
            and self._private_key
            and self._installation_id
            and self._validate_private_key()
        )

    async def get_token(self) -> str:
        """Get valid token, refreshing if needed (thread-safe).

        Returns:
            GitHub installation access token

        Raises:
            AuthenticationError: If credentials are invalid or token cannot be obtained
        """
        if not self.is_configured():
            raise AuthenticationError(
                "GitHub App credentials not configured. "
                "Set GITHUB_APP_ID, GITHUB_INSTALLATION_ID, and GITHUB_PRIVATE_KEY."
            )

        async with self._lock:
            if self._is_expired():
                await self._refresh_token()
            if not self._token:
                raise AuthenticationError("Failed to get GitHub token")
            return self._token

    @async_retry(
        max_attempts=3,
        base_delay=1.0,
        exceptions=(GitHubAPIError, httpx.RequestError, httpx.TimeoutException),
    )
    async def _refresh_token(self):
        """Refresh token (must hold lock)."""
        if not self._validate_private_key():
            raise AuthenticationError("Invalid GITHUB_PRIVATE_KEY format")

        # Generate JWT
        now = int(time.time())
        payload = {"iat": now, "exp": now + 600, "iss": self._app_id}
        # Type ignore for jwt.encode - it accepts str for private_key
        jwt_token = jwt.encode(payload, self._private_key, algorithm="RS256")  # type: ignore[arg-type]

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

        if not self._http_client:
            raise AuthenticationError("HTTP client not initialized")

        try:
            response = await self._http_client.post(
                f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
                headers=headers,
                timeout=10.0,
            )

            if response.status_code != 201:
                raise GitHubAPIError(
                    f"Failed to get GitHub token: {response.status_code}",
                    status_code=response.status_code,
                )

            token = response.json()["token"]
            self._token = token
            self._expires_at = time.time() + self._cache_duration
            logger.info("GitHub token refreshed successfully")

        except httpx.RequestError as e:
            raise GitHubAPIError(f"GitHub API request failed: {e}") from e


# Global singleton instance for convenience
_global_auth_service: GitHubAuthService | None = None


async def get_github_auth_service() -> GitHubAuthService:
    """Get or create global GitHub auth service instance.

    Returns:
        Configured GitHubAuthService instance

    Note:
        This creates a singleton instance that persists for the lifetime of the process.
        The HTTP client is managed internally.
    """
    global _global_auth_service  # pylint: disable=global-statement

    if _global_auth_service is None:
        _global_auth_service = GitHubAuthService()
        # Initialize the client
        await _global_auth_service.__aenter__()

    return _global_auth_service
