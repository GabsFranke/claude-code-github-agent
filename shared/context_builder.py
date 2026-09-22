"""Async structural context generation for SDK worker jobs.

Generates file tree as a pre-built text string. Deep structure (call graph,
imports, inheritance) is available on-demand via the CodeGraph MCP server.
Called from process_job() BEFORE builder construction (outside the sync builder).
"""

import asyncio
import logging
from pathlib import Path

from .file_tree import generate_file_tree

logger = logging.getLogger(__name__)


async def generate_structural_context(repo_path: Path) -> str:
    """Generate file tree as a pre-built text string.

    Called from process_job() BEFORE builder construction to keep
    blocking operations outside the synchronous SDKOptionsBuilder.

    Deep structure (call graph, imports, inheritance) is available
    on-demand via the CodeGraph MCP server — the file tree
    provides orientation only.

    Args:
        repo_path: Path to the git worktree.

    Returns:
        file_tree_text — pre-built string.
    """
    file_tree_text = await asyncio.to_thread(generate_file_tree, repo_path, 3, 200)

    return file_tree_text
