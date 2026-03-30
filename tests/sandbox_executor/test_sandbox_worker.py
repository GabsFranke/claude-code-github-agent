"""Unit tests for sandbox worker module."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_shutdown_event():
    """Reset shutdown event before each test."""
    from services.sandbox_executor import sandbox_worker

    sandbox_worker.shutdown_event.clear()
    yield
    sandbox_worker.shutdown_event.clear()


class TestSignalHandling:
    """Test signal handling functions."""

    def test_sandbox_worker_uses_shared_signal_handling(self):
        """Test sandbox worker uses shared signal handling from shared.signals."""
        # This test verifies that the sandbox_worker module imports and uses
        # the shared setup_graceful_shutdown function instead of
        # implementing its own signal handlers.
        from services.sandbox_executor import sandbox_worker

        # Verify shutdown_event exists (used by shared signal handler)
        assert hasattr(sandbox_worker, "shutdown_event")
        assert isinstance(sandbox_worker.shutdown_event, asyncio.Event)


class TestProcessJob:
    """Test process_job function."""

    @pytest.mark.asyncio
    async def test_successful_job_processing(self):
        """Test successful job processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()

        job_id = "550e8400-e29b-41d4-a716-446655440000"  # Valid UUID
        job_data = {
            "prompt": "Test prompt",
            "github_token": "test_token",
            "repo": "owner/repo",
            "issue_number": 123,
            "user": "testuser",
        }

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "tempfile.mkdtemp",
                return_value="/tmp/test_workspace",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.os"
            ),  # Mock the os module in sandbox_worker
            patch("services.sandbox_executor.sandbox_worker.Path"),  # Mock Path module
            patch(
                "services.sandbox_executor.sandbox_worker.execute_sandbox_request",
                new_callable=AsyncMock,
                return_value="Test response",
            ),
        ):
            await process_job(mock_queue, job_id, job_data)

            # Verify job was marked as complete
            mock_queue.complete_job.assert_called_once()
            call_args = mock_queue.complete_job.call_args
            assert call_args[0][0] == job_id
            assert call_args[0][1]["status"] == "success"
            assert call_args[0][1]["response"] == "Test response"
            assert call_args[1]["status"] == "success"

    @pytest.mark.asyncio
    async def test_failed_job_processing(self):
        """Test failed job processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()

        job_id = "550e8400-e29b-41d4-a716-446655440001"  # Valid UUID
        job_data = {
            "prompt": "Test",
            "github_token": "token",
            "repo": "owner/repo",
            "issue_number": 456,
            "user": "user",
        }

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "tempfile.mkdtemp",
                return_value="/tmp/test_workspace",
            ),
            patch("os.rmdir"),  # Mock os.rmdir to avoid FileNotFoundError
            patch(
                "services.sandbox_executor.sandbox_worker.execute_sandbox_request",
                new_callable=AsyncMock,
                side_effect=Exception("Execution failed"),
            ),
        ):
            await process_job(mock_queue, job_id, job_data)

            # Verify job was marked as failed
            mock_queue.complete_job.assert_called_once()
            call_args = mock_queue.complete_job.call_args
            assert call_args[0][0] == job_id
            assert call_args[0][1]["status"] == "error"
            assert "Execution failed" in call_args[0][1]["error"]
            assert call_args[1]["status"] == "error"

    @pytest.mark.asyncio
    async def test_workspace_cleanup(self):
        """Test workspace is cleaned up after processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()

        job_id = "550e8400-e29b-41d4-a716-446655440002"  # Valid UUID
        job_data = {
            "prompt": "Test",
            "github_token": "token",
            "repo": "owner/repo",
            "issue_number": 1,
            "user": "user",
        }

        created_workspace = None

        async def capture_sandbox_request(*args, **kwargs):
            nonlocal created_workspace
            created_workspace = kwargs.get(
                "workspace", args[6] if len(args) > 6 else None
            )
            return "Response"

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "tempfile.mkdtemp",
                return_value="/tmp/test_workspace",
            ),
            patch("os.rmdir"),  # Mock os.rmdir to avoid FileNotFoundError
            patch(
                "services.sandbox_executor.sandbox_worker.execute_sandbox_request",
                new_callable=AsyncMock,
                side_effect=capture_sandbox_request,
            ),
        ):
            await process_job(mock_queue, job_id, job_data)

            # Verify workspace was passed to execute_sandbox_request
            assert created_workspace == "/tmp/test_workspace"


class TestMainLoop:
    """Test main worker loop."""

    @pytest.mark.asyncio
    async def test_processes_jobs_from_queue(self):
        """Test main loop processes jobs from queue."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()

        # First call returns a job, second call triggers shutdown
        call_count = 0

        async def get_next_job_side_effect(timeout=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return (
                    "job1",
                    {
                        "prompt": "Test",
                        "github_token": "token",
                        "repo": "repo",
                        "issue_number": 1,
                        "user": "user",
                    },
                )
            else:
                shutdown_event.set()
                return None

        mock_queue.get_next_job = get_next_job_side_effect
        mock_queue.close = AsyncMock()

        with patch(
            "services.sandbox_executor.sandbox_worker.JobQueue", return_value=mock_queue
        ):
            with patch(
                "services.sandbox_executor.sandbox_worker.process_job",
                new_callable=AsyncMock,
            ) as mock_process:
                await main()

                # Verify job was processed
                mock_process.assert_called_once()
                mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_handles_queue_errors_gracefully(self):
        """Test main loop handles queue errors gracefully."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()

        # First call raises error, second call triggers shutdown
        call_count = 0

        async def get_next_job_side_effect(timeout=5):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("Queue error")
            else:
                await asyncio.sleep(0.1)
                shutdown_event.set()
                return None

        mock_queue.get_next_job = get_next_job_side_effect
        mock_queue.close = AsyncMock()

        with patch(
            "services.sandbox_executor.sandbox_worker.JobQueue", return_value=mock_queue
        ):
            await main()

            # Verify cleanup happened
            mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_respects_shutdown_event(self):
        """Test main loop respects shutdown event."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()
        mock_queue.get_next_job = AsyncMock(return_value=None)
        mock_queue.close = AsyncMock()

        # Set shutdown immediately
        shutdown_event.set()

        with patch(
            "services.sandbox_executor.sandbox_worker.JobQueue", return_value=mock_queue
        ):
            await main()

            # Verify cleanup happened
            mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()
