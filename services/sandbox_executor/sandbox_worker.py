"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from repo_setup import RepoSetupEngine  # noqa: E402
from shared import (  # noqa: E402
    JobQueue,
    RepositorySyncError,
    SDKError,
    SDKTimeoutError,
    WorktreeCreationError,
    execute_git_command,
    setup_graceful_shutdown,
)
from shared.context_builder import (  # noqa: E402
    find_priority_focus_files,
    generate_structural_context,
)
from shared.logging_utils import setup_logging  # noqa: E402
from shared.sdk_executor import execute_sdk  # noqa: E402
from shared.sdk_factory import SDKOptionsBuilder  # noqa: E402
from subagents import AGENTS  # noqa: E402

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Configure Claude Agent SDK logger to match our log level
logging.getLogger("claude_agent_sdk").setLevel(os.getenv("LOG_LEVEL", "INFO"))

# SDK retry configuration
SDK_MAX_RETRIES = int(os.getenv("SDK_MAX_RETRIES", "3"))
SDK_RETRY_BASE_DELAY = float(os.getenv("SDK_RETRY_BASE_DELAY", "5.0"))

# Global state
shutdown_event = asyncio.Event()


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

    # Fallback: Check if repo directory exists (handles expired completion keys)
    if os.path.exists(repo_dir):
        logger.info(
            f"Repo {repo} directory exists (completion key expired but repo is synced)"
        )
        # Re-trigger sync to ensure it's up-to-date and refresh completion key
        from shared import get_queue

        sync_queue = get_queue(queue_name="agent:sync:requests")
        await sync_queue.publish({"repo": repo, "ref": ref})
        logger.info(f"Re-triggered sync for {repo} to refresh cache")

    # Subscribe to completion events
    completion_channel = "agent:sync:events"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(completion_channel)

    logger.info(f"Waiting for sync completion event for {repo}...")

    try:
        # Wait for completion event with reasonable timeout (5 minutes for large repos)
        timeout = 300  # 5 minutes
        start_time = asyncio.get_running_loop().time()

        async for message in pubsub.listen():
            # Check timeout
            if asyncio.get_running_loop().time() - start_time > timeout:
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

        # Create isolated workspace under /tmp — cleaned up explicitly after job
        workspace_base = tempfile.mkdtemp(
            prefix=f"job_{job_id[:8]}_",
            dir="/tmp",  # nosec B108
        )
        os.rmdir(workspace_base)  # git worktree add needs it to not exist
        workspace = workspace_base
        logger.info(f"Created workspace for job {job_id}: {workspace}")

        # Create worktree without creating a new branch
        # The agent will create branches as needed using git commands
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

        # Create worktree in detached HEAD state - agent will create branches as needed
        wt_cmd = (
            f"git --git-dir={repo_dir} worktree add --detach {workspace} {bare_ref}"
        )
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

            # Try with detected default branch in detached HEAD state
            wt_cmd_fallback = f"git --git-dir={repo_dir} worktree add --detach {workspace} {default_branch}"
            code, _out, err = await execute_git_command(wt_cmd_fallback)
            if code != 0:
                raise WorktreeCreationError(f"Failed to create worktree: {err}")

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

        # Run repository setup commands if configured
        try:
            setup_engine = RepoSetupEngine()
            setup_config = setup_engine.get_setup_config(repo)

            if setup_config:
                logger.info(f"Found setup configuration for {repo}")
                setup_result = await setup_engine.run_setup(
                    workspace, repo, setup_config
                )

                if not setup_result["all_successful"]:
                    logger.warning(
                        f"Some setup commands failed for {repo}, continuing anyway..."
                    )
                    # Log failed commands for debugging
                    for result in setup_result["results"]:
                        if not result.get("success"):
                            # Handle both old (command) and new (commands) structure
                            cmd_info = result.get(
                                "commands", result.get("command", "unknown")
                            )
                            if isinstance(cmd_info, list):
                                cmd_info = " && ".join(cmd_info)
                            logger.warning(
                                f"Failed command(s): {cmd_info} - {result.get('error', 'unknown error')}"
                            )
                else:
                    logger.info(
                        f"Setup completed successfully for {repo} in {setup_result['elapsed_seconds']:.1f}s"
                    )
            # If no setup config, silently skip (this is normal)

        except Exception as e:
            logger.warning(
                f"Error during repository setup for {repo}: {e}. Continuing with job execution...",
                exc_info=True,
            )
            # Don't fail the job if setup fails - agent can still work with source code

        # Generate structural context (file tree + repomap)
        # This is an async step outside the synchronous builder
        file_tree_text = ""
        repomap_text = ""
        try:
            # Determine personalization from workflow context
            mentioned_files = []
            context_budget = 4096  # Default repomap budget
            include_test_files = True  # Default: include test files

            # Get context profile from job data (set by WorkflowEngine)
            context_profile = job_data.get("context_profile", {})
            if context_profile:
                context_budget = context_profile.get("repomap_budget", 4096)
                include_test_files = context_profile.get("include_test_files", True)

            # Personalize repomap toward relevant files when configured
            workflow_name = job_data.get("workflow_name")
            if context_profile.get("personalized", False):
                # Strategy 1: Fetch PR changed files (works for PR-triggered workflows)
                issue_number = job_data.get("issue_number")
                github_token = job_data.get("github_token")
                if issue_number and github_token:
                    try:
                        import httpx

                        async with httpx.AsyncClient() as client:
                            url = f"https://api.github.com/repos/{repo}/pulls/{issue_number}/files"
                            headers = {
                                "Authorization": f"Bearer {github_token}",
                                "Accept": "application/vnd.github.v3+json",
                            }
                            resp = await client.get(url, headers=headers, timeout=10.0)
                            if resp.status_code == 200:
                                files = resp.json()
                                mentioned_files = [
                                    f["path"] for f in files if "path" in f
                                ]
                                logger.info(
                                    f"Personalizing repomap toward {len(mentioned_files)} changed files"
                                )
                    except Exception as e:
                        logger.debug(
                            f"PR file fetch skipped (not a PR or API error): {e}"
                        )

                # Strategy 2: Add files matching priority_focus patterns
                priority_focus = context_profile.get("priority_focus", [])
                if priority_focus:
                    focus_files = find_priority_focus_files(
                        Path(workspace), priority_focus
                    )
                    mentioned_files.extend(focus_files)
                    if focus_files:
                        logger.info(
                            f"Added {len(focus_files)} priority focus files "
                            f"for areas: {priority_focus}"
                        )

            file_tree_text, repomap_text = await generate_structural_context(
                repo_path=Path(workspace),
                repo=repo,
                mentioned_files=mentioned_files,
                token_budget=context_budget,
                include_test_files=include_test_files,
                cache_dir=Path("/home/bot/agent-memory"),
            )
            logger.info(
                f"Generated structural context: "
                f"file_tree={len(file_tree_text)} chars, "
                f"repomap={len(repomap_text)} chars"
            )
        except Exception as e:
            logger.warning(
                f"Structural context generation failed, continuing without: {e}",
                exc_info=True,
            )

        # Build SDK options using the factory builder
        model = os.getenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514")
        github_token = job_data["github_token"]
        workflow_name = job_data.get("workflow_name")
        system_context = job_data.get("system_context")
        claude_md = job_data.get("claude_md")
        memory_index = job_data.get("memory_index")

        # Inject GitHub token for tools/plugins to use
        os.environ["GITHUB_TOKEN"] = github_token

        # Log token availability for debugging (length only, no partial tokens)
        if github_token:
            logger.info(f"GitHub token available: {len(github_token)} characters")
        else:
            logger.warning("No GitHub token provided to sandbox executor")

        # Start with base configuration
        builder = SDKOptionsBuilder(cwd=workspace).with_model(model)

        # Add MCP servers conditionally
        if github_token:
            builder.with_github_mcp(github_token).with_github_actions_mcp(github_token)

        builder.with_memory_mcp(repo)
        builder.with_codebase_tools(workspace)
        builder.with_semantic_search(repo)

        # Get parent span ID for trace linking (if enabled)
        parent_span_id = job_data.get("parent_span_id")

        # Build final options with all sandbox-specific features
        builder = (
            builder.with_auto_discovered_plugins()
            .with_full_toolset()
            .with_agents(AGENTS)
            .with_langfuse_hooks(parent_span_id=parent_span_id)
            .with_transcript_staging(repo, workflow_name, ref=ref)
            .with_writable_dir(f"/home/bot/agent-memory/{repo}/memory")
            .with_system_prompt(system_context)  # Workflow-specific system context
            .with_repository_context(
                claude_md=claude_md, memory_index=memory_index
            )  # Repository context (prepended to system prompt)
            .with_structural_context(
                file_tree=file_tree_text, repomap=repomap_text
            )  # Structural context (file tree + repomap)
        )

        # Execute via centralized executor with retry
        result = await execute_sdk(
            prompt=job_data["prompt"],
            options_builder=builder,
            max_retries=SDK_MAX_RETRIES,
            retry_base_delay=SDK_RETRY_BASE_DELAY,
        )

        response = result["response"]

        # Flush buffered post-processing jobs. The SDK may fire
        # Stop/SubagentStop hooks multiple times per session — the
        # flush deduplicates by (transcript, event, job_type) and
        # enqueues only the final set.
        await builder.flush_pending_post_jobs()

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
        import time

        # Flush buffered jobs even on failure — partial sessions can
        # still produce useful retrospection data.
        try:
            await builder.flush_pending_post_jobs()
        except Exception as flush_err:
            logger.error(f"Failed to flush post-processing jobs: {flush_err}")

        logger.error(f"Job {job_id} failed: {e}", exc_info=True)

        # Categorize error type
        error_type = type(e).__name__
        error_category = "unknown"
        if isinstance(e, (WorktreeCreationError, RepositorySyncError)):
            error_category = "infrastructure"
        elif isinstance(e, (SDKError, SDKTimeoutError)):
            error_category = "sdk"
        elif isinstance(e, TimeoutError):
            error_category = "timeout"
        else:
            error_category = "execution"

        # Mark job as failed with detailed context
        await job_queue.complete_job(
            job_id,
            {
                "status": "error",
                "error": str(e),
                "error_type": error_type,
                "error_category": error_category,
                "timestamp": time.time(),
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
            },
            status="error",
        )

    finally:
        # CRITICAL: Clean up GITHUB_TOKEN from environment
        if "GITHUB_TOKEN" in os.environ:
            del os.environ["GITHUB_TOKEN"]
            logger.debug("Cleaned up GITHUB_TOKEN from environment")

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
            # Try cleanup with retry (up to 3 attempts)
            for attempt in range(3):
                try:
                    if repo_dir and os.path.exists(workspace):
                        # Remove worktree from bare repo tracking
                        await execute_git_command(
                            f"git --git-dir={repo_dir} worktree remove --force {workspace}"
                        )
                    elif os.path.exists(workspace):
                        shutil.rmtree(workspace)
                    logger.debug(f"Cleaned up workspace: {workspace}")
                    break  # Success, exit retry loop
                except Exception as e:
                    if attempt < 2:  # Not the last attempt
                        logger.warning(
                            f"Failed to cleanup workspace {workspace} (attempt {attempt + 1}/3): {e}. Retrying..."
                        )
                        await asyncio.sleep(1)  # Wait before retry
                    else:
                        logger.error(
                            f"Failed to cleanup workspace {workspace} after 3 attempts: {e}",
                            exc_info=True,
                        )


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
