"""Shared base for MCP servers using stdio JSON-RPC protocol."""

import asyncio
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

logger = logging.getLogger(__name__)

MAX_RESPONSE_BYTES = 1024 * 1024  # 1 MB — safety net for oversized tool responses


async def read_stdin_line() -> str | None:
    """Read a line from stdin asynchronously."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, sys.stdin.readline)


async def run_server(
    server_name: str,
    handle_request: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
    init_fn: Callable[[], Any] | None = None,
    cleanup_fn: Callable[[], Any] | None = None,
) -> None:
    """Run an MCP server loop over stdio JSON-RPC.

    Args:
        server_name: Name for logging (e.g., "memory", "codebase-tools").
        handle_request: Async function that processes JSON-RPC requests.
        init_fn: Optional callable (sync or async) invoked before the loop.
        cleanup_fn: Optional callable (sync or async) invoked after the loop.
    """
    logger.info(f"{server_name} MCP server starting...")

    if init_fn:
        result = init_fn()
        if asyncio.iscoroutine(result):
            await result

    try:
        while True:
            try:
                line = await read_stdin_line()
                if not line:
                    break

                request = json.loads(line)

                if "id" not in request:
                    continue

                response = await handle_request(request)

                output: dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
                if "error" in response:
                    output["error"] = response["error"]
                else:
                    output["result"] = response

                encoded = json.dumps(output)
                if len(encoded) > MAX_RESPONSE_BYTES:
                    logger.warning(
                        "Response for request %s exceeded %d bytes, truncating",
                        request.get("id"),
                        MAX_RESPONSE_BYTES,
                    )
                    output["result"] = {
                        "content": [
                            {
                                "type": "text",
                                "text": encoded[:MAX_RESPONSE_BYTES]
                                + "\n\n[RESPONSE TRUNCATED: exceeded 1MB limit]",
                            }
                        ],
                        "isError": True,
                    }
                    encoded = json.dumps(output)

                sys.stdout.write(encoded + "\n")
                sys.stdout.flush()

            except json.JSONDecodeError as e:
                input_preview = line[:200] if line and len(line) > 200 else line
                logger.error(
                    f"JSON parse error: {str(e)}",
                    exc_info=True,
                    extra={"input_preview": input_preview},
                )
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32700, "message": f"Parse error: {str(e)}"},
                    "id": None,
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()
            except Exception as e:
                logger.exception("Error processing request")
                error_response = {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                    "id": None,
                }
                sys.stdout.write(json.dumps(error_response) + "\n")
                sys.stdout.flush()
    finally:
        if cleanup_fn:
            result = cleanup_fn()
            if asyncio.iscoroutine(result):
                await result
        logger.info(f"{server_name} MCP server shutdown complete")
