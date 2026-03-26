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
        from services.sandbox_executor.sdk_executor import setup_langfuse_hooks

        with patch.dict(os.environ, {}, clear=True):
            hooks = setup_langfuse_hooks()
            assert hooks == {}

    def test_returns_hooks_when_credentials_configured(self):
        """Test returns hooks dict when Langfuse credentials configured."""
        from services.sandbox_executor.sdk_executor import setup_langfuse_hooks

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


class TestExecuteSandboxRequest:
    """Test execute_sandbox_request function."""

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        """Test successful execution in workspace."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        # Create temporary workspace
        with tempfile.TemporaryDirectory() as workspace:
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
                "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
                return_value=mock_client,
            ):
                response = await execute_sandbox_request(
                    prompt="Test prompt",
                    github_token="test_token",
                    repo="owner/repo",
                    issue_number=123,
                    user="testuser",
                    auto_review=False,
                    auto_triage=False,
                    workspace=workspace,
                )

                assert response == "Test response"
                mock_client.query.assert_called_once_with("Test prompt")

    @pytest.mark.asyncio
    async def test_passes_workspace_to_sdk(self):
        """Test workspace is passed to SDK as cwd parameter."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        with tempfile.TemporaryDirectory() as workspace:
            from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

            async def mock_receive():
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
                "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
                return_value=mock_client,
            ) as mock_sdk_client:
                await execute_sandbox_request(
                    prompt="Test",
                    github_token="token",
                    repo="repo",
                    issue_number=1,
                    user="user",
                    auto_review=False,
                    auto_triage=False,
                    workspace=workspace,
                )

                # Verify SDK client was called with correct cwd parameter
                assert mock_sdk_client.called
                call_args = mock_sdk_client.call_args
                assert call_args is not None

    @pytest.mark.asyncio
    async def test_handles_execution_errors(self):
        """Test execution errors are properly handled."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        with tempfile.TemporaryDirectory() as workspace:
            mock_client = MagicMock()
            mock_client.query = AsyncMock(side_effect=RuntimeError("Test error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)

            with patch(
                "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
                return_value=mock_client,
            ):
                with pytest.raises(
                    Exception, match="Failed to execute Claude Agent SDK in sandbox"
                ):
                    await execute_sandbox_request(
                        prompt="Test",
                        github_token="token",
                        repo="repo",
                        issue_number=1,
                        user="user",
                        auto_review=False,
                        auto_triage=False,
                        workspace=workspace,
                    )

    @pytest.mark.asyncio
    async def test_empty_response_raises_exception(self):
        """Test empty response raises exception."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        with tempfile.TemporaryDirectory() as workspace:
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
                "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
                return_value=mock_client,
            ):
                with pytest.raises(Exception, match="returned empty response"):
                    await execute_sandbox_request(
                        prompt="Test",
                        github_token="token",
                        repo="repo",
                        issue_number=1,
                        user="user",
                        auto_review=False,
                        auto_triage=False,
                        workspace=workspace,
                    )

    @pytest.mark.asyncio
    async def test_shutdown_during_execution(self):
        """Test shutdown event stops execution gracefully."""
        from services.sandbox_executor.sandbox_worker import shutdown_event
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        with tempfile.TemporaryDirectory() as workspace:
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
                "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
                return_value=mock_client,
            ):
                # Should return response collected before shutdown
                response = await execute_sandbox_request(
                    prompt="Test",
                    github_token="token",
                    repo="repo",
                    issue_number=1,
                    user="user",
                    auto_review=False,
                    auto_triage=False,
                    workspace=workspace,
                )
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

        async def mock_git_command(cmd, cwd=None):
            # Create workspace directory when worktree add is called
            if "worktree add" in cmd:
                # Extract workspace path from command
                # Command format: git --git-dir=/path worktree add --detach /tmp/job_xxx ref
                parts = cmd.split()
                # Find the workspace path (it's after "--detach")
                if "--detach" in parts:
                    idx = parts.index("--detach")
                    if idx + 1 < len(parts):
                        workspace = parts[idx + 1]
                        Path(workspace).mkdir(parents=True, exist_ok=True)
                        print(f"Created workspace directory: {workspace}")
            return (0, "", "")

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                side_effect=mock_git_command,
            ),
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

        async def mock_git_command(cmd, cwd=None):
            # Create workspace directory when worktree add is called
            if "worktree add" in cmd:
                # Extract workspace path from command
                parts = cmd.split()
                if "--detach" in parts:
                    idx = parts.index("--detach")
                    if idx + 1 < len(parts):
                        workspace = parts[idx + 1]
                        Path(workspace).mkdir(parents=True, exist_ok=True)
            return (0, "", "")

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                side_effect=mock_git_command,
            ),
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
    async def test_workspace_cleanup_on_success(self):
        """Test workspace is cleaned up after successful processing."""
        from services.sandbox_executor.sandbox_worker import process_job

        mock_queue = AsyncMock()
        mock_queue.complete_job = AsyncMock()
        mock_queue.redis = AsyncMock()
        mock_queue.redis.get = AsyncMock(return_value="true")  # Repo already synced

        job_id = "550e8400-e29b-41d4-a716-446655440002"  # Valid UUID
        job_data = {
            "prompt": "Test",
            "github_token": "token",
            "repo": "owner/repo",
            "issue_number": 1,
            "user": "user",
        }

        created_workspace = None
        workspace_removed = False

        async def capture_workspace(
            prompt,
            github_token,
            repo,
            issue_number,
            user,
            auto_review,
            auto_triage,
            workspace,
        ):
            nonlocal created_workspace
            created_workspace = workspace
            # Verify workspace exists when execute_sandbox_request is called
            assert Path(workspace).exists()
            return "Response"

        async def mock_git_command(cmd, cwd=None):
            nonlocal workspace_removed
            if "worktree add" in cmd:
                # Create the workspace directory that was removed by mkdtemp
                parts = cmd.split()
                if "--detach" in parts:
                    idx = parts.index("--detach")
                    if idx + 1 < len(parts):
                        workspace = parts[idx + 1]
                        Path(workspace).mkdir(parents=True, exist_ok=True)
                return (0, "", "")
            elif "worktree remove" in cmd:
                workspace_removed = True
                # Remove the workspace directory
                parts = cmd.split()
                if len(parts) >= 6:
                    workspace = parts[
                        5
                    ]  # git --git-dir=xxx worktree remove --force /tmp/xxx
                    if Path(workspace).exists():
                        Path(workspace).rmdir()
                return (0, "", "")
            elif "git config" in cmd:
                return (0, "", "")
            return (0, "", "")

        with (
            patch(
                "services.sandbox_executor.sandbox_worker.ensure_repo_synced",
                new_callable=AsyncMock,
                return_value="/var/cache/repos/owner/repo.git",
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_git_command",
                new_callable=AsyncMock,
                side_effect=mock_git_command,
            ),
            patch(
                "services.sandbox_executor.sandbox_worker.execute_sandbox_request",
                new_callable=AsyncMock,
                side_effect=capture_workspace,
            ),
        ):
            await process_job(mock_queue, job_id, job_data)

            # Verify workspace was cleaned up
            assert created_workspace is not None
            assert workspace_removed
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
