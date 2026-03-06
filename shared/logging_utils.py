"""Shared logging utilities for consistent logging configuration across services."""

import logging


def setup_logging(level: str | int = "INFO", silence_noisy: bool = False) -> None:
    """Setup logging with consistent format across all services.

    Args:
        level: Log level (string like "INFO" or int like logging.INFO)
        silence_noisy: Whether to silence noisy HTTP/network loggers
    """
    # Convert string level to int if needed
    if isinstance(level, str):
        numeric_level = getattr(logging, level.upper(), logging.INFO)
    else:
        numeric_level = level

    # Configure basic logging
    # Note: basicConfig only works on first call, so we also set root logger directly
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,  # Force reconfiguration (Python 3.8+)
    )

    # Silence noisy loggers if requested
    if silence_noisy:
        logging.getLogger("httpcore").setLevel(logging.INFO)
        logging.getLogger("httpx").setLevel(logging.INFO)
        logging.getLogger("urllib3").setLevel(logging.INFO)
