"""Signal handling utilities for graceful shutdown."""

import asyncio
import logging
import signal
from collections.abc import Callable


def setup_graceful_shutdown(
    shutdown_event: asyncio.Event,
    logger: logging.Logger | None = None,
) -> Callable[[], None]:
    """Setup signal handlers for graceful shutdown.

    Registers handlers for SIGTERM and SIGINT that set the provided shutdown event,
    allowing services to gracefully terminate their operations.

    Args:
        shutdown_event: Event to set when shutdown is requested
        logger: Optional logger for shutdown messages (defaults to module logger)

    Returns:
        Cleanup function to remove signal handlers and restore defaults

    Example:
        >>> shutdown_event = asyncio.Event()
        >>> cleanup = setup_graceful_shutdown(shutdown_event, logger)
        >>> # ... service runs ...
        >>> cleanup()  # Optional: restore default signal handlers
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    def handle_shutdown(signum: int, _frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.info("Received signal %s, initiating graceful shutdown...", signum)
        shutdown_event.set()

    # Register handlers for graceful shutdown
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    def cleanup() -> None:
        """Remove signal handlers and restore defaults."""
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

    return cleanup
