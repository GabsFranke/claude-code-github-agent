"""Sandbox worker that pulls jobs from queue and executes them in isolated workspaces."""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any

from repo_setup import RepoSetupEngine  # noqa: E402
from shared import (  # noqa: E402
    JobQueue,
    RepositorySyncError,
    SDKError,
    SDKTimeoutError,
    WorktreeCreationError,
    execute_git_command,
    setup_graceful_shutdown,
    wait_for_repo_sync,
)
from shared.context_builder import (  # noqa: E402
    find_priority_focus_files,
    generate_structural_context,
)
from shared.logging_utils import setup_logging  # noqa: E402
from shared.sdk_executor import execute_sdk  # noqa: E402
from shared.sdk_factory import SDKOptionsBuilder  # noqa: E402
from shared.session_store import SessionStore  # noqa: E402
from shared.worktree_lock import PendingPrompt, WorktreeKey, WorktreeLock  # noqa: E402
from subagents import AGENTS  # noqa: E402

from .worktree_manager import (  # noqa: E402
    cleanup_worktrees,
    cleanup_worktrees_by_branch,
    get_worktree_path,
    reuse_or_create_worktree,
)

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


async def process_job(job_queue: JobQueue, job_id: str, job_data: dict) -> None:
    """Process a single job in an isolated workspace.

    Args:
        job_queue: Job queue instance
        job_id: Job identifier
        job_data: Job data dictionary
    """
    workspace = None
    repo_dir = None
    builder = None
    persist_session = False

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

        repo_dir = await wait_for_repo_sync(repo, ref, job_queue.redis)

        # Session persistence: determine worktree path and session mode
        thread_type = job_data.get("thread_type", "issue")
        thread_id = str(job_data.get("issue_number", "0"))
        workflow_name = job_data.get("workflow_name", "generic")
        session_mode = job_data.get("session_mode", "new")
        session_id = job_data.get("session_id")
        conversation_config = job_data.get("conversation_config") or {}

        persist_session = conversation_config.get("persist", False)
        ttl_hours = conversation_config.get("ttl_hours", 168)

        # Build worktree key for locking (only needed for persistent sessions)
        worktree_key = None
        worktree_lock = None
        pending_prompt: PendingPrompt | None = None
        interrupted = False  # Flag to track if this job was superseded

        if persist_session:
            worktree_key = WorktreeKey(
                repo=repo,
                thread_type=thread_type,
                thread_id=thread_id,
                workflow=workflow_name,
            )
            worktree_lock = WorktreeLock(job_queue.redis, worktree_key)

            # Try to acquire lock; if held, set pending prompt and wait
            acquired = await worktree_lock.acquire(job_id, timeout=0)
            if not acquired:
                # Lock held by another job - set pending and wait
                lock_info = await worktree_lock.get_lock_info()
                logger.info(
                    f"Worktree locked by job {lock_info.job_id if lock_info else 'unknown'}, "
                    f"setting pending prompt and waiting..."
                )
                await worktree_lock.set_pending_prompt(
                    job_id, job_data.get("prompt", "")
                )
                await worktree_lock.send_cancel_signal()

                # Wait for lock to be released (with timeout)
                released = await worktree_lock.wait_for_release(timeout=300)
                if not released:
                    logger.error(f"Lock wait timeout for {worktree_key}")
                    await job_queue.complete_job(
                        job_id,
                        {
                            "status": "error",
                            "error": "Timeout waiting for worktree lock",
                            "repo": repo,
                            "issue_number": job_data.get("issue_number", 0),
                        },
                        status="error",
                    )
                    return

                # Try to acquire lock now
                acquired = await worktree_lock.acquire(job_id, timeout=0)
                if not acquired:
                    logger.error(
                        f"Failed to acquire lock after wait for {worktree_key}"
                    )
                    await job_queue.complete_job(
                        job_id,
                        {
                            "status": "error",
                            "error": "Failed to acquire worktree lock",
                            "repo": repo,
                            "issue_number": job_data.get("issue_number", 0),
                        },
                        status="error",
                    )
                    return

                # Check for pending prompt from previous job
                pending_prompt = await worktree_lock.get_pending_prompt()
                if pending_prompt:
                    logger.info(f"Resuming with pending prompt for {worktree_key}")
                    # Update the prompt
                    job_data["prompt"] = pending_prompt.prompt

                    # Look up session from SessionStore (previous job saved it)
                    session_store = SessionStore(job_queue.redis)
                    existing_session = await session_store.get_session(
                        repo, thread_type, thread_id, workflow_name
                    )
                    if existing_session and existing_session.session_id:
                        session_mode = "resume"
                        session_id = existing_session.session_id
                        logger.info(
                            f"Resuming interrupted session {session_id[:8]}... "
                            f"with new prompt"
                        )
                    else:
                        # No previous session found, start fresh
                        session_mode = "new"
                        logger.warning(
                            "Pending prompt found but no previous session, "
                            "starting fresh"
                        )

        # Use deterministic worktree for persistent sessions, random otherwise
        if persist_session:
            worktree_path = get_worktree_path(
                repo, thread_type, thread_id, workflow_name
            )
            await reuse_or_create_worktree(
                bare_repo=repo_dir,
                ref=ref,
                worktree_path=worktree_path,
                session_mode=session_mode,
            )
            workspace = str(worktree_path)
            logger.info(
                f"Using deterministic worktree: {workspace} (mode={session_mode})"
            )
        else:
            # Legacy path: ephemeral worktree
            workspace_base = tempfile.mkdtemp(
                prefix=f"job_{job_id[:8]}_",
                dir="/tmp",  # nosec B108
            )
            os.rmdir(workspace_base)  # git worktree add needs it to not exist
            workspace = workspace_base
            logger.info(f"Created ephemeral workspace for job {job_id}: {workspace}")

            # Create ephemeral worktree (legacy path)
            if ref.startswith("refs/pull/"):
                bare_ref = ref
            elif ref.startswith("refs/tags/"):
                bare_ref = ref
            else:
                base_ref = (
                    ref.replace("refs/heads/", "")
                    if ref.startswith("refs/heads/")
                    else ref.replace("refs/", "")
                )
                bare_ref = f"refs/remotes/origin/{base_ref}"

            wt_cmd = (
                f"git --git-dir={repo_dir} worktree add --detach {workspace} {bare_ref}"
            )
            code, _out, err = await execute_git_command(wt_cmd)

            if code != 0:
                logger.warning(
                    f"Worktree ref {bare_ref} failed: {err}. "
                    "Trying to detect default branch..."
                )
                list_cmd = f"git --git-dir={repo_dir} branch --list -r"
                list_code, list_out, list_err = await execute_git_command(list_cmd)
                default_branch = "refs/remotes/origin/main"
                if list_code == 0 and list_out:
                    branches = [
                        b.strip()
                        for b in list_out.split("\n")
                        if b.strip() and "origin/" in b
                    ]
                    if branches:
                        branch_name_only = branches[0].replace("origin/", "")
                        default_branch = f"refs/remotes/origin/{branch_name_only}"
                        logger.info(f"Detected default branch: {default_branch}")
                else:
                    logger.warning(
                        f"Could not list branches: {list_err}. "
                        f"Using fallback: {default_branch}"
                    )
                wt_cmd_fb = (
                    f"git --git-dir={repo_dir} worktree add --detach "
                    f"{workspace} {default_branch}"
                )
                code, _out, err = await execute_git_command(wt_cmd_fb)
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
        fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(
                fd,
                f"https://x-access-token:{job_data['github_token']}@github.com\n".encode(),
            )
        finally:
            os.close(fd)

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
                cache_dir=Path("/home/bot/.claude"),
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
            .with_writable_dir(f"/home/bot/.claude/memory/{repo}/memory")
            .with_system_prompt(system_context)  # Workflow-specific system context
            .with_repository_context(
                claude_md=claude_md, memory_index=memory_index
            )  # Repository context (prepended to system prompt)
            .with_structural_context(
                file_tree=file_tree_text, repomap=repomap_text
            )  # Structural context (file tree + repomap)
        )

        # Apply session resume/fork if applicable
        if session_mode == "resume" and session_id:
            logger.info(f"Resuming session {session_id[:8]}...")
            builder = builder.with_session_resume(session_id)
        elif session_mode == "fork" and session_id:
            logger.info(f"Forking from session {session_id[:8]}...")
            builder = builder.with_session_fork(session_id)
        elif session_mode == "continue":
            logger.info("Continuing most recent session...")
            builder = builder.with_session_continue()

        # Inject conversation summary as fallback context when full resume fails
        conversation_summary = job_data.get("conversation_summary")
        if conversation_summary and session_mode in ("resume", "continue"):
            summary_context = (
                f"\n\n## Previous Conversation Context\n{conversation_summary}"
            )
            builder = builder.with_system_prompt(
                (system_context or "") + summary_context
            )

        # For persistent sessions, wrap SDK execution with cancel subscription
        result = None
        sdk_task: asyncio.Task | None = None

        async def handle_cancel():
            """Callback when cancel signal received - interrupt SDK."""
            nonlocal interrupted
            interrupted = True
            logger.info(f"Cancel signal received, interrupting SDK for {worktree_key}")
            if sdk_task and not sdk_task.done():
                sdk_task.cancel()

        if worktree_lock:
            async with worktree_lock.cancel_subscription(handle_cancel):
                # Run SDK in a task so it can be cancelled
                sdk_task = asyncio.create_task(
                    execute_sdk(
                        prompt=job_data["prompt"],
                        options=builder.build(),
                        max_retries=SDK_MAX_RETRIES,
                        retry_base_delay=SDK_RETRY_BASE_DELAY,
                    )
                )

                try:
                    result = await sdk_task
                except asyncio.CancelledError:
                    logger.info(f"SDK execution cancelled for {worktree_key}")
                    interrupted = True
                    # Update lock status to indicate interruption
                    await worktree_lock.set_interrupted()
        else:
            # No lock needed (non-persistent session), execute normally
            result = await execute_sdk(
                prompt=job_data["prompt"],
                options=builder.build(),
                max_retries=SDK_MAX_RETRIES,
                retry_base_delay=SDK_RETRY_BASE_DELAY,
            )

        # Handle interrupted job
        if interrupted:
            logger.info(f"Job {job_id} interrupted, marking as superseded")
            await job_queue.complete_job(
                job_id,
                {
                    "status": "superseded",
                    "repo": repo,
                    "issue_number": job_data.get("issue_number", 0),
                    "message": "Interrupted by new prompt, session saved for continuation",
                },
                status="superseded",
            )
            logger.info(f"Job {job_id} completed as superseded")
            return  # Exit early, lock released in finally

        if not result:
            raise RuntimeError("SDK execution returned no result")

        response = result["response"]

        # Flush buffered post-processing jobs. The SDK may fire
        # Stop/SubagentStop hooks multiple times per session — the
        # flush deduplicates by (transcript, event, job_type) and
        # enqueues only the final set.
        await builder.flush_pending_post_jobs()

        # Save session metadata for persistent conversations
        new_session_id = result.get("session_id")
        if new_session_id and persist_session:
            try:
                session_store = SessionStore(job_queue.redis)
                await session_store.save_session(
                    repo=repo,
                    thread_type=thread_type,
                    thread_id=thread_id,
                    workflow=workflow_name,
                    session_id=new_session_id,
                    worktree_path=workspace,
                    ref=ref,
                    turn_count=result.get("num_turns", 0),
                    ttl_hours=ttl_hours,
                )
                logger.info(
                    f"Saved session {new_session_id[:8]}... for "
                    f"{repo}/{thread_type}/{thread_id}/{workflow_name}"
                )
                # Update lock with session_id for resume
                if worktree_lock:
                    await worktree_lock.set_session_id(new_session_id)
            except Exception as e:
                logger.warning(f"Failed to save session metadata: {e}")

        # Mark job as complete (agent already posted to GitHub via MCP)
        await job_queue.complete_job(
            job_id,
            {
                "status": "success",
                "response": response,
                "repo": job_data["repo"],
                "issue_number": job_data["issue_number"],
                "session_id": new_session_id,
            },
            status="success",
        )

        logger.info(f"Job {job_id} completed successfully")

    except Exception as e:
        import time

        # Flush buffered jobs even on failure — partial sessions can
        # still produce useful retrospection data.
        try:
            if builder is not None:
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
        # Release worktree lock if acquired
        if worktree_lock:
            try:
                await worktree_lock.release()
                logger.debug(f"Released worktree lock for {worktree_key}")
            except Exception as e:
                logger.warning(f"Failed to release worktree lock: {e}")

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

        # Cleanup workspace and worktree (only ephemeral worktrees)
        if workspace and not persist_session:
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
        elif workspace and persist_session:
            logger.debug(
                f"Preserving persistent worktree: {workspace} "
                f"(cleaned by TTL/event-based cleanup)"
            )


async def _process_cleanup_requests(redis: Any) -> None:
    """Process pending worktree cleanup requests from Redis.

    Webhook service queues cleanup events (PR close, issue close,
    branch delete) to the ``agent:worktree:cleanup`` Redis list.
    This function drains all pending requests each cycle.
    """
    import json as _json

    while True:
        raw = await redis.lpop("agent:worktree:cleanup")
        if not raw:
            break

        try:
            msg = _json.loads(raw)
            action = msg.get("action")
            repo = msg.get("repo", "")

            if action == "cleanup_thread":
                thread_type = msg.get("thread_type", "issue")
                thread_id = msg.get("thread_id", "")
                logger.info(
                    f"Cleaning up worktrees for {repo}/{thread_type}/{thread_id}"
                )
                await cleanup_worktrees(repo, thread_type, thread_id)

                # Also clean up session metadata
                try:
                    session_store = SessionStore(redis)
                    # Find and delete sessions for this thread
                    sessions = await session_store.list_sessions(repo)
                    for s in sessions:
                        if s.thread_type == thread_type and s.thread_id == thread_id:
                            await session_store.close_session(
                                repo, thread_type, thread_id, s.workflow_name
                            )
                            logger.info(
                                f"Closed session for {repo}/{thread_type}/{thread_id}/{s.workflow_name}"
                            )
                except Exception as e:
                    logger.warning(f"Failed to cleanup session metadata: {e}")

            elif action == "cleanup_branch":
                branch = msg.get("branch", "")
                logger.info(f"Cleaning up worktrees for branch {branch} in {repo}")
                await cleanup_worktrees_by_branch(repo, branch)

        except Exception as e:
            logger.error(f"Failed to process cleanup request: {e}", exc_info=True)


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
                # Process pending worktree cleanup requests
                await _process_cleanup_requests(job_queue.redis)

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
