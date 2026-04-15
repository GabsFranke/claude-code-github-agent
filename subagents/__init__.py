"""Custom subagent definitions for PR review."""

from .architecture_reviewer import ARCHITECTURE_REVIEWER
from .memory_extractor import MEMORY_EXTRACTOR

# Export all agents as a dict for easy use
AGENTS = {
    "architecture-reviewer": ARCHITECTURE_REVIEWER,
    "memory-extractor": MEMORY_EXTRACTOR,
}

__all__ = [
    "AGENTS",
    "ARCHITECTURE_REVIEWER",
    "MEMORY_EXTRACTOR",
]
