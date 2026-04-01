#!/usr/bin/env python3
"""Memory MCP server - stdio protocol implementation.

Provides memory_read and memory_write tools for controlled access to
repository memory files.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

# Setup logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mcp_servers.memory.tools import memory_read, memory_write  # noqa: E402


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle MCP requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "memory", "version": "1.0.0"},
        }

    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "memory_read",
                    "description": (
                        "Read memory files from the repository's memory directory. "
                        "Call without file_path to list all files, or with file_path to read a specific file. "
                        "Memory files contain persistent knowledge about the repository: architecture, "
                        "known issues, commands, and other facts learned from previous sessions."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": (
                                    "Optional: Path to memory file relative to memory root. "
                                    "Examples: 'index.md', 'architecture/auth.md', 'issues/bug-123.md'. "
                                    "Omit to list all available memory files."
                                ),
                            }
                        },
                    },
                },
                {
                    "name": "memory_write",
                    "description": (
                        "Create or update a memory file in the repository's memory directory. "
                        "Use for documenting architecture, known issues, commands, decisions, or other "
                        "persistent knowledge that should be remembered across sessions. "
                        "The file will be created if it doesn't exist, or overwritten if it does."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": (
                                    "Path to memory file relative to memory root. "
                                    "Examples: 'architecture/auth.md', 'issues/payment-race.md', 'commands.md'. "
                                    "Parent directories will be created automatically."
                                ),
                            },
                            "content": {
                                "type": "string",
                                "description": (
                                    "Full content to write to the file. This will overwrite existing content, "
                                    "so read the file first if you want to update rather than replace."
                                ),
                            },
                        },
                        "required": ["file_path", "content"],
                    },
                },
            ]
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        # Get repo from environment (set by SDK)
        repo = os.getenv("GITHUB_REPOSITORY")
        if not repo:
            return {
                "error": {
                    "code": -32603,
                    "message": "GITHUB_REPOSITORY environment variable not set",
                }
            }

        try:
            if tool_name == "memory_read":
                result = memory_read(file_path=arguments.get("file_path"), repo=repo)
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            if tool_name == "memory_write":
                result = memory_write(
                    file_path=arguments["file_path"],
                    content=arguments["content"],
                    repo=repo,
                )
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

        except FileNotFoundError as e:
            return {"error": {"code": -32603, "message": f"File not found: {str(e)}"}}
        except ValueError as e:
            return {"error": {"code": -32602, "message": f"Invalid input: {str(e)}"}}
        except Exception as e:
            logger.exception(f"Tool execution failed: {tool_name}")
            return {
                "error": {
                    "code": -32603,
                    "message": f"Tool execution failed: {type(e).__name__}: {str(e)}",
                }
            }

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


async def main():
    """Main server loop - reads JSON-RPC requests from stdin, writes responses to stdout."""
    logger.info("Memory MCP server starting...")

    while True:
        try:
            # Read request from stdin
            line = sys.stdin.readline()
            if not line:
                break

            request = json.loads(line)

            # Notifications have no "id" - do not send a response
            if "id" not in request:
                continue

            # Handle request
            response = await handle_request(request)

            # Build proper JSON-RPC 2.0 response
            output: dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
            if "error" in response:
                output["error"] = response["error"]
            else:
                output["result"] = response

            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()

        except json.JSONDecodeError as e:
            # Log with sanitized input (first 200 chars only for security)
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


if __name__ == "__main__":
    asyncio.run(main())
