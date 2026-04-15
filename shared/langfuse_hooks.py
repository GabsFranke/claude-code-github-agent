"""Shared Langfuse observability hook factory.

All workers that run Claude Agent SDK sessions (sandbox_executor, memory_worker,
retrospector_worker) use this to build their hooks dict. The hook spawns
hooks/langfuse_hook.py as a subprocess, passing the stop-event payload via stdin.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)


def setup_langfuse_hooks(parent_span_id: str | None = None) -> dict:
    """Return a hooks dict wired to Langfuse, or {} if keys are not configured."""
    from claude_agent_sdk import HookMatcher

    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
        process = None
        try:
            hook_payload = json.dumps(input_data)
            env = {
                "TRACE_TO_LANGFUSE": "true",
                "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                "LANGFUSE_BASE_URL": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                "CC_LANGFUSE_DEBUG": "true",
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": os.environ.get("HOME", "/home/bot"),
            }
            if parent_span_id:
                env["PARENT_SPAN_ID"] = parent_span_id

            process = await asyncio.create_subprocess_exec(
                "python3",
                "/app/hooks/langfuse_hook.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except ProcessLookupError:
                    pass
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
