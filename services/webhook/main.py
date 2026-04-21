"""GitHub webhook receiver."""

import logging
import re
import sys

from fastapi import FastAPI, HTTPException, Request
from payload_extractor import PayloadExtractor
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
cleanup_queue = get_queue(queue_name="agent:worktree:cleanup")

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

# Initialize payload extractor for generic field extraction
extractor = PayloadExtractor()


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

        # Handle cleanup events for persistent worktrees
        if repo and event_type in ("pull_request", "issues", "delete"):
            cleanup_msg = _build_cleanup_message(event_type, action, data, repo)
            if cleanup_msg:
                await cleanup_queue.publish(cleanup_msg)
                logger.info(
                    "Queued worktree cleanup: %s for %s",
                    cleanup_msg.get("action"),
                    repo,
                )
                # For close events without a matching workflow, return early
                if not workflow_engine.get_workflow_for_event(event_type, action):
                    return {
                        "status": "accepted",
                        "message": f"Worktree cleanup queued ({cleanup_msg['action']})",
                    }

        # Extract state if available (for zombie revival prevention)
        issue_state = (
            data.get("issue", {}).get("state")
            or data.get("pull_request", {}).get("state")
            or data.get("discussion", {}).get("state")
            or "open"
        )

        # Determine event data and user query
        event_data = {
            "event_type": event_type,
            "action": action,
            "issue_state": issue_state,
        }
        user_query = ""
        command = None

        # --- Overlay: command parsing for issue_comment events ---
        if event_type == "issue_comment" and action == "created":
            body = data.get("comment", {}).get("body", "")

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

                logger.info(
                    "Command '%s' on issue #%s with query: %s",
                    command,
                    data.get("issue", {}).get("number"),
                    user_query[:50] if user_query else "(none)",
                )
            else:
                logger.debug("Comment does not contain a command")  # type: ignore[unreachable]
                # Note: This early return means issue_comment events
                # without commands cannot trigger workflows. If a
                # workflow needs to respond to all issue_comment events,
                # this check must be moved after workflow routing.
                return {"status": "ignored", "message": "No command found in comment"}

        # --- Generic extraction for all event types ---
        try:
            fields = extractor.extract(event_type, action, data)
        except ValueError as e:
            logger.warning("Failed to extract fields: %s", e)
            raise HTTPException(status_code=422, detail=str(e)) from e

        issue_number = fields.issue_number
        ref = fields.ref

        # For issue_comment on a PR, compute the PR head ref
        if (
            event_type == "issue_comment"
            and "pull_request" in data.get("issue", {})
            and issue_number
        ):
            ref = f"refs/pull/{issue_number}/head"

        # Merge extra fields into event_data
        event_data.update(fields.extra)

        logger.info(
            "Extracted: event=%s.%s issue=%s ref=%s user=%s",
            event_type,
            action,
            issue_number,
            ref,
            fields.user,
        )

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

        # Check declarative payload filters (only for event triggers, not commands)
        if not command:
            event_key = f"{event_type}.{action}" if action else event_type
            if not workflow_engine.check_filters(workflow_name, data, event_key):
                logger.info(
                    "Workflow '%s' filters did not match payload - ignoring",
                    workflow_name,
                )
                return {
                    "status": "ignored",
                    "message": f"Payload did not match filters for workflow '{workflow_name}'",
                }

        # Get user who triggered this (from extractor)
        user = fields.user

        # Check if we should skip events from the bot itself
        # The 'sender' field in GitHub webhooks is always the user who triggered the event
        event_actor = data.get("sender", {}).get("login", "")
        bot_username = config.webhook_bot_username

        if bot_username and workflow_engine.should_skip_self(
            workflow_name, event_actor, bot_username
        ):
            logger.info(
                f"Skipping workflow '{workflow_name}' - event triggered by bot itself "
                f"(actor: {event_actor}, skip_self: true)"
            )
            return {
                "status": "ignored",
                "message": f"Skipping event from bot itself (skip_self enabled for workflow '{workflow_name}')",
            }

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


def _build_cleanup_message(
    event_type: str, action: str, data: dict, repo: str
) -> dict | None:
    """Build a worktree cleanup message from a GitHub event.

    Returns None if the event doesn't warrant cleanup.
    """
    if event_type == "pull_request" and action == "closed":
        pr_number = data.get("pull_request", {}).get("number")
        if pr_number:
            return {
                "action": "expire_thread",
                "repo": repo,
                "thread_type": "pr",
                "thread_id": str(pr_number),
            }

    if event_type == "issues" and action == "closed":
        issue_number = data.get("issue", {}).get("number")
        if issue_number:
            return {
                "action": "expire_thread",
                "repo": repo,
                "thread_type": "issue",
                "thread_id": str(issue_number),
            }

    if event_type == "discussion" and action in ("closed", "deleted", "locked"):
        discussion_number = data.get("discussion", {}).get("number")
        if discussion_number:
            return {
                "action": "expire_thread",
                "repo": repo,
                "thread_type": "discussion",
                "thread_id": str(discussion_number),
            }

    if event_type == "delete" and data.get("ref_type") == "branch":
        branch = data.get("ref")
        if branch:
            return {
                "action": "cleanup_branch",
                "repo": repo,
                "branch": branch,
            }

    if event_type == "pull_request" and action == "reopened":
        pr_number = data.get("pull_request", {}).get("number")
        if pr_number:
            return {
                "action": "revive_thread",
                "repo": repo,
                "thread_type": "pr",
                "thread_id": str(pr_number),
            }

    if event_type == "issues" and action == "reopened":
        issue_number = data.get("issue", {}).get("number")
        if issue_number:
            return {
                "action": "revive_thread",
                "repo": repo,
                "thread_type": "issue",
                "thread_id": str(issue_number),
            }

    if event_type == "discussion" and action in ("reopened", "unlocked"):
        discussion_number = data.get("discussion", {}).get("number")
        if discussion_number:
            return {
                "action": "revive_thread",
                "repo": repo,
                "thread_type": "discussion",
                "thread_id": str(discussion_number),
            }

    return None


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=config.port)
