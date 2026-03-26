#!/usr/bin/env python3
"""
Standalone MCP server for GitHub Actions tools.

This server runs as a separate process and communicates via stdio.
It's automatically discovered and loaded by the Claude Agent SDK plugin system.
"""

import asyncio
import json
import sys
from typing import Any, Dict

from tools.github_actions import (
    get_failed_steps,
    get_job_logs,
    get_workflow_run_summary,
    search_job_logs,
)


async def handle_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Handle MCP tool requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "github-actions", "version": "1.0.0"},
        }

    elif method == "tools/list":
        return {
            "tools": [
                {
                    "name": "get_workflow_run_summary",
                    "description": "Get high-level summary of a workflow run without logs. Use this FIRST to identify failed jobs.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {
                                "type": "string",
                                "description": "Repository owner",
                            },
                            "repo": {
                                "type": "string",
                                "description": "Repository name",
                            },
                            "run_id": {
                                "type": "string",
                                "description": "Workflow run ID",
                            },
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
                            "owner": {
                                "type": "string",
                                "description": "Repository owner",
                            },
                            "repo": {
                                "type": "string",
                                "description": "Repository name",
                            },
                            "job_id": {
                                "type": "string",
                                "description": "Job ID from workflow run",
                            },
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
                    "name": "get_job_logs",
                    "description": "Get full logs for a specific job. Use only if failed steps aren't enough. Large logs written to .ci-logs/ directory.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "owner": {
                                "type": "string",
                                "description": "Repository owner",
                            },
                            "repo": {
                                "type": "string",
                                "description": "Repository name",
                            },
                            "job_id": {
                                "type": "string",
                                "description": "Job ID from workflow run",
                            },
                            "max_lines": {
                                "type": "integer",
                                "description": "Optional limit to last N lines",
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
                            "owner": {
                                "type": "string",
                                "description": "Repository owner",
                            },
                            "repo": {
                                "type": "string",
                                "description": "Repository name",
                            },
                            "job_id": {
                                "type": "string",
                                "description": "Job ID from workflow run",
                            },
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
        }

    elif method == "tools/call":
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
                                [
                                    f"  - [{job['conclusion'] or 'running'}] {job['name']} (ID: {job['id']})"
                                    for job in result["jobs"]
                                ]
                            ),
                        }
                    ]
                }

            elif tool_name == "get_failed_steps":
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

                steps_text = "\n\n".join([f"""Step {step['number']}: {step['name']}
Status: {step['status']} / {step['conclusion']}
Started: {step['started_at']}
Completed: {step['completed_at']}

Log Excerpt:
{step['log_excerpt']}""" for step in result["failed_steps"]])

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

            elif tool_name == "get_job_logs":
                result = await get_job_logs(
                    owner=arguments["owner"],
                    repo=arguments["repo"],
                    job_id=arguments["job_id"],
                    max_lines=arguments.get("max_lines"),
                )

                response_text = f"""Job Logs: {result['job_name']}
Status: {result['status']} / {result['conclusion']}
Original Size: {result['original_size']} characters
Truncated: {result['logs_truncated']}
"""

                if result.get("logs_file"):
                    response_text += f"\nLogs written to: {result['logs_file']}\n\nPreview:\n{result['logs']}"
                else:
                    response_text += f"\n{result['logs']}"

                return {"content": [{"type": "text", "text": response_text}]}

            elif tool_name == "search_job_logs":
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

                matches_text = "\n\n".join([f"""Match at line {match['line_number']}:
{match['matched_line']}

Context:
{match['context']}""" for match in result["matches"]])

                truncated_note = ""
                if result["truncated"]:
                    truncated_note = f"\n\nNote: Showing first 50 of {result['total_matches']} matches."

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

            else:
                return {
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
                }

        except Exception as e:
            return {
                "error": {"code": -32603, "message": f"Tool execution failed: {str(e)}"}
            }

    else:
        return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


async def main():
    """Main server loop - reads JSON-RPC requests from stdin, writes responses to stdout."""
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
            output: Dict[str, Any] = {"jsonrpc": "2.0", "id": request.get("id")}
            if "error" in response:
                output["error"] = response["error"]
            else:
                output["result"] = response

            sys.stdout.write(json.dumps(output) + "\n")
            sys.stdout.flush()

        except json.JSONDecodeError as e:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": f"Parse error: {str(e)}"},
                "id": None,
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()
        except Exception as e:
            error_response = {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
                "id": None,
            }
            sys.stdout.write(json.dumps(error_response) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
