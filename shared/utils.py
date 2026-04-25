"""Shared utility functions."""

from typing import Any

# ─── Thread type / URL segment mapping ──────────────────────────────────────

_THREAD_TYPE_TO_URL = {"pr": "pull", "issue": "issues", "discussion": "discussions"}
_URL_SEGMENT_TO_THREAD_TYPE = {
    "pull": "pr",
    "issues": "issue",
    "discussions": "discussion",
}


def thread_type_to_url_segment(thread_type: str) -> str:
    """Map internal thread_type to URL-friendly segment.

    "pr" → "pull", "issue" → "issues", "discussion" → "discussions".
    Defaults to "issues" for unknown types.
    """
    return _THREAD_TYPE_TO_URL.get(thread_type, "issues")


def url_segment_to_thread_type(segment: str) -> str:
    """Map URL segment back to internal thread_type.

    "pull" → "pr", "issues" → "issue", "discussions" → "discussion".
    Defaults to "issue" for unknown segments.
    """
    return _URL_SEGMENT_TO_THREAD_TYPE.get(segment, "issue")


def build_session_url(
    base_url: str,
    owner: str,
    repo_name: str,
    thread_type: str,
    issue_number: int,
    workflow: str,
) -> str:
    """Build the full human-readable session proxy URL.

    Args:
        base_url: The SESSION_PROXY_URL base (e.g., "http://localhost:10001").
        owner: Repository owner (e.g., "GabsFranke").
        repo_name: Repository name without the owner (e.g., "sma").
        thread_type: Internal thread type ("pr", "issue", "discussion").
        issue_number: GitHub issue/PR number.
        workflow: Workflow name (e.g., "triage-issue").

    Returns:
        Full session URL, e.g.,
        "http://localhost:10001/session/GabsFranke/sma/issues/84/triage-issue".
        Returns "" if base_url is empty.
    """
    if not base_url:
        return ""
    type_segment = thread_type_to_url_segment(thread_type)
    return (
        f"{base_url.rstrip('/')}/session/"
        f"{owner}/{repo_name}/{type_segment}/{issue_number}/{workflow}"
    )


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
