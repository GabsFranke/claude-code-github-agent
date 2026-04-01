"""Claude Code worker that processes GitHub requests from message queue."""

import asyncio
import logging
import sys

import httpx
from langfuse import get_client

# Import shared utilities
from shared import JobQueue, MultiRateLimiter, get_queue, setup_graceful_shutdown
from shared.config import get_worker_config, handle_config_error
from shared.health import HealthChecker
from shared.logging_utils import setup_logging

# Import modularized components
from .processors import RequestProcessor

# Load configuration first with detailed error reporting
try:
    config = get_worker_config()
except Exception as e:
    handle_config_error(e, "worker")

# Configure logging
setup_logging(level=config.log_level, silence_noisy=True)
logger = logging.getLogger(__name__)

logger.info(f"Logging configured at {config.log_level} level")
logger.info(f"Configuration loaded: GitHub App ID={config.github.github_app_id}")

# Initialize Langfuse client (module-level, never shutdown)
langfuse = None
if config.langfuse.is_enabled:
    langfuse = get_client()
    logger.info("Langfuse observability enabled")
else:
    logger.info("Langfuse not configured - skipping observability")

# Global state
http_client = None
shutdown_event = asyncio.Event()
processor = None
health_checker = None
rate_limiters = None
job_queue = None


async def main():
    """Main worker loop - subscribes to queue and processes messages."""
    global http_client, processor, health_checker, rate_limiters, job_queue  # pylint: disable=global-statement

    logger.info("Starting Claude Agent SDK worker (job queue mode)")

    # Setup signal handlers
    setup_graceful_shutdown(shutdown_event, logger)

    # Initialize HTTP client
    http_client = httpx.AsyncClient(
        timeout=30.0,
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )

    # Initialize health checker
    health_checker = HealthChecker(
        health_file=config.health_check_file,
        update_interval=config.health_check_interval,
        max_idle_time=config.health_check_max_idle,
    )
    health_checker.start()
    logger.info(f"Health checker started: {config.health_check_file}")

    # Initialize rate limiters with Redis backend for distributed rate limiting
    try:
        from shared.rate_limiter import create_redis_rate_limiter_backend

        logger.info("Initializing distributed rate limiting with Redis...")
        redis_backend = await create_redis_rate_limiter_backend(
            redis_url=config.queue.redis_url, password=config.queue.redis_password
        )
        rate_limiters = MultiRateLimiter(backend=redis_backend)
        logger.info("Using Redis-based distributed rate limiting (multi-worker safe)")
    except (ImportError, ConnectionError, TimeoutError) as e:
        logger.warning(
            f"Failed to initialize Redis rate limiting: {e}. "
            "Falling back to in-memory rate limiting (single worker only)"
        )
        rate_limiters = MultiRateLimiter()  # Falls back to in-memory backend
    except Exception as e:
        # Catch Redis-specific errors (AuthenticationError, ResponseError, etc.)
        logger.warning(
            f"Redis error during rate limiter initialization: {e}. "
            "Falling back to in-memory rate limiting (single worker only)"
        )
        rate_limiters = MultiRateLimiter()  # Falls back to in-memory backend

    rate_limiters.add_limiter(
        "github",
        max_requests=config.github_rate_limit,
        time_window=3600,  # 1 hour
    )
    rate_limiters.add_limiter(
        "anthropic",
        max_requests=config.anthropic_rate_limit,
        time_window=60,  # 1 minute
    )
    logger.info(
        f"Rate limiters configured: GitHub={config.github_rate_limit}/hour, "
        f"Anthropic={config.anthropic_rate_limit}/min"
    )

    # Initialize job queue
    job_queue = JobQueue(
        redis_url=config.queue.redis_url,
        password=config.queue.redis_password,
        job_ttl=3600,  # 1 hour
    )
    logger.info("Job queue initialized")

    try:
        # Initialize shared GitHub auth service
        from shared import GitHubAuthService

        token_manager = GitHubAuthService(
            app_id=config.github.github_app_id,
            private_key=config.github.github_private_key,
            installation_id=config.github.github_installation_id,
            http_client=http_client,
        )

        # Initialize request processor
        processor = RequestProcessor(
            token_manager=token_manager,
            http_client=http_client,
            job_queue=job_queue,
            langfuse_client=langfuse,
            shutdown_event=shutdown_event,
            rate_limiters=rate_limiters,
            health_checker=health_checker,
        )

        logger.info("Worker initialized successfully")

        # Initialize queue
        queue = get_queue()

        # Subscribe and process messages
        async def callback(message: dict):
            if shutdown_event.is_set():
                logger.info("Shutdown in progress, skipping message")
                return

            try:
                repo = message.get("repository")
                issue_number = message.get("issue_number")
                event_data = message.get("event_data", {})
                user_query = message.get("user_query", "")
                user = message.get("user", "unknown")
                ref = message.get("ref")
                workflow_name = message.get("workflow_name")

                logger.info(
                    f"Received message with ref: {ref}, workflow: {workflow_name}"
                )
                logger.info(f"Message keys: {list(message.keys())}")
                logger.info(f"Event data: {event_data}")

                if not all([repo, event_data, workflow_name]):
                    logger.error(f"Invalid message format: {message}")
                    health_checker.record_error()
                    return

                # Type assertions after validation
                assert isinstance(repo, str)
                assert isinstance(event_data, dict)
                assert isinstance(user_query, str)
                assert isinstance(user, str)
                assert isinstance(workflow_name, str)

                job_id = await processor.process(
                    repo,
                    issue_number,
                    event_data,
                    user_query,
                    user,
                    ref,
                    workflow_name,
                )

                # Check if event was ignored
                if job_id == "ignored":
                    logger.debug("Event ignored, no workflow configured")
                    health_checker.record_activity()
                    return

                # Record successful processing
                health_checker.record_activity()

            except AssertionError as e:
                logger.error(f"Message validation failed: {e}", exc_info=True)
                health_checker.record_error()
            except Exception as e:
                logger.error(f"Error in callback: {e}", exc_info=True)
                health_checker.record_error()

        # Start listening
        logger.info("Worker ready, waiting for messages...")
        await queue.subscribe(callback)

    finally:
        # Cleanup
        logger.info("Cleaning up resources...")
        if health_checker:
            await health_checker.stop()
        if processor:
            await processor.cleanup()
        if rate_limiters:
            await rate_limiters.cleanup()
        if job_queue:
            await job_queue.close()
        await http_client.aclose()
        if langfuse:
            langfuse.flush()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt")
    except Exception as e:
        logger.error(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
