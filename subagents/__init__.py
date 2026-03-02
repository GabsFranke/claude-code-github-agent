"""Custom subagent definitions for PR review."""

from .architecture_reviewer import ARCHITECTURE_REVIEWER

# Export all agents as a dict for easy use
AGENTS = {
    "architecture-reviewer": ARCHITECTURE_REVIEWER,
}

__all__ = [
    "AGENTS",
    "ARCHITECTURE_REVIEWER",
]
