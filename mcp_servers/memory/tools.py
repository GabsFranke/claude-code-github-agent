"""Memory tools implementation."""

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _get_memory_dir(repo: str) -> Path:
    """Get the memory directory for a repository."""
    return Path(f"/home/bot/.claude/memory/{repo}/memory")


def _validate_path(full_path: Path, base_dir: Path) -> None:
    """Validate that path doesn't escape the memory directory.

    Args:
        full_path: The resolved full path to validate
        base_dir: The base directory that path must be within

    Raises:
        ValueError: If path escapes the base directory
    """
    try:
        full_path.resolve().relative_to(base_dir.resolve())
    except ValueError as e:
        raise ValueError(
            f"Invalid path: outside memory directory ({full_path} not in {base_dir})"
        ) from e


def _list_files(directory: Path, prefix: str = "") -> list[str]:
    """Recursively list all files in a directory.

    Args:
        directory: Directory to list
        prefix: Path prefix for recursive calls

    Returns:
        List of relative file paths
    """
    files = []
    try:
        for item in sorted(directory.iterdir()):
            relative_path = f"{prefix}{item.name}"
            if item.is_file():
                files.append(relative_path)
            elif item.is_dir():
                files.extend(_list_files(item, f"{relative_path}/"))
    except Exception as e:
        logger.warning(f"Failed to list directory {directory}: {e}")
    return files


def memory_read(file_path: str | None, repo: str) -> dict[str, Any]:
    """Read memory files from repository memory directory.

    Args:
        file_path: Optional path to specific file (relative to memory root).
                  If None, lists all files.
        repo: Repository name (e.g., "owner/repo")

    Returns:
        Dict with either 'files' (list) or 'content' (str)

    Raises:
        ValueError: If path is invalid or escapes memory directory
        FileNotFoundError: If file doesn't exist
    """
    memory_dir = _get_memory_dir(repo)

    # List all files if no path provided
    if file_path is None:
        if not memory_dir.exists():
            return {"files": []}
        files = _list_files(memory_dir)
        logger.info(f"Listed {len(files)} memory files for {repo}")
        return {"files": files}

    # Read specific file
    full_path = (memory_dir / file_path).resolve()
    _validate_path(full_path, memory_dir)

    if not full_path.exists():
        raise FileNotFoundError(f"Memory file not found: {file_path}")

    if not full_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    content = full_path.read_text(encoding="utf-8")
    logger.info(f"Read memory file {file_path} for {repo} ({len(content)} chars)")
    return {"content": content}


def memory_write(file_path: str, content: str, repo: str) -> dict[str, Any]:
    """Write content to a memory file.

    Args:
        file_path: Path to file (relative to memory root)
        content: Content to write
        repo: Repository name (e.g., "owner/repo")

    Returns:
        Dict with success status and file path

    Raises:
        ValueError: If path is invalid or escapes memory directory
    """
    memory_dir = _get_memory_dir(repo)
    full_path = (memory_dir / file_path).resolve()
    _validate_path(full_path, memory_dir)

    # Create parent directories
    full_path.parent.mkdir(parents=True, exist_ok=True)

    # Write content
    full_path.write_text(content, encoding="utf-8")

    logger.info(f"Wrote memory file {file_path} for {repo} ({len(content)} chars)")
    return {"success": True, "path": file_path, "size": len(content)}
