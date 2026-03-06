"""GitHub webhook receiver."""

import logging
import re
import sys

from fastapi import FastAPI, HTTPException, Request
from validators import verify_signature

from shared import get_queue
from shared.config import get_webhook_config, handle_config_error
from shared.logging_utils import setup_logging
from workflows import WorkflowEngine

# Load configuration with detailed error reporting
try:
    config = get_webhook_config()
except Exception as e:
    handle_config_error(e, "webhook service")

# Configure logging
setup_logging(level=config.log_level)
logger = logging.getLogger(__name__)

logger.info(f"Logging configured at {config.log_level} level")
logger.info(f"Configuration loaded: Port={config.port}")

app = FastAPI(title="ClaudeCodeGitHubAgent Webhook Service")

# Initialize queue
queue = get_queue()
sync_queue = get_queue(queue_name="agent:sync:requests")

# Initialize workflow engine for event filtering
try:
    workflow_engine = WorkflowEngine()
    logger.info(
        f"Loaded {len(workflow_engine.workflows)} workflows for event filtering"
    )
except Exception as e:
    logger.error(f"Failed to load workflow engine: {e}", exc_info=True)
    print("\nFATAL ERROR: Failed to load workflows.yaml", file=sys.stderr)
    print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)


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
        command = None
        if event_type == "issue_comment" and action == "created":
            body = data.get("comment", {}).get("body", "")
            issue_number = data.get("issue", {}).get("number")

            # Validate issue_number exists
            if issue_number is None:
                logger.warning("Issue comment event missing issue number")
                return {
                    "status": "error",
                    "message": "Invalid issue comment: missing issue number",
                }

            # Parse command from comment
            match = re.match(r"^(/\S+)\s*(.*)", body.strip())
            if match:
                command = match.group(1)
                user_query = match.group(2).strip()

                # Validate command format
                if len(command) > 50:
                    logger.warning(f"Command too long: {command[:50]}...")
                    return {
                        "status": "error",
                        "message": "Command is too long (max 50 characters)",
                    }

                if not re.match(r"^/[a-z0-9\-]+$", command):
                    logger.warning(f"Invalid command format: {command}")  # type: ignore[unreachable]
                    return {
                        "status": "error",
                        "message": "Invalid command format. Use lowercase letters, numbers, and hyphens only.",
                    }

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
            if issue_number is None:
                logger.warning("Pull request event missing PR number")
                return {
                    "status": "error",
                    "message": "Invalid pull request: missing PR number",
                }
            ref = f"refs/pull/{issue_number}/head"
            logger.info("Event %s.%s for issue #%s", event_type, action, issue_number)
        elif event_type == "issues":
            # For issue events, extract issue number
            issue_number = data.get("issue", {}).get("number")
            if issue_number is None:
                logger.warning("Issue event missing issue number")
                return {
                    "status": "error",
                    "message": "Invalid issue: missing issue number",
                }
            logger.info("Event %s.%s for issue #%s", event_type, action, issue_number)

        # Check if we have a workflow configured for this event/command
        workflow_name = None
        if command:
            workflow_name = workflow_engine.get_workflow_for_command(command)
            logger.info(f"Command '{command}' -> workflow '{workflow_name}'")
        elif event_type:
            workflow_name = workflow_engine.get_workflow_for_event(event_type, action)
            logger.info(f"Event {event_type}.{action} -> workflow '{workflow_name}'")

        if not workflow_name:
            logger.info(
                f"No workflow configured for event={event_type}.{action} command={command} - ignoring"
            )
            return {
                "status": "ignored",
                "message": "No workflow configured for this event",
            }

        # Get user who triggered this
        user = "unknown"
        if event_type == "issue_comment":
            user = data.get("comment", {}).get("user", {}).get("login", "unknown")
        elif event_type == "pull_request":
            user = data.get("pull_request", {}).get("user", {}).get("login", "unknown")
        elif event_type == "issues":
            user = data.get("issue", {}).get("user", {}).get("login", "unknown")

        # Queue agent job with event data
        job = {
            "repository": repo,
            "issue_number": issue_number,
            "event_data": event_data,
            "user_query": user_query,
            "user": user,
            "ref": ref,
            "workflow_name": workflow_name,  # Pass workflow name to worker
        }

        logger.info(
            "Queueing job: workflow=%s, event=%s.%s, issue=%s, query=%s",
            workflow_name,
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
