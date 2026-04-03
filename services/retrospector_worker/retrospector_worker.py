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

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    UserMessage,
)

from shared import close_github_auth_service
from shared.git_utils import execute_git_command
from shared.github_auth import get_github_auth_service
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.signals import setup_graceful_shutdown
from shared.transcript_parser import extract_retrospector_summary

setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)


from shared.langfuse_hooks import setup_langfuse_hooks  # noqa: E402

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

    if not transcript_path or not os.path.exists(transcript_path):
        logger.warning(f"Retrospector: transcript not found: {transcript_path}")
        return

    async with _workflow_locks[workflow_name]:
        logger.info(f"Running retrospection for {target_repo} [{workflow_name}]")

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

            # Extract a summary instead of passing the raw transcript path
            # to avoid hitting the SDK's 1MB JSON buffer limit
            transcript_summary = extract_retrospector_summary(transcript_path)

            # Write summary to a temp file that the SDK can read
            summary_path = os.path.join(workspace, "transcript_summary.md")
            Path(summary_path).write_text(transcript_summary, encoding="utf-8")

            prompt = (
                f"/retrospector:retrospect "
                f"{summary_path} "
                f"{workflow_name} "
                f"{target_repo} "
                f"{num_turns} "
                f"{'error' if is_error else ''}"
            ).strip()

            model = os.getenv(
                "ANTHROPIC_DEFAULT_SONNET_MODEL", "claude-sonnet-4-20250514"
            )

            # Load the retrospector plugin from the pre-installed container path.
            # The worktree is checked out from origin/main which may not yet have the
            # plugin (e.g. when running from a feature branch). The Dockerfile always
            # copies plugins/retrospector/ to /app/plugins/retrospector/.
            plugins = [
                {
                    "type": "local",
                    "path": "/app/plugins/retrospector",
                }
            ]

            # GitHub MCP for PR creation
            mcp_servers: dict = {}
            if github_token:
                mcp_servers["github"] = {
                    "type": "http",
                    "url": "https://api.githubcopilot.com/mcp",
                    "headers": {"Authorization": f"Bearer {github_token}"},
                }

            hooks = setup_langfuse_hooks()

            def _stderr_callback(message: str) -> None:
                logger.warning(f"Retrospector SDK stderr: {message}")

            options = ClaudeAgentOptions(
                model=model,
                allowed_tools=[
                    "Skill",
                    "Bash",
                    "Glob",
                    "Grep",
                    "Read",
                    "Write",
                    "Edit",
                    "mcp__github__*",
                ],
                setting_sources=[
                    "user",
                    "project",
                    "local",
                ],
                permission_mode="acceptEdits",
                mcp_servers=mcp_servers,  # type: ignore[arg-type]
                plugins=plugins,  # type: ignore[arg-type]
                hooks=hooks,
                cwd=workspace,
                add_dirs=[workspace],
                stderr=_stderr_callback,
            )

            logger.info(
                f"Invoking retrospector in worktree {workspace} "
                f"for workflow={workflow_name}, turns={num_turns}, "
                f"duration={duration_ms}ms"
            )

            try:
                async with ClaudeSDKClient(options=options) as client:
                    await client.query(prompt)
                    async for msg in client.receive_messages():
                        if isinstance(msg, SystemMessage):
                            logger.debug(f"Retrospector system init: {str(msg)[:300]}")
                        elif isinstance(msg, AssistantMessage):
                            for block in msg.content:
                                if isinstance(block, TextBlock):
                                    logger.info(
                                        f"Retrospector [{workflow_name}] text: "
                                        f"{repr(block.text[:500])}"
                                    )
                                elif isinstance(block, ToolUseBlock):
                                    logger.info(
                                        f"Retrospector [{workflow_name}] tool: "
                                        f"{block.name}({str(block.input)[:200]})"
                                    )
                        elif isinstance(msg, UserMessage):
                            logger.debug(
                                f"Retrospector [{workflow_name}] user msg: "
                                f"{str(msg)[:200]}"
                            )
                        elif isinstance(msg, ResultMessage):
                            level = logger.warning if msg.is_error else logger.info
                            level(
                                f"Retrospection done [{workflow_name}] — "
                                f"{msg.num_turns} turns, {msg.duration_ms}ms, "
                                f"is_error={msg.is_error}, subtype={msg.subtype}"
                            )
                            break
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
