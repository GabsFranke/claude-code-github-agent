"""Unit tests for sandbox worker module."""

import asyncio
import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@asynccontextmanager
async def _noop_lease(*args, **kwargs):
    """Stand-in for JobQueue.job_lease on AsyncMock queues.

    AsyncMock returns a coroutine for every attribute call, which is not an
    async context manager, so the lease has to be replaced explicitly.
    """
    yield


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
        from services.sandbox_executor import sandbox_worker

        assert hasattr(sandbox_worker, "shutdown_event")
        assert isinstance(sandbox_worker.shutdown_event, asyncio.Event)


class TestProcessJob:
    """Test process_job function."""

    @staticmethod
    def _create_patches(execute_sdk_config):
        """Create all patches needed for process_job tests.

        Returns (patches_flat, engine_patch) — call start() on each item
        in patches_flat, stop() in reverse after test completes.
        """
        mkdtemp_patch = patch(
            "services.sandbox_executor.processor.tempfile.mkdtemp",
            return_value="/tmp/test_workspace",
        )
        engine_patch = patch(
            "services.sandbox_executor.processor.RepoSetupEngine",
        )

        # conftest sets GITHUB_INSTALLATION_ID, so _setup_worktree would
        # otherwise build a real GitHubAuthService and call api.github.com.
        mock_auth = MagicMock()
        mock_auth.get_token = AsyncMock(return_value="test_token")
        mock_auth.__aenter__ = AsyncMock(return_value=mock_auth)
        mock_auth.__aexit__ = AsyncMock(return_value=False)

        patches_flat = [
            # --- core behaviour ---
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
                "services.sandbox_executor.git_setup.execute_git_command",
                new_callable=AsyncMock,
                return_value=(0, "", ""),
            ),
            patch(
                "services.sandbox_executor.processor.execute_sdk",
                new_callable=AsyncMock,
                **execute_sdk_config,
            ),
            mkdtemp_patch,
            patch(
                "services.sandbox_executor.processor.configure_git",
                new_callable=AsyncMock,
            ),
            # --- filesystem / cleanup ---
            # "processor.os" resolves to the global os module, so every patch
            # here replaces the attribute process-wide for the duration of the
            # test. Only patch what processor.py actually calls. os.close in
            # particular must stay real: on POSIX, subprocess.Popen closes the
            # parent end of its error pipe with os.close, and a mocked close
            # leaves the parent blocked forever in os.read() waiting for an EOF
            # that can never arrive. That deadlocked the whole CI test run.
            patch("services.sandbox_executor.processor.os.rmdir"),
            patch("services.sandbox_executor.processor.os.remove"),
            patch(
                "services.sandbox_executor.processor.os.path.exists",
                return_value=False,
            ),
            # _prepare_context shells out to `codegraph init`; a unit test must
            # not spawn a real process.
            patch("services.sandbox_executor.processor.subprocess.run"),
            # --- no real GitHub traffic ---
            patch("shared.github_auth.GitHubAuthService", return_value=mock_auth),
            patch(
                "shared.thread_history.fetch_and_format_thread_history",
                new_callable=AsyncMock,
                return_value="",
            ),
            engine_patch,
            patch("shared.mcp_json_writer.write_mcp_json"),
            patch(
                "services.sandbox_executor.processor.generate_structural_context",
                new_callable=AsyncMock,
                return_value="",
            ),
        ]

        return patches_flat, engine_patch

    @pytest.mark.asyncio
    async def test_successful_job_processing(self):
        """Test successful job processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()

        job_id = "550e8400-e29b-41d4-a716-446655440000"
        job_data = {
            "prompt": "Test prompt",
            "github_token": "test_token",
            "repo": "owner/repo",
            "issue_number": 123,
            "user": "testuser",
        }

        patches_flat, engine_patch = self._create_patches(
            execute_sdk_config={
                "return_value": {
                    "response": "Test response",
                    "num_turns": 1,
                    "duration_ms": 1000,
                    "is_error": False,
                    "messages": [],
                },
            }
        )

        # Start patches manually to avoid 20-block nesting limit
        for p in patches_flat:
            p.start()
        try:
            mock_engine = MagicMock()
            mock_engine.get_setup_config.return_value = None
            engine_patch.return_value = mock_engine

            await process_job(mock_queue, job_id, job_data)

            mock_queue.complete_job.assert_called_once()
            call_args = mock_queue.complete_job.call_args
            assert call_args[0][0] == job_id
            assert call_args[0][1]["status"] == "success"
            assert call_args[0][1]["response"] == "Test response"
            assert call_args[1]["status"] == "success"
        finally:
            for p in reversed(patches_flat):
                p.stop()

    @pytest.mark.asyncio
    async def test_failed_job_processing(self):
        """Test failed job processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()

        job_id = "550e8400-e29b-41d4-a716-446655440001"
        job_data = {
            "prompt": "Test",
            "github_token": "token",
            "repo": "owner/repo",
            "issue_number": 456,
            "user": "user",
        }

        patches_flat, engine_patch = self._create_patches(
            execute_sdk_config={
                "side_effect": Exception("Execution failed"),
            }
        )

        for p in patches_flat:
            p.start()
        try:
            mock_engine = MagicMock()
            mock_engine.get_setup_config.return_value = None
            engine_patch.return_value = mock_engine

            await process_job(mock_queue, job_id, job_data)

            mock_queue.complete_job.assert_called_once()
            call_args = mock_queue.complete_job.call_args
            assert call_args[0][0] == job_id
            assert call_args[0][1]["status"] == "error"
            assert "Execution failed" in call_args[0][1]["error"]
            assert call_args[1]["status"] == "error"
        finally:
            for p in reversed(patches_flat):
                p.stop()


class TestMainLoop:
    """Test main worker loop."""

    @pytest.mark.asyncio
    async def test_processes_jobs_from_queue(self):
        """Test main loop processes jobs from queue."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()
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
        mock_queue.job_lease = _noop_lease

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
            mock_process.assert_called_once()
            mock_queue.close.assert_called_once()

        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_job_runs_inside_lease(self):
        """The lease is held for the whole job, not just around the pull.

        Regression: JOB_TTL_SECONDS used to expire mid-run, so another
        worker's reclaim sweep picked up a job that was still executing and
        both drove the same shared worktree.
        """
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        events = []
        mock_queue = AsyncMock()
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
            shutdown_event.set()
            return None

        @asynccontextmanager
        async def tracking_lease(job_id):
            events.append(f"lease_enter:{job_id}")
            try:
                yield
            finally:
                events.append(f"lease_exit:{job_id}")

        async def tracking_process_job(queue, job_id, job_data):
            events.append(f"process:{job_id}")

        mock_queue.get_next_job = get_next_job_side_effect
        mock_queue.close = AsyncMock()
        mock_queue.job_lease = tracking_lease

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
                side_effect=tracking_process_job,
            ),
        ):
            await main()

        assert events == [
            "lease_enter:job1",
            "process:job1",
            "lease_exit:job1",
        ]

        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_lease_released_when_job_raises(self):
        """A failing job must still release its lease."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        events = []
        mock_queue = AsyncMock()
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
            shutdown_event.set()
            return None

        @asynccontextmanager
        async def tracking_lease(job_id):
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        mock_queue.get_next_job = get_next_job_side_effect
        mock_queue.close = AsyncMock()
        mock_queue.job_lease = tracking_lease

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
                side_effect=RuntimeError("job blew up"),
            ),
        ):
            await main()

        assert events == ["enter", "exit"]

        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_handles_queue_errors_gracefully(self):
        """Test main loop handles queue errors gracefully."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()
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
            mock_queue.close.assert_called_once()

        shutdown_event.clear()

    @pytest.mark.asyncio
    async def test_respects_shutdown_event(self):
        """Test main loop respects shutdown event."""
        from services.sandbox_executor.sandbox_worker import main, shutdown_event

        mock_queue = AsyncMock()
        mock_queue.get_next_job = AsyncMock(return_value=None)
        mock_queue.close = AsyncMock()

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
            mock_queue.close.assert_called_once()

        shutdown_event.clear()
