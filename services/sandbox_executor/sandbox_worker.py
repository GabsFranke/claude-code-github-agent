"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

# Add parent directory to path for shared imports - must be before other imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# flake8: noqa: E402 - imports after path modification
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from shared import JobQueue
from subagents import AGENTS

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global state
shutdown_event = asyncio.Event()


def handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    logger.info("Received signal %s, initiating graceful shutdown...", signum)
    shutdown_event.set()


def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


def setup_langfuse_hooks() -> dict:
    """Setup Langfuse observability hooks if configured."""
    span_id = os.getenv("CURRENT_SPAN_ID")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
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
                    "PARENT_SPAN_ID": span_id or "",
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/root"),
                },
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=hook_payload.encode()), timeout=30.0
                )
                if process.returncode != 0:
                    logger.warning(
                        f"Langfuse hook failed: {stderr.decode()}\nOutput: {stdout.decode()}"
                    )
                else:
                    logger.debug(f"Langfuse hook succeeded: {stdout.decode()}")
                    return {"success": True}
            except TimeoutError:
                logger.warning("Langfuse hook timed out after 30s")
                process.kill()
                await process.wait()

        except Exception as e:
            logger.warning(f"Error running Langfuse hook: {e}")

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def execute_in_workspace(workspace: str, job_data: dict) -> str:
    """Execute Claude Agent SDK in isolated workspace.

    Args:
        workspace: Path to isolated workspace directory
        job_data: Job data containing prompt, github_token, etc.

    Returns:
        Agent response text
    """
    original_cwd = os.getcwd()

    try:
        # Change to isolated workspace
        os.chdir(workspace)
        logger.info(f"Executing in workspace: {workspace}")

        # Build MCP server configuration
        mcp_servers = {
            "github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp",
                "headers": {"Authorization": f"Bearer {job_data['github_token']}"},
            }
        }

        # Setup hooks
        hooks = setup_langfuse_hooks()

        # Build Claude Agent options
        options = ClaudeAgentOptions(
            allowed_tools=["Task", "mcp__github__*"],
            permission_mode="acceptEdits",
            mcp_servers=mcp_servers,
            agents=AGENTS,
            plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],
            hooks=hooks,
            max_turns=50,
        )

        # Execute SDK
        logger.info("Starting Claude Agent SDK execution...")
        response_parts = []

        async with asyncio.timeout(1800):  # 30 minutes
            async with ClaudeSDKClient(options=options) as client:
                await client.query(job_data["prompt"])

                async for message in client.receive_messages():
                    # Check for shutdown
                    if shutdown_event.is_set():
                        logger.warning("Shutdown requested, stopping SDK execution")
                        break

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                                logger.debug(
                                    f"Received text block: {block.text[:100]}..."
                                )
                            elif isinstance(block, ToolUseBlock):
                                logger.info(f"Tool use: {block.name}")

                    elif isinstance(message, ResultMessage):
                        logger.info(
                            f"SDK completed - {message.num_turns} turns, "
                            f"{message.duration_ms}ms"
                        )
                        if message.total_cost_usd:
                            logger.info(f"Cost: ${message.total_cost_usd:.4f}")
                        break

        response = "\n".join(response_parts)

        if not response or not response.strip():
            raise Exception("Claude Agent SDK returned empty response")

        logger.info("SDK execution completed successfully")
        return response

    except TimeoutError as e:
        raise Exception("Claude Agent SDK execution timed out after 30 minutes") from e
    except Exception as e:
        raise Exception(f"Failed to execute Claude Agent SDK: {e}") from e
    finally:
        # Always restore original working directory
        os.chdir(original_cwd)


async def process_job(job_queue: JobQueue, job_id: str, job_data: dict) -> None:
    """Process a single job in an isolated workspace.

    Args:
        job_queue: Job queue instance
        job_id: Job identifier
        job_data: Job data dictionary
    """
    workspace = None

    try:
        # Create isolated workspace in tmpfs mount for automatic cleanup
        # /tmp is intentional - mounted as tmpfs in Docker for security
        workspace = tempfile.mkdtemp(
            prefix=f"job_{job_id[:8]}_",
            dir="/tmp",  # nosec B108
        )
        logger.info(f"Created workspace for job {job_id}: {workspace}")

        # Execute in isolated workspace
        response = await execute_in_workspace(workspace, job_data)

        # Mark job as complete (agent already posted to GitHub via MCP)
        await job_queue.complete_job(
            job_id,
            {
                "status": "success",
                "response": response,
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
            },
            status="success",
        )

        logger.info(f"Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)

        # Mark job as failed
        await job_queue.complete_job(
            job_id,
            {
                "status": "error",
                "error": str(e),
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
            },
            status="error",
        )

    finally:
        # Cleanup workspace
        if workspace:
            try:
                shutil.rmtree(workspace)
                logger.debug(f"Cleaned up workspace: {workspace}")
            except Exception as e:
                logger.warning(f"Failed to cleanup workspace {workspace}: {e}")


async def main():
    """Main sandbox worker loop."""
    logger.info("Starting sandbox worker")

    # Setup signal handlers
    setup_signal_handlers()

    # Initialize job queue
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    job_queue = JobQueue(
        redis_url=redis_url,
        password=redis_password,
        job_ttl=3600,
    )

    logger.info("Sandbox worker ready, waiting for jobs...")

    try:
        while not shutdown_event.is_set():
            try:
                # Pull next job (blocking with timeout)
                result = await job_queue.get_next_job(timeout=5)

                if not result:
                    # Timeout, check shutdown and continue
                    continue

                job_id, job_data = result
                logger.info(
                    f"Processing job {job_id} for {job_data['repo']}#{job_data['issue_number']}"
                )

                # Process job
                await process_job(job_queue, job_id, job_data)

            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    finally:
        logger.info("Shutting down sandbox worker...")
        await job_queue.close()
        logger.info("Sandbox worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
