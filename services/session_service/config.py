"""Session Service configuration via pydantic-settings."""

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class SessionServiceConfig(BaseSettings):
    """Configuration for the dedicated session service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    redis_url: str = Field(
        default="redis://redis:6379",
        description="Redis connection URL",
    )
    redis_password: str | None = Field(
        default=None,
        description="Redis password (None = no auth)",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level",
    )
    port: int = Field(
        default=8000,
        description="HTTP server port",
        ge=1,
        le=65535,
    )
    allowed_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        description="Comma-separated CORS allowed origins",
    )

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid_levels:
            raise ValueError(
                f"Invalid log level: {v}. Must be one of {sorted(valid_levels)}"
            )
        return upper

    @property
    def origins_list(self) -> list[str]:
        """Parse comma-separated origins into a list."""
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]
