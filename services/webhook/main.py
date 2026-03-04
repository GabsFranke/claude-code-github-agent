"""GitHub webhook receiver."""

import logging
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

# Add shared to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from handlers import (
    handle_comment_created,
    handle_issue_opened,
    handle_pr_opened,
    handle_pr_other_action,
)
from validators import verify_signature

from shared import get_queue
from shared.config import get_webhook_config

# Load configuration with detailed error reporting
try:
    config = get_webhook_config()
except Exception as e:
    # Setup basic logging for error reporting before config is loaded
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    error_logger = logging.getLogger(__name__)

    error_logger.error(
        "FATAL: Configuration validation failed. Cannot start webhook service.",
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
    print("  4. GITHUB_WEBHOOK_SECRET is set correctly", file=sys.stderr)
    print("\nSee docs/CONFIGURATION.md for details.", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)

    sys.exit(1)

# Configure logging
logging.basicConfig(
    level=getattr(logging, config.log_level, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

logger.info(f"Logging configured at {config.log_level} level")
logger.info(f"Configuration loaded: Port={config.port}")

app = FastAPI(title="ClaudeCodeGitHubAgent Webhook Service")

# Initialize queue
queue = get_queue()
sync_queue = get_queue(queue_name="agent:sync:requests")


@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "ClaudeCodeGitHubAgent webhook service is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "webhook",
        "queue_type": config.queue.queue_type,
    }


@app.post("/webhook")
async def webhook(request: Request):
    """Handle GitHub webhook events."""
    try:
        # Get payload and headers
        payload = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        event_type = request.headers.get("X-GitHub-Event", "")

        # Verify signature
        webhook_secret = config.github.github_webhook_secret
        if webhook_secret and not verify_signature(payload, signature, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse payload
        data = await request.json()
        action = data.get("action", "N/A")

        logger.info("Received %s event (action: %s)", event_type, action)

        # Handle push events for proactive cache warming
        if event_type == "push":
            ref = data.get("ref")
            repo = data.get("repository", {}).get("full_name")
            logger.info(
                "Handling push event to %s in %s for proactive cache warming", ref, repo
            )
            if repo and ref:
                await sync_queue.publish({"repo": repo, "ref": ref})
                return {"status": "accepted", "message": "Proactive sync triggered"}
            return {"status": "ignored", "message": "Push event missing repo or ref"}

        # Route to appropriate handler
        if event_type == "issues" and action == "opened":
            return await handle_issue_opened(data, queue, sync_queue)

        if event_type == "issue_comment" and action == "created":
            result = await handle_comment_created(data, queue, sync_queue)
            if result:
                return result
            # No command found, return ignored
            return {"status": "ignored", "message": "No /agent command found"}

        if event_type == "pull_request":
            if action == "opened":
                return await handle_pr_opened(data, queue, sync_queue)
            return handle_pr_other_action(action, data["pull_request"]["number"])

        return {"status": "ignored", "message": "Event not handled"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing webhook: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port)
