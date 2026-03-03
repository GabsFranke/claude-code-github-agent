"""Unit tests for custom exceptions module."""

import pytest

from shared.exceptions import (
    AgentError,
    ConfigurationError,
    GitHubAPIError,
    QueueError,
    RateLimitError,
)


class TestCustomExceptions:
    """Test custom exception classes."""

    def test_agent_error(self):
        """Test AgentError exception."""
        with pytest.raises(AgentError, match="Agent failed"):
            raise AgentError("Agent failed")

    def test_configuration_error(self):
        """Test ConfigurationError exception."""
        with pytest.raises(ConfigurationError, match="Invalid config"):
            raise ConfigurationError("Invalid config")

    def test_github_api_error(self):
        """Test GitHubAPIError exception."""
        error = GitHubAPIError("API call failed", status_code=404)
        assert error.status_code == 404
        assert "API call failed" in str(error)

    def test_queue_error(self):
        """Test QueueError exception."""
        with pytest.raises(QueueError, match="Queue connection lost"):
            raise QueueError("Queue connection lost")

    def test_rate_limit_error(self):
        """Test RateLimitError exception."""
        error = RateLimitError("Rate limit exceeded")
        assert "Rate limit exceeded" in str(error)
