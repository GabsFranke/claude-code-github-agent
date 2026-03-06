"""Claude Code worker that processes GitHub requests from message queue."""

import asyncio
import logging
import signal
import sys

import httpx
from langfuse import Langfuse

# Import shared utilities
from shared import JobQueue, MultiRateLimiter, get_queue
from shared.config import get_worker_config
from shared.health import HealthChecker

# Import modularized components
from .processors import RequestProcessor

# Load configuration first with detailed error reporting
try:
    config = get_worker_config()
except Exception as e:
    # Setup basic logging for error reporting before config is loaded
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    error_logger = logging.getLogger(__name__)

    error_logger.error(
        "FATAL: Configuration validation failed. Cannot start worker.",
        exc_info=True,
    )
    error_logger.error(f"Error details: {type(e).__name__}: {e}")
    error_logger.error(
        "Please check your .env file and ensure all required environment variables are set correctly."
    )
    error_logger.error("See docs/CONFIGURATION.md for configuration requirements.")

    # Also print to stderr for container logs
    print(f"\n{'='*60}", file=sys.stderr)
    print("FATAL ERROR: Configuration Validation Failed", file=sys.stderr)
    print(f"{'='*60}", file=sys.stderr)
    print(f"Error: {type(e).__name__}: {e}", file=sys.stderr)
    print("\nPlease verify:", file=sys.stderr)
    print("  1. .env file exists and is readable", file=sys.stderr)
    print("  2. All required environment variables are set", file=sys.stderr)
    print("  3. Values are in correct format (URLs, integers, etc.)", file=sys.stderr)
    print("\nSee docs/CONFIGURATION.md for details.", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Silence noisy HTTP debug logs
logging.getLogger("httpcore").setLevel(logging.INFO)
logging.getLogger("httpx").setLevel(logging.INFO)
logging.getLogger("urllib3").setLevel(logging.INFO)

logger.info(f"Logging configured at {config.log_level} level")
logger.info(f"Configuration loaded: GitHub App ID={config.github.github_app_id}")

# Initialize Langfuse client (module-level, never shutdown)
langfuse = None
if config.langfuse.is_enabled:
    langfuse = Langfuse(
        public_key=config.langfuse.langfuse_public_key,
        secret_key=config.langfuse.langfuse_secret_key,
        host=config.langfuse.langfuse_host,
    )
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


def handle_shutdown(signum, _frame):
    """Handle shutdown signals gracefully."""
    logger.info("Received signal %s, initiating graceful shutdown...", signum)
    shutdown_event.set()


def setup_signal_handlers():
    """Setup signal handlers for graceful shutdown."""
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


async def main():
    """Main worker loop - subscribes to queue and processes messages."""
    global http_client, processor, health_checker, rate_limiters, job_queue  # pylint: disable=global-statement

    logger.info("Starting Claude Agent SDK worker (job queue mode)")

    # Setup signal handlers
    setup_signal_handlers()

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

                logger.info(f"Received message with ref: {ref}")
                logger.info(f"Message keys: {list(message.keys())}")
                logger.info(f"Event data: {event_data}")

                if not all([repo, event_data]):
                    logger.error(f"Invalid message format: {message}")
                    health_checker.record_error()
                    return

                # Type assertions after validation
                assert isinstance(repo, str)
                assert isinstance(event_data, dict)
                assert isinstance(user_query, str)
                assert isinstance(user, str)

                job_id = await processor.process(
                    repo,
                    issue_number,
                    event_data,
                    user_query,
                    user,
                    ref,
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
