"""Configuration management for the scheduler service."""

from pydantic import Field, field_validator

from shared.config import BaseConfig, GitHubConfig, QueueConfig


class SchedulerConfig(BaseConfig):
    """Scheduler service configuration."""

    port: int = Field(
        default=8082, description="Scheduler service port", ge=1, le=65535
    )
    log_level: str = Field(default="INFO", description="Logging level")
    health_check_file: str = Field(
        default="/tmp/scheduler_health",  # nosec B108
        description="Health check file path",
    )
    scheduler_internal_token: str = Field(
        default="",
        description=(
            "Shared secret required on POST /schedule/one-shot. The endpoint "
            "is reachable by every container on the compose network, including "
            "sandbox agents driven by attacker-authored issue and PR text, so "
            "an empty value disables one-shot scheduling rather than leaving "
            "it open."
        ),
    )

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


_scheduler_config: SchedulerConfig | None = None


def get_scheduler_config() -> SchedulerConfig:
    """Get scheduler configuration singleton."""
    global _scheduler_config
    if _scheduler_config is None:
        _scheduler_config = SchedulerConfig()
    return _scheduler_config
