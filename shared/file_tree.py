"""Generate compact file tree summaries for repository context injection.

Produces a directory tree showing repository structure at a configurable
depth. Respects standard exclude patterns AND .gitignore/.ignore files
to skip noise directories and generated files.
"""

import os
from collections.abc import Iterator
from pathlib import Path


def collection_name_for_repo(repo: str) -> str:
    """Convert a repo slug (owner/repo) to a SurrealDB-safe collection name."""
    return repo.replace("/", "__")


try:
    import pathspec  # type: ignore[import-untyped]

except ImportError:
    pathspec = None  # type: ignore[assignment]

# Directories to always exclude from the tree
EXCLUDE_DIRS = frozenset(
    {
        "node_modules",
        "vendor",
        "third_party",
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "site-packages",
        "egg-info",
        ".eggs",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".fleet",
        ".cargo",
    }
)

# File patterns to exclude (checked via suffix / name match)
EXCLUDE_FILES = frozenset(
    {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile.lock",
        "poetry.lock",
        "uv.lock",
        ".DS_Store",
        "Thumbs.db",
    }
)

EXCLUDE_SUFFIXES = frozenset(
    {
        ".min.js",
        ".min.css",
        ".bundle.js",
        ".pb.go",
        "_pb2.py",
        "_pb2.pyi",
        "_pb2_grpc.py",
        ".pyc",
        ".pyo",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
    }
)


def load_ignore_spec(repo_path: Path) -> "pathspec.PathSpec | None":
    """Load gitignore-style patterns from .gitignore and .ignore at repo root.

    Returns a PathSpec for matching, or None if no ignore files exist or
    pathspec is not installed.
    """
    if pathspec is None:
        return None  # type: ignore[unreachable]

    lines: list[str] = []
    for filename in (".gitignore", ".ignore"):
        ignore_file = repo_path / filename
        if ignore_file.is_file():
            try:
                lines.extend(ignore_file.read_text(encoding="utf-8").splitlines())
            except OSError:
                pass

    if not lines:
        return None

    try:
        return pathspec.PathSpec.from_lines("gitignore", lines)
    except Exception:
        return None


def walk_source_files(
    repo_path: Path,
    ignore_spec: "pathspec.PathSpec | None" = None,
) -> Iterator[Path]:
    """Walk repo yielding source file paths, respecting exclusions and ignore files.

    This is the canonical directory-walk-and-filter logic shared by
    ``_count_summary``, ``RepoMap._iter_source_files``, and
    ``chunk_repo`` (full-scan branch).

    Skips:
    - Directories in ``EXCLUDE_DIRS`` and dot-directories
    - Files in ``EXCLUDE_FILES``
    - Files ending with suffixes in ``EXCLUDE_SUFFIXES``
    - ``.git`` file guard (worktrees create a ``.git`` file)
    - Files containing ``.generated.``, ``.min.``, or ``.bundle.`` substrings
    - Paths matching the optional ``ignore_spec`` (from ``load_ignore_spec``)

    Args:
        repo_path: Absolute path to the repository root.
        ignore_spec: Optional ``pathspec.PathSpec`` from ``load_ignore_spec``.

    Yields:
        Absolute ``Path`` objects for each non-excluded source file.
    """
    repo_path = Path(repo_path)

    for root, dirs, filenames in os.walk(repo_path):
        rel_root = str(Path(root).relative_to(repo_path)).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""

        # Filter excluded and ignored directories in-place (sorted for determinism)
        filtered_dirs: list[str] = []
        for d in sorted(dirs):
            if d in EXCLUDE_DIRS or d.startswith("."):
                continue
            rel = f"{rel_root}/{d}" if rel_root else d
            if ignore_spec and (
                ignore_spec.match_file(rel + "/") or ignore_spec.match_file(rel)
            ):
                continue
            filtered_dirs.append(d)
        dirs[:] = filtered_dirs

        for name in sorted(filenames):
            # .git file guard (worktrees create a .git file, not a directory)
            if name == ".git":
                continue
            if name in EXCLUDE_FILES:
                continue
            if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
                continue
            if any(pat in name.lower() for pat in (".generated.", ".min.", ".bundle.")):
                continue
            rel = f"{rel_root}/{name}" if rel_root else name
            if ignore_spec and ignore_spec.match_file(rel):
                continue
            yield Path(root) / name


def _should_exclude_dir(name: str) -> bool:
    return name in EXCLUDE_DIRS or name.startswith(".")


def _should_exclude_file(name: str) -> bool:
    if name in EXCLUDE_FILES:
        return True
    return any(name.endswith(s) for s in EXCLUDE_SUFFIXES)


def generate_file_tree(
    repo_path: Path,
    max_depth: int = 3,
    max_entries: int = 200,
) -> str:
    """Generate a compact file tree showing directory structure.

    Respects .gitignore and .ignore files at the repo root in addition
    to the built-in exclude patterns.

    Args:
        repo_path: Path to the repository root.
        max_depth: Maximum directory depth to traverse.
        max_entries: Maximum number of entries to include.

    Returns:
        Compact tree string suitable for system prompt injection.
    """
    repo_path = Path(repo_path)
    if not repo_path.is_dir():
        return ""

    ignore_spec = load_ignore_spec(repo_path)

    lines: list[str] = []
    entry_count = 0

    def _is_ignored(rel_path: str, is_dir: bool) -> bool:
        if ignore_spec is None:
            return False
        # pathspec: check both with and without trailing slash for dirs
        if is_dir:
            result: bool = bool(
                ignore_spec.match_file(rel_path + "/")
                or ignore_spec.match_file(rel_path)
            )
            return result
        return bool(ignore_spec.match_file(rel_path))

    def _walk(directory: Path, prefix: str, depth: int, rel_prefix: str) -> None:
        nonlocal entry_count
        if depth > max_depth or entry_count >= max_entries:
            return

        try:
            entries = sorted(
                directory.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
            )
        except PermissionError:
            return

        visible = []
        for e in entries:
            rel = f"{rel_prefix}{e.name}" if rel_prefix else e.name
            if e.is_dir():
                if _should_exclude_dir(e.name) or _is_ignored(rel, True):
                    continue
            else:
                if _should_exclude_file(e.name) or _is_ignored(rel, False):
                    continue
            visible.append((e, rel))

        for i, (entry, rel) in enumerate(visible):
            if entry_count >= max_entries:
                lines.append(
                    f"{prefix}... (truncated, {len(visible) - i} more entries)"
                )
                return

            is_last = i == len(visible) - 1
            connector = "└── " if is_last else "├── "
            child_prefix = "    " if is_last else "│   "

            if entry.is_dir():
                lines.append(f"{prefix}{connector}{entry.name}/")
                entry_count += 1
                _walk(entry, prefix + child_prefix, depth + 1, rel + "/")
            else:
                lines.append(f"{prefix}{connector}{entry.name}")
                entry_count += 1

    # Use repo directory name as root
    root_name = repo_path.name
    lines.append(f"{root_name}/")
    _walk(repo_path, "", depth=1, rel_prefix="")

    if not lines:
        return ""

    summary = _count_summary(repo_path, ignore_spec)
    return "\n".join(lines) + f"\n\n({summary})"


def _count_summary(
    repo_path: Path, ignore_spec: "pathspec.PathSpec | None" = None
) -> str:
    """Generate a one-line summary of file counts."""
    counts: dict[str, int] = {}
    total = 0

    for filepath in walk_source_files(repo_path, ignore_spec):
        ext = filepath.suffix.lower()
        if ext:
            counts[ext] = counts.get(ext, 0) + 1
        else:
            counts["(no ext)"] = counts.get("(no ext)", 0) + 1
        total += 1

    if not total:
        return "empty repository"

    # Show top 5 extensions
    top = sorted(counts.items(), key=lambda x: -x[1])[:5]
    ext_summary = ", ".join(f"{ext}: {n}" for ext, n in top)
    return f"{total} files total — {ext_summary}"
