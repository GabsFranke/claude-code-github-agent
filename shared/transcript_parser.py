"""Utilities for parsing Claude Agent SDK transcript files (JSONL format)."""

import json
import logging
from collections.abc import Iterator
from pathlib import Path

logger = logging.getLogger(__name__)


def _iter_transcript_lines(file_path: str | Path) -> Iterator[dict]:
    """Yield parsed JSON objects from a JSONL transcript file.

    Skips blank lines and malformed JSON. Logs warnings for issues.
    """
    path = Path(file_path)
    if not path.exists():
        logger.warning(f"Transcript file not found: {path}")
        return
    with open(path, encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.debug(f"Skipping malformed line {line_num} in {path}")
                continue


def extract_conversation(transcript_path: str) -> str:
    """Parse a Claude JSONL transcript and return clean conversation text.

    Strips all metadata noise (parentUuid, usage stats, thinking blocks, etc.)
    and returns only the human-readable conversation turns.

    Used by memory_worker for indexing conversational content.
    """
    lines: list[str] = []
    try:
        for entry in _iter_transcript_lines(transcript_path):
            entry_type = entry.get("type")

            # Skip internal queue telemetry
            if entry_type == "queue-operation":
                continue

            msg = entry.get("message", {})
            role = msg.get("role") or entry_type  # fallback for older formats
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    lines.append(f"User: {content}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_result":
                                inner = block.get("content", "")
                                if isinstance(inner, list):
                                    text = " ".join(
                                        b.get("text", "")
                                        for b in inner
                                        if isinstance(b, dict)
                                    )
                                else:
                                    text = str(inner)
                                lines.append(f"Tool result: {text[:500]}")
                            elif block.get("type") == "text":
                                lines.append(f"User: {block.get('text', '')}")

            elif role == "assistant":
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            lines.append(f"Assistant: {block.get('text', '')}")
                        elif btype == "tool_use":
                            tool_input = json.dumps(block.get("input", {}))
                            lines.append(
                                f"Tool call: {block.get('name')}({tool_input[:300]})"
                            )
                        # skip "thinking" blocks — not useful for memory

    except Exception as e:
        logger.warning(f"Failed to parse transcript {transcript_path}: {e}")

    return "\n".join(lines)


def extract_retrospector_summary(transcript_path: str) -> str | None:
    """Parse a Claude JSONL transcript and extract a concise summary for retrospection.

    Returns a structured text summary instead of the raw JSONL to avoid hitting
    the SDK's 1MB JSON buffer limit when passing large transcripts.

    Returns None if parsing fails or the file does not exist.

    Used by retrospector_worker for instruction improvement analysis.
    """
    if not Path(transcript_path).exists():
        return None

    lines: list[str] = []
    turn_count = 0
    error_count = 0
    tool_errors: list[str] = []
    subagents_used: set[str] = set()

    try:
        for entry in _iter_transcript_lines(transcript_path):
            entry_type = entry.get("type")
            if entry_type == "queue-operation":
                continue

            msg = entry.get("message", {})
            role = msg.get("role") or entry_type
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    lines.append(f"\n[Turn {turn_count}] User: {content[:500]}")
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_result":
                                tool_id = block.get("tool_use_id", "")
                                is_error = block.get("is_error", False)
                                inner = block.get("content", "")
                                if isinstance(inner, list):
                                    text = " ".join(
                                        b.get("text", "")
                                        for b in inner
                                        if isinstance(b, dict)
                                    )
                                else:
                                    text = str(inner)

                                if is_error:
                                    error_count += 1
                                    tool_errors.append(f"Tool {tool_id}: {text[:300]}")
                                    lines.append(
                                        f"[Turn {turn_count}] Tool ERROR: {text[:300]}"
                                    )
                                else:
                                    lines.append(
                                        f"[Turn {turn_count}] Tool result: {text[:300]}"
                                    )
                            elif block.get("type") == "text":
                                lines.append(
                                    f"[Turn {turn_count}] User: {block.get('text', '')[:500]}"
                                )

            elif role == "assistant":
                turn_count += 1
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            lines.append(
                                f"\n[Turn {turn_count}] Assistant: {text[:500]}"
                            )
                        elif btype == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = json.dumps(block.get("input", {}))

                            # Track subagent invocations via Agent tool
                            if tool_name == "Agent":
                                agent_input = block.get("input", {})
                                agent_name = agent_input.get("name", "unknown")
                                agent_type = agent_input.get(
                                    "subagent_type", agent_name
                                )
                                subagents_used.add(f"{agent_name} ({agent_type})")

                            lines.append(
                                f"[Turn {turn_count}] Tool call: {tool_name}({tool_input[:200]})"
                            )

    except Exception as e:
        logger.error(f"Failed to parse transcript {transcript_path}: {e}")
        return None

    summary = f"""# Session Transcript Summary

**Total turns:** {turn_count}
**Tool errors:** {error_count}
**Subagents invoked:** {', '.join(sorted(subagents_used)) if subagents_used else 'none'}

## Detailed Timeline

{''.join(lines)}

## Tool Errors Summary

{chr(10).join(f'- {err}' for err in tool_errors) if tool_errors else 'No tool errors'}
"""
    return summary


def extract_conversation_summary(transcript_path: str) -> str | None:
    """Extract a compact conversation summary for session fallback context.

    Produces a shorter summary than ``extract_retrospector_summary`` —
    intended to be injected as system prompt context when a full session
    resume is not possible.

    Returns None if parsing fails.
    """
    turn_count = 0
    files_examined: set[str] = set()
    files_modified: set[str] = set()
    tools_used: set[str] = set()
    last_action = ""
    user_queries: list[str] = []
    assistant_responses: list[str] = []

    try:
        for entry in _iter_transcript_lines(transcript_path):
            entry_type = entry.get("type")
            if entry_type == "queue-operation":
                continue

            msg = entry.get("message", {})
            role = msg.get("role") or entry_type
            content = msg.get("content", "")

            if role == "user":
                if isinstance(content, str):
                    user_queries.append(content[:200])
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            user_queries.append(block.get("text", "")[:200])

            elif role == "assistant":
                turn_count += 1
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            text = block.get("text", "")
                            if text:
                                assistant_responses.append(text[:300])
                            last_action = f"Assistant response ({len(text)} chars)"
                        elif btype == "tool_use":
                            tool_name = block.get("name", "")
                            tools_used.add(tool_name)
                            tool_input = block.get("input", {})

                            if tool_name == "Read" and "file_path" in tool_input:
                                files_examined.add(tool_input["file_path"])
                                last_action = f"Read {tool_input['file_path']}"
                            elif (
                                tool_name in ("Edit", "Write")
                                and "file_path" in tool_input
                            ):
                                files_modified.add(tool_input["file_path"])
                                last_action = f"{tool_name} {tool_input['file_path']}"
                            elif tool_name == "Bash":
                                cmd = tool_input.get("command", "")[:100]
                                last_action = f"Bash: {cmd}"
                            elif tool_name == "Agent":
                                last_action = (
                                    f"Agent: {tool_input.get('name', 'unknown')}"
                                )
                            else:
                                last_action = f"{tool_name}"

    except Exception as e:
        logger.warning(
            f"Failed to extract conversation summary from {transcript_path}: {e}"
        )
        return None

    if turn_count == 0:
        return None

    parts = [
        f"Conversation had {turn_count} turns.",
    ]
    if user_queries:
        parts.append(f"User queries: {'; '.join(user_queries[-5:])}")
    if files_examined:
        parts.append(f"Files examined: {', '.join(sorted(files_examined)[-20:])}")
    if files_modified:
        parts.append(f"Files modified: {', '.join(sorted(files_modified)[-10:])}")
    if tools_used:
        parts.append(f"Tools used: {', '.join(sorted(tools_used))}")
    if last_action:
        parts.append(f"Last action: {last_action}")
    if assistant_responses:
        parts.append(f"Last response excerpt: {assistant_responses[-1][:500]}")

    return "\n".join(parts)
