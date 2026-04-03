"""Utilities for parsing Claude Agent SDK transcript files (JSONL format)."""

import json
import logging
import re

logger = logging.getLogger(__name__)


def extract_conversation(transcript_path: str) -> str:
    """Parse a Claude JSONL transcript and return clean conversation text.

    Strips all metadata noise (parentUuid, usage stats, thinking blocks, etc.)
    and returns only the human-readable conversation turns.

    Used by memory_worker for indexing conversational content.
    """
    lines: list[str] = []
    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

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


def extract_retrospector_summary(transcript_path: str) -> str:
    """Parse a Claude JSONL transcript and extract a concise summary for retrospection.

    Returns a structured text summary instead of the raw JSONL to avoid hitting
    the SDK's 1MB JSON buffer limit when passing large transcripts.

    Used by retrospector_worker for instruction improvement analysis.
    """
    lines: list[str] = []
    turn_count = 0
    error_count = 0
    tool_errors: list[str] = []
    subagents_used: set[str] = set()

    try:
        with open(transcript_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue

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
                                        tool_errors.append(
                                            f"Tool {tool_id}: {text[:300]}"
                                        )
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
                                # Extract subagent invocations
                                if "@" in text:
                                    for match in re.finditer(r"@([\w-]+)", text):
                                        subagents_used.add(match.group(1))
                                lines.append(
                                    f"\n[Turn {turn_count}] Assistant: {text[:500]}"
                                )
                            elif btype == "tool_use":
                                tool_name = block.get("name", "")
                                tool_input = json.dumps(block.get("input", {}))
                                lines.append(
                                    f"[Turn {turn_count}] Tool call: {tool_name}({tool_input[:200]})"
                                )

    except Exception as e:
        logger.warning(f"Failed to parse transcript {transcript_path}: {e}")
        return f"Error parsing transcript: {e}"

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
