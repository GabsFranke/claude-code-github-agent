"""Background worker that processes retrospection jobs from a Redis queue.

Listens on `agent:retrospector:requests`. For each job it:
1. Syncs the bot's own repository into the bare repo cache.
2. Creates an isolated git worktree of the bot repo.
3. Invokes /retrospector:retrospect inside that worktree via Claude Agent SDK.
4. The command analyses the session transcript and opens a PR to develop with
   any proposed instruction improvements.
5. Cleans up the worktree.

Transcripts are persisted permanently in the shared volume for future analysis.

Note: This worker processes jobs sequentially (one at a time). If you need to scale
to multiple worker instances, you'll need to implement distributed locking (e.g., Redis locks)
to prevent concurrent retrospection of the same workflow.
"""

import asyncio
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from shared import close_github_auth_service, wait_for_repo_sync
from shared.dlq import enqueue_for_retry, is_transient_error
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

# Default retry configuration
SDK_MAX_RETRIES = 3
SDK_RETRY_BASE_DELAY = 5.0  # seconds (exponential: 5s, 15s, 45s)

# DLQ configuration
_DLQ_KEY = "agent:retrospector:dead_letter"
_QUEUE_KEY = "agent:retrospector:requests"


def _validate_git_config_value(value: str, name: str) -> str:
    """Validate a value to be used in git config.

    While using list-form commands prevents shell injection, we still validate
    to catch configuration errors early and prevent problematic values from
    being stored in git config.

    Args:
        value: The value to validate
        name: Name of the config item (for error messages)

    Returns:
        The validated value

    Raises:
        ValueError: If value contains problematic characters
    """
    # Git config values should not contain newlines (would break config file)
    if "\n" in value or "\r" in value:
        raise ValueError(
            f"Invalid {name}: contains newline character. "
            f"Git config values cannot contain newlines."
        )
    return value


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

    logger.info(
        f"Running retrospection for {target_repo} [{workflow_name}] "
        f"(hook_event={hook_event})"
    )

    bot_repo = os.getenv("BOT_REPO", "GabsFranke/claude-code-github-agent")
    workspace = None
    repo_dir = None  # Initialize to prevent NameError in finally block

    try:
        repo_dir = await wait_for_repo_sync(bot_repo, "main", redis_client)

        # Create isolated worktree for the bot repo
        workspace = tempfile.mkdtemp(prefix="retro_", dir="/tmp")  # nosec B108
        os.rmdir(workspace)  # git worktree add requires the path to not exist

        wt_cmd = [
            "git",
            f"--git-dir={repo_dir}",
            "worktree",
            "add",
            "--detach",
            workspace,
            "refs/remotes/origin/main",
        ]
        code, _out, err = await execute_git_command(wt_cmd)
        if code != 0:
            # Fallback: try develop
            wt_cmd = [
                "git",
                f"--git-dir={repo_dir}",
                "worktree",
                "add",
                "--detach",
                workspace,
                "refs/remotes/origin/develop",
            ]
            code, _out, err = await execute_git_command(wt_cmd)
            if code != 0:
                raise RuntimeError(f"Failed to create bot repo worktree: {err}")

        # Configure git identity and credentials in the worktree
        auth_service = await get_github_auth_service()
        github_token = None
        if auth_service.is_configured():
            github_token = await auth_service.get_token()

        bot_username = _validate_git_config_value(
            os.getenv("BOT_USERNAME", "Claude Code Agent"), "BOT_USERNAME"
        )
        bot_email = _validate_git_config_value(
            os.getenv(
                "BOT_USER_EMAIL",
                "claude-code-agent[bot]@users.noreply.github.com",
            ),
            "BOT_USER_EMAIL",
        )
        await execute_git_command(
            ["git", "config", "user.name", bot_username], cwd=workspace
        )
        await execute_git_command(
            ["git", "config", "user.email", bot_email], cwd=workspace
        )
        if github_token:
            credentials_file = os.path.join(os.path.expanduser("~"), ".git-credentials")
            fd = os.open(credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(
                    fd,
                    f"https://x-access-token:{github_token}@github.com\n".encode(),
                )
            finally:
                os.close(fd)
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
        if transcript_summary is None:
            logger.error(
                f"Failed to extract summary from {transcript_path}, skipping job"
            )
            return

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

        # Auto-fetch bot repo context for retrospection
        # The retrospector analyzes the bot's own behavior, so it needs bot repo context
        builder = await builder.with_repository_context_auto(
            repo=bot_repo, fetch_claude_md=True, fetch_memory=True
        )

        # Build final options with retrospector-specific features
        builder = (
            builder.with_plugin("/app/plugins/retrospector")
            .with_retrospector_toolset()
            .with_memory_mcp(bot_repo)
            .with_memory_toolset()
            .with_langfuse_hooks()
            .with_writable_dir(workspace)
        )

        logger.info(
            f"Invoking retrospector in worktree {workspace} "
            f"for workflow={workflow_name}, turns={num_turns}, "
            f"duration={duration_ms}ms, is_subagent={is_subagent}"
        )

        try:
            # Execute via centralized executor with retry logic
            result = await execute_sdk(
                prompt=prompt,
                options=builder.build(),
                collect_text=False,
                max_retries=SDK_MAX_RETRIES,
                retry_base_delay=SDK_RETRY_BASE_DELAY,
            )

            logger.info(
                f"Retrospection done [{workflow_name}] — "
                f"{result['num_turns']} turns, {result['duration_ms']}ms, "
                f"is_error={result['is_error']}"
            )

        except Exception as e:
            logger.error(
                f"Retrospection failed after retries [{workflow_name}]: {e}",
                exc_info=True,
            )

    finally:
        # Clean up worktree
        if workspace and os.path.exists(workspace):
            if repo_dir:
                try:
                    await execute_git_command(
                        [
                            "git",
                            f"--git-dir={repo_dir}",
                            "worktree",
                            "remove",
                            "--force",
                            workspace,
                        ]
                    )
                except Exception as e:
                    logger.warning(f"Failed to remove worktree {workspace}: {e}")
                    try:
                        shutil.rmtree(workspace, ignore_errors=True)
                    except Exception:
                        pass
            else:
                logger.warning(
                    f"Skipping worktree git cleanup - repo_dir is None, "
                    f"manually removing {workspace}"
                )
                shutil.rmtree(workspace, ignore_errors=True)

        # Clean up credentials
        credentials_file = os.path.join(os.path.expanduser("~"), ".git-credentials")
        if os.path.exists(credentials_file):
            try:
                os.remove(credentials_file)
                logger.debug("Cleaned up git credentials file")
            except Exception as e:
                # Log error - don't silently pass
                logger.error(
                    f"CRITICAL: Failed to remove credentials file: {e}",
                    exc_info=True,
                )

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
        try:
            await process_retrospector_job(message, redis_client)
        except Exception as e:
            if is_transient_error(e):
                await enqueue_for_retry(redis_client, _QUEUE_KEY, _DLQ_KEY, message, e)
            else:
                logger.error(
                    "Non-transient error for retrospector job %s, "
                    "sending to DLQ: %s",
                    message.get("repo", "unknown"),
                    e,
                    exc_info=True,
                )
                await enqueue_for_retry(
                    redis_client,
                    _QUEUE_KEY,
                    _DLQ_KEY,
                    message,
                    e,
                    max_retries=0,
                )

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
