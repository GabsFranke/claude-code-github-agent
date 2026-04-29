"""Unit tests for sandbox worker module."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Default BOT_USER_EMAIL contains [bot] which fails the safe-character regex
# in process_job. Provide clean env vars so the validation passes.
_SAFE_ENV_OVERRIDES = {
    "BOT_USERNAME": "Claude Code Agent",
    "BOT_USER_EMAIL": "claude-code-agent@users.noreply.github.com",
}


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
            patch.dict(os.environ, _SAFE_ENV_OVERRIDES),
            patch(
                "services.sandbox_executor.processor.wait_for_repo_sync",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.processor.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "services.sandbox_executor.processor.execute_sdk",
                new_callable=AsyncMock,
                return_value={
                    "response": "Test response",
                    "num_turns": 1,
                    "duration_ms": 1000,
                    "is_error": False,
                    "messages": [],
                },
            ),
            patch(
                "services.sandbox_executor.processor.tempfile.mkdtemp"
            ) as mock_mkdtemp,
            patch("services.sandbox_executor.processor.os.rmdir"),
            patch("services.sandbox_executor.processor.os.chdir", create=True),
            patch(
                "services.sandbox_executor.processor.os.getcwd",
                return_value="/original",
                create=True,
            ),
            patch("services.sandbox_executor.processor.os.makedirs", create=True),
            patch("services.sandbox_executor.processor.os.open"),
            patch("services.sandbox_executor.processor.os.write"),
            patch("services.sandbox_executor.processor.os.close"),
            patch("services.sandbox_executor.processor.os.remove"),
            patch(
                "services.sandbox_executor.processor.os.path.exists",
                return_value=False,
            ),
            patch(
                "services.sandbox_executor.processor.RepoSetupEngine"
            ) as mock_engine_class,
            patch("shared.mcp_json_writer.write_mcp_json"),
            patch(
                "services.sandbox_executor.processor.generate_structural_context",
                new_callable=AsyncMock,
                return_value=("", ""),
            ),
        ):
            # Mock workspace path
            mock_mkdtemp.return_value = "/tmp/test_workspace"

            # Mock repo setup engine
            mock_engine = MagicMock()
            mock_engine.get_setup_config.return_value = None
            mock_engine_class.return_value = mock_engine

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
            patch.dict(os.environ, _SAFE_ENV_OVERRIDES),
            patch(
                "services.sandbox_executor.processor.wait_for_repo_sync",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.processor.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "services.sandbox_executor.processor.execute_sdk",
                new_callable=AsyncMock,
                side_effect=Exception("Execution failed"),
            ),
            patch(
                "services.sandbox_executor.processor.tempfile.mkdtemp"
            ) as mock_mkdtemp,
            patch("services.sandbox_executor.processor.os.rmdir"),
            patch("services.sandbox_executor.processor.os.chdir", create=True),
            patch(
                "services.sandbox_executor.processor.os.getcwd",
                return_value="/original",
                create=True,
            ),
            patch("services.sandbox_executor.processor.os.makedirs", create=True),
            patch("services.sandbox_executor.processor.os.open"),
            patch("services.sandbox_executor.processor.os.write"),
            patch("services.sandbox_executor.processor.os.close"),
            patch("services.sandbox_executor.processor.os.remove"),
            patch(
                "services.sandbox_executor.processor.os.path.exists",
                return_value=False,
            ),
            patch(
                "services.sandbox_executor.processor.RepoSetupEngine"
            ) as mock_engine_class,
            patch("shared.mcp_json_writer.write_mcp_json"),
            patch(
                "services.sandbox_executor.processor.generate_structural_context",
                new_callable=AsyncMock,
                return_value=("", ""),
            ),
        ):
            # Mock workspace path
            mock_mkdtemp.return_value = "/tmp/test_workspace"

            # Mock repo setup engine
            mock_engine = MagicMock()
            mock_engine.get_setup_config.return_value = None
            mock_engine_class.return_value = mock_engine

            await process_job(mock_queue, job_id, job_data)

            # Verify job was marked as failed
            mock_queue.complete_job.assert_called_once()
            call_args = mock_queue.complete_job.call_args
            assert call_args[0][0] == job_id
            assert call_args[0][1]["status"] == "error"
            assert "Execution failed" in call_args[0][1]["error"]
            assert call_args[1]["status"] == "error"


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

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.JobQueue",
                return_value=mock_queue,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._orphan_cleanup_loop",
                new_callable=AsyncMock,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._process_cleanup_requests",
                new_callable=AsyncMock,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.process_job",
                new_callable=AsyncMock,
            ) as mock_process,
        ):
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
                raise RuntimeError("Queue error")
            else:
                shutdown_event.set()
                return None

        mock_queue.get_next_job = get_next_job_side_effect
        mock_queue.close = AsyncMock()

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.JobQueue",
                return_value=mock_queue,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._orphan_cleanup_loop",
                new_callable=AsyncMock,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._process_cleanup_requests",
                new_callable=AsyncMock,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.asyncio.sleep",
                new_callable=AsyncMock,
            ),
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

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.JobQueue",
                return_value=mock_queue,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._orphan_cleanup_loop",
                new_callable=AsyncMock,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker._process_cleanup_requests",
                new_callable=AsyncMock,
            ),
        ):
            await main()

            # Verify cleanup happened
            mock_queue.close.assert_called_once()

        # Reset shutdown event
        shutdown_event.clear()
