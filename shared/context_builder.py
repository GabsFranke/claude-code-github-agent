"""Async structural context generation for SDK worker jobs.

Generates file tree and repomap as pre-built text strings, with
commit-based caching in the agent-memory volume. Called from
process_job() BEFORE builder construction (outside the sync builder).
"""

import asyncio
import fnmatch
import hashlib
import logging
from pathlib import Path

from .file_tree import generate_file_tree
from .repomap import RepoMap

logger = logging.getLogger(__name__)

# Default cache directory within agent-memory volume
DEFAULT_CACHE_DIR = Path("/home/bot/agent-memory")

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
) -> tuple[str, str]:
    """Generate file tree and repomap as pre-built text strings.

    Called from process_job() BEFORE builder construction to keep
    blocking operations outside the synchronous SDKOptionsBuilder.

    Args:
        repo_path: Path to the git worktree.
        repo: Repository identifier (owner/repo) for caching.
        mentioned_files: Files to bias PageRank toward (e.g., PR changed files).
        mentioned_idents: Identifiers to bias PageRank toward.
        token_budget: Max tokens for the repomap.
        cache_dir: If provided, cache repomaps at cache_dir/{repo}/repomap/{hash}.txt.
        include_test_files: Whether to boost test files in ranking (default True).

    Returns:
        (file_tree_text, repomap_text) — both pre-built strings.
    """
    mentioned_files = mentioned_files or []
    mentioned_idents = mentioned_idents or []

    # Generate file tree (fast, no caching needed)
    file_tree_text = await asyncio.to_thread(generate_file_tree, repo_path, 3, 200)

    # Try cache lookup for repomap
    commit_hash = _get_head_commit(repo_path)
    cache_key = _cache_key(repo, commit_hash, mentioned_files)

    repomap_text = _get_cached_repomap(cache_key, cache_dir)
    if repomap_text is not None:
        logger.info(
            f"Cache hit for repomap: {repo}@{commit_hash[:8]} "
            f"({len(repomap_text)} chars)"
        )
        return file_tree_text, repomap_text

    # Generate repomap (CPU-bound, run in thread)
    repomap_text = await asyncio.to_thread(
        _generate_repomap_sync,
        repo_path,
        mentioned_files,
        mentioned_idents,
        token_budget,
        include_test_files,
    )

    # Cache the result
    if repomap_text and cache_dir:
        _cache_repomap(cache_key, repomap_text, cache_dir)
        logger.info(
            f"Cached repomap for {repo}@{commit_hash[:8]} "
            f"({len(repomap_text)} chars, {_approx_tokens(repomap_text)} tokens)"
        )

    return file_tree_text, repomap_text


def _generate_repomap_sync(
    repo_path: Path,
    mentioned_files: list[str],
    mentioned_idents: list[str],
    token_budget: int,
    include_test_files: bool = True,
) -> str:
    """Synchronous repomap generation (runs in thread pool)."""
    try:
        rm = RepoMap(repo_path)
        return rm.get_repo_map(
            mentioned_files=mentioned_files,
            mentioned_idents=mentioned_idents,
            token_budget=token_budget,
            include_test_files=include_test_files,
        )
    except Exception as e:
        logger.error(f"Repomap generation failed: {e}", exc_info=True)
        return ""


def _get_head_commit(repo_path: Path) -> str:
    """Get the HEAD commit hash from a git worktree."""
    head_file = repo_path / ".git" / "HEAD"
    try:
        # Worktrees have a gitdir reference in .git
        if head_file.is_file():
            head_content = head_file.read_text().strip()
            if head_content.startswith("ref:"):
                ref_path = head_content.split(":", 1)[1].strip()
                # For worktrees, .git is a file pointing to the actual git dir
                git_file = repo_path / ".git"
                if git_file.is_file():
                    gitdir = git_file.read_text().strip()
                    if gitdir.startswith("gitdir:"):
                        gitdir = gitdir.split(":", 1)[1].strip()
                        ref_full = Path(gitdir) / ref_path
                        if ref_full.exists():
                            return ref_full.read_text().strip()
                # Direct reference
                ref_full = repo_path / ".git" / ref_path
                if ref_full.exists():
                    return ref_full.read_text().strip()
            else:
                return head_content
    except (OSError, ValueError):
        pass

    # Fallback: try git command
    try:
        import subprocess

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass

    return "unknown"


def _cache_key(repo: str, commit_hash: str, mentioned_files: list[str]) -> str:
    """Generate a cache key from repo, commit, and personalization."""
    raw = f"{repo}:{commit_hash}:{','.join(sorted(mentioned_files))}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _get_cached_repomap(cache_key: str, cache_dir: Path | None) -> str | None:
    """Check if a cached repomap exists."""
    if not cache_dir:
        return None

    cache_path = cache_dir / "repomap_cache" / f"{cache_key}.txt"
    try:
        if cache_path.exists():
            return cache_path.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def _cache_repomap(cache_key: str, repomap_text: str, cache_dir: Path) -> None:
    """Store the generated repomap for reuse."""
    cache_path = cache_dir / "repomap_cache" / f"{cache_key}.txt"
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(repomap_text, encoding="utf-8")
    except OSError as e:
        logger.warning(f"Failed to cache repomap: {e}")


def _approx_tokens(text: str) -> int:
    """Rough token count estimate."""
    return max(1, int(len(text.split()) * 1.3))
