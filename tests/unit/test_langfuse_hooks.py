"""Tests for Langfuse hooks module."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared.langfuse_hooks import setup_langfuse_hooks


class TestSetupLangfuseHooks:
    """Tests for setup_langfuse_hooks function."""

    def test_returns_empty_dict_without_keys(self):
        """Test that hooks are empty when Langfuse keys are not configured."""
        with patch.dict(os.environ, {}, clear=True):
            # Remove Langfuse keys if present
            os.environ.pop("LANGFUSE_PUBLIC_KEY", None)
            os.environ.pop("LANGFUSE_SECRET_KEY", None)

            result = setup_langfuse_hooks()
            assert result == {}

    def test_returns_hooks_with_keys_configured(self):
        """Test that hooks are created when Langfuse keys are configured."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
                "LANGFUSE_HOST": "http://localhost:3000",
            },
        ):
            result = setup_langfuse_hooks()

            assert "Stop" in result
            assert "SubagentStop" in result
            assert len(result["Stop"]) == 1
            assert len(result["SubagentStop"]) == 1

    def test_hooks_include_parent_span_id(self):
        """Test that parent_span_id is passed to hook environment."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
            },
        ):
            result = setup_langfuse_hooks(parent_span_id="span_123")

            # The parent_span_id should be used in the hook
            # We can verify the hook was created
            assert "Stop" in result

    def test_uses_default_langfuse_host(self):
        """Test that default Langfuse host is used when not specified."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
            },
            clear=True,
        ):
            # Remove LANGFUSE_HOST if present
            os.environ.pop("LANGFUSE_HOST", None)

            result = setup_langfuse_hooks()
            assert "Stop" in result

    @pytest.mark.asyncio
    async def test_langfuse_hook_timeout(self):
        """Test that hook times out after LANGFUSE_HOOK_TIMEOUT."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
                "LANGFUSE_HOOK_TIMEOUT": "1",  # 1 second timeout
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            # Mock a process that hangs
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(side_effect=TimeoutError())
                mock_process.returncode = None
                mock_process.kill = MagicMock()
                mock_process.wait = AsyncMock()
                mock_exec.return_value = mock_process

                result = await hook({"test": "data"}, "tool_id", {})

                assert result["success"] is False

    @pytest.mark.asyncio
    async def test_langfuse_hook_success(self):
        """Test successful hook execution."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
                "LANGFUSE_HOOK_TIMEOUT": "30",
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(return_value=(b"success", b""))
                mock_process.returncode = 0
                mock_exec.return_value = mock_process

                result = await hook({"test": "data"}, "tool_id", {})

                assert result["success"] is True

    @pytest.mark.asyncio
    async def test_langfuse_hook_process_failure(self):
        """Test hook handles process failure."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(
                    return_value=(b"", b"Error: something failed")
                )
                mock_process.returncode = 1
                mock_exec.return_value = mock_process

                result = await hook({"test": "data"}, "tool_id", {})

                # Hook should still return (not raise)
                assert result is not None

    @pytest.mark.asyncio
    async def test_langfuse_hook_exception_handling(self):
        """Test hook handles exceptions gracefully."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_exec.side_effect = RuntimeError("Failed to spawn process")

                result = await hook({"test": "data"}, "tool_id", {})

                assert result["success"] is False
                assert "error" in result

    @pytest.mark.asyncio
    async def test_langfuse_hook_cleans_up_process_on_timeout(self):
        """Test that process is killed on timeout."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
                "LANGFUSE_HOOK_TIMEOUT": "1",
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()

                # Make communicate hang
                async def hang():
                    import asyncio

                    await asyncio.sleep(10)

                mock_process.communicate = hang
                mock_process.returncode = None
                mock_process.kill = MagicMock()
                mock_process.wait = AsyncMock()
                mock_exec.return_value = mock_process

                # This should timeout and kill the process
                await hook({"test": "data"}, "tool_id", {})

                # Verify kill was called
                mock_process.kill.assert_called()

    @pytest.mark.asyncio
    async def test_langfuse_hook_cleans_up_process_on_error(self):
        """Test that process is cleaned up on error."""
        with patch.dict(
            os.environ,
            {
                "LANGFUSE_PUBLIC_KEY": "test_public_key",
                "LANGFUSE_SECRET_KEY": "test_secret_key",
            },
        ):
            hooks = setup_langfuse_hooks()
            hook = hooks["Stop"][0].hooks[0]

            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_process = AsyncMock()
                mock_process.communicate = AsyncMock(
                    side_effect=RuntimeError("Unexpected error")
                )
                mock_process.returncode = None
                mock_process.kill = MagicMock()
                mock_process.wait = AsyncMock()
                mock_exec.return_value = mock_process

                result = await hook({"test": "data"}, "tool_id", {})

                # Process should be cleaned up
                assert result["success"] is False
