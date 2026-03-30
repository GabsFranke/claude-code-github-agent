"""Background worker solely responsible for syncing and caching bare git repositories."""

import asyncio
import json
import logging
import os
import sys

from shared.git_utils import execute_git_command
from shared.github_auth import get_github_auth_service
from shared.logging_utils import setup_logging
from shared.queue import RedisQueue
from shared.signals import setup_graceful_shutdown

# Configure logging
setup_logging(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

# Global state
shutdown_event = asyncio.Event()


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
    # Get lock timeout from environment (default: 5 minutes)
    lock_timeout = int(os.getenv("REPO_SYNC_LOCK_TIMEOUT", "300"))
    lock = redis_client.lock(lock_key, timeout=lock_timeout)
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
                # Publish error event
                completion_channel = "agent:sync:events"
                error_event = json.dumps(
                    {
                        "repo": repo,
                        "ref": ref,
                        "status": "error",
                        "error": f"Clone failed: {err}",
                    }
                )
                await redis_client.publish(completion_channel, error_event)
                return

            # After initial clone, configure remote and fetch all refs properly
            # In a bare repo, we need to manually configure the fetch refspec
            logger.info(f"Configuring fetch refspec for {repo}...")
            config_cmd = f"git --git-dir={repo_dir} config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'"
            code, _out, err = await execute_git_command(config_cmd)
            if code != 0:
                logger.warning(f"Failed to configure fetch refspec for {repo}: {err}")

            # Fetch all branches, tags, and PR refs
            logger.info(f"Fetching all refs for {repo}...")
            cmd = f"git --git-dir={repo_dir} fetch origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*' '+refs/pull/*/head:refs/pull/*/head'"
            code, _out, err = await execute_git_command(cmd)
            if code != 0:
                logger.warning(f"Failed to fetch all refs for {repo}: {err}")
                # Don't fail the whole sync, just log warning
        else:
            logger.info(f"Fetching updates for {repo}...")
            # Update remote URL with authentication token for fetch
            if token:
                set_url_cmd = f"git --git-dir={repo_dir} remote set-url origin {auth_prefix}github.com/{repo}.git"
                await execute_git_command(set_url_cmd)

            # Fetch all branches, tags, and PR refs into the bare repo
            # Use refs/remotes/origin/* to avoid conflicts with checked-out worktrees
            # PR refs are stored as refs/pull/*/head
            cmd = f"git --git-dir={repo_dir} fetch origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*' '+refs/pull/*/head:refs/pull/*/head'"
            code, _out, err = await execute_git_command(cmd)

            if code != 0:
                logger.error(f"Failed to fetch {repo}. Code: {code}, Err: {err}")
                # Publish error event
                completion_channel = "agent:sync:events"
                error_event = json.dumps(
                    {
                        "repo": repo,
                        "ref": ref,
                        "status": "error",
                        "error": f"Fetch failed: {err}",
                    }
                )
                await redis_client.publish(completion_channel, error_event)
                return

        # Publish completion signal with shorter TTL to avoid Redis bloat
        await redis_client.set(complete_key, "1", ex=300)  # Expires in 5 minutes

        # Publish completion event to pub/sub channel for waiting workers
        completion_channel = "agent:sync:events"
        completion_event = json.dumps({"repo": repo, "ref": ref, "status": "complete"})
        await redis_client.publish(completion_channel, completion_event)

        logger.info(f"Successfully synced {repo} (ref {ref}).")

    except Exception as e:
        logger.error(f"Error while syncing {repo}: {e}", exc_info=True)
        # Publish error event
        try:
            completion_channel = "agent:sync:events"
            error_event = json.dumps(
                {"repo": repo, "ref": ref, "status": "error", "error": str(e)}
            )
            await redis_client.publish(completion_channel, error_event)
        except Exception as pub_error:
            logger.error(f"Failed to publish error event: {pub_error}")
    finally:
        await lock.release()


async def main():
    logger.info("Starting Repository Sync Worker...")
    setup_graceful_shutdown(shutdown_event, logger)

    redis_url = os.getenv("REDIS_URL", "redis://redis:6379")
    redis_password = os.getenv("REDIS_PASSWORD")

    # Queue for listening to requests
    queue = RedisQueue(
        redis_url=redis_url, queue_name="agent:sync:requests", password=redis_password
    )
    await queue._connect()
    redis_client = queue.redis

    # Track background task for proper cleanup
    cleanup_task = asyncio.create_task(cleanup_old_repos())

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

        # Cancel background task
        if not cleanup_task.done():
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                logger.info("Cleanup task cancelled")

        # Cleanup global auth service
        from shared import close_github_auth_service

        await close_github_auth_service()

        await queue.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
