"""Handle GitHub issue events."""

import logging
import sys
from pathlib import Path
from typing import Any

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.command_parser import parse_command  # noqa: E402

logger = logging.getLogger(__name__)


async def handle_issue_opened(
    data: dict[str, Any], queue, sync_queue
) -> dict[str, str]:
    """Handle issue opened event."""
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
        logger.info("Agent command detected in new issue: /agent %s", command)
    else:
        # No /agent command - do automatic triage
        request_data = {
            "repository": data["repository"]["full_name"],
            "issue_number": data["issue"]["number"],
            "command": f"Triage this issue: {issue_title}",
            "user": data["issue"]["user"]["login"],
            "auto_triage": True,
        }
        logger.info("Auto-triaging new issue #%s", data["issue"]["number"])

    logger.info(
        "Processing request for %s issue #%s",
        request_data["repository"],
        request_data["issue_number"],
    )

    ref = "main"
    if "pull_request" in data["issue"]:
        ref = f"refs/pull/{request_data['issue_number']}/head"

    # Publish sync request - sync worker will use GitHub App credentials
    await sync_queue.publish({"repo": request_data["repository"], "ref": ref})
    await queue.publish(request_data)

    return {"status": "accepted", "message": "Agent is processing your request"}
