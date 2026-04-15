"""Shared utility functions."""

from typing import Any


def resolve_path(data: dict, path: str) -> Any:
    """Walk a dot-separated path through a nested dict.

    Args:
        data: The dict to traverse.
        path: Dot-separated path like "pull_request.user.login".

    Returns:
        The value at the path, or None if any segment is missing.
    """
    current: Any = data
    for key in path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return None
    return current
