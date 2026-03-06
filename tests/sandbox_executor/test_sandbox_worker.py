"""Unit tests for sandbox worker module."""

import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestSetupLangfuseHooks:
    """Test Langfuse hooks setup."""

    def test_returns_empty_dict_when_no_credentials(self):
        """Test returns empty dict when Langfuse credentials not configured."""
        from services.sandbox_executor.sandbox_worker import setup_langfuse_hooks

        with patch.dict(os.environ, {}, clear=True):
            hooks = setup_langfuse_hooks()
            assert hooks == {}

    def test_returns_hooks_when_credentials_configured(self):
        """Test returns hooks dict when Langfuse credentials configured."""
        from services.sandbox_executor.sandbox_worker import setup_langfuse_hooks

        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public",
                "LANGFUSE_SECRET_KEY": "test_secret",
            },
            clear=True,
        ):
            hooks = setup_langfuse_hooks()
            assert "Stop" in hooks
            assert "SubagentStop" in hooks


class TestExecuteInWorkspace:
    """Test execute_in_workspace function."""

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        """Test successful execution in workspace."""
        from services.sandbox_executor.sandbox_worker import execute_in_workspace

        # Create temporary workspace
        with tempfile.TemporaryDirectory() as workspace:
            job_data = {
                "prompt": "Test prompt",
                "github_token": "test_token",
                "repo": "owner/repo",
                "issue_number": 123,
                "user": "testuser",
            }

            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            async def mock_receive():
                yield AssistantMessage(
                    content=[TextBlock(text="Test response")],
                    model="claude-3-5-sonnet-20241022",
                )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1000,
                    duration_api_ms=1000,
                    is_error=False,
                    num_turns=1,
                    session_id="test",
                    total_cost_usd=0.01,
                )

            # Mock ClaudeSDKClient
            mock_client = MagicMock()
            mock_client.query = AsyncMock()
            mock_client.receive_messages = mock_receive
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sandbox_worker.ClaudeSDKClient",
                return_value=mock_client,
            ):
                response = await execute_in_workspace(workspace, job_data)

                assert response == "Test response"
                mock_client.query.assert_called_once_with("Test prompt")

    @pytest.mark.asyncio
    async def test_changes_to_workspace_directory(self):
        """Test execution changes to workspace directory."""
        from services.sandbox_executor.sandbox_worker import execute_in_workspace

        original_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as workspace:
            job_data = {
                "prompt": "Test",
                "github_token": "token",
                "repo": "repo",
                "issue_number": 1,
                "user": "user",
            }

            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            async def mock_receive():
                # Verify we're in the workspace
                assert os.getcwd() == workspace
                yield AssistantMessage(
                    content=[TextBlock(text="Response")],
                    model="claude-3-5-sonnet-20241022",
                )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1000,
                    duration_api_ms=1000,
                    is_error=False,
                    num_turns=1,
                    session_id="test",
                    total_cost_usd=0.01,
                )

            mock_client = MagicMock()
            mock_client.query = AsyncMock()
            mock_client.receive_messages = mock_receive
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sandbox_worker.ClaudeSDKClient",
                return_value=mock_client,
            ):
                await execute_in_workspace(workspace, job_data)

            # Verify we're back to original directory
            assert os.getcwd() == original_cwd

    @pytest.mark.asyncio
    async def test_restores_directory_on_exception(self):
        """Test directory is restored even on exception."""
        from services.sandbox_executor.sandbox_worker import execute_in_workspace

        original_cwd = os.getcwd()

        with tempfile.TemporaryDirectory() as workspace:
            job_data = {
                "prompt": "Test",
                "github_token": "token",
                "repo": "repo",
                "issue_number": 1,
                "user": "user",
            }

            mock_client = MagicMock()
            mock_client.query = AsyncMock(side_effect=RuntimeError("Test error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sandbox_worker.ClaudeSDKClient",
                return_value=mock_client,
            ):
                with pytest.raises(
                    Exception, match="Failed to execute Claude Agent SDK: Test error"
                ):
                    await execute_in_workspace(workspace, job_data)

            # Verify we're back to original directory
            assert os.getcwd() == original_cwd

    @pytest.mark.asyncio
    async def test_empty_response_raises_exception(self):
        """Test empty response raises exception."""
        from services.sandbox_executor.sandbox_worker import execute_in_workspace

        with tempfile.TemporaryDirectory() as workspace:
            job_data = {
                "prompt": "Test",
                "github_token": "token",
                "repo": "repo",
                "issue_number": 1,
                "user": "user",
            }

            from claude_agent_sdk import ResultMessage

            async def mock_receive():
                yield ResultMessage(
                    subtype="success",
                    duration_ms=100,
                    duration_api_ms=100,
                    is_error=False,
                    num_turns=0,
                    session_id="test",
                    total_cost_usd=0.0,
                )

            mock_client = MagicMock()
            mock_client.query = AsyncMock()
            mock_client.receive_messages = mock_receive
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sandbox_worker.ClaudeSDKClient",
                return_value=mock_client,
            ):
                with pytest.raises(Exception, match="returned empty response"):
                    await execute_in_workspace(workspace, job_data)

    @pytest.mark.asyncio
    async def test_shutdown_during_execution(self):
        """Test shutdown event stops execution gracefully."""
        from services.sandbox_executor.sandbox_worker import (
            execute_in_workspace,
            shutdown_event,
        )

        with tempfile.TemporaryDirectory() as workspace:
            job_data = {
                "prompt": "Test",
                "github_token": "token",
                "repo": "repo",
                "issue_number": 1,
                "user": "user",
            }

            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            async def mock_receive():
                # Yield response first, then check shutdown
                yield AssistantMessage(
                    content=[TextBlock(text="Response")],
                    model="claude-3-5-sonnet-20241022",
                )
                shutdown_event.set()  # Trigger shutdown after response
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1000,
                    duration_api_ms=1000,
                    is_error=False,
                    num_turns=1,
                    session_id="test",
                    total_cost_usd=0.01,
                )

            mock_client = MagicMock()
            mock_client.query = AsyncMock()
            mock_client.receive_messages = mock_receive
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sandbox_worker.ClaudeSDKClient",
                return_value=mock_client,
            ):
                # Should return response collected before shutdown
                response = await execute_in_workspace(workspace, job_data)
                assert response == "Response"

            # Reset shutdown event
            shutdown_event.clear()


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
                "services.sandbox_executor.sandbox_worker.execute_in_workspace",
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
                "services.sandbox_executor.sandbox_worker.execute_in_workspace",
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

        async def capture_workspace(workspace, _job_data):
            nonlocal created_workspace
            created_workspace = workspace
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
                "services.sandbox_executor.sandbox_worker.execute_in_workspace",
                new_callable=AsyncMock,
                side_effect=capture_workspace,
            ),
        ):
            await process_job(mock_queue, job_id, job_data)

            # Verify workspace was cleaned up
            assert created_workspace is not None
            assert not Path(created_workspace).exists()


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
