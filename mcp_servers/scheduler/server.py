#!/usr/bin/env python3
"""MCP server for scheduling time-based workflow runs.

Auto-discovered from mcp_servers/scheduler/ by the SDK factory.
"""

import asyncio
import datetime
import logging
import os
import sys
from pathlib import Path
from typing import Any

import httpx

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from mcp_servers.base import run_server  # noqa: E402

logger = logging.getLogger(__name__)

TOOLS = [
    {
        "name": "schedule_one_shot_task",
        "description": "Schedule a one-shot execution of a workflow on a repository to run at a specific time or after a delay.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflow_name": {
                    "type": "string",
                    "description": "The name of the workflow to run (e.g. 'review-pr', 'stale-pr-checker')",
                },
                "run_at": {
                    "type": "string",
                    "description": "ISO-8601 formatted absolute datetime string (e.g., '2026-06-01T12:00:00Z'). Either this or delay_seconds is required.",
                },
                "delay_seconds": {
                    "type": "integer",
                    "description": "Number of seconds from now to delay execution. Either this or run_at is required.",
                },
                "issue_number": {
                    "type": "integer",
                    "description": "Optional pull request or issue number to target.",
                },
                "ref": {
                    "type": "string",
                    "description": "Git ref/branch to use (defaults to 'main').",
                    "default": "main",
                },
                "user_query": {
                    "type": "string",
                    "description": "Optional instructions or query text to pass into the workflow run.",
                    "default": "",
                },
            },
            "required": ["workflow_name"],
        },
    }
]


async def handle_request(request: dict[str, Any]) -> dict[str, Any]:
    """Handle MCP tool requests."""
    method = request.get("method")
    params = request.get("params", {})

    if method == "initialize":
        return {
            "protocolVersion": params.get("protocolVersion", "2024-11-05"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "scheduler", "version": "1.0.0"},
        }

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        return await _handle_tool_call(params)

    return {"error": {"code": -32601, "message": f"Unknown method: {method}"}}


async def _call_scheduler_api(payload: dict[str, Any]) -> dict[str, Any]:
    """Call the scheduler service FastAPI endpoint, attempting fallback hosts if needed."""
    # Build list of candidate URLs
    scheduler_url = os.getenv("SCHEDULER_URL")
    urls = []
    if scheduler_url:
        urls.append(scheduler_url.rstrip("/") + "/schedule/one-shot")
    urls.append("http://scheduler:8082/schedule/one-shot")
    urls.append("http://localhost:8082/schedule/one-shot")

    # The scheduler requires a shared secret on this endpoint; without it the
    # call is rejected rather than silently scheduling on an open endpoint.
    internal_token = os.getenv("SCHEDULER_INTERNAL_TOKEN", "")
    if not internal_token:
        raise Exception(
            "SCHEDULER_INTERNAL_TOKEN is not set for this MCP server, so the "
            "scheduler will reject the request. Ask the operator to configure it."
        )
    headers = {"X-Internal-Token": internal_token}

    last_error = None
    async with httpx.AsyncClient(timeout=10.0) as client:
        for url in urls:
            try:
                logger.info(f"Attempting to call scheduler service at: {url}")
                response = await client.post(url, json=payload, headers=headers)
                if response.status_code == 200:
                    result: dict[str, Any] = response.json()
                    return result
                logger.warning(
                    f"Scheduler at {url} returned status code: {response.status_code}"
                )
                last_error = f"HTTP {response.status_code}: {response.text}"
            except Exception as e:
                logger.warning(f"Failed to connect to scheduler at {url}: {e}")
                last_error = str(e)

    raise Exception(f"Could not connect to scheduler service. Last error: {last_error}")


async def _handle_tool_call(params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tools/call request to schedule a task."""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name != "schedule_one_shot_task":
        return {"error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}}

    workflow_name = arguments.get("workflow_name")
    run_at_str = arguments.get("run_at")
    delay_seconds = arguments.get("delay_seconds")
    issue_number = arguments.get("issue_number")
    ref = arguments.get("ref", "main")
    user_query = arguments.get("user_query", "")

    # Get repository from environment
    repo = os.getenv("GITHUB_REPOSITORY")
    if not repo:
        return {
            "error": {
                "code": -32603,
                "message": "GITHUB_REPOSITORY environment variable not set",
            }
        }

    # Validate trigger inputs
    if not run_at_str and delay_seconds is None:
        return {
            "error": {
                "code": -32602,
                "message": "Either 'run_at' or 'delay_seconds' must be provided.",
            }
        }

    # Resolve schedule run_at datetime
    run_at_dt = None
    if run_at_str:
        try:
            # Parse ISO datetime
            run_at_str_clean = run_at_str.replace("Z", "+00:00")
            run_at_dt = datetime.datetime.fromisoformat(run_at_str_clean)
        except ValueError as e:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Invalid ISO-8601 date format for 'run_at': {e}",
                }
            }
    else:
        # Calculate run_at based on delay
        try:
            delay = int(delay_seconds)
            if delay < 0:
                raise ValueError("Delay cannot be negative")
            run_at_dt = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
                seconds=delay
            )
        except ValueError as e:
            return {
                "error": {
                    "code": -32602,
                    "message": f"Invalid value for 'delay_seconds': {e}",
                }
            }

    # Prepare payload for API call
    payload = {
        "workflow_name": workflow_name,
        "repo": repo,
        "run_at": run_at_dt.isoformat(),
        "issue_number": issue_number,
        "ref": ref,
        "user": "agent",
        "user_query": user_query,
    }

    try:
        result = await _call_scheduler_api(payload)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"One-shot workflow '{workflow_name}' successfully scheduled.\n"
                    f"- Job ID: {result.get('job_id')}\n"
                    f"- Execution time: {result.get('run_at')} (UTC)\n"
                    f"- Target Repository: {repo}",
                }
            ]
        }
    except Exception as e:
        logger.error(f"Tool execution failed: {e}", exc_info=True)
        return {
            "error": {
                "code": -32603,
                "message": f"Failed to communicate with scheduler service: {e}",
            }
        }


if __name__ == "__main__":
    asyncio.run(run_server("scheduler", handle_request))
