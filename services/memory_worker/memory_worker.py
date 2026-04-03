"""Background worker that processes repository memory extraction jobs from a Redis queue.

Listens on `agent:memory:requests`. For each job it:
1. Reads the persisted session transcript from the agent-memory volume.
2. Invokes the @memory-extractor subagent (via Claude Agent SDK / Haiku) to update index.md.
3. Serialises access per-repo using asyncio.Lock to prevent concurrent write races.
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

from shared.langfuse_hooks import setup_langfuse_hooks  # noqa: E402
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.signals import setup_graceful_shutdown

# Import the memory extractor subagent definition
from subagents import AGENTS

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

shutdown_event = asyncio.Event()

# Per-repo locks — prevents concurrent index.md writes for the same repository.
# defaultdict so we create a lock on first access without a global init step.
_repo_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


def extract_conversation(transcript_path: str) -> str:
    """Parse a Claude JSONL transcript and return clean conversation text.

    Strips all metadata noise (parentUuid, usage stats, thinking blocks, etc.)
    and returns only the human-readable conversation turns.
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


async def _release_transcript(transcript_path: str, redis_client) -> None:
    """Decrement the transcript ref counter; delete the file when it hits zero."""
    ref_key = f"agent:transcript:ref:{Path(transcript_path).stem}"
    try:
        remaining = await redis_client.decr(ref_key)
        if remaining <= 0:
            try:
                os.remove(transcript_path)
                logger.debug(f"Deleted transcript (last consumer): {transcript_path}")
            except FileNotFoundError:
                pass
            except Exception as e:
                logger.warning(f"Failed to delete transcript {transcript_path}: {e}")
    except Exception as e:
        logger.warning(f"Failed to decrement transcript ref counter: {e}")


async def process_memory_job(message: dict, redis_client) -> None:
    """Invoke memory-extractor subagent for one transcript."""
    repo = message.get("repo")
    transcript_path = message.get("transcript_path")
    hook_event = message.get("hook_event", "Stop")

    if not repo or not transcript_path:
        logger.error(f"Memory job missing required fields: {message}")
        return

    if not os.path.exists(transcript_path):
        logger.warning(f"Memory job: transcript no longer exists: {transcript_path}")
        return

    # Serialise per-repo to avoid concurrent index.md writes
    async with _repo_locks[repo]:
        logger.info(
            f"Processing memory job for {repo} [{hook_event}]: {transcript_path}"
        )

        try:
            memory_dir = f"/home/bot/agent-memory/{repo}/memory"
            os.makedirs(memory_dir, exist_ok=True)

            conversation_text = extract_conversation(transcript_path)
            if not conversation_text:
                logger.info(
                    f"No conversation content extracted from {transcript_path}, skipping."
                )
                return

            # Use XML format for better structure (inspired by Claude Code)
            prompt = f"""<repository>{repo}</repository>
<session_event>{hook_event}</session_event>

<session_transcript>
{conversation_text}
</session_transcript>

<memory_directory>{memory_dir}</memory_directory>

Extract memorable facts from the session transcript and update the memory files accordingly."""

            memory_model = os.getenv(
                "ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001"
            )

            hooks = setup_langfuse_hooks()

            mcp_servers = {
                "memory": {
                    "type": "stdio",
                    "command": "python3",
                    "args": ["/app/mcp_servers/memory/server.py"],
                    "env": {
                        "GITHUB_REPOSITORY": repo,
                        "PYTHONPATH": "/app",
                    },
                }
            }

            options = ClaudeAgentOptions(
                model=memory_model,
                allowed_tools=["Read", "Write", "Edit", "List", "mcp__memory__*"],
                permission_mode="acceptEdits",
                mcp_servers=mcp_servers,  # type: ignore[arg-type]
                setting_sources=[
                    "user",
                    "project",
                    "local",
                ],
                agents=AGENTS,
                hooks=hooks,
                cwd=memory_dir,  # Working directory is the persistent memory dir
                add_dirs=[memory_dir],  # Allow writes to memory directory
            )

            try:
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(f"@memory-extractor {prompt}")

                    async for msg in client.receive_messages():
                        if isinstance(msg, ResultMessage):
                            logger.info(
                                f"Memory extraction done for {repo} — "
                                f"{msg.num_turns} turns, {msg.duration_ms}ms"
                            )
                            break

            except Exception as e:
                logger.warning(
                    f"Memory extraction failed for {repo} [{hook_event}]: {e}",
                    exc_info=True,
                )

        finally:
            await _release_transcript(transcript_path, redis_client)


async def main() -> None:
    """Main memory worker loop."""
    logger.info("Starting memory worker")
    setup_graceful_shutdown(shutdown_event, logger)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    queue = RedisQueue(
        redis_url=redis_url,
        queue_name="agent:memory:requests",
        password=redis_password,
    )
    await queue._connect()
    redis_client = queue.redis

    logger.info("Memory worker ready, waiting for jobs...")

    async def message_handler(message: dict) -> None:
        if shutdown_event.is_set():
            return
        await process_memory_job(message, redis_client)

    try:
        await queue.subscribe(message_handler)
    finally:
        logger.info("Memory worker shutting down...")
        await queue.close()
        logger.info("Memory worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
