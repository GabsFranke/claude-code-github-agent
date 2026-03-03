"""Observability manager for Langfuse integration and hooks."""

import asyncio
import json
import logging
import os

from claude_agent_sdk import HookMatcher

logger = logging.getLogger(__name__)


class ObservabilityManager:
    """Manages observability hooks and Langfuse integration."""

    def setup_langfuse_hooks(self) -> dict:
        """Setup Langfuse hooks if configured."""
        langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
        langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

        if not (langfuse_public_key and langfuse_secret_key):
            return {}

        async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
            """Async hook for Langfuse (unused params prefixed with _)."""
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
                        "LANGFUSE_HOST": os.getenv(
                            "LANGFUSE_HOST", "http://langfuse:3000"
                        ),
                        "LANGFUSE_BASE_URL": os.getenv(
                            "LANGFUSE_HOST", "http://langfuse:3000"
                        ),
                        "CC_LANGFUSE_DEBUG": "true",
                        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                        "HOME": os.environ.get("HOME", "/root"),
                    },
                )

                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(input=hook_payload.encode()), timeout=30.0
                    )

                    if process.returncode != 0:
                        error_msg = f"Langfuse hook failed with code {process.returncode}: {stderr.decode()}"
                        logger.warning(error_msg)
                    else:
                        logger.debug(
                            f"Langfuse hook completed: {stdout.decode()[:100]}"
                        )
                        return {"success": True}

                except TimeoutError:
                    error_msg = "Langfuse hook timed out after 30s"
                    logger.warning(error_msg)
                    process.kill()
                    try:
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except TimeoutError:
                        logger.error("Process failed to terminate after kill signal")

            except OSError as e:
                error_msg = f"Error running Langfuse hook: {e}"
                logger.warning(error_msg)
                if "process" in locals() and process.returncode is None:
                    try:
                        process.kill()
                        await asyncio.wait_for(process.wait(), timeout=5.0)
                    except OSError as cleanup_error:
                        logger.error(f"Failed to cleanup process: {cleanup_error}")

            return {"success": False, "error": error_msg}

        return {
            "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
            "SubagentStop": [
                HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])
            ],
        }
