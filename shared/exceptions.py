"""Custom exceptions for the GitHub Agent system."""


class AgentError(Exception):
    """Base exception for all agent errors."""


class ConfigurationError(AgentError):
    """Raised when configuration is invalid or missing."""


class AuthenticationError(AgentError):
    """Raised when authentication fails."""


class GitHubAPIError(AgentError):
    """Raised when GitHub API calls fail."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class TokenRefreshError(AuthenticationError):
    """Raised when token refresh fails."""


class SDKError(AgentError):
    """Raised when Claude SDK operations fail."""


class QueueError(AgentError):
    """Raised when queue operations fail."""


class RetryExhaustedError(AgentError):
    """Raised when all retry attempts are exhausted."""


class RateLimitError(AgentError):
    """Raised when rate limit is exceeded."""


class WebhookValidationError(AgentError):
    """Raised when webhook validation fails."""


class CommandExecutionError(AgentError):
    """Raised when command execution fails."""


class SDKTimeoutError(SDKError):
    """Raised when Claude SDK execution times out."""


class SDKInitializationError(SDKError):
    """Raised when Claude SDK initialization fails."""


class RepositorySyncError(AgentError):
    """Raised when repository sync operations fail."""


class WorktreeCreationError(AgentError):
    """Raised when git worktree creation fails."""


class IndexingTimeoutError(AgentError):
    """Raised when indexing wait times out."""
