"""Shared utilities for the GitHub Agent system.

This package uses lazy imports to avoid forcing heavy dependencies
(httpx, pyjwt, etc.) on lightweight services that only need a subset
of utilities. Import what you need directly:

    from shared.logging_utils import setup_logging        # no heavy deps
    from shared.streaming_session import StreamingSessionStore  # redis only

Using `from shared import X` still works — it just defers the import
until first access, so missing optional deps only fail if you actually
use the module that needs them.
"""

import importlib as _importlib

# Lazy-imported names — {name: module_path} where getattr(module, name) gives the value.
_LAZY_NAMES = {
    # Config
    "AnthropicConfig": ".config",
    "GitHubConfig": ".config",
    "LangfuseConfig": ".config",
    "QueueConfig": ".config",
    "WebhookConfig": ".config",
    "WorkerConfig": ".config",
    "get_webhook_config": ".config",
    "get_worker_config": ".config",
    # Exceptions
    "AgentError": ".exceptions",
    "AuthenticationError": ".exceptions",
    "CommandExecutionError": ".exceptions",
    "ConfigurationError": ".exceptions",
    "GitHubAPIError": ".exceptions",
    "IndexingTimeoutError": ".exceptions",
    "QueueError": ".exceptions",
    "RateLimitError": ".exceptions",
    "RepositorySyncError": ".exceptions",
    "RetryExhaustedError": ".exceptions",
    "SDKError": ".exceptions",
    "SDKInitializationError": ".exceptions",
    "SDKTimeoutError": ".exceptions",
    "TokenRefreshError": ".exceptions",
    "WebhookValidationError": ".exceptions",
    "WorktreeCreationError": ".exceptions",
    # File tree
    "EXCLUDE_DIRS": ".file_tree",
    "EXCLUDE_FILES": ".file_tree",
    "EXCLUDE_SUFFIXES": ".file_tree",
    "load_ignore_spec": ".file_tree",
    # Git utils
    "execute_git_command": ".git_utils",
    # GitHub auth
    "GitHubAuthService": ".github_auth",
    "get_github_auth_service": ".github_auth",
    "close_github_auth_service": ".github_auth",
    # Health
    "HealthChecker": ".health",
    "HealthStatus": ".health",
    # HTTP client
    "AsyncHTTPClient": ".http_client",
    "get_http_client": ".http_client",
    "close_http_client": ".http_client",
    # Job queue
    "JobQueue": ".job_queue",
    # Logging
    "setup_logging": ".logging_utils",
    # Models
    "AgentRequest": ".models",
    "AgentResponse": ".models",
    # Queue
    "MessageQueue": ".queue",
    "RedisQueue": ".queue",
    "PubSubQueue": ".queue",
    "get_queue": ".queue",
    "wait_for_indexing": ".queue",
    "wait_for_repo_sync": ".queue",
    # Rate limiting
    "RateLimiter": ".rate_limiter",
    "MultiRateLimiter": ".rate_limiter",
    # Session store
    "SessionStore": ".session_store",
    "resolve_thread_type": ".session_store",
    # Retry
    "async_retry": ".retry",
    # Signals
    "setup_graceful_shutdown": ".signals",
    # Utils
    "_MISSING": ".utils",
    "resolve_path": ".utils",
}

# Lazy-imported submodules — `from shared import dlq` returns the module object.
_LAZY_SUBMODULES = frozenset({"dlq"})


def __getattr__(name: str):
    if name in _LAZY_NAMES:
        module = _importlib.import_module(_LAZY_NAMES[name], __name__)
        value = getattr(module, name)
        globals()[name] = value
        return value
    if name in _LAZY_SUBMODULES:
        module = _importlib.import_module(f".{name}", __name__)
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = sorted(_LAZY_NAMES.keys()) + sorted(_LAZY_SUBMODULES)
