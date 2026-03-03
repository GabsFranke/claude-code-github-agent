"""Unit tests for health check module."""

import asyncio
import tempfile
from pathlib import Path

import pytest

from shared.health import HealthChecker, HealthStatus


class TestHealthChecker:
    """Test HealthChecker class."""

    def test_health_checker_initialization(self):
        """Test health checker initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"
            health = HealthChecker(health_file=str(health_file))

            assert health.health_file == health_file
            assert health._processed_count == 0
            assert health._error_count == 0

    def test_record_activity(self):
        """Test recording activity."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"
            health = HealthChecker(health_file=str(health_file))

            initial_count = health._processed_count
            health.record_activity()

            assert health._processed_count == initial_count + 1

    def test_record_error(self):
        """Test recording errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"
            health = HealthChecker(health_file=str(health_file))

            initial_count = health._error_count
            health.record_error()

            assert health._error_count == initial_count + 1

    def test_get_status(self):
        """Test getting health status."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"
            health = HealthChecker(health_file=str(health_file))

            health.record_activity()
            status = health.get_status()

            assert isinstance(status, HealthStatus)
            assert status.healthy is True
            assert status.processed_count == 1
            assert status.error_count == 0

    @pytest.mark.asyncio
    async def test_start_and_stop(self):
        """Test starting and stopping health checker."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"
            health = HealthChecker(health_file=str(health_file), update_interval=1)

            health.start()
            assert health._running is True

            # Wait a bit for file to be created
            await asyncio.sleep(1.5)

            await health.stop()
            assert health._running is False

            # Check health file was created
            assert health_file.exists()

    @pytest.mark.asyncio
    async def test_async_context_manager(self):
        """Test async context manager."""
        with tempfile.TemporaryDirectory() as tmpdir:
            health_file = Path(tmpdir) / "health"

            async with HealthChecker(
                health_file=str(health_file), update_interval=1
            ) as health:
                assert health._running is True
                health.record_activity()

            # Should be stopped after context exit
            assert health._running is False
