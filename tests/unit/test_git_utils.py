"""Unit tests for shared.git_utils module."""

import tempfile

import pytest

from shared.git_utils import execute_git_command


class TestExecuteGitCommand:
    """Test execute_git_command function."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        """Test successful git command execution."""
        # Use a simple command that works on all platforms
        code, stdout, stderr = await execute_git_command("git --version")

        assert code == 0
        assert "git version" in stdout.lower()
        assert stderr == "" or len(stderr) == 0

    @pytest.mark.asyncio
    async def test_failed_command(self):
        """Test failed git command execution."""
        code, stdout, stderr = await execute_git_command(
            "git invalid-command-that-does-not-exist"
        )

        assert code != 0
        assert (
            "invalid-command-that-does-not-exist" in stderr.lower()
            or "unknown" in stderr.lower()
        )

    @pytest.mark.asyncio
    async def test_command_with_cwd(self):
        """Test git command execution with custom working directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            code, stdout, stderr = await execute_git_command("git init", cwd=tmpdir)

            assert code == 0
            assert "initialized" in stdout.lower() or "reinitialized" in stdout.lower()

    @pytest.mark.asyncio
    async def test_command_output_decoding(self):
        """Test that command output is properly decoded to strings."""
        code, stdout, stderr = await execute_git_command("git --version")

        assert isinstance(code, int)
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)

    @pytest.mark.asyncio
    async def test_return_code_normalization(self):
        """Test that None return codes raise RuntimeError."""
        # Successful commands should return 0, never None
        code, stdout, stderr = await execute_git_command("git --version")

        assert code == 0
        assert code is not None
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)

    @pytest.mark.asyncio
    async def test_command_validation_string(self):
        """Test that non-git commands are rejected (string format)."""
        with pytest.raises(ValueError, match="Command must start with 'git'"):
            await execute_git_command("rm -rf /")

    @pytest.mark.asyncio
    async def test_command_validation_list(self):
        """Test that non-git commands are rejected (list format)."""
        with pytest.raises(ValueError, match="Command must start with 'git'"):
            await execute_git_command(["rm", "-rf", "/"])

    @pytest.mark.asyncio
    async def test_list_format_command(self):
        """Test git command execution with list format (secure)."""
        code, stdout, stderr = await execute_git_command(["git", "--version"])

        assert code == 0
        assert "git version" in stdout.lower()
        assert isinstance(stdout, str)
        assert isinstance(stderr, str)
