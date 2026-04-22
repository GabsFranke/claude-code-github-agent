"""Tests for SDK executor module.

These tests focus on the error handling and control flow of execute_sdk,
mocking the SDK client to avoid actual API calls.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.exceptions import SDKError, SDKTimeoutError
from shared.sdk_executor import execute_sdk


def _mock_options(tmp_path):
    """Create a mock ClaudeAgentOptions."""
    options = MagicMock()
    options.model = "test-model"
    options.cwd = str(tmp_path)
    options.setting_sources = ["user", "project", "local"]
    options.allowed_tools = []
    return options


class TestExecuteSDK:
    """Tests for execute_sdk function."""

    @pytest.fixture
    def mock_options(self, tmp_path):
        return _mock_options(tmp_path)

    @pytest.mark.asyncio
    async def test_execute_sdk_exception_raises_sdk_error(self, mock_options):
        """Test that SDK exceptions are wrapped in SDKError."""
        with patch("shared.sdk_executor.ClaudeSDKClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(side_effect=RuntimeError("SDK failed"))
            mock_client_class.return_value = mock_client

            with pytest.raises(SDKError, match="SDK execution failed"):
                await execute_sdk(
                    prompt="test",
                    options=mock_options,
                )

    @pytest.mark.asyncio
    async def test_execute_sdk_timeout_raises_error(self, mock_options):
        """Test that timeout raises SDKTimeoutError."""
        with patch("shared.sdk_executor.ClaudeSDKClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.query = AsyncMock()

            # Create an async generator that hangs
            async def hang_forever():
                await asyncio.sleep(100)  # This will cause timeout
                yield None

            mock_client.receive_messages.return_value = hang_forever()
            mock_client_class.return_value = mock_client

            with pytest.raises(SDKTimeoutError, match="timed out"):
                await execute_sdk(
                    prompt="test",
                    options=mock_options,
                    timeout=1,  # 1 second timeout
                )

    @pytest.mark.asyncio
    async def test_execute_sdk_empty_response_raises_error(self, mock_options):
        """Test that empty response raises SDKError when collect_text=True."""
        # Mock the message types
        mock_assistant_msg = MagicMock()
        mock_assistant_msg.content = []  # Empty content

        mock_result_msg = MagicMock()
        mock_result_msg.num_turns = 1
        mock_result_msg.duration_ms = 100
        mock_result_msg.is_error = False

        with patch("shared.sdk_executor.ClaudeSDKClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.query = AsyncMock()

            async def mock_messages():
                yield mock_assistant_msg
                yield mock_result_msg

            mock_client.receive_messages.return_value = mock_messages()
            mock_client_class.return_value = mock_client

            with pytest.raises(SDKError, match="empty response"):
                await execute_sdk(
                    prompt="test",
                    options=mock_options,
                    collect_text=True,
                )

    @pytest.mark.asyncio
    async def test_execute_sdk_multiple_text_blocks(self, mock_options):
        """Test SDK execution with multiple text blocks."""
        # Mock multiple text blocks
        mock_text_block1 = MagicMock()
        mock_text_block1.text = "First part"
        mock_text_block2 = MagicMock()
        mock_text_block2.text = "Second part"

        # Mock the assistant message
        mock_assistant_msg = MagicMock()
        mock_assistant_msg.content = [mock_text_block1, mock_text_block2]

        # Mock the result message
        mock_result_msg = MagicMock()
        mock_result_msg.num_turns = 1
        mock_result_msg.duration_ms = 200
        mock_result_msg.is_error = False

        with patch("shared.sdk_executor.ClaudeSDKClient") as mock_client_class:
            mock_client = MagicMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.query = AsyncMock()

            async def mock_messages():
                yield mock_assistant_msg
                yield mock_result_msg

            mock_client.receive_messages.return_value = mock_messages()
            mock_client_class.return_value = mock_client

            result = await execute_sdk(
                prompt="test",
                options=mock_options,
                collect_text=True,
            )

            assert "First part" in result["response"]
            assert "Second part" in result["response"]


class TestExecuteSDKRetry:
    """Tests for execute_sdk retry logic."""

    @pytest.fixture
    def mock_options(self, tmp_path):
        return _mock_options(tmp_path)

    @pytest.mark.asyncio
    async def test_success_on_first_attempt(self, mock_options):
        """Test successful execution on first attempt with retry enabled."""
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            mock_execute.return_value = {
                "response": "success",
                "num_turns": 1,
                "duration_ms": 100,
                "is_error": False,
                "messages": [],
            }

            result = await execute_sdk(
                prompt="test",
                options=mock_options,
                max_retries=3,
            )

            assert result["response"] == "success"
            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_retries_on_failure(self, mock_options):
        """Test that execution retries on failure with exponential backoff."""
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            # Fail twice, then succeed
            mock_execute.side_effect = [
                RuntimeError("First failure"),
                RuntimeError("Second failure"),
                {
                    "response": "success",
                    "num_turns": 1,
                    "duration_ms": 100,
                    "is_error": False,
                    "messages": [],
                },
            ]

            with patch("shared.sdk_executor.asyncio.sleep") as mock_sleep:
                result = await execute_sdk(
                    prompt="test",
                    options=mock_options,
                    max_retries=3,
                    retry_base_delay=5.0,
                )

                assert result["response"] == "success"
                assert mock_execute.call_count == 3

                # Verify exponential backoff: 5s, 15s (3^0 * 5, 3^1 * 5)
                assert mock_sleep.call_count == 2
                mock_sleep.assert_any_call(5.0)  # First retry: 5s
                mock_sleep.assert_any_call(15.0)  # Second retry: 15s

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self, mock_options):
        """Test that exception is raised after max retries exhausted."""
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            mock_execute.side_effect = RuntimeError("Always fails")

            with patch("shared.sdk_executor.asyncio.sleep"):
                with pytest.raises(RuntimeError, match="Always fails"):
                    await execute_sdk(
                        prompt="test",
                        options=mock_options,
                        max_retries=2,
                    )

                assert mock_execute.call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_when_max_retries_is_one(self, mock_options):
        """Test that no retry happens when max_retries=1 (default)."""
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            mock_execute.side_effect = RuntimeError("Immediate failure")

            with pytest.raises(RuntimeError, match="Immediate failure"):
                await execute_sdk(
                    prompt="test",
                    options=mock_options,
                    max_retries=1,  # Default - no retry
                )

            # Should only be called once
            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_timeout_never_retried(self, mock_options):
        """Test that SDKTimeoutError is not retried even with max_retries > 1.

        Retrying a timeout would just re-run the full session up to the same
        wall — wasting up to (max_retries × timeout_seconds) before failing.
        """
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            mock_execute.side_effect = SDKTimeoutError("timed out after 1800s")

            with pytest.raises(SDKTimeoutError):
                await execute_sdk(
                    prompt="test",
                    options=mock_options,
                    max_retries=3,  # Would normally allow 3 attempts
                )

            # Must only be called once — timeout is not retryable
            mock_execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_exponential_backoff_calculation(self, mock_options):
        """Test that exponential backoff uses correct formula (base * 3^attempt)."""
        with patch("shared.sdk_executor._execute_sdk_once") as mock_execute:
            mock_execute.side_effect = [
                RuntimeError("Fail 1"),
                RuntimeError("Fail 2"),
                RuntimeError("Fail 3"),
            ]

            with patch("shared.sdk_executor.asyncio.sleep") as mock_sleep:
                with pytest.raises(RuntimeError):
                    await execute_sdk(
                        prompt="test",
                        options=mock_options,
                        max_retries=3,
                        retry_base_delay=5.0,
                    )

                # Verify delays: 5s (3^0 * 5), 15s (3^1 * 5)
                assert mock_sleep.call_count == 2
                calls = [call[0][0] for call in mock_sleep.call_args_list]
                assert calls == [5.0, 15.0]
