#!/usr/bin/env python3
"""Enqueue a job into a Redis queue for testing services.

Run with --dry-run to see the message without pushing it.

Usage:
    python scripts/enqueue.py sync --repo owner/repo --ref main
    python scripts/enqueue.py cleanup --action expire_thread --repo owner/repo --thread-type pr --thread-id 42
    python scripts/enqueue.py indexing --repo owner/repo --ref main --trigger manual
    python scripts/enqueue.py memory --repo owner/repo --transcript-path /tmp/test.jsonl --hook-event Stop
    python scripts/enqueue.py retrospector --repo owner/repo --transcript-path /tmp/test.jsonl --hook-event Stop
    python scripts/enqueue.py agent --repo owner/repo --issue-number 42 --ref main --user tester --workflow-name review

Subcommands and arguments:

+---------------+---------------------------+----------------------------------------------+
| Subcommand    | Queue                     | Arguments                                    |
+===============+===========================+==============================================+
| sync          | agent:sync:requests      | --repo REQ  --ref REQ                        |
| cleanup       | agent:worktree:cleanup    | --action REQ  --repo REQ                     |
|               |                           |   expire_thread: --thread-type --thread-id   |
|               |                           |   revive_thread: --thread-type --thread-id   |
|               |                           |   cleanup_branch: --branch                   |
| indexing      | agent:indexing:requests  | --repo REQ  --ref REQ  [--trigger manual]   |
| memory        | agent:memory:requests    | --repo REQ  --transcript-path REQ            |
|               |                           | --hook-event {Stop,SubagentStop}             |
|               |                           | [--claude-md] [--memory-index]               |
| retrospector  | agent:retrospector:reqs  | --repo REQ  --transcript-path REQ            |
|               |                           | --hook-event {Stop,SubagentStop}            |
|               |                           | [--workflow-name] [--duration-ms]            |
|               |                           | [--num-turns] [--is-error] [--agent-id]      |
|               |                           | [--agent-type]                               |
| agent         | agent-requests           | --repo REQ  [--ref main] [--issue-number]   |
|               |                           | [--user] [--workflow-name] [--user-query]   |
|               |                           | [--command] [--event-type] [--event-action]  |
|               |                           | [--issue-state] [--installation-id]          |
| sandbox       | agent:jobs:pending       | --repo REQ  [--ref main] [--issue-number]   |
|               |                           | [--user] [--workflow-name] [--prompt]       |
|               |                           | [--github-token] [--session-mode]            |
|               |                           | [--thread-type] [--thread-id] [--streaming] |
|               |                           | [--persist] [--ttl-hours] [--max-turns]     |
|               |                           | [--auto-continue] [--summary-fallback]      |
+---------------+---------------------------+----------------------------------------------+
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

# Load .env file before importing shared modules
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    env_file = Path(__file__).resolve().parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value.strip('"').strip("'")

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.constants import JOB_DATA_PREFIX, JOB_STATUS_PREFIX, PENDING_JOB_QUEUE
from shared.queue import get_queue

# ---------------------------------------------------------------------------
# Queue definitions: name, description, builder function
# ---------------------------------------------------------------------------

QUEUE_REGISTRY = {
    "sync": {
        "queue": "agent:sync:requests",
        "description": "Repo sync worker — clones/fetches a repo ref",
    },
    "cleanup": {
        "queue": "agent:worktree:cleanup",
        "description": "Sandbox executor — worktree cleanup (expire/revive threads, cleanup branches)",
    },
    "indexing": {
        "queue": "agent:indexing:requests",
        "description": "Indexing worker — semantic code indexing",
    },
    "memory": {
        "queue": "agent:memory:requests",
        "description": "Memory worker — extract memories from transcripts",
    },
    "retrospector": {
        "queue": "agent:retrospector:requests",
        "description": "Retrospector worker — post-job analysis",
    },
    "agent": {
        "queue": "agent-requests",
        "description": "Agent worker — main entry point for webhook-triggered jobs",
    },
    "sandbox": {
        "queue": PENDING_JOB_QUEUE,
        "description": "Sandbox executor — full job lifecycle (creates JobQueue entry)",
    },
}


def build_sync_message(args: argparse.Namespace) -> dict:
    return {"repo": args.repo, "ref": args.ref}


def build_cleanup_message(args: argparse.Namespace) -> dict:
    action = args.action
    msg: dict = {"action": action, "repo": args.repo}
    if action == "expire_thread":
        msg["thread_type"] = args.thread_type
        msg["thread_id"] = args.thread_id
    elif action == "revive_thread":
        msg["thread_type"] = args.thread_type
        msg["thread_id"] = args.thread_id
    elif action == "cleanup_branch":
        msg["branch"] = args.branch
    return msg


def build_indexing_message(args: argparse.Namespace) -> dict:
    return {"repo": args.repo, "ref": args.ref, "trigger": args.trigger}


def build_memory_message(args: argparse.Namespace) -> dict:
    return {
        "repo": args.repo,
        "transcript_path": args.transcript_path,
        "hook_event": args.hook_event,
        "claude_md": args.claude_md,
        "memory_index": args.memory_index,
    }


def build_retrospector_message(args: argparse.Namespace) -> dict:
    return {
        "repo": args.repo,
        "transcript_path": args.transcript_path,
        "hook_event": args.hook_event,
        "workflow_name": args.workflow_name,
        "session_meta": {
            "duration_ms": args.duration_ms,
            "num_turns": args.num_turns,
            "is_error": args.is_error,
            "agent_id": args.agent_id,
            "agent_type": args.agent_type,
        },
    }


def build_agent_message(args: argparse.Namespace) -> dict:
    return {
        "repository": args.repo,
        "issue_number": args.issue_number,
        "event_data": {
            "event_type": args.event_type,
            "action": args.event_action,
            "issue_state": args.issue_state,
            "installation_id": args.installation_id,
            "command": args.command or None,
        },
        "user_query": args.user_query or "",
        "user": args.user,
        "ref": args.ref,
        "workflow_name": args.workflow_name,
    }


def build_sandbox_message(args: argparse.Namespace) -> dict:
    """Build a full JobQueue-compatible job and return the job_id + data.

    The sandbox worker uses JobQueue, which stores job data in a separate
    Redis key and pushes only the job_id onto the pending list. We need
    to create both the data key and the pending entry.
    """
    job_id = str(uuid.uuid4())
    job_data = {
        "repo": args.repo,
        "issue_number": args.issue_number,
        "ref": args.ref,
        "prompt": args.prompt or "Review the changes in this PR.",
        "system_context": args.system_context or None,
        "claude_md": args.claude_md or None,
        "memory_index": args.memory_index or None,
        "github_token": args.github_token or "test-token",
        "user": args.user,
        "workflow_name": args.workflow_name,
        "user_query": args.user_query or "",
        "event_data": {
            "event_type": args.event_type,
            "action": args.event_action,
            "issue_state": args.issue_state,
            "installation_id": args.installation_id,
        },
        "parent_span_id": None,
        "context_profile": None,
        "session_mode": args.session_mode,
        "session_id": None,
        "thread_type": args.thread_type,
        "thread_id": (
            args.thread_id or str(args.issue_number) if args.issue_number else None
        ),
        "conversation_config": {
            "persist": args.persist,
            "ttl_hours": args.ttl_hours,
            "max_turns": args.max_turns,
            "auto_continue": args.auto_continue,
            "summary_fallback": args.summary_fallback,
        },
        "conversation_summary": None,
        "streaming_enabled": args.streaming,
        "session_token": None,
    }
    return {"job_id": job_id, "job_data": job_data}


BUILDERS = {
    "sync": build_sync_message,
    "cleanup": build_cleanup_message,
    "indexing": build_indexing_message,
    "memory": build_memory_message,
    "retrospector": build_retrospector_message,
    "agent": build_agent_message,
    "sandbox": build_sandbox_message,
}


# ---------------------------------------------------------------------------
# Push logic
# ---------------------------------------------------------------------------


async def push_job(queue_name: str, message: dict, *, dry_run: bool = False) -> None:
    """Push a message to a Redis queue, or print it if dry_run."""
    if dry_run:
        print(f"\nQueue: {queue_name}")
        print(f"Message:\n{json.dumps(message, indent=2)}\n")
        return

    q = get_queue(queue_name=queue_name)
    await q.publish(message)
    await q.close()
    print(f"Pushed to {queue_name}")


async def push_sandbox_job(
    job_id: str, job_data: dict, *, dry_run: bool = False
) -> None:
    """Create a full JobQueue entry for the sandbox executor."""
    import redis.asyncio as redis

    if dry_run:
        print(f"\nQueue: {PENDING_JOB_QUEUE}")
        print(f"Job ID: {job_id}")
        print(f"Job data key: {JOB_DATA_PREFIX}{job_id}")
        print(f"Job status key: {JOB_STATUS_PREFIX}{job_id}")
        print(f"Job data:\n{json.dumps(job_data, indent=2)}\n")
        return

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379")
    redis_password = os.getenv("REDIS_PASSWORD")
    r = await redis.from_url(redis_url, decode_responses=True, password=redis_password)

    ttl = int(os.getenv("JOB_TTL_SECONDS", "3600"))
    await r.setex(f"{JOB_DATA_PREFIX}{job_id}", ttl, json.dumps(job_data))
    await r.setex(f"{JOB_STATUS_PREFIX}{job_id}", ttl, "pending")
    await r.rpush(PENDING_JOB_QUEUE, job_id)
    await r.aclose()

    print(f"Created sandbox job {job_id}")
    print(f"  Data:   {JOB_DATA_PREFIX}{job_id}")
    print(f"  Status: {JOB_STATUS_PREFIX}{job_id}")
    print(f"  Queue:  {PENDING_JOB_QUEUE}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo", required=True, help="Repository (e.g. owner/repo)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the message without pushing to Redis",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enqueue a test job into a Redis queue",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  {name:15s} {info['description']}"
            for name, info in QUEUE_REGISTRY.items()
        ),
    )
    sub = parser.add_subparsers(dest="service", required=True)

    # -- sync --
    p = sub.add_parser("sync", help="Enqueue a repo sync job")
    add_common_args(p)
    p.add_argument("--ref", required=True, help="Git ref (branch, tag, SHA)")

    # -- cleanup --
    p = sub.add_parser("cleanup", help="Enqueue a worktree cleanup job")
    add_common_args(p)
    p.add_argument(
        "--action",
        required=True,
        choices=["expire_thread", "revive_thread", "cleanup_branch"],
    )
    p.add_argument("--thread-type", help="Thread type (pr, issue, discussion)")
    p.add_argument("--thread-id", help="Thread ID (issue/PR number)")
    p.add_argument("--branch", help="Branch name (for cleanup_branch action)")

    # -- indexing --
    p = sub.add_parser("indexing", help="Enqueue an indexing job")
    add_common_args(p)
    p.add_argument("--ref", required=True, help="Git ref")
    p.add_argument(
        "--trigger", default="manual", help="Trigger reason (default: manual)"
    )

    # -- memory --
    p = sub.add_parser("memory", help="Enqueue a memory extraction job")
    add_common_args(p)
    p.add_argument("--transcript-path", required=True, help="Path to transcript file")
    p.add_argument(
        "--hook-event",
        required=True,
        choices=["Stop", "SubagentStop"],
        help="Hook event",
    )
    p.add_argument("--claude-md", default=None, help="CLAUDE.md content")
    p.add_argument("--memory-index", default=None, help="Memory index content")

    # -- retrospector --
    p = sub.add_parser("retrospector", help="Enqueue a retrospector job")
    add_common_args(p)
    p.add_argument("--transcript-path", required=True, help="Path to transcript file")
    p.add_argument(
        "--hook-event",
        required=True,
        choices=["Stop", "SubagentStop"],
        help="Hook event",
    )
    p.add_argument("--workflow-name", default=None, help="Workflow name")
    p.add_argument(
        "--duration-ms", type=int, default=5000, help="Session duration in ms"
    )
    p.add_argument("--num-turns", type=int, default=1, help="Number of turns")
    p.add_argument("--is-error", action="store_true", help="Session ended in error")
    p.add_argument("--agent-id", default="test-agent", help="Agent ID")
    p.add_argument("--agent-type", default="claude", help="Agent type")

    # -- agent (webhook entry) --
    p = sub.add_parser("agent", help="Enqueue an agent worker job (webhook format)")
    add_common_args(p)
    p.add_argument("--ref", default="main", help="Git ref (default: main)")
    p.add_argument("--issue-number", type=int, default=None, help="Issue/PR number")
    p.add_argument("--user", default="tester", help="Triggering user")
    p.add_argument("--workflow-name", default="review", help="Workflow name")
    p.add_argument("--user-query", default="", help="User query text")
    p.add_argument("--command", default=None, help="Slash command (e.g. /review)")
    p.add_argument("--event-type", default="issue_comment", help="GitHub event type")
    p.add_argument("--event-action", default="created", help="GitHub event action")
    p.add_argument("--issue-state", default="open", help="Issue state")
    p.add_argument("--installation-id", default="0", help="GitHub installation ID")

    # -- sandbox (full JobQueue entry) --
    p = sub.add_parser("sandbox", help="Create a full sandbox executor job")
    add_common_args(p)
    p.add_argument("--ref", default="main", help="Git ref")
    p.add_argument("--issue-number", type=int, default=None, help="Issue/PR number")
    p.add_argument("--user", default="tester", help="Triggering user")
    p.add_argument("--workflow-name", default="review", help="Workflow name")
    p.add_argument("--prompt", default=None, help="Prompt for the agent")
    p.add_argument("--user-query", default="", help="User query text")
    p.add_argument(
        "--github-token", default=None, help="GitHub token (default: test-token)"
    )
    p.add_argument("--claude-md", default=None, help="CLAUDE.md content")
    p.add_argument("--memory-index", default=None, help="Memory index content")
    p.add_argument("--system-context", default=None, help="System context")
    p.add_argument("--session-mode", default="new", choices=["new", "resume", "fork"])
    p.add_argument("--thread-type", default="pr", help="Thread type")
    p.add_argument("--thread-id", default=None, help="Thread ID")
    p.add_argument("--event-type", default="issue_comment", help="Event type")
    p.add_argument("--event-action", default="created", help="Event action")
    p.add_argument("--issue-state", default="open", help="Issue state")
    p.add_argument("--installation-id", default="0", help="Installation ID")
    p.add_argument(
        "--persist", action="store_true", default=True, help="Persist conversation"
    )
    p.add_argument("--ttl-hours", type=int, default=720, help="Conversation TTL hours")
    p.add_argument("--max-turns", type=int, default=50, help="Max turns")
    p.add_argument(
        "--auto-continue", action="store_true", default=True, help="Auto-continue"
    )
    p.add_argument(
        "--summary-fallback", action="store_true", default=True, help="Summary fallback"
    )
    p.add_argument(
        "--streaming", action="store_true", default=False, help="Enable streaming"
    )

    return parser


async def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    service = args.service
    if service not in BUILDERS:
        parser.error(f"Unknown service: {service}")

    builder = BUILDERS[service]
    message = builder(args)

    if service == "sandbox":
        job_id = message.pop("job_id")
        job_data = message.pop("job_data")
        await push_sandbox_job(job_id, job_data, dry_run=args.dry_run)
    else:
        queue_name = QUEUE_REGISTRY[service]["queue"]
        await push_job(queue_name, message, dry_run=args.dry_run)


if __name__ == "__main__":
    asyncio.run(main())
