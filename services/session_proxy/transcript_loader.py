"""Transcript loader — converts SDK JSONL transcripts to browser WSMessage format.

Reads the Claude Agent SDK transcript files from the shared ~/.claude volume
and converts each entry into the same JSON format that the browser renders
via WebSocket.

The SDK writes JSONL to:
    ~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl

Each line is a JSON object with:
    - type: "user" | "assistant" | "progress" | "system" | "attachment"
    - message.role: "user" | "assistant"
    - message.content: str or [content_blocks...]
"""

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shared.constants import sanitize_repo_key

logger = logging.getLogger(__name__)

CLAUDE_HOME = Path(os.getenv("CLAUDE_HOME", str(Path.home() / ".claude")))
PROJECTS_DIR = CLAUDE_HOME / "projects"

# Session ID validation to prevent path traversal
_SESSION_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_session_id(session_id: str) -> None:
    """Validate session_id to prevent path traversal."""
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"Invalid session_id format: {session_id!r}")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def find_transcript(session_id: str) -> Path | None:
    """Locate the transcript JSONL file for a given session_id.

    Tries the direct path first (if we know the workspace),
    then falls back to scanning all project directories.
    """
    _validate_session_id(session_id)

    if not PROJECTS_DIR.exists():
        return None

    # Scan project directories for the session file
    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return candidate

    return None


def find_transcript_by_repo(repo: str, issue_number: int, workflow: str) -> Path | None:
    """Find the most recent transcript file matching a repo/issue/workflow.

    Scans project directories whose name contains the repo (owner/name with
    / replaced by --), then checks the first line of each JSONL file for
    a matching issue number and workflow.

    Returns the most recently modified matching transcript, or None.
    """
    if not PROJECTS_DIR.exists():
        return None

    # The SDK sanitizes CWD paths: / → -, special chars removed.
    # Repo "GabsFranke/sma" becomes "GabsFranke--sma" in the project dir name.
    repo_segment = sanitize_repo_key(repo)

    candidates: list[tuple[float, Path]] = []

    for project_dir in PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        # Project dirs contain the repo name as a path segment
        if repo_segment not in project_dir.name:
            continue

        # Check each JSONL file in matching project dirs
        for jsonl_file in project_dir.glob("*.jsonl"):
            if _transcript_matches(jsonl_file, issue_number, workflow):
                candidates.append((jsonl_file.stat().st_mtime, jsonl_file))

    if not candidates:
        return None

    # Return the most recently modified transcript
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _transcript_matches(path: Path, issue_number: int, workflow: str) -> bool:
    """Check if a transcript file is for the given issue number and workflow.

    Reads only the first few lines looking for a user message that references
    the issue number. Falls back to a quick check of the file path or
    project dir name if no match is found in the first lines.
    """
    issue_str = f"#{issue_number}"
    try:
        with open(path, encoding="utf-8") as f:
            for i, raw_line in enumerate(f):
                if i >= 30:  # Only scan first 30 lines
                    break
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Check user messages for issue number reference
                if entry.get("type") == "user":
                    content = entry.get("message", {}).get("content", "")
                    if isinstance(content, str) and issue_str in content:
                        return True
                    if isinstance(content, list):
                        for block in content:
                            if (
                                isinstance(block, dict)
                                and block.get("type") == "text"
                                and issue_str in block.get("text", "")
                            ):
                                return True
    except Exception as e:
        logger.debug(f"Error scanning transcript {path.name}: {e}")

    return False


def find_all_transcripts(session_ids: list[str]) -> list[Path]:
    """Find transcript files for multiple session IDs (for multi-run history)."""
    found = []
    for sid in session_ids:
        _validate_session_id(sid)
        path = find_transcript(sid)
        if path:
            found.append(path)
    return found


def load_transcript_meta(transcript_path: Path) -> dict[str, Any]:
    """Load sidecar metadata from a .meta.json file next to a transcript.

    Returns an empty dict if the metadata file doesn't exist or can't be read.
    The metadata file is written by sandbox_worker when a session completes
    and contains installation_id, ref, user, thread_type, and conversation_config
    needed for re-invoke after Redis expires.
    """
    meta_path = transcript_path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        result: dict[str, Any] = json.loads(meta_path.read_text(encoding="utf-8"))
        return result
    except Exception as e:
        logger.warning(f"Failed to read transcript metadata from {meta_path}: {e}")
        return {}


def load_transcript_history(transcript_path: Path) -> list[dict]:
    """Parse a transcript JSONL file and return WSMessage-format dicts.

    Converts SDK transcript entries to the same format that the browser
    renders, so loading from a transcript produces identical output to
    receiving live WebSocket messages.

    Returns:
        List of WSMessage dicts (json-serializable), oldest first.
    """
    messages: list[dict] = []

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw_line in f:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    entry = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")

                # Skip internal entries
                if entry_type in ("queue-operation", "progress", "system"):
                    continue

                msg = entry.get("message", {})
                role = msg.get("role") or entry_type
                content = msg.get("content", "")

                if role == "user":
                    text = _extract_user_text(content)
                    if text:
                        messages.append(
                            {
                                "type": "user_message",
                                "data": {"content": text},
                                "ts": _now_iso(),
                            }
                        )

                elif role == "assistant":
                    blocks = _extract_assistant_blocks(content)
                    if blocks:
                        messages.append(
                            {
                                "type": "assistant_message",
                                "data": {"content": blocks},
                                "ts": _now_iso(),
                            }
                        )

    except Exception as e:
        logger.warning(f"Failed to load transcript {transcript_path}: {e}")

    return messages


def _extract_user_text(content: str | list) -> str:
    """Extract displayable text from a user message content field."""
    if isinstance(content, str):
        return content
    # content is list (str already handled above)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text", ""))
    return " ".join(parts)


def _extract_assistant_blocks(
    content: str | list,
) -> list[dict]:
    """Extract content blocks from an assistant message for browser rendering."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    # content is list (str already handled above)
    blocks = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": block.get("text", "")})
        elif btype == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "name": block.get("name", ""),
                    "input": block.get("input", {}),
                }
            )
        elif btype == "thinking":
            blocks.append({"type": "thinking"})
    return blocks
