"""GitHub webhook receiver."""

import hashlib
import hmac
import logging
import os
import sys
from typing import Any, Dict

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

# Add shared to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from shared.queue import get_queue  # noqa: E402

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="SimpleClaudeCodeGitHubAgent Webhook Service")

# Initialize queue
queue = get_queue()


class WebhookPayload(BaseModel):
    """GitHub webhook payload model."""

    action: str
    issue: Dict[str, Any] = None
    comment: Dict[str, Any] = None
    repository: Dict[str, Any]


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    """Verify GitHub webhook signature."""
    if not signature:
        return False

    expected_signature = (
        "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    )

    return hmac.compare_digest(expected_signature, signature)


def parse_command(comment_body: str) -> str:
    """Extract agent command from comment."""
    lines = comment_body.strip().split("\n")
    for line in lines:
        line = line.strip()
        if line.startswith("/agent"):
            # Return everything after /agent
            return line[6:].strip()
    return ""


@app.get("/")
async def root():
    """Root endpoint."""
    return {"status": "SimpleClaudeCodeGitHubAgent webhook service is running"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy"}


@app.post("/webhook")
async def webhook(request: Request):
    """Handle GitHub webhook events."""
    try:
        # Get payload
        payload = await request.body()
        signature = request.headers.get("X-Hub-Signature-256", "")
        event_type = request.headers.get("X-GitHub-Event", "")

        # Verify signature (optional for testing)
        webhook_secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
        if webhook_secret and not verify_signature(payload, signature, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid signature")

        # Parse payload
        data = await request.json()

        action = data.get("action", "N/A")
        logger.info(f"Received {event_type} event (action: {action})")

        # Explicitly ignore push events (these happen on every commit)
        if event_type == "push":
            logger.debug(f"Ignoring push event to {data.get('ref', 'unknown ref')}")
            return {"status": "ignored", "message": "Push events are not handled"}

        # Handle issues event (when issue is opened)
        if event_type == "issues" and data.get("action") == "opened":
            issue_body = data["issue"]["body"] or ""
            issue_title = data["issue"]["title"]
            command = parse_command(issue_body)

            if command:
                # Explicit /agent command in issue body
                request_data = {
                    "repository": data["repository"]["full_name"],
                    "issue_number": data["issue"]["number"],
                    "command": command,
                    "user": data["issue"]["user"]["login"],
                }

                logger.info(f"Agent command detected in new issue: /agent {command}")
            else:
                # No /agent command - do automatic triage
                request_data = {
                    "repository": data["repository"]["full_name"],
                    "issue_number": data["issue"]["number"],
                    "command": f"Triage this issue: {issue_title}",
                    "user": data["issue"]["user"]["login"],
                    "auto_triage": True,
                }

                logger.info(f"Auto-triaging new issue #{data['issue']['number']}")

            logger.info(
                f"Processing request for {request_data['repository']} issue #{request_data['issue_number']}"
            )

            # Publish to queue (async processing)
            await queue.publish(request_data)

            return {"status": "accepted", "message": "Agent is processing your request"}

        # Handle issue_comment events (manual /agent commands in comments)
        if event_type == "issue_comment" and data.get("action") == "created":
            comment_body = data["comment"]["body"]
            command = parse_command(comment_body)

            if command:
                # Extract context
                request_data = {
                    "repository": data["repository"]["full_name"],
                    "issue_number": data["issue"]["number"],
                    "command": command,
                    "user": data["comment"]["user"]["login"],
                }

                logger.info(f"Agent command detected: /agent {command}")
                logger.info(
                    f"Processing request for {request_data['repository']} issue #{request_data['issue_number']}"
                )

                # Publish to queue (async processing)
                await queue.publish(request_data)

                return {
                    "status": "accepted",
                    "message": "Agent is processing your request",
                }
            else:
                logger.debug(
                    f"Comment on issue #{data['issue']['number']} does not contain /agent command"
                )

        # Handle pull_request events (automatic review only when opened)
        if event_type == "pull_request":
            action = data.get("action")

            # Only auto-review when PR is first opened, ignore updates/syncs
            if action == "opened":
                pr_number = data["pull_request"]["number"]
                pr_title = data["pull_request"]["title"]
                pr_author = data["pull_request"]["user"]["login"]

                # Auto-review command
                request_data = {
                    "repository": data["repository"]["full_name"],
                    "issue_number": pr_number,  # PRs are issues too
                    "command": f"Review this pull request: {pr_title}",
                    "user": pr_author,
                    "auto_review": True,
                }

                logger.info(
                    f"Auto-reviewing PR #{pr_number} in {request_data['repository']}"
                )

                # Publish to queue
                await queue.publish(request_data)

                return {"status": "accepted", "message": "Agent will review this PR"}
            else:
                # Log ignored actions for debugging
                logger.info(
                    f"Ignoring pull_request action '{action}' for PR #{data['pull_request']['number']}"
                )
                return {
                    "status": "ignored",
                    "message": f"PR action '{action}' not handled",
                }

        return {"status": "ignored", "message": "Event not handled"}

    except Exception as e:
        logger.error(f"Error processing webhook: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
