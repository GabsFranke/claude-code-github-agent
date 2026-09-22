"""Transcript fallback for session resolution when Redis sessions expire.

Provides filesystem-based transcript lookup and loading for the
pseudo-token mechanism (``transcript:{session_id}``).  This module is
used by the session service API to:

1. Find transcript files on disk when no Redis session exists
2. Load transcript content for pseudo-token resolution

Cache: transcript lookups are cached for 60 seconds to avoid
repeated filesystem scans.

Ported from ``services/session_proxy/transcript_loader.py`` (do NOT
import from that module — this is a standalone port).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from shared.constants import sanitize_repo_key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths (overridable via env for tests)
# ---------------------------------------------------------------------------

CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude")))
PROJECTS_DIR = CLAUDE_HOME / "projects"

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_session_id(session_id: str) -> None:
    """Validate *session_id* to prevent path traversal attacks."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id format: {session_id!r}")


# ---------------------------------------------------------------------------
# Simple TTL-based in-memory cache
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Path | None]] = {}
_CACHE_TTL = 60.0  # seconds


def _cache_key(repo: str, thread_type: str, thread_id: str, workflow: str) -> str:
    """Build a deterministic cache key from session identifiers."""
    return f"{repo}:{thread_type}:{thread_id}:{workflow}"


def _cached_get(key: str) -> Path | None:
    """Return a cached transcript path if it is still valid, otherwise ``None``."""
    entry = _cache.get(key)
    if entry is None:
        return None
    ts, value = entry
    if time.monotonic() - ts > _CACHE_TTL:
        del _cache[key]
        return None
    return value  # type: ignore[return-value]


def _cached_set(key: str, value: Any) -> None:
    """Store *value* in the cache with the current timestamp."""
    _cache[key] = (time.monotonic(), value)


# ---------------------------------------------------------------------------
# Transcript lookup
# ---------------------------------------------------------------------------


def find_transcript(
    repo: str, thread_type: str, thread_id: str, workflow: str
) -> Path | None:
    """Find the most recent transcript file for a session context.

    Scans ``~/.claude/projects/`` for directories matching the repo +
    issue + workflow pattern, then returns the most recently modified
    JSONL file.  Results are cached for 60 s.

    Args:
        repo: GitHub repository (``owner/repo``).
        thread_type: Thread type (``pr``, ``issue``, ``discussion``).
        thread_id: Thread number.
        workflow: Workflow name.

    Returns:
        Path to the transcript ``.jsonl`` file, or ``None``.
    """
    if not PROJECTS_DIR.exists():
        return None

    ckey = _cache_key(repo, thread_type, thread_id, workflow)
    cached = _cached_get(ckey)
    if cached is not None:
        logger.debug("[transcript] Cache hit for %s", ckey)
        return cached

    result = find_transcript_by_repo(repo, thread_type, thread_id, workflow)
    _cached_set(ckey, result)
    return result


def find_transcript_by_repo(
    repo: str, thread_type: str, thread_id: str, workflow: str
) -> Path | None:
    """Find the most recent transcript by scanning project directories.

    Searches ``PROJECTS_DIR`` for directories whose name contains
    the sanitized repo key, then checks directory names for the
    issue number and workflow.  Falls back to scanning the first
    30 lines of each ``.jsonl`` file for legacy session formats.

    Args:
        repo: GitHub repository (``owner/repo``).
        thread_type: Thread type.
        thread_id: Thread number.
        workflow: Workflow name.

    Returns:
        The most recently modified matching transcript, or ``None``.
    """
    if not PROJECTS_DIR.exists():
        return None

    repo_segment = sanitize_repo_key(repo)
    issue_str = str(thread_id)

    candidates: list[tuple[float, Path]] = []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        # Project directory must contain the repo name segment
        if repo_segment not in project_dir.name:
            continue

        dir_name = project_dir.name

        # Dir must encode the workflow
        if workflow not in dir_name:
            continue

        # Match by directory name encoding
        if _dir_encodes_issue(dir_name, issue_str):
            for jsonl_file in project_dir.glob("*.jsonl"):
                candidates.append((jsonl_file.stat().st_mtime, jsonl_file))
            continue

        # Legacy: scan file contents for issue reference
        ref = f"#{issue_str}"
        for jsonl_file in project_dir.glob("*.jsonl"):
            if _transcript_contains_ref(jsonl_file, ref):
                candidates.append((jsonl_file.stat().st_mtime, jsonl_file))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dir_encodes_issue(dir_name: str, issue_str: str) -> bool:
    """Return ``True`` if *dir_name* encodes the given issue number.

    The SDK sanitises worktree paths into project directory names.
    For example ``...-issue-5-ralph`` encodes both ``issue-5`` and
    ``ralph``.
    """
    return f"issue-{issue_str}" in dir_name or f"-{issue_str}-" in dir_name


def _transcript_contains_ref(path: Path, ref: str) -> bool:
    """Check whether a transcript file's user message contains *ref*.

    Scans at most the first 30 lines to avoid I/O for large files.
    """
    try:
        with open(path, encoding="utf-8") as f:
            for i, raw_line in enumerate(f):
                if i >= 30:
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if entry.get("type") != "user":
                    continue

                content = entry.get("message", {}).get("content", "")
                if isinstance(content, str) and ref in content:
                    return True
                if isinstance(content, list):
                    for block in content:
                        if (
                            isinstance(block, dict)
                            and block.get("type") == "text"
                            and ref in block.get("text", "")
                        ):
                            return True
    except Exception as e:
        logger.debug("Error scanning transcript %s: %s", path.name, e)
    return False


# ---------------------------------------------------------------------------
# Transcript loading
# ---------------------------------------------------------------------------


def load_transcript_history(transcript_path: Path) -> list[dict]:
    """Parse a JSONL transcript file and return raw message entries.

    Each line is parsed as JSON.  Blank lines are skipped silently.
    Malformed JSON lines are logged as warnings and skipped — the
    remaining valid lines are still returned.

    Args:
        transcript_path: Path to the transcript JSONL file.

    Returns:
        List of parsed JSON entries (dicts), in file order.
        Returns an empty list if the file is missing or unreadable.
    """
    messages: list[dict] = []

    if not transcript_path.exists():
        logger.warning("[transcript] File not found: %s", transcript_path)
        return messages

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    messages.append(entry)
                except json.JSONDecodeError:
                    logger.warning(
                        "[transcript] Skipping malformed JSONL line %d in %s",
                        line_num,
                        transcript_path.name,
                    )
                    continue
    except OSError as e:
        logger.warning("[transcript] Failed to read %s: %s", transcript_path, e)
        return []

    return messages


# ---------------------------------------------------------------------------
# Pseudo-token
# ---------------------------------------------------------------------------


def build_pseudo_token(session_id: str) -> str:
    """Build a pseudo-token for transcript-only sessions.

    Format: ``transcript:{session_id}``

    Used by the SPA to indicate history should be loaded from a
    transcript file rather than Redis.

    Args:
        session_id: The SDK session identifier (validated).

    Returns:
        A string like ``transcript:ses_abc123``.

    Raises:
        ValueError: If *session_id* contains invalid characters.
    """
    _validate_session_id(session_id)
    return f"transcript:{session_id}"
