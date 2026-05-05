"""Python and TypeScript import-to-file-path resolution.

Resolves module paths in import statements to actual files within a
repository. Supports PEP-328 relative imports, absolute imports from
repo root, and package (__init__.py) resolution.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Standard library modules for common languages (avoid resolving these)
_PY_STDLIB: frozenset[str] = frozenset(
    {
        "abc",
        "argparse",
        "asyncio",
        "base64",
        "collections",
        "concurrent",
        "contextlib",
        "copy",
        "csv",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "functools",
        "glob",
        "hashlib",
        "http",
        "importlib",
        "inspect",
        "io",
        "itertools",
        "json",
        "logging",
        "math",
        "multiprocessing",
        "operator",
        "os",
        "pathlib",
        "pickle",
        "platform",
        "pprint",
        "queue",
        "random",
        "re",
        "shlex",
        "shutil",
        "signal",
        "socket",
        "sqlite3",
        "ssl",
        "string",
        "struct",
        "subprocess",
        "sys",
        "tempfile",
        "textwrap",
        "threading",
        "time",
        "traceback",
        "types",
        "typing",
        "unittest",
        "urllib",
        "uuid",
        "warnings",
        "weakref",
        "xml",
        "zipfile",
    }
)


def is_stdlib(module_path: str) -> bool:
    """Check if a module path is a standard library module."""
    top_level = module_path.split(".")[0]
    return top_level in _PY_STDLIB


def resolve_python_import(
    module_path: str,
    from_file: str,
    repo_path: Path,
) -> str | None:
    """Resolve a Python module path to a file within the repository.

    Handles:
    - Relative imports: ``from .utils import foo``, ``from ..shared import bar``
    - Absolute imports from package dir: ``from database import Database``
    - Absolute imports from repo root: ``from shared.config import Settings``

    Args:
        module_path: The module being imported (e.g., "database", ".utils").
        from_file: The file containing the import, relative to repo root.
        repo_path: Absolute path to the repository root.

    Returns:
        Relative file path within the repo (e.g., "database.py"), or None
        if the module is external, stdlib, or unresolvable.
    """
    if is_stdlib(module_path):
        return None

    from_dir = (repo_path / from_file).parent

    # Relative import: from . import X  or  from .. import X
    if module_path.startswith("."):
        return _resolve_relative(module_path, from_dir, repo_path)

    # Try relative to the importing file's directory first
    result = _find_module(from_dir / module_path.replace(".", "/"), repo_path)
    if result:
        return result

    # Then try from repo root (for projects with src/ or flat layouts)
    result = _find_module(repo_path / module_path.replace(".", "/"), repo_path)
    if result:
        return result

    logger.debug("Unresolved import: %s from %s", module_path, from_file)
    return None


def resolve_ts_import(
    module_path: str,
    from_file: str,
    repo_path: Path,
) -> str | None:
    """Resolve a TypeScript/JavaScript module path to a file.

    Handles relative imports (./foo, ../bar) and bare module specifiers.
    Bare specifiers (lodash, react) are treated as external.

    Args:
        module_path: The module being imported (e.g., "./utils", "../shared").
        from_file: The file containing the import, relative to repo root.
        repo_path: Absolute path to the repository root.

    Returns:
        Relative file path within the repo, or None if external/unresolvable.
    """
    if not module_path.startswith("."):
        # Bare specifier — external package
        return None

    from_dir = (repo_path / from_file).parent
    target = (from_dir / module_path).resolve()
    try:
        target.relative_to(repo_path)
    except ValueError:
        return None

    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"):
        candidate = target.with_suffix(ext)
        if candidate.is_file():
            return str(candidate.relative_to(repo_path)).replace("\\", "/")

    index_candidates = [
        target / f"index{ext}" for ext in (".ts", ".tsx", ".js", ".jsx")
    ]
    for candidate in index_candidates:
        if candidate.is_file():
            return str(candidate.relative_to(repo_path)).replace("\\", "/")

    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_relative(
    module_path: str,
    from_dir: Path,
    repo_path: Path,
) -> str | None:
    """Resolve a PEP-328 relative import path."""
    dots = 0
    for c in module_path:
        if c == ".":
            dots += 1
        else:
            break
    rest = module_path[dots:]

    # Walk up dots-1 directories (one dot = current package)
    target_dir = from_dir
    for _ in range(dots - 1):
        target_dir = target_dir.parent

    if rest:
        target = target_dir / rest.replace(".", "/")
    else:
        target = target_dir

    return _find_module(target, repo_path)


def _find_module(base: Path, repo_path: Path) -> str | None:
    """Try to find a Python module file given a base path.

    Checks: ``base.py``, ``base/__init__.py``.
    """
    candidates = [base.with_suffix(".py"), base / "__init__.py"]
    for candidate in candidates:
        if candidate.is_file():
            try:
                return str(candidate.relative_to(repo_path)).replace("\\", "/")
            except ValueError:
                return None
    return None
