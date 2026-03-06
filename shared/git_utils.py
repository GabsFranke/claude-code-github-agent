"""Git command execution utilities."""

import asyncio


async def execute_git_command(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Execute a git command asynchronously.

    Args:
        cmd: Git command to execute
        cwd: Optional working directory

    Returns:
        Tuple of (return_code, stdout, stderr)
    """
    process = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()
