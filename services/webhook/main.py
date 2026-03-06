"""GitHub webhook receiver."""

import logging
import re
import sys

from fastapi import FastAPI, HTTPException, Request
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
            logger.warning(
                "Webhook signature verification failed for %s event from %s",
                event_type,
                request.client.host if request.client else "unknown",
            )
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse payload
        data = await request.json()
        action = data.get("action", "")
        repo = data.get("repository", {}).get("full_name")

        logger.info("Received %s event (action: %s) for %s", event_type, action, repo)

        # Handle push events for proactive cache warming (special case)
        if event_type == "push":
            ref = data.get("ref")
            logger.info(
                "Handling push event to %s in %s for proactive cache warming", ref, repo
            )
            if repo and ref:
                await sync_queue.publish({"repo": repo, "ref": ref})
                return {"status": "accepted", "message": "Proactive sync triggered"}
            return {"status": "ignored", "message": "Push event missing repo or ref"}

        # Determine event data and user query
        event_data = {
            "event_type": event_type,
            "action": action,
        }
        user_query = ""
        issue_number = None
        ref = "main"

        # Check if it's a comment with a command
        if event_type == "issue_comment" and action == "created":
            body = data.get("comment", {}).get("body", "")
            issue_number = data.get("issue", {}).get("number")

            # Parse command from comment
            match = re.match(r"^(/\S+)\s*(.*)", body.strip())
            if match:
                command = match.group(1)
                user_query = match.group(2).strip()
                event_data["command"] = command

                # Determine ref for PRs
                if "pull_request" in data.get("issue", {}):
                    ref = f"refs/pull/{issue_number}/head"

                logger.info(
                    "Command '%s' on issue #%s with query: %s",
                    command,
                    issue_number,
                    user_query[:50] if user_query else "(none)",
                )
            else:
                logger.debug("Comment does not contain a command")  # type: ignore[unreachable]
                return {"status": "ignored", "message": "No command found in comment"}
        elif event_type == "pull_request":
            # For PR events, extract PR number
            issue_number = data.get("pull_request", {}).get("number")
            ref = f"refs/pull/{issue_number}/head"
            logger.info("Event %s.%s for issue #%s", event_type, action, issue_number)
        elif event_type == "issues":
            # For issue events, extract issue number
            issue_number = data.get("issue", {}).get("number")
            logger.info("Event %s.%s for issue #%s", event_type, action, issue_number)

        # Get user who triggered this
        user = "unknown"
        if event_type == "issue_comment":
            user = data.get("comment", {}).get("user", {}).get("login", "unknown")
        elif event_type == "pull_request":
            user = data.get("pull_request", {}).get("user", {}).get("login", "unknown")
        elif event_type == "issues":
            user = data.get("issue", {}).get("user", {}).get("login", "unknown")

        # Queue agent job with raw event data (worker will decide if sync is needed)
        job = {
            "repository": repo,
            "issue_number": issue_number,
            "event_data": event_data,  # Raw event info for worker to route
            "user_query": user_query,
            "user": user,
            "ref": ref,
        }

        logger.info(
            "Queueing job: event=%s.%s, issue=%s, query=%s",
            event_type,
            action,
            issue_number,
            user_query[:50] if user_query else "(none)",
        )
        await queue.publish(job)

        return {"status": "accepted", "message": "Agent is processing your request"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error processing webhook: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port)
