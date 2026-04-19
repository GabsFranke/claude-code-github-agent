"""Shared utility functions."""

from typing import Any

# Sentinel object used by resolve_path to distinguish "field is None" from
# "field is missing".  Callers can test ``result is _MISSING`` to detect
# absent fields vs. fields that are explicitly null in the payload.
_MISSING = object()


def resolve_path(data: dict, path: str, default: Any = None) -> Any:
    """Walk a dot-separated path through a nested dict.

    Args:
        data: The dict to traverse.
        path: Dot-separated path like "pull_request.user.login".
        default: Value to return when any segment is missing (defaults to None).
            Use ``_MISSING`` as the default if you need to distinguish between
            an explicitly-null field and a missing field::

                val = resolve_path(payload, "some.path", default=_MISSING)
                if val is _MISSING:
                    # field is absent from the payload
                elif val is None:
                    # field exists but is null

    Returns:
        The value at the path, or *default* if any segment is missing.
    """
    current: Any = data
    for key in path.split("."):
        if isinstance(current, dict):
            if key not in current:
                return default
            current = current[key]
        else:
            return default
    return current
