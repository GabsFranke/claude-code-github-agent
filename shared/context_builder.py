"""Async structural context generation for SDK worker jobs.

Generates file tree as a pre-built text string, with commit-based
caching. Deep structure (call graph, imports, inheritance) is available
on-demand via the codebase_tools MCP server. Called from process_job()
BEFORE builder construction (outside the sync builder).
"""

import asyncio
import fnmatch
import logging
from pathlib import Path

from .file_tree import generate_file_tree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority focus area patterns
# Maps abstract focus area names to file glob patterns.
# When a workflow sets priority_focus, files matching these patterns
# get added to mentioned_files for PageRank personalization.
# ---------------------------------------------------------------------------

FOCUS_PATTERNS: dict[str, list[str]] = {
    "build_system": [
        "Dockerfile*",
        "docker-compose*",
        "Makefile*",
        "*.toml",
        "*.cfg",
        "setup.py",
        "setup.cfg",
        "requirements*.txt",
        "Pipfile*",
        "pyproject.toml",
        ".github/workflows/*.yml",
        ".github/workflows/*.yaml",
        "Jenkinsfile",
        "Gemfile",
        "build.gradle*",
        "pom.xml",
        "package.json",
    ],
    "test_structure": [
        "conftest.py",
        "pytest.ini",
        "tox.ini",
        "test_*.py",
        "*_test.py",
        "tests/**",
        "spec/**",
        "__tests__/**",
        ".mocharc.*",
        "jest.config.*",
        "karma.conf.*",
    ],
    "api_surface": [
        "api/**",
        "routes/**",
        "endpoints/**",
        "views.py",
        "urls.py",
        "router.py",
        "app.py",
        "main.py",
        "server.py",
    ],
    "dependencies": [
        "requirements*.txt",
        "Pipfile*",
        "pyproject.toml",
        "package.json",
        "go.mod",
        "go.sum",
        "Cargo.toml",
        "Gemfile",
        "*.csproj",
        "yarn.lock",
        "package-lock.json",
    ],
}

# Max files to return per focus area (avoid flooding mentioned_files)
_MAX_FOCUS_FILES_PER_AREA = 50


def find_priority_focus_files(repo_path: Path, focus_areas: list[str]) -> list[str]:
    """Find files matching priority focus area patterns.

    Scans the worktree for files matching the glob patterns defined
    for each focus area and returns their relative paths.

    Args:
        repo_path: Path to the git worktree.
        focus_areas: List of focus area names (e.g., ["build_system", "test_structure"]).

    Returns:
        List of relative file paths matching the focus areas.
    """
    import os

    from .file_tree import EXCLUDE_DIRS

    # Collect all patterns for requested areas
    patterns: list[str] = []
    for area in focus_areas:
        area_patterns = FOCUS_PATTERNS.get(area)
        if area_patterns:
            patterns.extend(area_patterns)
        else:
            logger.warning(f"Unknown priority focus area: {area}")

    if not patterns:
        return []

    # Walk the repo and match against patterns
    matches: list[str] = []
    for root, dirs, filenames in os.walk(repo_path):
        # Filter excluded directories in-place
        dirs[:] = [d for d in sorted(dirs) if d not in EXCLUDE_DIRS]

        for name in sorted(filenames):
            rel = str(Path(root) / name).replace("\\", "/")
            rel = rel.replace(str(repo_path).replace("\\", "/") + "/", "")

            for pattern in patterns:
                if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern):
                    matches.append(rel)
                    break

            if len(matches) >= _MAX_FOCUS_FILES_PER_AREA * len(focus_areas):
                return matches

    return matches


async def generate_structural_context(
    repo_path: Path,
    repo: str = "",
    mentioned_files: list[str] | None = None,
    mentioned_idents: list[str] | None = None,
    token_budget: int = 4096,
    cache_dir: Path | None = None,
    include_test_files: bool = True,
) -> str:
    """Generate file tree as a pre-built text string.

    Called from process_job() BEFORE builder construction to keep
    blocking operations outside the synchronous SDKOptionsBuilder.

    Deep structure (call graph, imports, inheritance) is available
    on-demand via the codebase_tools MCP server — the file tree
    provides orientation only.

    Args:
        repo_path: Path to the git worktree.
        repo: Repository identifier (owner/repo) for caching.
        mentioned_files: Kept for API compatibility (unused).
        mentioned_idents: Kept for API compatibility (unused).
        token_budget: Kept for API compatibility (unused).
        cache_dir: Kept for API compatibility (unused).
        include_test_files: Kept for API compatibility (unused).

    Returns:
        file_tree_text — pre-built string.
    """
    # Generate file tree (fast, no caching needed)
    file_tree_text = await asyncio.to_thread(generate_file_tree, repo_path, 3, 200)

    return file_tree_text
