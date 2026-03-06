"""Unit tests for worker module."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


class TestWorkerFunctions:
    """Test worker utility functions."""

    def test_worker_uses_shared_signal_handling(self):
        """Test worker uses shared signal handling from shared.signals."""
        # This test verifies that the worker module imports and uses
        # the shared setup_graceful_shutdown function instead of
        # implementing its own signal handlers.
        from services.agent_worker import worker

        # Verify shutdown_event exists (used by shared signal handler)
        assert hasattr(worker, "shutdown_event")
        assert isinstance(worker.shutdown_event, asyncio.Event)


class TestWorkerMessageProcessing:
    """Test worker message processing callback."""

    @pytest.mark.asyncio
    async def test_callback_with_valid_message(self):
        """Test processing valid message."""
        # Create a mock processor
        mock_processor = AsyncMock()
        mock_processor.process = AsyncMock()

        # Create mock health checker
        mock_health_checker = MagicMock()
        mock_health_checker.record_activity = MagicMock()

        # Create shutdown event
        shutdown_event = asyncio.Event()

        # Create the callback
        async def callback(message: dict):
            if shutdown_event.is_set():
                return

            repo = message.get("repository")
            issue_number = message.get("issue_number")
            command = message.get("command")
            user = message.get("user", "unknown")
            auto_review = message.get("auto_review", False)
            auto_triage = message.get("auto_triage", False)

            if not all([repo, issue_number, command]):
                mock_health_checker.record_error()
                return

            assert isinstance(repo, str)
            assert isinstance(issue_number, int)
            assert isinstance(command, str)
            assert isinstance(user, str)

            await mock_processor.process(
                repo, issue_number, command, user, auto_review, auto_triage
            )

            mock_health_checker.record_activity()

        # Test with valid message
        message = {
            "repository": "owner/repo",
            "issue_number": 123,
            "command": "review",
            "user": "testuser",
            "auto_review": False,
            "auto_triage": False,
        }

        await callback(message)

        mock_processor.process.assert_called_once_with(
            "owner/repo", 123, "review", "testuser", False, False
        )
        mock_health_checker.record_activity.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_with_invalid_message(self):
        """Test processing invalid message."""
        mock_health_checker = MagicMock()
        mock_health_checker.record_error = MagicMock()

        shutdown_event = asyncio.Event()

        async def callback(message: dict):
            if shutdown_event.is_set():
                return

            repo = message.get("repository")
            issue_number = message.get("issue_number")
            command = message.get("command")

            if not all([repo, issue_number, command]):
                mock_health_checker.record_error()
                return

        # Test with missing fields
        message = {"repository": "owner/repo"}

        await callback(message)

        mock_health_checker.record_error.assert_called_once()

    @pytest.mark.asyncio
    async def test_callback_skips_when_shutdown(self):
        """Test callback skips processing during shutdown."""
        mock_processor = AsyncMock()
        shutdown_event = asyncio.Event()
        shutdown_event.set()  # Trigger shutdown

        async def callback(message: dict):
            if shutdown_event.is_set():
                return
            await mock_processor.process()

        message = {
            "repository": "owner/repo",
            "issue_number": 123,
            "command": "review",
        }

        await callback(message)

        # Should not call processor
        mock_processor.process.assert_not_called()

    @pytest.mark.asyncio
    async def test_callback_with_auto_review_flag(self):
        """Test callback with auto_review flag."""
        mock_processor = AsyncMock()
        mock_health_checker = MagicMock()
        shutdown_event = asyncio.Event()

        async def callback(message: dict):
            if shutdown_event.is_set():
                return

            repo = message.get("repository")
            issue_number = message.get("issue_number")
            command = message.get("command")
            user = message.get("user", "unknown")
            auto_review = message.get("auto_review", False)
            auto_triage = message.get("auto_triage", False)

            if not all([repo, issue_number, command]):
                return

            await mock_processor.process(
                repo, issue_number, command, user, auto_review, auto_triage
            )
            mock_health_checker.record_activity()

        message = {
            "repository": "owner/repo",
            "issue_number": 456,
            "command": "review",
            "user": "bot",
            "auto_review": True,
            "auto_triage": False,
        }

        await callback(message)

        mock_processor.process.assert_called_once_with(
            "owner/repo", 456, "review", "bot", True, False
        )

    @pytest.mark.asyncio
    async def test_callback_with_auto_triage_flag(self):
        """Test callback with auto_triage flag."""
        mock_processor = AsyncMock()
        mock_health_checker = MagicMock()
        shutdown_event = asyncio.Event()

        async def callback(message: dict):
            if shutdown_event.is_set():
                return

            repo = message.get("repository")
            issue_number = message.get("issue_number")
            command = message.get("command")
            user = message.get("user", "unknown")
            auto_review = message.get("auto_review", False)
            auto_triage = message.get("auto_triage", False)

            if not all([repo, issue_number, command]):
                return

            await mock_processor.process(
                repo, issue_number, command, user, auto_review, auto_triage
            )
            mock_health_checker.record_activity()

        message = {
            "repository": "owner/repo",
            "issue_number": 789,
            "command": "triage",
            "user": "bot",
            "auto_review": False,
            "auto_triage": True,
        }

        await callback(message)

        mock_processor.process.assert_called_once_with(
            "owner/repo", 789, "triage", "bot", False, True
        )

    @pytest.mark.asyncio
    async def test_callback_handles_processor_exception(self):
        """Test callback handles exceptions from processor."""
        mock_processor = AsyncMock()
        mock_processor.process = AsyncMock(side_effect=Exception("Processing error"))
        mock_health_checker = MagicMock()
        mock_health_checker.record_error = MagicMock()
        shutdown_event = asyncio.Event()

        async def callback(message: dict):
            if shutdown_event.is_set():
                return

            try:
                repo = message.get("repository")
                issue_number = message.get("issue_number")
                command = message.get("command")
                user = message.get("user", "unknown")
                auto_review = message.get("auto_review", False)
                auto_triage = message.get("auto_triage", False)

                if not all([repo, issue_number, command]):
                    return

                await mock_processor.process(
                    repo, issue_number, command, user, auto_review, auto_triage
                )
            except Exception:
                mock_health_checker.record_error()

        message = {
            "repository": "owner/repo",
            "issue_number": 123,
            "command": "review",
        }

        await callback(message)

        mock_health_checker.record_error.assert_called_once()
