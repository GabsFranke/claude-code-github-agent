#!/usr/bin/env python3
"""Codebase tools MCP server - stdio JSON-RPC protocol.

Provides structured code search tools (find_definitions, find_references,
search_codebase, read_file_summary) for agents to explore codebases
efficiently.

Communicates via stdin/stdout using JSON-RPC 2.0, following the same
pattern as mcp_servers/memory/server.py.
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
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mcp_servers.codebase_tools.tools import (  # noqa: E402
    find_definitions,
    find_references,
    init_repo,
    read_file_summary,
    search_codebase,
)


def _tool_definitions() -> list[dict[str, Any]]:
    """Return the tool schema definitions for MCP tools/list."""
    return [
        {
            "name": "find_definitions",
            "description": (
                "Find where a symbol (class, function, method) is defined in the codebase. "
                "Returns the file path, line number, kind, and source signature for each definition. "
                "Use this for quick lookups of where things are defined."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": (
                            "Exact name of the symbol to find. "
                            "Examples: 'Application', 'process_job', 'SDKOptionsBuilder'."
                        ),
                    },
                },
                "required": ["symbol_name"],
            },
        },
        {
            "name": "find_references",
            "description": (
                "Find all references to a symbol across the codebase. "
                "Returns the file path, line number, and surrounding context for each reference. "
                "Use this to understand how a symbol is used and what depends on it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": (
                            "Exact name of the symbol to find references to. "
                            "Examples: 'RepoMap', 'process_job', 'generate_structural_context'."
                        ),
                    },
                },
                "required": ["symbol_name"],
            },
        },
        {
            "name": "search_codebase",
            "description": (
                "Search the codebase for a regex or literal pattern. "
                "Returns structured results with file, line number, matched text, and context. "
                "More token-efficient than raw Bash grep for structured exploration."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex or literal pattern to search for.",
                    },
                    "file_type": {
                        "type": "string",
                        "description": (
                            "Optional file type filter. "
                            "Examples: 'python', 'js', 'ts', 'go', 'rust', 'java'."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default 20, max 100).",
                        "default": 20,
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "read_file_summary",
            "description": (
                "Read a compact summary of a file: docstring, imports, and all class/function "
                "signatures. Skips implementation bodies. Typically 10-20% of original file size. "
                "Use this to quickly understand a file's API surface without reading the full content."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to file relative to repo root. Example: 'shared/repomap.py'.",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Maximum lines to include in output (default 80, max 200).",
                        "default": 80,
                    },
                },
                "required": ["file_path"],
            },
        },
    ]


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle MCP JSON-RPC requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codebase_tools", "version": "1.0.0"},
        }

    if method == "tools/list":
        return {"tools": _tool_definitions()}

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "find_definitions":
                result = find_definitions(
                    symbol_name=arguments["symbol_name"],
                )
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            if tool_name == "find_references":
                result = find_references(
                    symbol_name=arguments["symbol_name"],
                )
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            if tool_name == "search_codebase":
                result = search_codebase(
                    pattern=arguments["pattern"],
                    file_type=arguments.get("file_type"),
                    max_results=arguments.get("max_results", 20),
                )
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            if tool_name == "read_file_summary":
                result = read_file_summary(
                    file_path=arguments["file_path"],
                    max_lines=arguments.get("max_lines", 80),
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
    from mcp_servers.base import run_server

    repo_path = os.getenv("REPO_PATH")
    if repo_path:
        try:
            init_repo(repo_path)
            logger.info(f"Codebase tools server initialized for: {repo_path}")
        except Exception as e:
            logger.error(f"Failed to initialize repo: {e}")
    else:
        logger.warning("REPO_PATH not set, tools will be unavailable")

    await run_server("codebase_tools", handle_request)


if __name__ == "__main__":
    asyncio.run(main())
