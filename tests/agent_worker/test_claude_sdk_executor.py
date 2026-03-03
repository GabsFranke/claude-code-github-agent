"""Unit tests for ClaudeSDKExecutor."""

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from services.agent_worker.processors.claude_sdk_executor import ClaudeSDKExecutor


class TestClaudeSDKExecutor:
    """Test ClaudeSDKExecutor class."""

    def test_initialization(self):
        """Test ClaudeSDKExecutor initialization."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        assert executor.shutdown_event == shutdown_event
        assert executor.rate_limiters is None
        assert executor.temp_files_to_cleanup == []

    def test_initialization_with_rate_limiters(self):
        """Test initialization with rate limiters."""
        shutdown_event = asyncio.Event()
        rate_limiters = MagicMock()
        executor = ClaudeSDKExecutor(shutdown_event, rate_limiters)

        assert executor.rate_limiters == rate_limiters

    @patch.dict(os.environ, {}, clear=True)
    def test_setup_anthropic_environment_with_api_key(self):
        """Test setting up Anthropic environment with API key."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_API_KEY": "test-key",
                "ANTHROPIC_BASE_URL": "https://test.api",
            },
        ):
            executor.setup_anthropic_environment()

            assert os.environ["ANTHROPIC_API_KEY"] == "test-key"
            assert os.environ["ANTHROPIC_BASE_URL"] == "https://test.api"

    @patch.dict(os.environ, {}, clear=True)
    def test_setup_anthropic_environment_with_auth_token(self):
        """Test setting up Anthropic environment with auth token."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        with patch.dict(os.environ, {"ANTHROPIC_AUTH_TOKEN": "auth-token"}):
            executor.setup_anthropic_environment()

            assert os.environ["ANTHROPIC_API_KEY"] == "auth-token"

    @pytest.mark.asyncio
    @patch.dict(os.environ, {}, clear=True)
    async def test_setup_vertex_ai_credentials_not_configured(self):
        """Test Vertex AI setup when not configured."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        result = await executor.setup_vertex_ai_credentials()

        assert result is None

    @pytest.mark.asyncio
    async def test_setup_vertex_ai_credentials_with_config(self):
        """Test Vertex AI setup with configuration."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        credentials = {"type": "service_account", "project_id": "test"}
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_VERTEX_PROJECT_ID": "test-project",
                "ANTHROPIC_VERTEX_REGION": "us-central1",
                "GOOGLE_APPLICATION_CREDENTIALS_JSON": json.dumps(credentials),
            },
        ):
            result = await executor.setup_vertex_ai_credentials()

            assert result is not None
            assert os.path.exists(result)
            assert result in executor.temp_files_to_cleanup

            # Cleanup
            executor.cleanup_temp_file(result)

    def test_cleanup_temp_file(self):
        """Test cleaning up temporary file."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        # Create a temp file
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as f:
            temp_path = f.name

        executor.temp_files_to_cleanup.append(temp_path)

        # Cleanup
        executor.cleanup_temp_file(temp_path)

        assert not os.path.exists(temp_path)
        assert temp_path not in executor.temp_files_to_cleanup

    def test_cleanup_temp_file_not_found(self):
        """Test cleaning up non-existent file."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        # Should not raise exception
        executor.cleanup_temp_file("/nonexistent/file.json")

    @pytest.mark.asyncio
    async def test_cleanup(self):
        """Test cleanup method."""
        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        # Create temp files
        import tempfile

        temp_files = []
        for _ in range(2):
            with tempfile.NamedTemporaryFile(delete=False) as f:
                temp_files.append(f.name)

        executor.temp_files_to_cleanup = temp_files.copy()

        # Cleanup
        await executor.cleanup()

        assert executor.temp_files_to_cleanup == []
        for temp_file in temp_files:
            assert not os.path.exists(temp_file)


class TestClaudeSDKExecutorMessageProcessing:
    """Test ClaudeSDKExecutor message processing methods."""

    def test_process_sdk_message_with_shutdown(self):
        """Test message processing stops when shutdown is requested."""
        shutdown_event = asyncio.Event()
        shutdown_event.set()  # Trigger shutdown
        executor = ClaudeSDKExecutor(shutdown_event)

        response_parts = []
        message = MagicMock()

        result = executor._process_sdk_message(message, response_parts)

        assert result is True  # Should stop processing

    def test_handle_system_message_with_plugins(self):
        """Test handling system message with plugin data."""
        from claude_agent_sdk import SystemMessage

        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        message = SystemMessage(
            subtype="init", data={"plugins": ["plugin1", "plugin2"]}
        )

        # Should not raise exception
        executor._handle_system_message(message)

    def test_handle_assistant_message_with_text_block(self):
        """Test handling assistant message with text content."""
        from claude_agent_sdk import AssistantMessage, TextBlock

        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        text_block = TextBlock(text="Test response text")
        message = AssistantMessage(content=[text_block], model="claude-3")
        response_parts = []

        executor._handle_assistant_message(message, response_parts)

        assert len(response_parts) == 1
        assert response_parts[0] == "Test response text"

    def test_handle_assistant_message_with_tool_use(self):
        """Test handling assistant message with tool use."""
        from claude_agent_sdk import AssistantMessage, ToolUseBlock

        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        tool_block = ToolUseBlock(id="tool_123", name="test_tool", input={})
        message = AssistantMessage(content=[tool_block], model="claude-3")
        response_parts = []

        # Should not raise exception
        executor._handle_assistant_message(message, response_parts)

    def test_handle_result_message(self):
        """Test handling result message."""
        from claude_agent_sdk import ResultMessage

        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        message = ResultMessage(
            subtype="complete",
            num_turns=5,
            duration_ms=1500,
            duration_api_ms=1200,
            total_cost_usd=0.05,
            is_error=False,
            session_id="session_123",
        )

        # Should not raise exception
        executor._handle_result_message(message)

    def test_handle_result_message_without_cost(self):
        """Test handling result message without cost."""
        from claude_agent_sdk import ResultMessage

        shutdown_event = asyncio.Event()
        executor = ClaudeSDKExecutor(shutdown_event)

        message = ResultMessage(
            subtype="complete",
            num_turns=3,
            duration_ms=1000,
            duration_api_ms=800,
            total_cost_usd=None,
            is_error=False,
            session_id="session_456",
        )

        # Should not raise exception
        executor._handle_result_message(message)
