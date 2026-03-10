"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import uuid

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from shared import (
    JobQueue,
    RepositorySyncError,
    SDKError,
    SDKTimeoutError,
    WorktreeCreationError,
    execute_git_command,
    setup_graceful_shutdown,
)
from shared.logging_utils import setup_logging
from subagents import AGENTS

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Configure Claude Agent SDK logger to match our log level
logging.getLogger("claude_agent_sdk").setLevel(os.getenv("LOG_LEVEL", "INFO"))

# Global state
shutdown_event = asyncio.Event()


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
                except ProcessLookupError:
                    pass  # Expected - process already terminated
                except OSError as e:
                    logger.warning(f"Failed to cleanup Langfuse hook process: {e}")
                except Exception as e:
                    logger.error(
                        f"Unexpected error cleaning up Langfuse hook process: {e}",
                        exc_info=True,
                    )

        return {"success": False, "error": error_msg}

    return {
        "Stop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
        "SubagentStop": [HookMatcher(matcher="*", hooks=[langfuse_stop_hook_async])],
    }


async def ensure_repo_synced(
    repo: str, ref: str, redis_client, github_token: str
) -> str:
    """Ensure bare repo is synced by waiting for completion event via pub/sub.

    This function subscribes to Redis pub/sub and waits for the repo sync worker
    to publish a completion event. No polling, no arbitrary timeouts.
    """
    complete_key = f"agent:sync:complete:{repo}:{ref}"
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{repo}.git")

    # First check if already synced (fast path)
    is_complete = await redis_client.get(complete_key)
    if is_complete:
        logger.info(f"Repo {repo} already synced (cached)")
        return repo_dir

    # Subscribe to completion events
    completion_channel = "agent:sync:events"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(completion_channel)

    logger.info(f"Waiting for sync completion event for {repo}...")

    try:
        # Wait for completion event with reasonable timeout (5 minutes for large repos)
        timeout = 300  # 5 minutes
        start_time = asyncio.get_event_loop().time()

        async for message in pubsub.listen():
            # Check timeout
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise RepositorySyncError(
                    f"Sync timeout for {repo} after {timeout}s - repo sync worker may be down"
                )

            if message["type"] == "message":
                try:
                    event = json.loads(message["data"])
                    if event.get("repo") == repo and event.get("ref") == ref:
                        if event.get("status") == "complete":
                            logger.info(f"Received sync completion event for {repo}")
                            return repo_dir
                        elif event.get("status") == "error":
                            raise RepositorySyncError(
                                f"Repo sync failed for {repo}: {event.get('error', 'unknown error')}"
                            )
                except json.JSONDecodeError:
                    logger.warning(f"Invalid JSON in sync event: {message['data']}")
                    continue

        # If we exit the loop without returning, something went wrong
        raise RepositorySyncError(
            f"Sync event stream ended unexpectedly for {repo} - no completion event received"
        )
    finally:
        await pubsub.unsubscribe(completion_channel)
        await pubsub.close()


async def execute_in_workspace(workspace: str, job_data: dict) -> str:
    """Execute Claude Agent SDK in isolated workspace.

    Args:
        workspace: Path to isolated workspace directory
        job_data: Job data containing prompt, github_token, etc.

    Returns:
        Agent response text

    Raises:
        SDKTimeoutError: If execution exceeds timeout
        SDKError: If SDK execution fails
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
            plugins=[
                {"type": "local", "path": "/app/plugins/pr-review-toolkit"},
                {"type": "local", "path": "/app/plugins/ci-failure-toolkit"},
            ],
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
            raise SDKError("Claude Agent SDK returned empty response")

        logger.info("SDK execution completed successfully")
        return response

    except TimeoutError as e:
        raise SDKTimeoutError(
            "Claude Agent SDK execution timed out after 30 minutes"
        ) from e
    except Exception as e:
        raise SDKError(f"Failed to execute Claude Agent SDK: {e}") from e
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
        logger.info(f"Job data keys: {list(job_data.keys())}")
        logger.info(f"Job data ref value: {job_data.get('ref', 'NOT_FOUND')}")
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
        # Handle different ref formats:
        # - refs/heads/main -> refs/remotes/origin/main (regular branch)
        # - refs/pull/30/head -> refs/pull/30/head (PR ref, keep as-is)
        # - refs/tags/v1.0 -> refs/tags/v1.0 (tag, keep as-is)
        if ref.startswith("refs/pull/"):
            # PR refs need to be kept as-is
            bare_ref = ref
        elif ref.startswith("refs/tags/"):
            # Tag refs need to be kept as-is
            bare_ref = ref
        else:
            # Regular branch refs: convert refs/heads/main -> refs/remotes/origin/main
            base_ref = (
                ref.replace("refs/heads/", "")
                if ref.startswith("refs/heads/")
                else ref.replace("refs/", "")
            )
            bare_ref = f"refs/remotes/origin/{base_ref}"

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
            list_cmd = f"git --git-dir={repo_dir} branch --list -r"
            list_code, list_out, list_err = await execute_git_command(list_cmd)

            default_branch = "refs/remotes/origin/main"  # Fallback
            if list_code == 0 and list_out:
                # Output is like "  origin/main" or "  origin/master", pick first branch
                branches = [
                    b.strip()
                    for b in list_out.split("\n")
                    if b.strip() and "origin/" in b
                ]
                if branches:
                    # branches[0] is like "origin/main", convert to refs/remotes/origin/main
                    branch_name_only = branches[0].replace("origin/", "")
                    default_branch = f"refs/remotes/origin/{branch_name_only}"
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
                    raise WorktreeCreationError(
                        f"Failed to create worktree after all attempts: {err}"
                    )

        # Inject git credentials into the workspace
        # Configure git to use credential helper
        config_code, _, config_err = await execute_git_command(
            "git config credential.helper store", cwd=workspace
        )
        if config_code != 0:
            raise WorktreeCreationError(
                f"Failed to configure git credentials: {config_err}"
            )

        # Write credentials to home directory where git expects them
        home_dir = os.path.expanduser("~")
        os.makedirs(home_dir, exist_ok=True)
        credentials_file = os.path.join(home_dir, ".git-credentials")
        with open(credentials_file, "w", encoding="utf-8") as f:
            f.write(f"https://x-access-token:{job_data['github_token']}@github.com\n")

        # Configure git user for commits (required for git commit to work)
        bot_username = os.getenv("BOT_USERNAME", "Claude Code Agent")
        bot_email = os.getenv(
            "BOT_USER_EMAIL", "claude-code-agent[bot]@users.noreply.github.com"
        )
        await execute_git_command(
            f'git config user.name "{bot_username}"', cwd=workspace
        )
        await execute_git_command(f'git config user.email "{bot_email}"', cwd=workspace)

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
        # Cleanup credentials
        try:
            credentials_file = os.path.join(os.path.expanduser("~"), ".git-credentials")
            if os.path.exists(credentials_file):
                os.remove(credentials_file)
                logger.debug("Cleaned up git credentials")
        except Exception as e:
            logger.warning(f"Failed to cleanup credentials: {e}")

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
    setup_graceful_shutdown(shutdown_event, logger)

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
