"""Signal handling utilities for graceful shutdown."""

import asyncio
import contextlib
import logging
import signal
from collections.abc import Callable, Generator


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

    # Store old handlers
    old_sigterm = signal.signal(signal.SIGTERM, handle_shutdown)
    old_sigint = signal.signal(signal.SIGINT, handle_shutdown)

    def cleanup() -> None:
        """Remove signal handlers and restore previous handlers."""
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)

    return cleanup


@contextlib.contextmanager
def graceful_shutdown_context(
    shutdown_event: asyncio.Event,
    logger: logging.Logger | None = None,
) -> Generator[None, None, None]:
    """Context manager for graceful shutdown signal handling.

    Automatically sets up and tears down signal handlers, ensuring cleanup
    even if the process crashes or exits unexpectedly.

    Args:
        shutdown_event: Event to set when shutdown is requested
        logger: Optional logger for shutdown messages

    Example:
        >>> shutdown_event = asyncio.Event()
        >>> with graceful_shutdown_context(shutdown_event, logger):
        ...     # Service runs here
        ...     pass
        >>> # Handlers automatically restored
    """
    if logger is None:
        logger = logging.getLogger(__name__)

    def handle_shutdown(signum: int, _frame) -> None:
        """Handle shutdown signals gracefully."""
        logger.info("Received signal %s, initiating graceful shutdown...", signum)
        shutdown_event.set()

    # Store old handlers
    old_sigterm = signal.signal(signal.SIGTERM, handle_shutdown)
    old_sigint = signal.signal(signal.SIGINT, handle_shutdown)

    try:
        yield
    finally:
        # Always restore previous handlers
        signal.signal(signal.SIGTERM, old_sigterm)
        signal.signal(signal.SIGINT, old_sigint)
