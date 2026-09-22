"""Incremental transcript writing hook for durable SDK message persistence.

Task 23: Core durability fix for Bug #1 (in-flight message loss on restart).

Provides IncrementalTranscriptHook that persists each SDK message to a JSONL
file immediately using O_APPEND | O_DSYNC crash-safe writes. If the process
is killed mid-write, all messages up to the crash point remain recoverable.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class IncrementalTranscriptHook:
    """Persists SDK messages to a durable JSONL file incrementally.

    Writes each message immediately using O_APPEND | O_DSYNC so that even
    if the process is killed, all messages up to the crash point are
    recoverable. Malformed last-line (from partial write during crash) is
    gracefully skipped on load.

    Supports two modes of transcript path resolution:
      1. Explicit: pass ``transcript_path`` to ``__init__`` (used in tests).
      2. Predictive: leave ``transcript_path=None``; the path is predicted
         from ``session_id`` + ``cwd`` at runtime, matching the SDK's
         ``~/.claude/projects/<sanitized>/<session_id>.jsonl`` pattern.

    Usage::

        hook = IncrementalTranscriptHook(transcript_path="/path/to/file.jsonl")
        hooks_dict = hook.build_hooks_dict()
        # Pass hooks_dict to ClaudeAgentOptions(hooks=hooks_dict)
    """

    def __init__(self, transcript_path: str | None = None):
        """Initialize the incremental transcript hook.

        Args:
            transcript_path: Optional explicit path to the durable JSONL file.
                If None, the path is predicted from session_id + cwd at runtime.
        """
        self.transcript_path = transcript_path
        self._seen_hashes: set[int] = set()

    # ── Path resolution ─────────────────────────────────────────────────

    @staticmethod
    def _predict_path(session_id: str, cwd: str) -> str | None:
        """Predict the SDK transcript path from session_id and cwd.

        Matches the SDK convention: ``~/.claude/projects/<sanitized-cwd>/<session_id>.jsonl``
        where ``<sanitized-cwd>`` replaces non-alphanumeric chars with ``-``.
        """
        if not re.match(r"^[a-zA-Z0-9_-]+$", session_id):
            logger.warning("Invalid session_id for path prediction: %s", session_id)
            return None
        claude_home = Path.home() / ".claude"
        sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        if not sanitized:
            sanitized = "default"
        return str(claude_home / "projects" / sanitized / f"{session_id}.jsonl")

    def _resolve_path(self, input_data: dict) -> str | None:
        """Resolve the transcript path, preferring explicit then predictive."""
        # Already resolved
        if self.transcript_path:
            return self.transcript_path

        # Direct path from Stop/SubagentStop hooks
        direct: str | None = (
            input_data.get("transcriptPath")
            or input_data.get("transcript_path")
            or input_data.get("agent_transcript_path")
        )
        if direct:
            self.transcript_path = direct
            return direct

        # Predict from session metadata
        session_id = input_data.get("session_id", "")
        cwd = input_data.get("cwd", "")
        if session_id and cwd:
            predicted = self._predict_path(session_id, cwd)
            if predicted:
                # Ensure parent directory exists
                try:
                    Path(predicted).parent.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    logger.warning(
                        "Cannot create transcript directory for %s: %s", predicted, e
                    )
                    return None
                self.transcript_path = predicted
                return predicted

        return None

    # ── Low-level crash-safe I/O ────────────────────────────────────────

    @staticmethod
    def _get_open_flags() -> int:
        """Return OS-specific flags for crash-safe append writes.

        Uses O_APPEND (all writes go to end of file) and O_DSYNC (data
        synced to storage before write() returns on Linux). Falls back to
        O_APPEND-only on platforms where O_DSYNC is unavailable (Windows).
        """
        flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
        if hasattr(os, "O_DSYNC"):
            flags |= os.O_DSYNC
        return flags

    async def _append_line(self, entry: dict) -> bool:
        """Append a single JSON line to the durable transcript file.

        Uses low-level os.open with crash-safe flags. Deduplicates by
        content hash (same semantic content written only once).

        Returns:
            True if the line was written, False if skipped (duplicate or error).
        """
        # Deduplication by content hash (sort keys for stability)
        try:
            serialized = json.dumps(entry, sort_keys=True, default=str)
        except (TypeError, ValueError) as e:
            logger.warning("Cannot serialize transcript entry: %s", e)
            return False

        content_hash = hash(serialized)
        if content_hash in self._seen_hashes:
            return False
        self._seen_hashes.add(content_hash)

        path = self.transcript_path
        if not path:
            logger.warning("No transcript path available, skipping write")
            return False

        try:
            line = serialized + "\n"
            fd = os.open(path, self._get_open_flags(), 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            return True
        except OSError as e:
            logger.warning("Failed to write transcript line to %s: %s", path, e)
            return False
        except Exception as e:
            logger.warning("Unexpected error writing transcript: %s", e)
            return False

    # ── Hook handlers ───────────────────────────────────────────────────

    async def on_post_tool_use(
        self,
        input_data: dict,
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict:
        """Handle PostToolUse hook event.

        Writes a tool-use entry to the transcript immediately after every
        successful tool execution.

        Args:
            input_data: The SDK hook payload.
            tool_use_id: Tool use identifier, passed positionally by the
                SDK and preferred over the payload copy when present.
            context: SDK hook context (abort signal placeholder); unused.
        """
        self._resolve_path(input_data)

        entry = {
            "type": "tool_use",
            "hook_event": "PostToolUse",
            "tool_name": input_data.get("tool_name", "unknown"),
            "tool_input": input_data.get("tool_input", {}),
            "tool_use_id": tool_use_id or input_data.get("tool_use_id", ""),
            "session_id": input_data.get("session_id", ""),
            "agent_id": input_data.get("agent_id"),
            "agent_type": input_data.get("agent_type"),
        }
        if "tool_response" in input_data:
            entry["tool_response"] = input_data["tool_response"]

        await self._append_line(entry)
        return {"continue_": True}

    async def on_user_prompt_submit(
        self,
        input_data: dict,
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict:
        """Handle UserPromptSubmit hook event.

        Writes a user-prompt entry to the transcript immediately when
        a user prompt is submitted.
        """
        self._resolve_path(input_data)

        entry = {
            "type": "user_prompt",
            "hook_event": "UserPromptSubmit",
            "prompt": input_data.get("prompt", ""),
            "session_id": input_data.get("session_id", ""),
        }
        await self._append_line(entry)
        return {"continue_": True}

    async def on_stop(
        self,
        input_data: dict,
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict:
        """Handle Stop hook event.

        Syncs the SDK's complete transcript (from transcriptPath) to the
        durable file so that no messages are lost.
        """
        path = self._resolve_path(input_data)

        # If we used a predicted path but the SDK gives us the real path,
        # sync from the SDK's transcript
        sdk_path = input_data.get("transcriptPath") or input_data.get("transcript_path")
        if sdk_path and sdk_path != path:
            await self._sync_transcript(sdk_path)

        return {"continue_": True}

    async def on_subagent_stop(
        self,
        input_data: dict,
        tool_use_id: str | None = None,
        context: Any = None,
    ) -> dict:
        """Handle SubagentStop hook event.

        Syncs the subagent's transcript to the durable file.
        """
        self._resolve_path(input_data)

        subagent_path = input_data.get("agent_transcript_path")
        if subagent_path:
            await self._sync_transcript(subagent_path)

        return {"continue_": True}

    async def _sync_transcript(self, source_path: str) -> None:
        """Read all lines from an SDK transcript and append new ones.

        Only lines not already present (by content hash) are written.
        Malformed JSON lines are silently skipped.
        """
        try:
            source = Path(source_path)
            if not source.exists():
                logger.debug("Transcript source not found: %s", source_path)
                return

            with open(source, encoding="utf-8") as f:
                for line in f:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entry = json.loads(stripped)
                    except json.JSONDecodeError:
                        logger.debug(
                            "Skipping malformed line in transcript: %s", source_path
                        )
                        continue
                    await self._append_line(entry)
        except OSError as e:
            logger.warning("Failed to read SDK transcript %s: %s", source_path, e)
        except Exception as e:
            logger.warning("Unexpected error syncing transcript: %s", e)

    # ── Hook registration ───────────────────────────────────────────────

    def build_hooks_dict(self) -> dict:
        """Build a hooks dict compatible with ``ClaudeAgentOptions``.

        Returns a dict keyed by hook event names, each mapping to a list
        of ``HookMatcher`` instances. Compatible with the SDK's
        ``ClaudeAgentOptions(hooks=...)`` parameter.

        Returns:
            dict: Hook event → [HookMatcher, ...] mapping
        """
        matcher_cls: Any
        try:
            from claude_agent_sdk import HookMatcher

            matcher_cls = HookMatcher
        except ImportError:
            # Allow testing without the SDK installed
            matcher_cls = _FakeHookMatcher

        return {
            "PostToolUse": [matcher_cls(matcher="*", hooks=[self.on_post_tool_use])],
            "UserPromptSubmit": [
                matcher_cls(matcher="*", hooks=[self.on_user_prompt_submit])
            ],
            "Stop": [matcher_cls(matcher="*", hooks=[self.on_stop])],
            "SubagentStop": [matcher_cls(matcher="*", hooks=[self.on_subagent_stop])],
        }


# ── Fallback for environments without claude_agent_sdk ──────────────────


class _FakeHookMatcher:
    """Minimal HookMatcher stand-in for environments without the SDK.

    Used in unit tests where ``claude_agent_sdk`` is mocked. Provides
    the same attribute surface as the real ``HookMatcher``.
    """

    def __init__(self, matcher: str = "*", hooks: list | None = None):
        self.matcher = matcher
        self.hooks = hooks or []
