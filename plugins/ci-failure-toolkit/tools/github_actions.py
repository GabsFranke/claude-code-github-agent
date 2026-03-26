"""
GitHub Actions workflow tools for CI failure analysis.

Provides progressive access to workflow run data:
1. Summary - High-level overview of run and jobs
2. Job logs - Full logs for specific job
3. Search - Find specific patterns in logs
4. Failed steps - Extract only failed step logs

For large outputs, automatically writes to temporary files.
"""

import os
import re
from pathlib import Path
from typing import Any

import httpx

# Threshold for writing to file (characters)
MAX_INLINE_SIZE = 10000  # ~10KB


def _maybe_write_to_file(content: str, prefix: str) -> dict[str, Any]:
    """
    Write content to file if it exceeds size threshold.

    Returns dict with either inline content or file path.
    """
    if len(content) <= MAX_INLINE_SIZE:
        return {"content": content, "truncated": False}

    # Write to temporary file in workspace
    workspace = os.getcwd()
    temp_dir = Path(workspace) / ".ci-logs"
    temp_dir.mkdir(exist_ok=True)

    # Create file with descriptive name
    temp_file = temp_dir / f"{prefix}_{os.getpid()}.log"
    temp_file.write_text(content, encoding="utf-8")

    # Return file path and preview
    preview = content[:1000] + "\n\n... (content too large, written to file) ..."

    return {
        "content": preview,
        "file_path": str(temp_file),
        "full_size": len(content),
        "truncated": True,
        "note": f"Full content written to {temp_file} ({len(content)} chars)",
    }


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
        raise ValueError("GITHUB_TOKEN not available in environment")

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


async def get_job_logs(
    owner: str,
    repo: str,
    job_id: str,
    max_lines: int | None = None,
) -> dict[str, Any]:
    """
    Get full logs for a specific job.

    Use after identifying failed job from summary.
    Optionally limit to last N lines for very long logs.
    Large logs are automatically written to file.

    Args:
        owner: Repository owner
        repo: Repository name
        job_id: Job ID from workflow run
        max_lines: Optional limit to last N lines (default: all)

    Returns:
        Dict with job info and logs (or file path if too large)
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN not available in environment")

    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Get job details
        job_url = f"https://api.github.com/repos/{owner}/{repo}/actions/jobs/{job_id}"
        job_resp = await client.get(job_url, headers=headers)
        job_resp.raise_for_status()
        job_data = job_resp.json()

        # Get logs
        logs_url = f"{job_url}/logs"
        logs_resp = await client.get(logs_url, headers=headers, follow_redirects=True)
        logs_resp.raise_for_status()
        logs_text = logs_resp.text

        # Optionally limit to last N lines
        original_size = len(logs_text)
        if max_lines:
            lines = logs_text.split("\n")
            if len(lines) > max_lines:
                logs_text = "\n".join(lines[-max_lines:])
                logs_text = (
                    f"... (showing last {max_lines} of {len(lines)} lines) ...\n\n"
                    + logs_text
                )

        # Write to file if too large
        logs_result = _maybe_write_to_file(logs_text, f"job_{job_id}_logs")

        return {
            "job_id": job_data["id"],
            "job_name": job_data["name"],
            "status": job_data["status"],
            "conclusion": job_data.get("conclusion"),
            "logs": logs_result["content"],
            "logs_file": logs_result.get("file_path"),
            "logs_truncated": logs_result["truncated"]
            or (max_lines is not None and original_size > len(logs_text)),
            "original_size": original_size,
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
        raise ValueError("GITHUB_TOKEN not available in environment")

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
    Extract only failed steps from a job with their logs.

    Automatically identifies failed steps and returns their logs.
    Most efficient way to diagnose CI failures.

    Args:
        owner: Repository owner
        repo: Repository name
        job_id: Job ID from workflow run
        log_lines_per_step: Max lines to include per failed step

    Returns:
        Dict with failed steps and their log excerpts
    """
    github_token = os.getenv("GITHUB_TOKEN")
    if not github_token:
        raise ValueError("GITHUB_TOKEN not available in environment")

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
                # Extract logs for this step (GitHub logs include timestamps and step markers)
                # This is a simplified extraction - in practice, you'd parse the log format
                step_name = step["name"]

                # Find step logs (simplified - actual implementation would parse log structure)
                step_logs = []
                in_step = False
                for line in logs_lines:
                    if step_name in line:
                        in_step = True
                    if in_step:
                        step_logs.append(line)
                        if len(step_logs) >= log_lines_per_step:
                            break

                failed_steps.append(
                    {
                        "name": step["name"],
                        "number": step["number"],
                        "status": step["status"],
                        "conclusion": step["conclusion"],
                        "started_at": step.get("started_at"),
                        "completed_at": step.get("completed_at"),
                        "log_excerpt": (
                            "\n".join(step_logs[-log_lines_per_step:])
                            if step_logs
                            else "No logs found"
                        ),
                    }
                )

        return {
            "job_id": job_data["id"],
            "job_name": job_data["name"],
            "job_conclusion": job_data.get("conclusion"),
            "failed_steps_count": len(failed_steps),
            "failed_steps": failed_steps,
        }
