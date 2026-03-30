"""Unit tests for sandbox executor module."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSetupLangfuseHooks:
    """Test Langfuse hooks setup."""

    def test_returns_empty_dict_when_no_credentials(self):
        """Test returns empty dict when Langfuse credentials not configured."""
        from services.sandbox_executor.sdk_executor import setup_langfuse_hooks

        with patch.dict(os.environ, {}, clear=True):
            hooks = setup_langfuse_hooks()
            assert hooks == {}

    def test_returns_empty_dict_when_partial_credentials(self):
        """Test returns empty dict when only partial credentials provided."""
        from services.sandbox_executor.sdk_executor import setup_langfuse_hooks

        with patch.dict(os.environ, {"LANGFUSE_PUBLIC_KEY": "test_key"}, clear=True):
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
            assert len(hooks["Stop"]) == 1
            assert len(hooks["SubagentStop"]) == 1

    @pytest.mark.asyncio
    async def test_langfuse_hook_execution_success(self):
        """Test Langfuse hook executes successfully."""
        from services.sandbox_executor.sdk_executor import setup_langfuse_hooks

        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public",
                "LANGFUSE_SECRET_KEY": "test_secret",
                "CURRENT_SPAN_ID": "test_span",
            },
            clear=True,
        ):
            hooks = setup_langfuse_hooks()
            hook_fn = hooks["Stop"][0].hooks[0]

            # Mock subprocess
            mock_process = MagicMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"success", b""))

            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                result = await hook_fn({"test": "data"}, "tool_id", {})
                assert result == {"success": True}

    @pytest.mark.asyncio
    async def test_langfuse_hook_execution_failure(self):
        """Test Langfuse hook handles execution failure."""
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
            hook_fn = hooks["Stop"][0].hooks[0]

            # Mock subprocess failure
            mock_process = MagicMock()
            mock_process.returncode = 1
            mock_process.communicate = AsyncMock(return_value=(b"", b"error message"))

            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                result = await hook_fn({"test": "data"}, "tool_id", {})
                assert result["success"] is False

    @pytest.mark.asyncio
    async def test_langfuse_hook_timeout(self):
        """Test Langfuse hook handles timeout."""
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
            hook_fn = hooks["Stop"][0].hooks[0]

            # Mock subprocess timeout
            mock_process = MagicMock()
            mock_process.communicate = AsyncMock(side_effect=TimeoutError)
            mock_process.kill = MagicMock()
            mock_process.wait = AsyncMock()

            with patch("asyncio.create_subprocess_exec", return_value=mock_process):
                result = await hook_fn({"test": "data"}, "tool_id", {})
                assert result["success"] is False
                mock_process.kill.assert_called_once()


class TestExecuteSandboxRequest:
    """Test execute_sandbox_request function."""

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        """Test successful SDK execution."""
        # Mock message stream
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        from services.sandbox_executor.sdk_executor import execute_sandbox_request

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
                workspace="/tmp/test-workspace",
            )

            assert response == "Test response"
            mock_client.query.assert_called_once_with("Test prompt")

    @pytest.mark.asyncio
    async def test_multiple_text_blocks(self):
        """Test handling multiple text blocks in response."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        mock_client = MagicMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock()
        mock_client.query = AsyncMock()

        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        async def mock_receive():
            yield AssistantMessage(
                content=[TextBlock(text="Part 1")], model="claude-3-5-sonnet-20241022"
            )
            yield AssistantMessage(
                content=[TextBlock(text="Part 2")], model="claude-3-5-sonnet-20241022"
            )
            yield ResultMessage(
                subtype="success",
                duration_ms=2000,
                duration_api_ms=2000,
                is_error=False,
                num_turns=2,
                session_id="test",
                total_cost_usd=0.02,
            )

        mock_client.receive_messages = mock_receive

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
                workspace="/tmp/test-workspace",
            )

            assert response == "Part 1\nPart 2"

    @pytest.mark.asyncio
    async def test_empty_response_raises_exception(self):
        """Test empty response raises exception."""
        from claude_agent_sdk import ResultMessage

        from services.sandbox_executor.sdk_executor import execute_sandbox_request

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
            with pytest.raises(
                Exception, match="Claude Agent SDK returned empty response"
            ):
                await execute_sandbox_request(
                    prompt="Test prompt",
                    github_token="test_token",
                    repo="owner/repo",
                    issue_number=123,
                    user="testuser",
                    auto_review=False,
                    auto_triage=False,
                    workspace="/tmp/test-workspace",
                )

    @pytest.mark.asyncio
    async def test_timeout_raises_exception(self):
        """Test timeout raises exception."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        async def mock_receive():
            await asyncio.sleep(2000)  # Simulate long execution
            yield None

        mock_client = MagicMock()
        mock_client.query = AsyncMock()
        mock_client.receive_messages = mock_receive
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        # Mock asyncio.timeout to trigger immediately
        with patch(
            "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
            return_value=mock_client,
        ):
            with patch("asyncio.timeout") as mock_timeout:
                mock_timeout.side_effect = TimeoutError()

                with pytest.raises(Exception, match="timed out after 30 minutes"):
                    await execute_sandbox_request(
                        prompt="Test prompt",
                        github_token="test_token",
                        repo="owner/repo",
                        issue_number=123,
                        user="testuser",
                        auto_review=False,
                        auto_triage=False,
                        workspace="/tmp/test-workspace",
                    )

    @pytest.mark.asyncio
    async def test_sdk_exception_propagates(self):
        """Test SDK exceptions are properly propagated."""
        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        mock_client = MagicMock()
        mock_client.query = AsyncMock(side_effect=RuntimeError("SDK error"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        with patch(
            "services.sandbox_executor.sdk_executor.ClaudeSDKClient",
            return_value=mock_client,
        ):
            with pytest.raises(Exception, match="Failed to execute Claude Agent SDK"):
                await execute_sandbox_request(
                    prompt="Test prompt",
                    github_token="test_token",
                    repo="owner/repo",
                    issue_number=123,
                    user="testuser",
                    auto_review=False,
                    auto_triage=False,
                    workspace="/tmp/test-workspace",
                )

    @pytest.mark.asyncio
    async def test_mcp_server_configuration(self):
        """Test MCP server is configured correctly."""
        from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

        from services.sandbox_executor.sdk_executor import execute_sandbox_request

        async def mock_receive():
            yield AssistantMessage(
                content=[TextBlock(text="Response")], model="claude-3-5-sonnet-20241022"
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
            "services.sandbox_executor.sdk_executor.ClaudeSDKClient"
        ) as mock_sdk:
            mock_sdk.return_value = mock_client

            await execute_sandbox_request(
                prompt="Test prompt",
                github_token="test_github_token",
                repo="owner/repo",
                issue_number=123,
                user="testuser",
                auto_review=False,
                auto_triage=False,
                workspace="/tmp/test-workspace",
            )

            # Verify ClaudeAgentOptions was created with correct MCP config
            call_args = mock_sdk.call_args
            options = call_args.kwargs["options"]

            assert "github" in options.mcp_servers
            assert options.mcp_servers["github"]["type"] == "http"
            assert (
                "test_github_token"
                in options.mcp_servers["github"]["headers"]["Authorization"]
            )
