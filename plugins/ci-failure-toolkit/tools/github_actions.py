"""
GitHub Actions workflow tools for CI failure analysis.

Provides progressive access to workflow run data:
1. Summary - High-level overview of run and jobs
2. Job logs - Full logs for specific job
3. Search - Find specific patterns in logs
4. Failed steps - Extract only failed step logs
"""

import os
import re
import sys
from pathlib import Path
from typing import Any

import httpx

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))

from shared.exceptions import AuthenticationError  # noqa: E402


async def get_workflow_run_summary(
    owner: str,
    repo: str,
    run_id: str,
) -> dict[str, Any]:
    """
    Get high-level summary of a workflow run without logs.

    Returns metadata and job list with status/conclusion.
    Use this first to identify which jobs failed.

    Args:
        owner: Repository owner
        repo: Repository name
        run_id: Workflow run ID

    Returns:
        Dict with run metadata and job summaries (no logs)
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise AuthenticationError("GITHUB_TOKEN not available in environment")

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # Get run info
        run_url = f"https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}"
        run_resp = await client.get(run_url, headers=headers)
        run_resp.raise_for_status()
        run_data = run_resp.json()

        # Get jobs
        jobs_url = f"{run_url}/jobs"
        jobs_resp = await client.get(jobs_url, headers=headers)
        jobs_resp.raise_for_status()
        jobs_data = jobs_resp.json()

        # Extract only essential job info (no logs)
        jobs_summary = []
        for job in jobs_data.get("jobs", []):
            jobs_summary.append(
                {
                    "id": job["id"],
                    "name": job["name"],
                    "status": job["status"],
                    "conclusion": job.get("conclusion"),
                    "started_at": job.get("started_at"),
                    "completed_at": job.get("completed_at"),
                }
            )

        return {
            "run_id": run_data["id"],
            "name": run_data["name"],
            "status": run_data["status"],
            "conclusion": run_data.get("conclusion"),
            "event": run_data["event"],
            "created_at": run_data["created_at"],
            "updated_at": run_data["updated_at"],
            "html_url": run_data["html_url"],
            "jobs": jobs_summary,
        }


async def get_job_logs_raw(
    owner: str,
    repo: str,
    job_id: str,
    start_line: int = 0,
    num_lines: int = 500,
) -> dict[str, Any]:
    """
    Get a paginated slice of job logs without formatting.

    Returns raw log lines with timestamps stripped. Call repeatedly with
    different start_line values to paginate through large logs.

    This is the PREFERRED way to read logs - it avoids "output too large" errors
    by letting you read logs in manageable chunks.

    Args:
        owner: Repository owner
        repo: Repository name
        job_id: Job ID from workflow run
        start_line: Line number to start from (0-indexed)
        num_lines: Number of lines to return (default: 500)

    Returns:
        Dict with:
        - total_lines: Total number of lines in the log
        - start_line: Starting line of this chunk
        - end_line: Ending line of this chunk
        - lines: The actual log content (timestamps stripped)

    Example:
        # Read first 500 lines
        chunk1 = get_job_logs_raw(owner, repo, job_id, start_line=0, num_lines=500)

        # Read next 500 lines
        chunk2 = get_job_logs_raw(owner, repo, job_id, start_line=500, num_lines=500)

        # Read last 500 lines (if total is 2000)
        chunk_end = get_job_logs_raw(owner, repo, job_id, start_line=1500, num_lines=500)
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise AuthenticationError("GITHUB_TOKEN not available in environment")

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get logs
        logs_url = (
            f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        )
        logs_resp = await client.get(logs_url, headers=headers, follow_redirects=True)
        logs_resp.raise_for_status()
        logs_text = logs_resp.text

        # Split into lines
        lines = logs_text.split("\n")

        # Strip GitHub Actions timestamps (format: 2024-03-27T12:34:56.789Z)
        clean_lines = [
            re.sub(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+", "", line) for line in lines
        ]

        # Extract requested chunk
        chunk = clean_lines[start_line : start_line + num_lines]

        return {
            "total_lines": len(clean_lines),
            "start_line": start_line,
            "end_line": start_line + len(chunk),
            "num_lines_returned": len(chunk),
            "lines": "\n".join(chunk),
        }


async def search_job_logs(
    owner: str,
    repo: str,
    job_id: str,
    pattern: str,
    context_lines: int = 5,
    case_sensitive: bool = False,
) -> dict[str, Any]:
    """
    Search for specific patterns in job logs.

    Returns matching lines with surrounding context.
    Useful for finding specific errors in very long logs.

    Args:
        owner: Repository owner
        repo: Repository name
        job_id: Job ID from workflow run
        pattern: Regex pattern or keyword to search
        context_lines: Number of lines before/after match to include
        case_sensitive: Whether search is case-sensitive

    Returns:
        Dict with matches and context
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise AuthenticationError("GITHUB_TOKEN not available in environment")

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get logs
        logs_url = (
            f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
        )
        logs_resp = await client.get(logs_url, headers=headers, follow_redirects=True)
        logs_resp.raise_for_status()
        logs_text = logs_resp.text

        # Search for pattern
        flags = 0 if case_sensitive else re.IGNORECASE
        lines = logs_text.split("\n")
        matches = []

        for i, line in enumerate(lines):
            if re.search(pattern, line, flags):
                # Extract context
                start = max(0, i - context_lines)
                end = min(len(lines), i + context_lines + 1)
                context = "\n".join(lines[start:end])

                matches.append(
                    {
                        "line_number": i + 1,
                        "matched_line": line,
                        "context": context,
                    }
                )

        return {
            "job_id": job_id,
            "pattern": pattern,
            "total_matches": len(matches),
            "matches": matches[:50],  # Limit to first 50 matches
            "truncated": len(matches) > 50,
        }


async def get_failed_steps(
    owner: str,
    repo: str,
    job_id: str,
    log_lines_per_step: int = 100,
) -> dict[str, Any]:
    """
    Extract failed steps from a job with relevant log sections.

    Returns failed step metadata and the last N lines of the full job log.
    GitHub doesn't provide per-step logs via API, so we return the full log
    with failed step markers to help identify relevant sections.

    Args:
        owner: Repository owner
        repo: Repository name
        job_id: Job ID from workflow run
        log_lines_per_step: Max lines from end of log to include (default: 100)

    Returns:
        Dict with failed steps metadata and log excerpt
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise AuthenticationError("GITHUB_TOKEN not available in environment")

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get job details with steps
        job_url = f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}"
        job_resp = await client.get(job_url, headers=headers)
        job_resp.raise_for_status()
        job_data = job_resp.json()

        # Get full logs
        logs_url = f"{job_url}/logs"
        logs_resp = await client.get(logs_url, headers=headers, follow_redirects=True)
        logs_resp.raise_for_status()
        logs_text = logs_resp.text
        logs_lines = logs_text.split("\n")

        # Find failed steps
        failed_steps = []
        for step in job_data.get("steps", []):
            if step.get("conclusion") == "failure":
                failed_steps.append(
                    {
                        "name": step["name"],
                        "number": step["number"],
                        "status": step["status"],
                        "conclusion": step["conclusion"],
                        "started_at": step.get("started_at"),
                        "completed_at": step.get("completed_at"),
                    }
                )

        # Calculate how many lines to include
        # For multiple failed steps, give more context
        total_lines = len(logs_lines)
        lines_to_include = min(
            log_lines_per_step * max(len(failed_steps), 1), total_lines
        )

        # Get the last N lines (where errors usually are)
        log_excerpt = "\n".join(logs_lines[-lines_to_include:])

        # Add truncation notice if needed
        if lines_to_include < total_lines:
            log_excerpt = (
                f"... (showing last {lines_to_include} of {total_lines} lines) ...\n\n"
                + log_excerpt
            )

        return {
            "job_id": job_data["id"],
            "job_name": job_data["name"],
            "job_conclusion": job_data.get("conclusion"),
            "failed_steps_count": len(failed_steps),
            "failed_steps": failed_steps,
            "log_excerpt": log_excerpt,
            "log_excerpt_lines": lines_to_include,
            "total_log_lines": total_lines,
        }
