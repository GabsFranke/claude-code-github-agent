"""Tests for signal handling utilities."""

import asyncio
import logging
import signal
from unittest.mock import MagicMock, patch

import pytest

from shared.signals import setup_graceful_shutdown


@pytest.fixture
def shutdown_event():
    """Create a fresh shutdown event for each test."""
    return asyncio.Event()


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    return MagicMock(spec=logging.Logger)


def test_setup_graceful_shutdown_registers_handlers(shutdown_event, mock_logger):
    """Test that signal handlers are registered correctly."""
    with patch("signal.signal") as mock_signal:
        cleanup = setup_graceful_shutdown(shutdown_event, mock_logger)

        # Verify SIGTERM and SIGINT handlers were registered
        assert mock_signal.call_count == 2
        calls = mock_signal.call_args_list
        assert calls[0][0][0] == signal.SIGTERM
        assert calls[1][0][0] == signal.SIGINT

        # Verify cleanup function is returned
        assert callable(cleanup)


def test_setup_graceful_shutdown_uses_default_logger(shutdown_event):
    """Test that default logger is used when none provided."""
    with patch("signal.signal"):
        cleanup = setup_graceful_shutdown(shutdown_event)
        assert callable(cleanup)


def test_signal_handler_sets_shutdown_event(shutdown_event, mock_logger):
    """Test that signal handler sets the shutdown event."""
    with patch("signal.signal") as mock_signal:
        setup_graceful_shutdown(shutdown_event, mock_logger)

        # Get the registered handler function
        handler = mock_signal.call_args_list[0][0][1]

        # Verify event is not set initially
        assert not shutdown_event.is_set()

        # Call the handler
        handler(signal.SIGTERM, None)

        # Verify event is now set
        assert shutdown_event.is_set()

        # Verify logger was called
        mock_logger.info.assert_called_once()
        assert "signal" in mock_logger.info.call_args[0][0].lower()


def test_signal_handler_logs_signal_number(shutdown_event, mock_logger):
    """Test that signal handler logs the signal number."""
    with patch("signal.signal") as mock_signal:
        setup_graceful_shutdown(shutdown_event, mock_logger)

        # Get the registered handler
        handler = mock_signal.call_args_list[0][0][1]

        # Call with SIGTERM
        handler(signal.SIGTERM, None)

        # Verify signal number is in log message
        log_call = mock_logger.info.call_args[0]
        assert str(signal.SIGTERM) in str(log_call) or "15" in str(log_call)


def test_cleanup_restores_default_handlers(shutdown_event, mock_logger):
    """Test that cleanup function restores default signal handlers."""
    with patch("signal.signal") as mock_signal:
        # Mock signal.signal to return the old handler (simulating real behavior)
        old_sigterm = signal.SIG_DFL
        old_sigint = signal.SIG_DFL
        mock_signal.side_effect = [old_sigterm, old_sigint, None, None]

        cleanup = setup_graceful_shutdown(shutdown_event, mock_logger)

        # Reset mock to track cleanup calls
        mock_signal.reset_mock()
        mock_signal.side_effect = None

        # Call cleanup
        cleanup()

        # Verify handlers were restored (the cleanup function restores the old handlers)
        assert mock_signal.call_count == 2
        calls = mock_signal.call_args_list
        assert calls[0][0][0] == signal.SIGTERM
        assert calls[1][0][0] == signal.SIGINT
        # The second argument should be the old handler (SIG_DFL in this case)
        assert calls[0][0][1] == old_sigterm
        assert calls[1][0][1] == old_sigint


def test_multiple_signal_types_set_same_event(shutdown_event, mock_logger):
    """Test that both SIGTERM and SIGINT set the same shutdown event."""
    with patch("signal.signal") as mock_signal:
        setup_graceful_shutdown(shutdown_event, mock_logger)

        # Get both handlers
        sigterm_handler = mock_signal.call_args_list[0][0][1]
        sigint_handler = mock_signal.call_args_list[1][0][1]

        # Verify event is not set
        assert not shutdown_event.is_set()

        # Call SIGTERM handler
        sigterm_handler(signal.SIGTERM, None)
        assert shutdown_event.is_set()

        # Reset event
        shutdown_event.clear()
        assert not shutdown_event.is_set()

        # Call SIGINT handler
        sigint_handler(signal.SIGINT, None)
        assert shutdown_event.is_set()
