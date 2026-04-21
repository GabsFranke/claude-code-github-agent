#!/usr/bin/env python3
"""MCP server for GitHub Actions CI/CD tools.

Auto-discovered from mcp_servers/github_actions/ by the SDK factory.
"""

import asyncio
import logging
from typing import Any

from mcp_servers.base import run_server
from mcp_servers.github_actions.tools.github_actions import (
    get_failed_steps,
    get_job_logs_raw,
    get_workflow_run_summary,
    search_job_logs,
)

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "get_workflow_run_summary",
        "description": "Get high-level summary of a workflow run without logs. Use this FIRST to identify failed jobs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "run_id": {"type": "string", "description": "Workflow run ID"},
            },
            "required": ["owner", "repo", "run_id"],
        },
    },
    {
        "name": "get_failed_steps",
        "description": "Extract only failed steps from a job with their logs. Most efficient for diagnosis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "job_id": {"type": "string", "description": "Job ID from workflow run"},
                "log_lines_per_step": {
                    "type": "integer",
                    "description": "Max lines to include per failed step",
                    "default": 100,
                },
            },
            "required": ["owner", "repo", "job_id"],
        },
    },
    {
        "name": "get_job_logs_raw",
        "description": "Get paginated job logs. Use start_line and num_lines to read logs in chunks (default: 500 lines per call).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "job_id": {"type": "string", "description": "Job ID from workflow run"},
                "start_line": {
                    "type": "integer",
                    "description": "Line number to start from (0-indexed, default: 0)",
                    "default": 0,
                },
                "num_lines": {
                    "type": "integer",
                    "description": "Number of lines to return (default: 500)",
                    "default": 500,
                },
            },
            "required": ["owner", "repo", "job_id"],
        },
    },
    {
        "name": "search_job_logs",
        "description": "Search for specific patterns in job logs. Returns matching lines with context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {"type": "string", "description": "Repository owner"},
                "repo": {"type": "string", "description": "Repository name"},
                "job_id": {"type": "string", "description": "Job ID from workflow run"},
                "pattern": {
                    "type": "string",
                    "description": "Regex pattern or keyword to search",
                },
                "context_lines": {
                    "type": "integer",
                    "description": "Number of lines before/after match to include",
                    "default": 5,
                },
                "case_sensitive": {
                    "type": "boolean",
                    "description": "Whether search is case-sensitive",
                    "default": False,
                },
            },
            "required": ["owner", "repo", "job_id", "pattern"],
        },
    },
]


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle MCP tool requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "github_actions", "version": "1.0.0"},
        }

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        return await _handle_tool_call(params)

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


async def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call request to the appropriate handler."""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    try:
        if tool_name == "get_workflow_run_summary":
            result = await get_workflow_run_summary(
                owner=arguments["owner"],
                repo=arguments["repo"],
                run_id=arguments["run_id"],
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"""Workflow Run Summary:
- Run ID: {result['run_id']}
- Name: {result['name']}
- Status: {result['status']}
- Conclusion: {result['conclusion']}
- Event: {result['event']}
- URL: {result['html_url']}

Jobs ({len(result['jobs'])}):
"""
                        + "\n".join(
                            f"  - [{job['conclusion'] or 'running'}] {job['name']} (ID: {job['id']})"
                            for job in result["jobs"]
                        ),
                    }
                ]
            }

        if tool_name == "get_failed_steps":
            result = await get_failed_steps(
                owner=arguments["owner"],
                repo=arguments["repo"],
                job_id=arguments["job_id"],
                log_lines_per_step=arguments.get("log_lines_per_step", 100),
            )
            if result["failed_steps_count"] == 0:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"Job '{result['job_name']}' has no failed steps.",
                        }
                    ]
                }
            steps_text = "\n\n".join(f"""Step {step['number']}: {step['name']}
Status: {step['status']} / {step['conclusion']}
Started: {step['started_at']}
Completed: {step['completed_at']}

Log Excerpt:
{step['log_excerpt']}""" for step in result["failed_steps"])
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"""Failed Steps for Job: {result['job_name']}
Job Conclusion: {result['job_conclusion']}
Failed Steps: {result['failed_steps_count']}

{steps_text}""",
                    }
                ]
            }

        if tool_name == "get_job_logs_raw":
            result = await get_job_logs_raw(
                owner=arguments["owner"],
                repo=arguments["repo"],
                job_id=arguments["job_id"],
                start_line=arguments.get("start_line", 0),
                num_lines=arguments.get("num_lines", 500),
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"""Job Logs (Paginated)
Total Lines: {result['total_lines']}
Showing: Lines {result['start_line']} to {result['end_line']} ({result['num_lines_returned']} lines)

{result['lines']}""",
                    }
                ]
            }

        if tool_name == "search_job_logs":
            result = await search_job_logs(
                owner=arguments["owner"],
                repo=arguments["repo"],
                job_id=arguments["job_id"],
                pattern=arguments["pattern"],
                context_lines=arguments.get("context_lines", 5),
                case_sensitive=arguments.get("case_sensitive", False),
            )
            if result["total_matches"] == 0:
                return {
                    "content": [
                        {
                            "type": "text",
                            "text": f"No matches found for pattern: {result['pattern']}",
                        }
                    ]
                }
            matches_text = "\n\n".join(f"""Match at line {match['line_number']}:
{match['matched_line']}

Context:
{match['context']}""" for match in result["matches"])
            truncated_note = (
                f"\n\nNote: Showing first 50 of {result['total_matches']} matches."
                if result["truncated"]
                else ""
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": f"""Search Results for: {result['pattern']}
Total Matches: {result['total_matches']}{truncated_note}

{matches_text}""",
                    }
                ]
            }

        return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

    except Exception as e:
        logger.error(
            f"Tool execution failed: {tool_name}",
            exc_info=True,
            extra={
                "tool_name": tool_name,
                "error_type": type(e).__name__,
                "arguments": arguments,
            },
        )
        import traceback

        return {
            "error": {
                "code": -32603,
                "message": f"Tool execution failed: {type(e).__name__}: {e}",
                "data": {"traceback": traceback.format_exc()},
            }
        }


if __name__ == "__main__":
    asyncio.run(run_server("github_actions", handle_request))
