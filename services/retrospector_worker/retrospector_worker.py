"""Background worker that processes retrospection jobs from a Redis queue.

Listens on `agent:retrospector:requests`. For each job it:
1. Syncs the bot's own repository into the bare repo cache.
2. Creates an isolated git worktree of the bot repo.
3. Invokes /retrospector:retrospect inside that worktree via Claude Agent SDK.
4. The command analyses the session transcript and opens a PR to develop with
   any proposed instruction improvements.
5. Cleans up the worktree.

Transcripts are persisted permanently in the shared volume for future analysis.
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

from shared import close_github_auth_service
from shared.git_utils import execute_git_command
from shared.github_auth import get_github_auth_service
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.sdk_executor import execute_sdk
from shared.sdk_factory import SDKOptionsBuilder
from shared.signals import setup_graceful_shutdown
from shared.transcript_parser import extract_retrospector_summary

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

shutdown_event = asyncio.Event()

# Per-workflow locks — prevent concurrent instruction edits for the same workflow.
_workflow_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _ensure_bot_repo_synced(bot_repo: str, redis_client) -> str:
    """Ensure the bot's own repo is synced in the bare cache.

    Publishes a sync request and waits for the repo_sync worker to confirm
    completion via pub/sub — same pattern as sandbox_worker.ensure_repo_synced.
    """
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{bot_repo}.git")
    ref = "main"

    complete_key = f"agent:sync:complete:{bot_repo}:{ref}"
    is_complete = await redis_client.get(complete_key)
    if is_complete and os.path.exists(repo_dir):
        logger.info(f"Bot repo {bot_repo} already synced (cached)")
        return repo_dir

    # Request sync
    await redis_client.rpush(
        "agent:sync:requests",
        json.dumps({"repo": bot_repo, "ref": ref}),
    )
    logger.info(f"Requested sync for bot repo {bot_repo}")

    completion_channel = "agent:sync:events"
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(completion_channel)

    try:
        timeout = 300
        start = asyncio.get_event_loop().time()
        async for message in pubsub.listen():
            if asyncio.get_event_loop().time() - start > timeout:
                raise RuntimeError(f"Sync timeout for {bot_repo} after {timeout}s")
            if message["type"] == "message":
                try:
                    event = json.loads(message["data"])
                    if event.get("repo") == bot_repo and event.get("ref") == ref:
                        if event.get("status") == "complete":
                            logger.info(f"Bot repo {bot_repo} synced")
                            return repo_dir
                        elif event.get("status") == "error":
                            raise RuntimeError(
                                f"Bot repo sync failed: {event.get('error', 'unknown')}"
                            )
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("Sync event stream ended without completion")
    finally:
        await pubsub.unsubscribe(completion_channel)
        await pubsub.close()


async def process_retrospector_job(message: dict, redis_client) -> None:
    """Run /retrospector:retrospect for one session transcript."""
    workflow_name = message.get("workflow_name") or "generic"
    transcript_path = message.get("transcript_path")
    target_repo = message.get("repo", "unknown")
    session_meta = message.get("session_meta", {})
    hook_event = message.get("hook_event", "Stop")

    # For subagent sessions, extract agent info from session_meta
    agent_id = session_meta.get("agent_id")  # e.g., "comment-analyzer"
    agent_type = session_meta.get(
        "agent_type"
    )  # e.g., "pr-review-toolkit:comment-analyzer"

    if hook_event == "SubagentStop" and agent_id:
        # Use agent_id as the workflow name for retrospection
        workflow_name = agent_id
        logger.info(
            f"Subagent session detected: agent_id={agent_id}, agent_type={agent_type}"
        )

    if not transcript_path or not os.path.exists(transcript_path):
        logger.warning(f"Retrospector: transcript not found: {transcript_path}")
        return

    async with _workflow_locks[workflow_name]:
        logger.info(
            f"Running retrospection for {target_repo} [{workflow_name}] "
            f"(hook_event={hook_event})"
        )

        bot_repo = os.getenv("BOT_REPO", "GabsFranke/claude-code-github-agent")
        workspace = None
        repo_dir = None  # Initialize to prevent NameError in finally block

        try:
            repo_dir = await _ensure_bot_repo_synced(bot_repo, redis_client)

            # Create isolated worktree for the bot repo
            workspace = tempfile.mkdtemp(prefix="retro_", dir="/tmp")  # nosec B108
            os.rmdir(workspace)  # git worktree add requires the path to not exist

            wt_cmd = (
                f"git --git-dir={repo_dir} worktree add --detach "
                f"{workspace} refs/remotes/origin/main"
            )
            code, _out, err = await execute_git_command(wt_cmd)
            if code != 0:
                # Fallback: try develop
                wt_cmd = (
                    f"git --git-dir={repo_dir} worktree add --detach "
                    f"{workspace} refs/remotes/origin/develop"
                )
                code, _out, err = await execute_git_command(wt_cmd)
                if code != 0:
                    raise RuntimeError(f"Failed to create bot repo worktree: {err}")

            # Configure git identity and credentials in the worktree
            auth_service = await get_github_auth_service()
            github_token = None
            if auth_service.is_configured():
                github_token = await auth_service.get_token()

            bot_username = os.getenv("BOT_USERNAME", "Claude Code Agent")
            bot_email = os.getenv(
                "BOT_USER_EMAIL",
                "claude-code-agent[bot]@users.noreply.github.com",
            )
            await execute_git_command(
                f'git config user.name "{bot_username}"', cwd=workspace
            )
            await execute_git_command(
                f'git config user.email "{bot_email}"', cwd=workspace
            )
            if github_token:
                credentials_file = os.path.join(
                    os.path.expanduser("~"), ".git-credentials"
                )
                Path(credentials_file).write_text(
                    f"https://x-access-token:{github_token}@github.com\n",
                    encoding="utf-8",
                )
                await execute_git_command(
                    "git config credential.helper store", cwd=workspace
                )

            # Build the plugin command prompt
            duration_ms = int(session_meta.get("duration_ms", 0))
            num_turns = int(session_meta.get("num_turns", 0))
            is_error = bool(session_meta.get("is_error", False))
            is_subagent = hook_event == "SubagentStop"

            # Extract a summary instead of passing the raw transcript path
            # to avoid hitting the SDK's 1MB JSON buffer limit
            transcript_summary = extract_retrospector_summary(transcript_path)

            # Write summary to a temp file that the SDK can read
            summary_path = os.path.join(workspace, "transcript_summary.md")
            Path(summary_path).write_text(transcript_summary, encoding="utf-8")

            # Build the retrospector command
            # For subagents, workflow_name is the agent_id (e.g., "comment-analyzer")
            # The retrospector will use agent_type to find the correct file
            prompt = (
                f"/retrospector:retrospect "
                f"{summary_path} "
                f"{workflow_name} "
                f"{target_repo} "
                f"{num_turns} "
                f"{'error' if is_error else ''} "
                f"{'subagent' if is_subagent else ''}"
            ).strip()

            # Build SDK options using the factory builder
            builder = SDKOptionsBuilder(cwd=workspace).with_sonnet()

            # Add GitHub MCP conditionally
            if github_token:
                builder.with_github_mcp(github_token)

            # Build final options with retrospector-specific features
            builder = (
                builder.with_plugin("/app/plugins/retrospector")
                .with_retrospector_toolset()
                .with_langfuse_hooks()
                .with_writable_dir(workspace)
            )

            logger.info(
                f"Invoking retrospector in worktree {workspace} "
                f"for workflow={workflow_name}, turns={num_turns}, "
                f"duration={duration_ms}ms, is_subagent={is_subagent}"
            )

            try:
                # Execute via centralized executor
                result = await execute_sdk(
                    prompt=prompt,
                    options_builder=builder,
                    collect_text=False,  # We don't need text response
                )

                logger.info(
                    f"Retrospection done [{workflow_name}] — "
                    f"{result['num_turns']} turns, {result['duration_ms']}ms, "
                    f"is_error={result['is_error']}"
                )

            except Exception as e:
                logger.warning(
                    f"Retrospection failed [{workflow_name}]: {e}",
                    exc_info=True,
                )

        finally:
            # Clean up worktree
            if workspace and os.path.exists(workspace):
                try:
                    await execute_git_command(
                        f"git --git-dir={repo_dir} worktree remove --force {workspace}"
                    )
                except Exception as e:
                    logger.warning(f"Failed to remove worktree {workspace}: {e}")
                    try:
                        shutil.rmtree(workspace, ignore_errors=True)
                    except Exception:
                        pass

            # Clean up credentials
            credentials_file = os.path.join(os.path.expanduser("~"), ".git-credentials")
            if os.path.exists(credentials_file):
                try:
                    os.remove(credentials_file)
                except Exception:
                    pass

            await close_github_auth_service()


async def main() -> None:
    """Main retrospector worker loop."""
    logger.info("Starting retrospector worker")
    setup_graceful_shutdown(shutdown_event, logger)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    queue = RedisQueue(
        redis_url=redis_url,
        queue_name="agent:retrospector:requests",
        password=redis_password,
    )
    await queue._connect()
    redis_client = queue.redis

    logger.info("Retrospector worker ready, waiting for jobs...")

    async def message_handler(message: dict) -> None:
        if shutdown_event.is_set():
            return
        await process_retrospector_job(message, redis_client)

    try:
        await queue.subscribe(message_handler)
    finally:
        logger.info("Retrospector worker shutting down...")
        await queue.close()
        logger.info("Retrospector worker shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
