"""Custom subagent definitions for PR review."""

from .architecture_reviewer import ARCHITECTURE_REVIEWER
from .security_reviewer import SECURITY_REVIEWER
from .bug_hunter import BUG_HUNTER
from .code_quality_reviewer import CODE_QUALITY_REVIEWER

# Export all agents as a dict for easy use
AGENTS = {
    "architecture-reviewer": ARCHITECTURE_REVIEWER,
    "security-reviewer": SECURITY_REVIEWER,
    "bug-hunter": BUG_HUNTER,
    "code-quality-reviewer": CODE_QUALITY_REVIEWER,
}

__all__ = [
    "AGENTS",
    "ARCHITECTURE_REVIEWER",
    "SECURITY_REVIEWER",
    "BUG_HUNTER",
    "CODE_QUALITY_REVIEWER",
]
