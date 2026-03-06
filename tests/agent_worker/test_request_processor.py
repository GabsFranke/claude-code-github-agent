"""Unit tests for RequestProcessor."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_worker.processors import RequestProcessor


class TestRequestProcessor:
    """Test RequestProcessor class."""

    def test_initialization(self):
        """Test RequestProcessor initialization."""
        token_manager = MagicMock()
        http_client = MagicMock()
        job_queue = MagicMock()

        with patch("services.agent_worker.processors.request_processor.WorkflowEngine"):
            processor = RequestProcessor(token_manager, http_client, job_queue)

            assert processor.token_manager == token_manager
            assert processor.http_client == http_client
            assert processor.job_queue == job_queue
            assert processor.langfuse is None
            assert processor.shutdown_event is not None
            assert processor.context_loader is not None
            assert processor.workflow_engine is not None

    def test_initialization_with_optional_params(self):
        """Test initialization with optional parameters."""
        token_manager = MagicMock()
        http_client = MagicMock()
        job_queue = MagicMock()
        langfuse_client = MagicMock()
        shutdown_event = asyncio.Event()
        rate_limiters = MagicMock()
        health_checker = MagicMock()

        with patch("services.agent_worker.processors.request_processor.WorkflowEngine"):
            processor = RequestProcessor(
                token_manager,
                http_client,
                job_queue,
                langfuse_client,
                shutdown_event,
                rate_limiters,
                health_checker,
            )

            assert processor.langfuse == langfuse_client
            assert processor.shutdown_event == shutdown_event
            assert processor.rate_limiters == rate_limiters
            assert processor.health_checker == health_checker

    @pytest.mark.asyncio
    async def test_cleanup(self):
        """Test cleanup method."""
        token_manager = MagicMock()
        http_client = MagicMock()
        job_queue = AsyncMock()
        job_queue.close = AsyncMock()

        with patch("services.agent_worker.processors.request_processor.WorkflowEngine"):
            processor = RequestProcessor(token_manager, http_client, job_queue)

            await processor.cleanup()

            job_queue.close.assert_called_once()


class TestRequestProcessorExecution:
    """Test RequestProcessor execution methods."""

    @pytest.mark.asyncio
    async def test_execute_with_workflow_name(self):
        """Test executing request with workflow name provided."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-123")

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                mock_engine = MagicMock()
                mock_engine.build_prompt = MagicMock(return_value="Review PR prompt")
                mock_engine.workflows = {
                    "review-pr": MagicMock()
                }  # Add workflow to dict
                mock_engine_class.return_value = mock_engine

                processor = RequestProcessor(token_manager, http_client, job_queue)
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

                job_id = await processor._execute(
                    repo="owner/repo",
                    issue_number=123,
                    event_data={"event_type": "pull_request", "action": "opened"},
                    user_query="",
                    user="testuser",
                    ref="main",
                    workflow_name="review-pr",
                )

                assert job_id == "job-123"
                mock_engine.build_prompt.assert_called_once()
                job_queue.create_job.assert_called_once()
                token_manager.get_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_with_command_workflow(self):
        """Test executing request with command workflow."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-456")

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                mock_engine = MagicMock()
                mock_engine.build_prompt = MagicMock(return_value="Generic prompt")
                mock_engine.workflows = {"generic": MagicMock()}  # Add workflow to dict
                mock_engine_class.return_value = mock_engine

                processor = RequestProcessor(token_manager, http_client, job_queue)
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

                job_id = await processor._execute(
                    repo="owner/repo",
                    issue_number=123,
                    event_data={"command": "/agent"},
                    user_query="help me fix this",
                    user="testuser",
                    workflow_name="generic",
                )

                assert job_id == "job-456"
                mock_engine.build_prompt.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_with_claude_md(self):
        """Test executing request with CLAUDE.md content."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-789")

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                mock_engine = MagicMock()
                mock_engine.build_prompt = MagicMock(return_value="Test prompt")
                mock_engine.workflows = {"generic": MagicMock()}  # Add workflow to dict
                mock_engine_class.return_value = mock_engine

                processor = RequestProcessor(token_manager, http_client, job_queue)
                processor.context_loader.fetch_claude_md = AsyncMock(
                    return_value="# Repository Guidelines"
                )

                job_id = await processor._execute(
                    repo="owner/repo",
                    issue_number=123,
                    event_data={"command": "/agent"},
                    user_query="test",
                    user="testuser",
                    workflow_name="generic",
                )

                assert job_id == "job-789"
                processor.context_loader.fetch_claude_md.assert_called_once_with(
                    "owner/repo"
                )
                call_args = job_queue.create_job.call_args[0][0]
                assert "# Repository Guidelines" in call_args["prompt"]

    @pytest.mark.asyncio
    async def test_execute_no_workflow_provided(self):
        """Test execution when no workflow name is provided."""
        token_manager = AsyncMock()
        http_client = AsyncMock()
        job_queue = AsyncMock()

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            mock_engine = MagicMock()
            mock_engine_class.return_value = mock_engine

            processor = RequestProcessor(token_manager, http_client, job_queue)

            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                event_data={"event_type": "unknown", "action": "unknown"},
                user_query="",
                user="testuser",
                workflow_name=None,
            )

            assert job_id == "ignored"
            job_queue.create_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_claude_md_fetch_failure(self):
        """Test execution continues when CLAUDE.md fetch fails."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-202")

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                mock_engine = MagicMock()
                mock_engine.build_prompt = MagicMock(return_value="Test prompt")
                mock_engine.workflows = {"generic": MagicMock()}  # Add workflow to dict
                mock_engine_class.return_value = mock_engine

                processor = RequestProcessor(token_manager, http_client, job_queue)
                processor.context_loader.fetch_claude_md = AsyncMock(
                    side_effect=Exception("Network error")
                )

                job_id = await processor._execute(
                    repo="owner/repo",
                    issue_number=123,
                    event_data={"command": "/agent"},
                    user_query="test",
                    user="testuser",
                    workflow_name="generic",
                )

                assert job_id == "job-202"
                job_queue.create_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_with_langfuse(self):
        """Test process method with Langfuse integration."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-303")
        langfuse_client = MagicMock()

        mock_span = MagicMock()
        mock_span.update = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        langfuse_client.start_as_current_span = MagicMock(return_value=mock_span)
        langfuse_client.flush = MagicMock()

        with patch(
            "services.agent_worker.processors.request_processor.WorkflowEngine"
        ) as mock_engine_class:
            with patch("shared.get_queue") as mock_get_queue:
                mock_sync_queue = AsyncMock()
                mock_get_queue.return_value = mock_sync_queue

                mock_engine = MagicMock()
                mock_engine.build_prompt = MagicMock(return_value="Test prompt")
                mock_engine.workflows = {"generic": MagicMock()}  # Add workflow to dict
                mock_engine_class.return_value = mock_engine

                processor = RequestProcessor(
                    token_manager, http_client, job_queue, langfuse_client
                )
                processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

                job_id = await processor.process(
                    repo="owner/repo",
                    issue_number=123,
                    event_data={"command": "/agent"},
                    user_query="test",
                    user="testuser",
                    workflow_name="generic",
                )

                assert job_id == "job-303"
                langfuse_client.start_as_current_span.assert_called_once()
                langfuse_client.flush.assert_called_once()
