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

        processor = RequestProcessor(token_manager, http_client, job_queue)

        assert processor.token_manager == token_manager
        assert processor.http_client == http_client
        assert processor.job_queue == job_queue
        assert processor.langfuse is None
        assert processor.shutdown_event is not None
        assert processor.context_loader is not None

    def test_initialization_with_optional_params(self):
        """Test initialization with optional parameters."""
        token_manager = MagicMock()
        http_client = MagicMock()
        job_queue = MagicMock()
        langfuse_client = MagicMock()
        shutdown_event = asyncio.Event()
        rate_limiters = MagicMock()
        health_checker = MagicMock()

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

        processor = RequestProcessor(token_manager, http_client, job_queue)

        await processor.cleanup()

        job_queue.close.assert_called_once()


class TestRequestProcessorExecution:
    """Test RequestProcessor execution methods."""

    @pytest.mark.asyncio
    async def test_execute_without_langfuse(self):
        """Test executing request without Langfuse."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-123")

        processor = RequestProcessor(token_manager, http_client, job_queue)

        # Mock all the dependencies
        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert job_id == "job-123"
            job_queue.create_job.assert_called_once()
            token_manager.get_token.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_with_claude_md(self):
        """Test executing request with CLAUDE.md content."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-456")

        processor = RequestProcessor(token_manager, http_client, job_queue)

        # Mock dependencies
        processor.context_loader.fetch_claude_md = AsyncMock(
            return_value="# Repository Guidelines"
        )

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert job_id == "job-456"
            # Verify CLAUDE.md was fetched
            processor.context_loader.fetch_claude_md.assert_called_once_with(
                "owner/repo"
            )
            # Verify job was created with CLAUDE.md prepended to prompt
            call_args = job_queue.create_job.call_args[0][0]
            assert "# Repository Guidelines" in call_args["prompt"]

    @pytest.mark.asyncio
    async def test_execute_with_auto_review(self):
        """Test executing request with auto_review flag."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-789")

        processor = RequestProcessor(token_manager, http_client, job_queue)

        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Review prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="review",
                user="bot",
                auto_review=True,
                auto_triage=False,
            )

            assert job_id == "job-789"
            # Verify job was created with auto_review flag
            call_args = job_queue.create_job.call_args[0][0]
            assert call_args["auto_review"] is True

    @pytest.mark.asyncio
    async def test_execute_with_auto_triage(self):
        """Test executing request with auto_triage flag."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-101")

        processor = RequestProcessor(token_manager, http_client, job_queue)

        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Triage prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=456,
                command="triage",
                user="bot",
                auto_review=False,
                auto_triage=True,
            )

            assert job_id == "job-101"
            # Verify job was created with auto_triage flag
            call_args = job_queue.create_job.call_args[0][0]
            assert call_args["auto_triage"] is True

    @pytest.mark.asyncio
    async def test_execute_claude_md_fetch_failure(self):
        """Test execution continues when CLAUDE.md fetch fails."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-202")

        processor = RequestProcessor(token_manager, http_client, job_queue)

        # Mock CLAUDE.md fetch to raise exception
        processor.context_loader.fetch_claude_md = AsyncMock(
            side_effect=Exception("Network error")
        )

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            # Should not raise exception, continues without CLAUDE.md
            job_id = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert job_id == "job-202"
            job_queue.create_job.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_claude_sdk_method(self):
        """Test process method with Langfuse integration."""
        token_manager = AsyncMock()
        token_manager.get_token = AsyncMock(return_value="test-token")
        http_client = AsyncMock()
        job_queue = AsyncMock()
        job_queue.create_job = AsyncMock(return_value="job-303")
        langfuse_client = MagicMock()

        # Mock Langfuse span context manager
        mock_span = MagicMock()
        mock_span.update = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)
        langfuse_client.start_as_current_span = MagicMock(return_value=mock_span)
        langfuse_client.flush = MagicMock()

        processor = RequestProcessor(
            token_manager, http_client, job_queue, langfuse_client
        )

        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            job_id = await processor.process(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert job_id == "job-303"
            langfuse_client.start_as_current_span.assert_called_once()
            langfuse_client.flush.assert_called_once()
