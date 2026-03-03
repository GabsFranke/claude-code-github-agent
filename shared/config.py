"""Configuration management using Pydantic Settings."""

import os
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class BaseConfig(BaseSettings):
    """Base configuration with common settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


class RedisConfig(BaseConfig):
    """Redis configuration."""

    redis_url: str = Field(default="redis://localhost:6379", description="Redis URL")
    redis_password: str | None = Field(default=None, description="Redis password")
    queue_name: str = Field(default="agent-requests", description="Redis queue name")


class GitHubConfig(BaseConfig):
    """GitHub App configuration."""

    github_app_id: str = Field(default="", description="GitHub App ID")
    github_installation_id: str = Field(
        default="", description="GitHub Installation ID"
    )
    github_private_key: str = Field(
        default="", description="GitHub App private key (PEM)"
    )
    github_webhook_secret: str = Field(default="", description="GitHub webhook secret")

    @field_validator("github_private_key")
    @classmethod
    def validate_private_key(cls, v: str) -> str:
        """Validate PEM format."""
        if not v:
            raise ValueError("GitHub private key cannot be empty")
        if not ("-----BEGIN" in v and "-----END" in v):
            raise ValueError("GitHub private key must be in PEM format")
        valid_markers = ["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY"]
        if not any(marker in v for marker in valid_markers):
            raise ValueError(f"GitHub private key must contain one of: {valid_markers}")
        return v


class AnthropicConfig(BaseConfig):
    """Anthropic API configuration."""

    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    anthropic_auth_token: str | None = Field(
        default=None, description="Anthropic auth token (alternative to API key)"
    )
    anthropic_base_url: str | None = Field(
        default=None, description="Custom Anthropic API base URL"
    )
    anthropic_default_sonnet_model: str | None = Field(
        default=None, description="Default Sonnet model"
    )
    anthropic_default_haiku_model: str | None = Field(
        default=None, description="Default Haiku model"
    )
    anthropic_default_opus_model: str | None = Field(
        default=None, description="Default Opus model"
    )

    # Vertex AI
    anthropic_vertex_project_id: str | None = Field(
        default=None, description="Google Cloud project ID for Vertex AI"
    )
    anthropic_vertex_region: str | None = Field(
        default=None, description="Google Cloud region for Vertex AI"
    )

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def get_api_key(cls, v: str | None) -> str | None:
        """Get API key from either field or auth token."""
        if v:
            return v
        # Fallback to auth token
        return os.getenv("ANTHROPIC_AUTH_TOKEN")

    def get_api_key_or_raise(self) -> str:
        """Get API key or raise error."""
        key = self.anthropic_api_key or self.anthropic_auth_token
        if not key:
            raise ValueError(
                "Either ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN must be set"
            )
        return key


class LangfuseConfig(BaseConfig):
    """Langfuse observability configuration."""

    langfuse_public_key: str | None = Field(
        default=None, description="Langfuse public key"
    )
    langfuse_secret_key: str | None = Field(
        default=None, description="Langfuse secret key"
    )
    langfuse_host: str = Field(
        default="https://cloud.langfuse.com", description="Langfuse host URL"
    )

    @property
    def is_enabled(self) -> bool:
        """Check if Langfuse is enabled."""
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


class QueueConfig(BaseConfig):
    """Message queue configuration."""

    queue_type: Literal["redis", "pubsub"] = Field(
        default="redis", description="Queue type"
    )

    # Redis
    redis_url: str = Field(default="redis://localhost:6379", description="Redis URL")
    redis_password: str | None = Field(default=None, description="Redis password")

    # Google Pub/Sub
    gcp_project_id: str | None = Field(
        default=None, description="Google Cloud project ID"
    )
    pubsub_topic_name: str = Field(
        default="agent-requests", description="Pub/Sub topic name"
    )
    pubsub_subscription_name: str = Field(
        default="agent-requests-sub", description="Pub/Sub subscription name"
    )


class WebhookConfig(BaseConfig):
    """Webhook service configuration."""

    port: int = Field(default=8080, description="Webhook service port", ge=1, le=65535)
    log_level: str = Field(default="INFO", description="Logging level")

    _github_config: GitHubConfig | None = None
    _queue_config: QueueConfig | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    @property
    def github(self) -> GitHubConfig:
        """Get GitHub config (lazy-loaded)."""
        if self._github_config is None:
            self._github_config = GitHubConfig()
        return self._github_config

    @property
    def queue(self) -> QueueConfig:
        """Get queue config (lazy-loaded)."""
        if self._queue_config is None:
            self._queue_config = QueueConfig()
        return self._queue_config


class WorkerConfig(BaseConfig):
    """Worker service configuration."""

    log_level: str = Field(default="INFO", description="Logging level")
    max_turns: int = Field(
        default=50, description="Maximum turns for Claude SDK", ge=1, le=200
    )
    sdk_timeout: int = Field(
        default=1800, description="SDK execution timeout in seconds", ge=60
    )

    # Health check
    health_check_interval: int = Field(
        default=30, description="Health check update interval in seconds", ge=5
    )
    health_check_file: str = Field(
        default="/tmp/worker_health",  # nosec B108
        description="Health check file path",
    )
    health_check_max_idle: int = Field(
        default=1800,
        description="Maximum idle time before unhealthy (seconds)",
        ge=60,
    )

    # Rate limiting
    github_rate_limit: int = Field(
        default=5000, description="GitHub API rate limit (requests per hour)", ge=1
    )
    anthropic_rate_limit: int = Field(
        default=100, description="Anthropic API rate limit (requests per minute)", ge=1
    )

    _github_config: GitHubConfig | None = None
    _anthropic_config: AnthropicConfig | None = None
    _langfuse_config: LangfuseConfig | None = None
    _queue_config: QueueConfig | None = None

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(f"Log level must be one of: {valid_levels}")
        return v_upper

    @property
    def github(self) -> GitHubConfig:
        """Get GitHub config (lazy-loaded)."""
        if self._github_config is None:
            self._github_config = GitHubConfig()
        return self._github_config

    @property
    def anthropic(self) -> AnthropicConfig:
        """Get Anthropic config (lazy-loaded)."""
        if self._anthropic_config is None:
            self._anthropic_config = AnthropicConfig()
        return self._anthropic_config

    @property
    def langfuse(self) -> LangfuseConfig:
        """Get Langfuse config (lazy-loaded)."""
        if self._langfuse_config is None:
            self._langfuse_config = LangfuseConfig()
        return self._langfuse_config

    @property
    def queue(self) -> QueueConfig:
        """Get queue config (lazy-loaded)."""
        if self._queue_config is None:
            self._queue_config = QueueConfig()
        return self._queue_config


# Singleton instances
_webhook_config: WebhookConfig | None = None
_worker_config: WorkerConfig | None = None


def get_webhook_config() -> WebhookConfig:
    """Get webhook configuration singleton."""
    global _webhook_config
    if _webhook_config is None:
        _webhook_config = WebhookConfig()
    return _webhook_config


def get_worker_config() -> WorkerConfig:
    """Get worker configuration singleton."""
    global _worker_config
    if _worker_config is None:
        _worker_config = WorkerConfig()
    return _worker_config
