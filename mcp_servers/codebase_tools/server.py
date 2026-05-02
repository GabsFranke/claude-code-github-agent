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
    detect_changes,
    find_definitions,
    find_references,
    get_context,
    get_file_overview,
    get_impact,
    get_routes_map,
    get_tools_map,
    init_repo,
    read_file_summary,
    search_codebase,
    trace_flow,
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
                "Search the codebase using text, semantic, or hybrid search. "
                "Text mode uses ripgrep for regex/pattern matching. "
                "Semantic mode embeds the query via Gemini and searches SurrealDB's HNSW vector index. "
                "Hybrid mode combines both and deduplicates results. "
                "More token-efficient than raw Bash grep for structured exploration."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": (
                            "For text/hybrid search: regex or literal pattern to search for. "
                            "For semantic search: natural language description of what to find."
                        ),
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
                    "search_type": {
                        "type": "string",
                        "description": (
                            "Search mode: 'text' (regex/ripgrep), 'semantic' (Gemini embedding + vector search), "
                            "or 'hybrid' (both combined with deduplication). Default: 'text'."
                        ),
                        "enum": ["text", "semantic", "hybrid"],
                        "default": "text",
                    },
                    "kind_filter": {
                        "type": "string",
                        "description": (
                            "For semantic/hybrid search: filter by symbol kind. "
                            "Examples: 'function', 'class', 'method', 'variable'."
                        ),
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
        {
            "name": "get_context",
            "description": (
                "Get a 360-degree view of a symbol: where it's defined, what calls it, what it "
                "calls, its inheritance hierarchy, and enclosing scope. More comprehensive than "
                "find_definitions for understanding a symbol's role in the codebase. "
                "If the name is ambiguous (multiple definitions), returns a disambiguation list."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": (
                            "Exact name of the symbol. "
                            "Examples: 'process_job', 'RepoMap', 'handle_request'."
                        ),
                    },
                    "file_hint": {
                        "type": "string",
                        "description": (
                            "Optional file path to disambiguate when the symbol exists "
                            "in multiple files. Example: 'shared/repomap.py'."
                        ),
                    },
                },
                "required": ["symbol_name"],
            },
        },
        {
            "name": "get_impact",
            "description": (
                "Get the blast radius of changes to a file or line range. Uses BFS graph traversal "
                "to find upstream (who depends on us) and downstream (what we depend on) impact "
                "through the call graph. Includes risk assessment (low/medium/high). "
                "Use this before making changes to understand downstream effects."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to file relative to repo root. Example: 'shared/repomap.py'.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "Optional start line to narrow the impact range.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Optional end line to narrow the impact range.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum BFS traversal depth (1-10, default 3).",
                        "default": 3,
                    },
                    "direction": {
                        "type": "string",
                        "description": (
                            "Impact direction: 'upstream' (who depends on us), "
                            "'downstream' (what we depend on), or 'both' (default)."
                        ),
                        "enum": ["upstream", "downstream", "both"],
                        "default": "both",
                    },
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "get_file_overview",
            "description": (
                "Get all symbols, imports, and class structure for a file. Returns a structured "
                "overview including definitions, imports, what files import this one, and class "
                "hierarchies. Use this to quickly understand a file's structure and dependencies."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Path to file relative to repo root. Example: 'shared/repomap.py'.",
                    },
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "detect_changes",
            "description": (
                "Detect which symbols are affected by pending git changes and assess "
                "the impact risk. Parses git diff to find changed lines, maps them "
                "to symbols in the index, and runs BFS impact analysis on each "
                "affected symbol. Use this before committing to understand the blast "
                "radius of pending changes."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": (
                            "Which changes to detect: 'staged' (default, checks "
                            "git diff --staged) or 'unstaged' (working tree changes)."
                        ),
                        "enum": ["staged", "unstaged"],
                        "default": "staged",
                    },
                },
            },
        },
        {
            "name": "trace_flow",
            "description": (
                "Trace the execution flow starting from a symbol by following "
                "call edges through the code graph. Builds a BFS-ordered list "
                "of all reachable function/method calls with depth markers and "
                "a nested call chain. Use this to understand the execution path "
                "of a function before modifying it."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entry_point": {
                        "type": "string",
                        "description": (
                            "Name of the symbol to start tracing from. "
                            "Examples: 'handle_request', 'Application.run'."
                        ),
                    },
                    "file_hint": {
                        "type": "string",
                        "description": (
                            "Optional file path to disambiguate when the "
                            "symbol exists in multiple files."
                        ),
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum BFS depth (1-50, default 20).",
                        "default": 20,
                    },
                },
                "required": ["entry_point"],
            },
        },
        {
            "name": "get_routes_map",
            "description": (
                "Get all API route definitions in the codebase. Extracts route "
                "decorators from FastAPI, Flask, and Django files and returns "
                "structured data including path, HTTP method, handler function, "
                "framework, and file location. Use this to understand the API "
                "surface of the codebase."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "framework": {
                        "type": "string",
                        "description": (
                            "Optional filter. One of 'fastapi', 'flask', "
                            "or 'django'."
                        ),
                        "enum": ["fastapi", "flask", "django"],
                    },
                },
            },
        },
        {
            "name": "get_tools_map",
            "description": (
                "Get all MCP tool definitions in the codebase. Extracts tool "
                "names, descriptions, and required parameters from MCP server "
                "JSON schema definitions. Use this to discover what agent tools "
                "are available across all MCP servers."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
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
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                find_definitions(symbol_name=arguments["symbol_name"]),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "find_references":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                find_references(symbol_name=arguments["symbol_name"]),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "search_codebase":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                search_codebase(
                                    pattern=arguments["pattern"],
                                    file_type=arguments.get("file_type"),
                                    max_results=arguments.get("max_results", 20),
                                    search_type=arguments.get("search_type", "text"),
                                    kind_filter=arguments.get("kind_filter"),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "read_file_summary":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                read_file_summary(
                                    file_path=arguments["file_path"],
                                    max_lines=arguments.get("max_lines", 80),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "get_context":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                get_context(
                                    symbol_name=arguments["symbol_name"],
                                    file_hint=arguments.get("file_hint"),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "get_impact":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                get_impact(
                                    file_path=arguments["file_path"],
                                    start_line=arguments.get("start_line"),
                                    end_line=arguments.get("end_line"),
                                    max_depth=arguments.get("max_depth", 3),
                                    direction=arguments.get("direction", "both"),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "get_file_overview":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                get_file_overview(
                                    file_path=arguments["file_path"],
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "detect_changes":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                detect_changes(
                                    scope=arguments.get("scope", "staged"),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "trace_flow":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                trace_flow(
                                    entry_point=arguments["entry_point"],
                                    file_hint=arguments.get("file_hint"),
                                    max_depth=arguments.get("max_depth", 20),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "get_routes_map":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                get_routes_map(
                                    framework=arguments.get("framework"),
                                ),
                                indent=2,
                            ),
                        }
                    ]
                }

            if tool_name == "get_tools_map":
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(get_tools_map(), indent=2),
                        }
                    ]
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
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _init_repo_safe, repo_path)
        logger.info(
            f"Codebase tools server starting (index building in background): {repo_path}"
        )
    else:
        logger.warning("REPO_PATH not set, tools will be unavailable")

    await run_server("codebase_tools", handle_request)


def _init_repo_safe(repo_path: str) -> None:
    """Wrapper to safely init the repo from a thread executor."""
    try:
        init_repo(repo_path)
    except Exception as e:
        logger.error(f"Failed to initialize repo: {e}")


if __name__ == "__main__":
    asyncio.run(main())
