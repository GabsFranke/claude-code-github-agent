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

        processor = RequestProcessor(token_manager, http_client)

        assert processor.token_manager == token_manager
        assert processor.http_client == http_client
        assert processor.langfuse is None
        assert processor.shutdown_event is not None
        assert processor.context_loader is not None
        assert processor.mcp_builder is not None
        assert processor.observability is not None
        assert processor.sdk_executor is not None

    def test_initialization_with_optional_params(self):
        """Test initialization with optional parameters."""
        token_manager = MagicMock()
        http_client = MagicMock()
        langfuse_client = MagicMock()
        shutdown_event = asyncio.Event()
        rate_limiters = MagicMock()
        health_checker = MagicMock()

        processor = RequestProcessor(
            token_manager,
            http_client,
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

        processor = RequestProcessor(token_manager, http_client)
        processor.sdk_executor.cleanup = AsyncMock()

        await processor.cleanup()

        processor.sdk_executor.cleanup.assert_called_once()


class TestRequestProcessorExecution:
    """Test RequestProcessor execution methods."""

    @pytest.mark.asyncio
    async def test_execute_without_langfuse(self):
        """Test executing request without Langfuse."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        # Mock all the dependencies
        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="SDK response")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            response = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert response == "SDK response"
            processor.sdk_executor.execute_sdk.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_with_claude_md(self):
        """Test executing request with CLAUDE.md content."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        # Mock dependencies
        processor.context_loader.fetch_claude_md = AsyncMock(
            return_value="# Repository Guidelines"
        )
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="SDK response")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            response = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert response == "SDK response"
            # Verify CLAUDE.md was fetched
            processor.context_loader.fetch_claude_md.assert_called_once_with(
                "owner/repo"
            )

    @pytest.mark.asyncio
    async def test_execute_with_auto_review(self):
        """Test executing request with auto_review flag."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="Review complete")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Review prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            response = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="review",
                user="bot",
                auto_review=True,
                auto_triage=False,
            )

            assert response == "Review complete"

    @pytest.mark.asyncio
    async def test_execute_with_auto_triage(self):
        """Test executing request with auto_triage flag."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        processor.context_loader.fetch_claude_md = AsyncMock(return_value="")
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="Triage complete")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Triage prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            response = await processor._execute(
                repo="owner/repo",
                issue_number=456,
                command="triage",
                user="bot",
                auto_review=False,
                auto_triage=True,
            )

            assert response == "Triage complete"

    @pytest.mark.asyncio
    async def test_execute_claude_md_fetch_failure(self):
        """Test execution continues when CLAUDE.md fetch fails."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        # Mock CLAUDE.md fetch to raise exception
        processor.context_loader.fetch_claude_md = AsyncMock(
            side_effect=Exception("Network error")
        )
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="SDK response")

        with patch(
            "services.agent_worker.processors.request_processor.get_command_registry"
        ) as mock_registry:
            mock_cmd_registry = MagicMock()
            mock_result = MagicMock()
            mock_result.prompt = "Test prompt"
            mock_cmd_registry.execute = AsyncMock(return_value=mock_result)
            mock_registry.return_value = mock_cmd_registry

            # Should not raise exception, continues without CLAUDE.md
            response = await processor._execute(
                repo="owner/repo",
                issue_number=123,
                command="test",
                user="testuser",
                auto_review=False,
                auto_triage=False,
            )

            assert response == "SDK response"

    @pytest.mark.asyncio
    async def test_execute_claude_sdk_method(self):
        """Test _execute_claude_sdk method."""
        token_manager = AsyncMock()
        http_client = AsyncMock()

        processor = RequestProcessor(token_manager, http_client)

        # Mock dependencies
        processor.mcp_builder.create_mcp_config = AsyncMock(return_value={"github": {}})
        processor.observability.setup_langfuse_hooks = MagicMock(return_value={})
        processor.mcp_builder.create_agent_options = MagicMock(return_value=MagicMock())
        processor.sdk_executor.execute_sdk = AsyncMock(return_value="SDK result")

        result = await processor._execute_claude_sdk("Test prompt")

        assert result == "SDK result"
        processor.mcp_builder.create_mcp_config.assert_called_once()
        processor.observability.setup_langfuse_hooks.assert_called_once()
        processor.sdk_executor.execute_sdk.assert_called_once()
