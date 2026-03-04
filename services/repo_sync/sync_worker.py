"""Background worker solely responsible for syncing and caching bare git repositories."""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from shared.github_auth import get_github_auth_service  # noqa: E402
from shared.queue import RedisQueue  # noqa: E402

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


async def cleanup_old_repos():
    """Background task to cleanup least recently used repos if disk space is low (dummy implementation)."""
    while not shutdown_event.is_set():
        try:
            # TODO: Add actual LRU eviction based on disk size.
            # Example: check 'df -h /var/cache/repos' and delete oldest st_atime folders
            pass
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")
        await asyncio.sleep(3600)  # Check every hour


async def process_sync_request(message: dict, redis_client):
    """Process a single repository sync message."""
    repo = message.get("repo")
    ref = message.get("ref", "main")

    if not repo:
        logger.error("Message missing 'repo' field.")
        return

    # Get token from shared GitHub App auth service
    token = None
    try:
        auth_service = await get_github_auth_service()
        if auth_service.is_configured():
            token = await auth_service.get_token()
        else:
            logger.warning(
                f"GitHub App not configured for {repo}, syncing might fail if private."
            )
    except Exception as e:
        logger.warning(f"Failed to get GitHub App token: {e}")

    auth_prefix = f"https://x-access-token:{token}@" if token else "https://"

    lock_key = f"agent:sync:lock:{repo}"
    complete_key = f"agent:sync:complete:{repo}:{ref}"
    cache_base = "/var/cache/repos"
    repo_dir = os.path.join(cache_base, f"{repo}.git")

    os.makedirs(os.path.dirname(repo_dir), exist_ok=True)

    # Acquire lock for this specific repo
    lock = redis_client.lock(lock_key, timeout=300)
    acquired = await lock.acquire(blocking=True, blocking_timeout=10)
    if not acquired:
        logger.warning(f"Could not acquire sync lock for {repo}, already syncing?")
        return

    try:
        logger.info(f"Syncing {repo} for ref {ref}...")

        if not os.path.exists(repo_dir):
            logger.info(f"Bare cache for {repo} not found. Cloning...")
            clone_url = f"{auth_prefix}github.com/{repo}.git"

            # Use --bare to create a pure database clone with no working tree
            cmd = f"git clone --bare {clone_url} {repo_dir}"
            code, _out, err = await execute_git_command(cmd)

            if code != 0:
                logger.error(f"Failed to clone {repo}. Code: {code}, Err: {err}")
                return
        else:
            logger.info(f"Fetching updates for {repo}...")
            # Fetch all branches and tags into the bare repo
            # In bare repos, remote branches are stored as refs/heads/* not refs/remotes/origin/*
            cmd = f"git --git-dir={repo_dir} fetch origin '+refs/heads/*:refs/heads/*' '+refs/tags/*:refs/tags/*'"
            code, _out, err = await execute_git_command(cmd)

            if code != 0:
                logger.error(f"Failed to fetch {repo}. Code: {code}, Err: {err}")
                return

        # Publish completion signal with shorter TTL to avoid Redis bloat
        await redis_client.set(complete_key, "1", ex=300)  # Expires in 5 minutes
        logger.info(f"Successfully synced {repo} (ref {ref}).")

    except Exception as e:
        logger.error(f"Error while syncing {repo}: {e}", exc_info=True)
    finally:
        await lock.release()


async def main():
    logger.info("Starting Repository Sync Worker...")
    setup_signal_handlers()

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    # Queue for listening to requests
    queue = RedisQueue(
        redis_url=redis_url, queue_name="agent:sync:requests", password=redis_password
    )
    await queue._connect()
    redis_client = queue.redis

    asyncio.create_task(cleanup_old_repos())

    logger.info("Sync worker ready. Waiting for requests...")

    async def message_handler(message: dict):
        if shutdown_event.is_set():
            return
        await process_sync_request(message, redis_client)

    try:
        # Subscribe blocks until running flag is false
        await queue.subscribe(message_handler)

    finally:
        logger.info("Shutting down Repository Sync Worker...")
        await queue.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
