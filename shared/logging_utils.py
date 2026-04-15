"""Shared logging utilities for consistent logging configuration across services."""

import logging

# Third-party loggers that are noisy at DEBUG/INFO and should be quieted.
_NOISY_LOGGERS = (
    "httpcore",
    "httpx",
    "urllib3",
    "google_genai",
    "google.auth",
    "qdrant_client",
    "redis",
    "asyncio",
    "filelock",
)


def setup_logging(level: str | int = "INFO", silence_noisy: bool = True) -> None:
    """Setup logging with consistent format across all services.

    Args:
        level: Log level (string like "INFO" or int like logging.INFO)
        silence_noisy: Whether to silence noisy third-party loggers (default True).
    """
    # Convert string level to int if needed
    if isinstance(level, str):
        numeric_level = getattr(logging, level.upper(), logging.INFO)
    else:
        numeric_level = level

    # Configure basic logging
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,
    )

    # Silence noisy third-party loggers unless explicitly disabled
    if silence_noisy:
        for name in _NOISY_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)
