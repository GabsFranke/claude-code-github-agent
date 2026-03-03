"""Unit tests for ObservabilityManager."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.agent_worker.processors.observability_manager import ObservabilityManager


class TestObservabilityManager:
    """Test ObservabilityManager class."""

    def test_initialization(self):
        """Test ObservabilityManager initialization."""
        manager = ObservabilityManager()
        assert manager is not None

    @patch.dict(os.environ, {}, clear=True)
    def test_setup_langfuse_hooks_not_configured(self):
        """Test Langfuse hooks when not configured."""
        manager = ObservabilityManager()
        hooks = manager.setup_langfuse_hooks()

        assert hooks == {}

    @patch.dict(
        os.environ,
        {"LANGFUSE_PUBLIC_KEY": "pk_test", "LANGFUSE_SECRET_KEY": "sk_test"},
    )
    def test_setup_langfuse_hooks_configured(self):
        """Test Langfuse hooks when configured."""
        manager = ObservabilityManager()
        hooks = manager.setup_langfuse_hooks()

        assert "Stop" in hooks
        assert "SubagentStop" in hooks
        assert len(hooks["Stop"]) > 0
        assert len(hooks["SubagentStop"]) > 0


class TestObservabilityManagerHooks:
    """Test ObservabilityManager hook functionality."""

    @pytest.mark.asyncio
    @patch.dict(
        os.environ,
        {
            "LANGFUSE_PUBLIC_KEY": "pk_test",
            "LANGFUSE_SECRET_KEY": "sk_test",
            "LANGFUSE_HOST": "http://localhost:3000",
        },
    )
    async def test_langfuse_hook_execution_success(self):
        """Test Langfuse hook execution with successful subprocess."""
        manager = ObservabilityManager()
        hooks = manager.setup_langfuse_hooks()

        # Get the hook function
        stop_hooks = hooks["Stop"]
        hook_matcher = stop_hooks[0]
        hook_func = hook_matcher.hooks[0]

        # Mock subprocess
        with patch("asyncio.create_subprocess_exec") as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.returncode = 0
            mock_process.communicate = AsyncMock(return_value=(b"Success", b""))
            mock_subprocess.return_value = mock_process

            input_data = {"test": "data"}
            result = await hook_func(input_data, "tool_id", {})

            assert result["success"] is True

    @pytest.mark.asyncio
    @patch.dict(
        os.environ,
        {
            "LANGFUSE_PUBLIC_KEY": "pk_test",
            "LANGFUSE_SECRET_KEY": "sk_test",
        },
    )
    async def test_langfuse_hook_execution_failure(self):
        """Test Langfuse hook execution with failed subprocess."""
        manager = ObservabilityManager()
        hooks = manager.setup_langfuse_hooks()

        stop_hooks = hooks["Stop"]
        hook_matcher = stop_hooks[0]
        hook_func = hook_matcher.hooks[0]

        with patch("asyncio.create_subprocess_exec") as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.returncode = 1
            mock_process.communicate = AsyncMock(return_value=(b"", b"Error occurred"))
            mock_subprocess.return_value = mock_process

            input_data = {"test": "data"}
            result = await hook_func(input_data, "tool_id", {})

            assert result["success"] is False
            assert "error" in result

    @pytest.mark.asyncio
    @patch.dict(
        os.environ,
        {
            "LANGFUSE_PUBLIC_KEY": "pk_test",
            "LANGFUSE_SECRET_KEY": "sk_test",
        },
    )
    async def test_langfuse_hook_execution_timeout(self):
        """Test Langfuse hook execution with timeout."""
        manager = ObservabilityManager()
        hooks = manager.setup_langfuse_hooks()

        stop_hooks = hooks["Stop"]
        hook_matcher = stop_hooks[0]
        hook_func = hook_matcher.hooks[0]

        with patch("asyncio.create_subprocess_exec") as mock_subprocess:
            mock_process = AsyncMock()
            mock_process.returncode = None
            mock_process.communicate = AsyncMock(side_effect=TimeoutError())
            mock_process.kill = MagicMock()
            mock_process.wait = AsyncMock()
            mock_subprocess.return_value = mock_process

            input_data = {"test": "data"}
            result = await hook_func(input_data, "tool_id", {})

            assert result["success"] is False
            assert "error" in result
            # Check for timeout in error message
            error_msg = str(result["error"])
            assert "timeout" in error_msg.lower() or "timed out" in error_msg.lower()
            mock_process.kill.assert_called_once()
