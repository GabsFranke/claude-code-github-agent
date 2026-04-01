"""Repository context loader for fetching repository-specific configuration."""

import logging
from typing import TYPE_CHECKING, Optional

import httpx

if TYPE_CHECKING:
    from shared import GitHubAuthService, MultiRateLimiter

logger = logging.getLogger(__name__)


class RepositoryContextLoader:
    """Handles fetching repository-specific context like CLAUDE.md."""

    def __init__(
        self,
        token_manager: "GitHubAuthService",
        http_client: httpx.AsyncClient,
        rate_limiters: Optional["MultiRateLimiter"] = None,
    ):
        self.token_manager = token_manager
        self.http_client = http_client
        self.rate_limiters = rate_limiters

    async def fetch_claude_md(self, repo: str) -> str:
        """
        Fetch CLAUDE.md from repository if it exists.

        Returns:
            Content of CLAUDE.md if found, empty string if file doesn't exist.

        Raises:
            GitHubAPIError: If API call fails (not 404)
            ConnectionError: If network connection fails
            TimeoutError: If request times out
        """
        from shared.exceptions import GitHubAPIError
        from shared.retry import async_retry

        @async_retry(
            max_attempts=3,
            base_delay=1.0,
            exceptions=(GitHubAPIError, ConnectionError, TimeoutError),
        )
        async def _fetch_with_retry() -> tuple[str, int]:
            # Apply rate limiting for GitHub API
            if self.rate_limiters:
                await self.rate_limiters.acquire("github", timeout=30.0)

            token = await self.token_manager.get_token()
            url = f"https://api.github.com/repos/{repo}/contents/CLAUDE.md"
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3.raw",
            }

            response = await self.http_client.get(url, headers=headers, timeout=10.0)

            if response.status_code == 200:
                logger.info(f"Successfully fetched CLAUDE.md from {repo}")
                return str(response.text), 200

            if response.status_code == 404:
                logger.debug(
                    f"CLAUDE.md not found in {repo} (expected if not configured)"
                )
                return "", 404

            raise GitHubAPIError(
                f"Failed to fetch CLAUDE.md from {repo}: HTTP {response.status_code}",
                status_code=response.status_code,
            )

        try:
            result, _ = await _fetch_with_retry()  # status_code not needed after fetch
            return str(result)
        except GitHubAPIError as e:
            # API errors (rate limits, auth issues, server errors) should be visible
            logger.error(
                f"GitHub API error fetching CLAUDE.md from {repo}: {e}. "
                "Repository context will be missing, which may affect agent behavior."
            )
            raise
        except (ConnectionError, TimeoutError) as e:
            # Network errors should also be visible
            logger.error(
                f"Network error fetching CLAUDE.md from {repo}: {e}. "
                "Repository context will be missing, which may affect agent behavior."
            )
            raise

    async def fetch_memory_index(self, repo: str) -> str:
        """
        Fetch index.md from the local agent-memory volume if it exists.

        Returns:
            Content of index.md if found, empty string if file doesn't exist.
        """
        import os

        memory_path = f"/home/bot/agent-memory/{repo}/memory/index.md"
        if not os.path.exists(memory_path):
            return ""

        try:
            with open(memory_path, encoding="utf-8") as f:
                content = f.read()
            logger.info(f"Successfully loaded index.md for {repo}")
            return content
        except Exception as e:
            logger.error(f"Failed to read index.md for {repo}: {e}")
            return ""
