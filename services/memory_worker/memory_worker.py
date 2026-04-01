"""Background worker that processes repository memory extraction jobs from a Redis queue.

Listens on `agent:memory:requests`. For each job it:
1. Reads the persisted session transcript from the agent-memory volume.
2. Invokes the @memory-extractor subagent (via Claude Agent SDK / Haiku) to update MEMORY.md.
3. Serialises access per-repo using asyncio.Lock to prevent concurrent write races.
"""

import asyncio
import json
import logging
import os
import sys
from collections import defaultdict

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
)

from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.signals import setup_graceful_shutdown

# Import the memory extractor subagent definition
from subagents import AGENTS

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

shutdown_event = asyncio.Event()

# Per-repo locks — prevents concurrent MEMORY.md writes for the same repository.
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
                        # Skip the injected "Prior Session Memory" preamble — it's
                        # system context, not a real user turn, and MEMORY.md already
                        # has this information.
                        if content.startswith("## Prior Session Memory"):
                            continue
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
                                    text = block.get("text", "")
                                    if not text.startswith("## Prior Session Memory"):
                                        lines.append(f"User: {text}")

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


def setup_langfuse_hooks() -> dict:
    """Setup Langfuse observability hooks for memory extraction sessions."""
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
        process = None
        try:
            hook_payload = json.dumps(input_data)
            process = await asyncio.create_subprocess_exec(
                "python3",
                "/app/hooks/langfuse_hook.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "TRACE_TO_LANGFUSE": "true",
                    "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                    "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                    "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                    "LANGFUSE_BASE_URL": os.getenv(
                        "LANGFUSE_HOST", "http://langfuse:3000"
                    ),
                    "CC_LANGFUSE_DEBUG": "true",
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/home/bot"),
                },
            )

            try:
                _stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=hook_payload.encode()),
                    timeout=float(os.getenv("LANGFUSE_HOOK_TIMEOUT", "30.0")),
                )
                if process.returncode != 0:
                    logger.warning(f"Langfuse hook failed: {stderr.decode()}")
                else:
                    return {"success": True}
            except TimeoutError:
                logger.warning("Langfuse hook timed out after 30s")
                process.kill()
                await process.wait()

        except Exception as e:
            logger.warning(f"Error running Langfuse hook: {e}")
            error_msg = str(e)
        finally:
            # Ensure process is cleaned up if it exists and hasn't been waited on
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass  # Expected - process already terminated
                except OSError as e:
                    logger.warning(f"Failed to cleanup Langfuse hook process: {e}")
                except Exception as e:
                    logger.error(
                        f"Unexpected error cleaning up Langfuse hook process: {e}",
                        exc_info=True,
                    )

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def process_memory_job(message: dict) -> None:
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

    # Serialise per-repo to avoid concurrent MEMORY.md writes
    async with _repo_locks[repo]:
        logger.info(
            f"Processing memory job for {repo} [{hook_event}]: {transcript_path}"
        )

        memory_dir = f"/home/bot/.claude/projects/{repo}/memory"
        os.makedirs(memory_dir, exist_ok=True)
        memory_file = os.path.join(memory_dir, "MEMORY.md")

        conversation_text = extract_conversation(transcript_path)
        if not conversation_text:
            logger.info(
                f"No conversation content extracted from {transcript_path}, skipping."
            )
            return

        prompt = (
            f"Repository: {repo}\n"
            f"Session event: {hook_event}\n\n"
            f"## Session Transcript\n\n"
            f"{conversation_text}\n\n"
            f"---\n\n"
            f"## Task\n\n"
            f"Read the existing memory file at: {memory_file}\n"
            f"Then extract any new facts from the transcript above and update {memory_file}.\n"
            f"If the file doesn't exist, create it with the header: # Repository Memory: {repo}\n"
            f"Only add NEW facts not already present. Do not duplicate existing entries."
        )

        memory_model = os.getenv(
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "claude-haiku-4-5-20251001"
        )

        hooks = setup_langfuse_hooks()

        options = ClaudeAgentOptions(
            model=memory_model,
            allowed_tools=["Read", "Write", "Edit", "List"],
            permission_mode="acceptEdits",
            agents=AGENTS,
            hooks=hooks,
            cwd=memory_dir,  # Working directory is the persistent memory dir
            system_prompt=(
                "You are a memory extraction agent. "
                "Read session transcripts and update MEMORY.md with actionable facts. "
                "Keep the file under 200 lines by grooming old/stale facts as needed."
            ),
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

    logger.info("Memory worker ready, waiting for jobs...")

    async def message_handler(message: dict) -> None:
        if shutdown_event.is_set():
            return
        await process_memory_job(message)

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
