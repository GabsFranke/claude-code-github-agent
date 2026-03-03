"""Thread-safe GitHub App token manager."""

import asyncio
import logging
import time

import httpx
import jwt

from shared.exceptions import AuthenticationError, GitHubAPIError
from shared.retry import async_retry

logger = logging.getLogger(__name__)


class GitHubTokenManager:
    """Thread-safe GitHub App token manager with caching."""

    def __init__(
        self,
        app_id: str,
        private_key: str,
        installation_id: str,
        http_client: httpx.AsyncClient,
    ):
        self._app_id = app_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._http_client = http_client

        # Thread-safe state
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0
        self._cache_duration = 540  # 9 minutes

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

    async def get_token(self) -> str:
        """Get valid token, refreshing if needed (thread-safe)."""
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
        jwt_token = jwt.encode(payload, self._private_key, algorithm="RS256")

        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Accept": "application/vnd.github.v3+json",
        }

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
            logger.info("GitHub token refreshed")

        except httpx.RequestError as e:
            raise GitHubAPIError(f"GitHub API request failed: {e}") from e
