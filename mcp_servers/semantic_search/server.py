#!/usr/bin/env python3
"""Semantic search MCP server - stdio JSON-RPC protocol.

Provides semantic_search tool for natural language code queries.
Connects to Qdrant for vector similarity search and Google Gemini
for query embedding.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mcp_servers.semantic_search.tools import (  # noqa: E402
    cleanup,
    init_config,
    semantic_search,
)


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle MCP JSON-RPC requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "semantic_search", "version": "1.0.0"},
        }

    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": "semantic_search",
                    "description": (
                        "Search the codebase using natural language or code queries. "
                        "Finds code that is semantically similar to your query, even if "
                        "the exact keywords don't match. Best for conceptual searches "
                        "like 'how does the queue handle retries' or 'authentication flow'. "
                        "Only available when semantic indexing is configured."
                    ),
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": (
                                    "Natural language or code query describing "
                                    "what to find. Examples: 'how does the queue "
                                    "handle retries', 'database connection pooling', "
                                    "'error handling middleware'."
                                ),
                            },
                            "max_results": {
                                "type": "integer",
                                "description": "Maximum results to return (default 10, max 50).",
                                "default": 10,
                            },
                            "file_filter": {
                                "type": "string",
                                "description": (
                                    "Optional glob pattern to filter by filepath. "
                                    "Example: 'shared/*.py'. Uses standard glob "
                                    "matching (not full-text search)."
                                ),
                            },
                            "kind_filter": {
                                "type": "string",
                                "description": (
                                    "Optional chunk kind filter. "
                                    "Values: 'function', 'class', 'method'."
                                ),
                            },
                        },
                        "required": ["query"],
                    },
                },
            ]
        }

    if method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        try:
            if tool_name == "semantic_search":
                result = semantic_search(
                    query=arguments["query"],
                    max_results=arguments.get("max_results", 10),
                    file_filter=arguments.get("file_filter"),
                    kind_filter=arguments.get("kind_filter"),
                )
                return {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}]
                }

            return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

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

    init_config()
    await run_server("semantic_search", handle_request, cleanup_fn=cleanup)


if __name__ == "__main__":
    asyncio.run(main())
