"""Handle GitHub pull request events."""

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def handle_pr_opened(data: dict[str, Any], queue, sync_queue) -> dict[str, str]:
    """Handle pull request opened event (auto-review)."""
    pr_number = data["pull_request"]["number"]
    pr_title = data["pull_request"]["title"]
    pr_author = data["pull_request"]["user"]["login"]

    request_data = {
        "repository": data["repository"]["full_name"],
        "issue_number": pr_number,  # PRs are issues too
        "command": f"Review this pull request: {pr_title}",
        "user": pr_author,
        "auto_review": True,
    }

    logger.info("Auto-reviewing PR #%s in %s", pr_number, request_data["repository"])

    # Publish sync request - sync worker will use GitHub App credentials
    await sync_queue.publish(
        {"repo": request_data["repository"], "ref": f"refs/pull/{pr_number}/head"}
    )
    await queue.publish(request_data)

    return {"status": "accepted", "message": "Agent will review this PR"}


def handle_pr_other_action(action: str, pr_number: int) -> dict[str, str]:
    """Handle other PR actions (ignored)."""
    logger.info("Ignoring pull_request action '%s' for PR #%s", action, pr_number)
    return {"status": "ignored", "message": f"PR action '{action}' not handled"}
