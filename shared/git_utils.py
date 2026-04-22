"""Git command execution utilities."""

import asyncio
import warnings


async def execute_git_command(
    cmd: str | list[str], cwd: str | None = None
) -> tuple[int, str, str]:
    """Execute a git command asynchronously.

    Args:
        cmd: Git command to execute (list preferred; string deprecated due to
             shell injection risk).
        cwd: Optional working directory

    Returns:
        Tuple of (return_code, stdout, stderr)

    Raises:
        RuntimeError: If git command fails to start or git is not installed
        ValueError: If command list doesn't start with 'git'
    """
    # Handle both string (legacy) and list (secure) formats
    if isinstance(cmd, list):
        # Validate that command starts with 'git'
        if not cmd or cmd[0] != "git":
            raise ValueError(f"Command must start with 'git', got: {cmd}")
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    else:
        # Legacy string format - shell injection risk, emit deprecation warning
        warnings.warn(
            "Passing a string to execute_git_command() is deprecated due to "
            "shell injection risk. Use list format instead: "
            '["git", "arg1", "arg2"]',
            DeprecationWarning,
            stacklevel=2,
        )
        if not cmd.strip().startswith("git ") and cmd.strip() != "git":
            raise ValueError(f"Command must start with 'git', got: {cmd}")
        process = await asyncio.create_subprocess_shell(
            cmd,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    stdout, stderr = await process.communicate()

    # Critical: Check if process failed to start (returncode is None)
    if process.returncode is None:
        raise RuntimeError(
            f"Git command failed to start: {cmd}. "
            f"Check that git is installed and working directory exists: {cwd}"
        )

    return process.returncode, stdout.decode().strip(), stderr.decode().strip()
