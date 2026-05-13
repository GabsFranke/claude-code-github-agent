import json
import logging
import re
from pathlib import Path

from shared.sdk_factory import SDKOptionsBuilder
from subagents import AGENTS

logger = logging.getLogger(__name__)


def write_transcript_meta(transcript_path: str, meta: dict) -> None:
    """Write a sidecar .meta.json file alongside a transcript JSONL.

    Persists session metadata (installation_id, ref, etc.) so that
    re-invoke works even after the Redis session expires.
    """
    try:
        meta_path = Path(transcript_path).with_suffix(".meta.json")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        logger.debug(f"Wrote transcript metadata to {meta_path}")
    except Exception as e:
        logger.warning(f"Failed to write transcript metadata: {e}")


def find_transcript_path(session_id: str, cwd: str) -> str | None:
    """Locate the SDK transcript JSONL file for a given session.

    The SDK writes transcripts to ~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl
    where <sanitized-cwd> replaces non-alphanumeric chars with '-'.

    If the exact path doesn't exist, scans all project dirs for the session_id.
    """
    # Validate session_id to prevent path traversal
    if not re.match(r"^[a-zA-Z0-9_-]+$", session_id):
        logger.warning(f"Invalid session_id format: {session_id}")  # type: ignore[unreachable]
        return None

    claude_home = Path.home() / ".claude"
    projects_dir = claude_home / "projects"

    if not projects_dir.exists():
        return None

    # Try direct path from sanitized cwd first
    if cwd:
        sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        direct = projects_dir / sanitized / f"{session_id}.jsonl"
        if direct.exists():
            return str(direct)

    # Fallback: limited directory scan (max 200 dirs to avoid DoS)
    count = 0
    for project_dir in projects_dir.iterdir():
        if count >= 200:
            logger.warning(f"Transcript scan exceeded limit for {session_id}")
            break
        count += 1
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.exists():
            return str(candidate)

    return None


def configure_builder(
    builder: SDKOptionsBuilder,
    *,
    repo: str,
    workflow_name: str,
    ref: str,
    parent_span_id: str | None,
    system_context: str | None,
    claude_md: str | None,
    memory_index: str | None,
    thread_history_text: str,
    file_tree_text: str,
) -> SDKOptionsBuilder:
    """Apply common sandbox-specific configuration to an SDKOptionsBuilder.

    Called both for the initial SDK invocation and for auto-continue
    rebuilds, ensuring both paths stay in sync.
    """
    return (
        builder.with_auto_discovered_plugins()
        .with_full_toolset()
        .with_agents(AGENTS)
        .with_langfuse_hooks(parent_span_id=parent_span_id)
        .with_transcript_staging(repo, workflow_name, ref=ref)
        .with_writable_dir(str(Path.home() / ".claude" / "memory" / repo / "memory"))
        .with_system_prompt(system_context)
        .with_repository_context(claude_md=claude_md, memory_index=memory_index)
        .with_thread_history(thread_history_text)
        .with_structural_context(file_tree=file_tree_text)
    )
