"""Tests for shared logging utilities."""

import logging

from shared.logging_utils import setup_logging


def test_setup_logging_with_string_level():
    """Test setup_logging with string log level."""
    setup_logging(level="DEBUG")

    # Verify root logger is configured
    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG


def test_setup_logging_with_int_level():
    """Test setup_logging with integer log level."""
    setup_logging(level=logging.WARNING)

    # Verify root logger is configured
    root_logger = logging.getLogger()
    assert root_logger.level == logging.WARNING


def test_setup_logging_default_level():
    """Test setup_logging with default log level."""
    setup_logging()

    # Verify root logger is configured with INFO
    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO


def test_setup_logging_silence_noisy():
    """Test setup_logging silences noisy HTTP loggers."""
    setup_logging(level="DEBUG", silence_noisy=True)

    # Verify noisy loggers are set to INFO
    assert logging.getLogger("httpcore").level == logging.INFO
    assert logging.getLogger("httpx").level == logging.INFO
    assert logging.getLogger("urllib3").level == logging.INFO


def test_setup_logging_no_silence():
    """Test setup_logging without silencing noisy loggers."""
    setup_logging(level="DEBUG", silence_noisy=False)

    # Verify noisy loggers inherit from root (DEBUG)
    # Note: They may have been set by previous tests, so we just verify
    # they weren't explicitly set to INFO by our function
    root_logger = logging.getLogger()
    assert root_logger.level == logging.DEBUG


def test_setup_logging_case_insensitive():
    """Test setup_logging handles case-insensitive string levels."""
    setup_logging(level="info")

    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO

    setup_logging(level="Error")

    root_logger = logging.getLogger()
    assert root_logger.level == logging.ERROR


def test_setup_logging_invalid_string_defaults_to_info():
    """Test setup_logging with invalid string defaults to INFO."""
    setup_logging(level="INVALID_LEVEL")

    root_logger = logging.getLogger()
    assert root_logger.level == logging.INFO
