"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Add parent directory to path for shared imports - must be before other imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# flake8: noqa: E402 - imports after path modification
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from shared import JobQueue
from subagents import AGENTS

# Configure logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Global state
shutdown_event = asyncio.Event()


def handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    logger.info("Received signal %s, initiating graceful shutdown...", signum)
    shutdown_event.set()


def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


def setup_langfuse_hooks() -> dict:
    """Setup Langfuse observability hooks if configured."""
    span_id = os.getenv("CURRENT_SPAN_ID")
    langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY")

    if not (langfuse_public_key and langfuse_secret_key):
        return {}

    async def langfuse_stop_hook_async(input_data, _tool_use_id, _context):
        error_msg = None
        process = None
        try:
            hook_payload = json.dumps(input_data)
            process = await asyncio.create_subprocess_exec(
                "python3",
                "/app/hooks/langfuse_hook.py",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "TRACE_TO_LANGFUSE": "true",
                    "LANGFUSE_PUBLIC_KEY": langfuse_public_key,
                    "LANGFUSE_SECRET_KEY": langfuse_secret_key,
                    "LANGFUSE_HOST": os.getenv("LANGFUSE_HOST", "http://langfuse:3000"),
                    "LANGFUSE_BASE_URL": os.getenv(
                        "LANGFUSE_HOST", "http://langfuse:3000"
                    ),
                    "CC_LANGFUSE_DEBUG": "true",
                    "PARENT_SPAN_ID": span_id or "",
                    "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                    "HOME": os.environ.get("HOME", "/root"),
                },
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(input=hook_payload.encode()), timeout=30.0
                )
                if process.returncode != 0:
                    logger.warning(
                        f"Langfuse hook failed: {stderr.decode()}\nOutput: {stdout.decode()}"
                    )
                else:
                    logger.debug(f"Langfuse hook succeeded: {stdout.decode()}")
                    return {"success": True}
            except TimeoutError:
                logger.warning("Langfuse hook timed out after 30s")
                process.kill()
                await process.wait()

        except Exception as e:
            logger.warning(f"Error running Langfuse hook: {e}")
            error_msg = str(e)
        finally:
            # Ensure process is cleaned up if it exists and hasn't been waited on
            if process and process.returncode is None:
                try:
                    process.kill()
                    await process.wait()
                except Exception:
                    pass  # Process already terminated

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def execute_git_command(cmd: str, cwd: str | None = None) -> tuple[int, str, str]:
    """Execute a git command asynchronously."""
    process = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode or 0, stdout.decode().strip(), stderr.decode().strip()


async def ensure_repo_synced(
    repo: str, ref: str, redis_client, github_token: str
) -> str:
    """Ensure bare repo is synced, tracking complete flag with fallback to lock + clone."""
    complete_key = f"agent:sync:complete:{repo}:{ref}"
    lock_key = f"agent:sync:lock:{repo}"
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{repo}.git")
    auth_prefix = f"https://x-access-token:{github_token}@"

    # Wait for up to 20 seconds for sync worker (increased from 15)
    for i in range(20):
        is_complete = await redis_client.get(complete_key)
        if is_complete:
            logger.info(f"Sync completed for {repo} after {i} seconds")
            return repo_dir
        await asyncio.sleep(1)

    logger.warning(
        f"Sync worker timeout for {repo} after 20s, executing fallback sync..."
    )
    os.makedirs(os.path.dirname(repo_dir), exist_ok=True)

    # Fallback to doing it ourselves
    try:
        lock = redis_client.lock(lock_key, timeout=300)
        acquired = await lock.acquire(blocking=True, blocking_timeout=10)
        if not acquired:
            logger.warning(
                f"Fallback couldn't acquire lock for {repo}, assuming it's synced"
            )
            return repo_dir

        try:
            if not os.path.exists(repo_dir):
                logger.info(f"Fallback clone for {repo}...")
                clone_url = f"{auth_prefix}github.com/{repo}.git"
                cmd = f"git clone --bare {clone_url} {repo_dir}"
                await execute_git_command(cmd)
            else:
                logger.info(f"Fallback fetch for {repo}...")
                cmd = f"git --git-dir={repo_dir} fetch origin '*:refs/remotes/origin/*'"
                await execute_git_command(cmd)

            await redis_client.set(complete_key, "1", ex=300)  # 5 minutes TTL
        finally:
            await lock.release()

    except Exception as e:
        logger.error(f"Fallback sync failed: {e}")

    return repo_dir


async def execute_in_workspace(workspace: str, job_data: dict) -> str:
    """Execute Claude Agent SDK in isolated workspace.

    Args:
        workspace: Path to isolated workspace directory
        job_data: Job data containing prompt, github_token, etc.

    Returns:
        Agent response text
    """
    original_cwd = os.getcwd()

    try:
        # Change to isolated workspace
        os.chdir(workspace)
        logger.info(f"Executing in workspace: {workspace}")

        # Set Claude temp directory to workspace to keep all files accessible
        os.environ["CLAUDE_TEMP_DIR"] = workspace
        os.environ["TMPDIR"] = workspace  # Fallback for general temp operations

        # Build MCP server configuration
        mcp_servers = {
            "github": {
                "type": "http",
                "url": "https://api.githubcopilot.com/mcp",
                "headers": {"Authorization": f"Bearer {job_data['github_token']}"},
            }
        }

        # Setup hooks
        hooks = setup_langfuse_hooks()

        # Build Claude Agent options
        options = ClaudeAgentOptions(
            allowed_tools=[
                "Task",
                "Bash",
                "Read",
                "Write",
                "Edit",
                "List",
                "Search",
                "mcp__github__*",
            ],
            permission_mode="acceptEdits",
            mcp_servers=mcp_servers,  # type: ignore[arg-type]
            agents=AGENTS,
            plugins=[{"type": "local", "path": "/app/plugins/pr-review-toolkit"}],
            hooks=hooks,
            max_turns=50,
            cwd=workspace,  # Set working directory to isolated workspace
        )

        # Execute SDK
        logger.info("Starting Claude Agent SDK execution...")
        response_parts = []

        async with asyncio.timeout(1800):  # 30 minutes
            async with ClaudeSDKClient(options=options) as client:
                await client.query(job_data["prompt"])

                async for message in client.receive_messages():
                    # Check for shutdown
                    if shutdown_event.is_set():
                        logger.warning("Shutdown requested, stopping SDK execution")
                        break

                    if isinstance(message, AssistantMessage):
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                response_parts.append(block.text)
                                logger.debug(
                                    f"Received text block: {block.text[:100]}..."
                                )
                            elif isinstance(block, ToolUseBlock):
                                logger.info(f"Tool use: {block.name}")

                    elif isinstance(message, ResultMessage):
                        logger.info(
                            f"SDK completed - {message.num_turns} turns, "
                            f"{message.duration_ms}ms"
                        )
                        if message.total_cost_usd:
                            logger.info(f"Cost: ${message.total_cost_usd:.4f}")
                        break

        response = "\n".join(response_parts)

        if not response or not response.strip():
            raise Exception("Claude Agent SDK returned empty response")

        logger.info("SDK execution completed successfully")
        return response

    except TimeoutError as e:
        raise Exception("Claude Agent SDK execution timed out after 30 minutes") from e
    except Exception as e:
        raise Exception(f"Failed to execute Claude Agent SDK: {e}") from e
    finally:
        # Always restore original working directory
        os.chdir(original_cwd)


async def process_job(job_queue: JobQueue, job_id: str, job_data: dict) -> None:
    """Process a single job in an isolated workspace.

    Args:
        job_queue: Job queue instance
        job_id: Job identifier
        job_data: Job data dictionary
    """
    workspace = None
    repo_dir = None

    try:
        # Validate job_id format for security (prevent directory traversal)
        try:
            uuid.UUID(job_id)
        except (ValueError, AttributeError):
            logger.error(f"Invalid job_id format: {job_id}")
            await job_queue.complete_job(
                job_id,
                {
                    "status": "error",
                    "error": f"Invalid job_id format: {job_id}",
                    "repo": job_data.get("repo", "unknown"),
                    "issue_number": job_data.get("issue_number", 0),
                },
                status="error",
            )
            return

        # Ensure repo is synced and setup worktree
        repo = job_data["repo"]
        ref = job_data.get("ref", "main")
        logger.info(f"Setting up worktree for {repo} (ref {ref})")

        repo_dir = await ensure_repo_synced(
            repo, ref, job_queue.redis, job_data["github_token"]
        )

        # Create isolated workspace in tmpfs mount for automatic cleanup
        # /tmp is intentional - mounted as tmpfs in Docker for security
        workspace_base = tempfile.mkdtemp(
            prefix=f"job_{job_id[:8]}_",
            dir="/tmp",  # nosec B108
        )
        os.rmdir(workspace_base)  # git worktree add needs it to not exist
        workspace = workspace_base
        logger.info(f"Created workspace for job {job_id}: {workspace}")

        # Create worktree with unique branch name
        base_ref = ref.replace("refs/", "") if ref.startswith("refs/") else ref
        # In bare repos, branches are at refs/heads/* not refs/remotes/origin/*
        bare_ref = base_ref if base_ref.startswith("heads/") else f"heads/{base_ref}"

        # Generate unique branch name with timestamp to avoid collisions
        timestamp = int(time.time() * 1000) % 1000000  # Last 6 digits of milliseconds
        branch_name = f"job-{job_id[:8]}-{timestamp}"

        # First, try to create worktree with new branch from the specified ref
        wt_cmd = f"git --git-dir={repo_dir} worktree add -b {branch_name} {workspace} {bare_ref}"
        code, _out, err = await execute_git_command(wt_cmd)

        if code != 0:
            logger.warning(
                f"Worktree ref {bare_ref} failed: {err}. Trying to detect default branch..."
            )

            # List all branches and pick the first one (usually main or master)
            list_cmd = f"git --git-dir={repo_dir} branch --list"
            list_code, list_out, list_err = await execute_git_command(list_cmd)

            default_branch = "heads/main"  # Fallback
            if list_code == 0 and list_out:
                # Output is like "* main" or "  master", pick first branch
                branches = [
                    b.strip().lstrip("* ") for b in list_out.split("\n") if b.strip()
                ]
                if branches:
                    default_branch = f"heads/{branches[0]}"
                    logger.info(f"Detected default branch: {default_branch}")
            else:
                logger.warning(
                    f"Could not list branches: {list_err}. Using fallback: {default_branch}"
                )

            # Try with detected default branch
            wt_cmd_fallback = f"git --git-dir={repo_dir} worktree add -b {branch_name} {workspace} {default_branch}"
            code, _out, err = await execute_git_command(wt_cmd_fallback)
            if code != 0:
                # If still failing, try without creating new branch (detached HEAD)
                logger.warning(
                    f"Worktree with new branch failed: {err}. Trying detached HEAD"
                )
                wt_cmd_detached = f"git --git-dir={repo_dir} worktree add --detach {workspace} {default_branch}"
                code, _out, err = await execute_git_command(wt_cmd_detached)
                if code != 0:
                    raise Exception(
                        f"Failed to create worktree after all attempts: {err}"
                    )

        # Inject git credentials into the workspace
        # Configure git to use credential helper
        await execute_git_command("git config credential.helper store", cwd=workspace)

        # Write credentials to home directory where git expects them
        home_dir = os.path.expanduser("~")
        os.makedirs(home_dir, exist_ok=True)
        with open(
            os.path.join(home_dir, ".git-credentials"), "w", encoding="utf-8"
        ) as f:
            f.write(f"https://x-access-token:{job_data['github_token']}@github.com\n")

        # Execute in isolated workspace
        response = await execute_in_workspace(workspace, job_data)

        # Mark job as complete (agent already posted to GitHub via MCP)
        await job_queue.complete_job(
            job_id,
            {
                "status": "success",
                "response": response,
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
            },
            status="success",
        )

        logger.info(f"Job {job_id} completed successfully")

    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}", exc_info=True)

        # Mark job as failed
        await job_queue.complete_job(
            job_id,
            {
                "status": "error",
                "error": str(e),
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
            },
            status="error",
        )

    finally:
        # Cleanup workspace and worktree
        if workspace:
            try:
                if repo_dir and os.path.exists(workspace):
                    # Remove worktree from bare repo tracking
                    await execute_git_command(
                        f"git --git-dir={repo_dir} worktree remove --force {workspace}"
                    )
                elif os.path.exists(workspace):
                    shutil.rmtree(workspace)
                logger.debug(f"Cleaned up workspace: {workspace}")
            except Exception as e:
                logger.warning(f"Failed to cleanup workspace {workspace}: {e}")


async def main():
    """Main sandbox worker loop."""
    logger.info("Starting sandbox worker")

    # Setup signal handlers
    setup_signal_handlers()

    # Initialize job queue
    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    job_queue = JobQueue(
        redis_url=redis_url,
        password=redis_password,
        job_ttl=3600,
    )

    logger.info("Sandbox worker ready, waiting for jobs...")

    try:
        while not shutdown_event.is_set():
            try:
                # Pull next job (blocking with timeout)
                result = await job_queue.get_next_job(timeout=5)

                if not result:
                    # Timeout, check shutdown and continue
                    continue

                job_id, job_data = result
                logger.info(
                    f"Processing job {job_id} for {job_data['repo']}#{job_data['issue_number']}"
                )

                # Process job
                await process_job(job_queue, job_id, job_data)

            except Exception as e:
                logger.error(f"Error in worker loop: {e}", exc_info=True)
                await asyncio.sleep(5)

    finally:
        logger.info("Shutting down sandbox worker...")
        await job_queue.close()
        logger.info("Sandbox worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
