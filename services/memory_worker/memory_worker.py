"""Background worker that processes repository memory extraction jobs from a Redis queue.

Listens on `agent:memory:requests`. For each job it:
1. Reads the persisted session transcript from the agent-memory volume.
2. Invokes the @memory-extractor subagent (via Claude Agent SDK / Haiku) to update index.md.

Transcripts are persisted permanently in the shared volume for future analysis.

Note: This worker processes jobs sequentially (one at a time). If you need to scale
to multiple worker instances, you'll need to implement distributed locking (e.g., Redis locks)
to prevent concurrent writes to the same repository's index.md file.
"""

import asyncio
import json
import logging
import os
import sys

from shared.dlq import enqueue_for_retry, is_transient_error
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.sdk_executor import execute_sdk
from shared.sdk_factory import SDKOptionsBuilder
from shared.signals import setup_graceful_shutdown
from shared.transcript_parser import extract_conversation

# Import the memory extractor subagent definition
from subagents import AGENTS

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# SDK retry configuration
SDK_MAX_RETRIES = int(os.getenv("SDK_MAX_RETRIES", "3"))
SDK_RETRY_BASE_DELAY = float(os.getenv("SDK_RETRY_BASE_DELAY", "5.0"))

shutdown_event = asyncio.Event()

# DLQ configuration
_DLQ_KEY = "agent:memory:dead_letter"
_QUEUE_KEY = "agent:memory:requests"


async def process_memory_job(message: dict, redis_client) -> None:
    """Invoke memory-extractor subagent for one transcript."""
    repo = message.get("repo")
    transcript_path = message.get("transcript_path")
    hook_event = message.get("hook_event", "Stop")
    claude_md = message.get("claude_md")  # Pre-fetched from sandbox worker
    memory_index = message.get("memory_index")  # Pre-fetched from sandbox worker

    if not repo or not transcript_path:
        logger.error(f"Memory job missing required fields: {message}")
        return

    if not os.path.exists(transcript_path):
        logger.warning(f"Memory job: transcript no longer exists: {transcript_path}")
        return

    logger.info(f"Processing memory job for {repo} [{hook_event}]: {transcript_path}")

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

    # Build SDK options using the factory builder
    # Use pre-fetched context if available, otherwise fetch
    builder = SDKOptionsBuilder(cwd=memory_dir).with_haiku()

    if claude_md is not None or memory_index is not None:
        # Use pre-fetched context (passed from sandbox worker)
        logger.info("Using pre-fetched repository context")
        builder = builder.with_repository_context(
            claude_md=claude_md, memory_index=memory_index
        )
    else:
        # Fallback: auto-fetch if not provided (shouldn't happen normally)
        logger.warning("Context not provided, auto-fetching (this is inefficient)")
        builder = await builder.with_repository_context_auto(repo)

    # Add remaining configuration
    builder = (
        builder.with_memory_mcp(repo)
        .with_memory_toolset()
        .with_agents(AGENTS)
        .with_langfuse_hooks()
        .with_writable_dir(memory_dir)
    )

    try:
        # Execute via centralized executor with retry
        result = await execute_sdk(
            prompt=f"@memory-extractor {prompt}",
            options=builder.build(),
            collect_text=False,
            max_retries=SDK_MAX_RETRIES,
            retry_base_delay=SDK_RETRY_BASE_DELAY,
        )

        logger.info(
            f"Memory extraction done for {repo} — "
            f"{result['num_turns']} turns, {result['duration_ms']}ms"
        )

    except Exception:
        logger.error(
            "Memory extraction failed for %s [%s]",
            repo,
            hook_event,
            exc_info=True,
        )
        raise


async def main() -> None:
    """Main memory worker loop with DLQ support."""
    logger.info("Starting memory worker")
    setup_graceful_shutdown(shutdown_event, logger)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    queue = RedisQueue(
        redis_url=redis_url,
        queue_name=_QUEUE_KEY,
        password=redis_password,
    )
    await queue._connect()
    redis_client = queue.redis

    logger.info("Memory worker ready, waiting for jobs...")

    try:
        while not shutdown_event.is_set():
            try:
                result = await redis_client.blpop(_QUEUE_KEY, timeout=5)
                if not result:
                    continue

                _, raw_message = result
                message = json.loads(raw_message)
                repo = message.get("repo", "unknown")
                logger.info(f"Processing memory job for {repo}")

                try:
                    await process_memory_job(message, redis_client)
                except Exception as e:
                    if is_transient_error(e):
                        await enqueue_for_retry(
                            redis_client, _QUEUE_KEY, _DLQ_KEY, message, e
                        )
                    else:
                        logger.error(
                            "Non-transient error for memory job %s, "
                            "sending to DLQ: %s",
                            repo,
                            e,
                            exc_info=True,
                        )
                        await enqueue_for_retry(
                            redis_client,
                            _QUEUE_KEY,
                            _DLQ_KEY,
                            message,
                            e,
                            max_retries=0,
                        )

            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON in memory request: {e}")
            except Exception as e:
                logger.error(f"Error in memory worker loop: {e}", exc_info=True)
                await asyncio.sleep(5)
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
