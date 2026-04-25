"""Codebase search tools for structured code exploration.

Provides find_definitions, find_references, search_codebase, and
read_file_summary tools that agents can use for targeted lookups
without burning tokens on raw Bash/Grep exploration.

Reuses Phase 1's tree-sitter infrastructure from shared.repomap.
"""

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from shared.file_tree import EXCLUDE_DIRS
from shared.repomap import Tag
from shared.ts_languages import EXTENSION_MAP, get_language

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (initialized once by init_repo)
# ---------------------------------------------------------------------------

_repo_path: Path | None = None
_tags_cache: list[Tag] = []
_ast_cache: dict[tuple[str, float], tuple[Any, bytes]] = {}
_AST_CACHE_MAX_SIZE = 500

# File type aliases for ripgrep
_FILE_TYPE_MAP: dict[str, str] = {
    "python": "py",
    "js": "js",
    "ts": "ts",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "ruby": "ruby",
    "c": "c",
    "cpp": "cpp",
}

# Regex patterns for read_file_summary fallback
_SUMMARY_REGEX_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("function", r"^\s*(?:async\s+)?def\s+(\w+)\s*\("),
        ("class", r"^\s*class\s+(\w+)"),
    ],
    "_generic": [
        ("function", r"^\s*(?:function|func|fn|def|sub)\s+(\w+)"),
        ("class", r"^\s*(?:class|struct|interface|type|enum)\s+(\w+)"),
    ],
}

# Python tree-sitter queries for read_file_summary
_PY_IMPORT_QUERY = (
    "(import_statement) @node",
    "(import_from_statement) @node",
)
_PY_DOCSTRING_QUERY = ("(expression_statement (string)) @node",)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def init_repo(repo_path: str) -> None:
    """Initialize module state. Called once at server startup.

    Args:
        repo_path: Absolute path to the git worktree.
    """
    global _repo_path, _tags_cache

    from shared.repomap import RepoMap

    _repo_path = Path(repo_path).resolve()

    if not _repo_path.is_dir():
        raise ValueError(f"Repo path does not exist: {_repo_path}")

    # Extract and cache all tags for find_definitions / find_references
    rm = RepoMap(_repo_path)
    _tags_cache = rm.extract_tags()
    logger.info(
        f"Initialized codebase tools for {_repo_path} "
        f"({len(_tags_cache)} tags cached)"
    )


# ---------------------------------------------------------------------------
# Path validation
# ---------------------------------------------------------------------------


def _resolve_and_validate(file_path: str) -> Path:
    """Resolve a path and ensure it stays within the repo root.

    Args:
        file_path: Relative file path to validate.

    Returns:
        Resolved absolute path.

    Raises:
        ValueError: If path escapes the repo directory.
    """
    if _repo_path is None:
        raise ValueError("Repository not initialized. Call init_repo() first.")

    resolved = (_repo_path / file_path).resolve()
    try:
        resolved.relative_to(_repo_path)
    except ValueError:
        raise ValueError(
            f"Invalid path: outside repository ({resolved} not in {_repo_path})"
        )
    return resolved


# ---------------------------------------------------------------------------
# AST caching
# ---------------------------------------------------------------------------


def _get_or_parse(filepath: Path) -> tuple[Any, bytes | None]:
    """Return cached tree-sitter tree + source, or parse and cache.

    Args:
        filepath: Absolute path to the source file.

    Returns:
        Tuple of (tree-sitter tree or None, source bytes or None).
    """
    try:
        mtime = filepath.stat().st_mtime
    except OSError:
        return None, None

    # Evict entire cache if it exceeds max size (simple but effective
    # for MCP server sessions where file access is bounded)
    if len(_ast_cache) > _AST_CACHE_MAX_SIZE:
        _ast_cache.clear()

    cache_key = (str(filepath), mtime)
    if cache_key in _ast_cache:
        return _ast_cache[cache_key]

    ext = filepath.suffix.lower()
    lang_name = EXTENSION_MAP.get(ext)
    if not lang_name:
        return None, None

    try:
        lang = get_language(lang_name)
        if lang is None:
            return None, None

        source = filepath.read_bytes()

        from tree_sitter import Parser

        parser = Parser(lang)  # type: ignore[arg-type]

        tree = parser.parse(source)
        if tree is None:
            return None, None  # type: ignore[unreachable]

        result = (tree, source)
        _ast_cache[cache_key] = result
        return result
    except Exception as e:
        logger.debug(f"Failed to parse {filepath}: {e}")
        return None, None


# ---------------------------------------------------------------------------
# Tool: find_definitions
# ---------------------------------------------------------------------------


def find_definitions(symbol_name: str) -> list[dict[str, Any]]:
    """Find where a symbol (class, function, method) is defined.

    Uses the cached tag index from Phase 1's RepoMap. Supports
    tree-sitter and regex-extracted tags.

    Args:
        symbol_name: Exact name of the symbol to find.

    Returns:
        List of dicts with keys: file, line, kind, signature, end_line.
    """
    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for tag in _tags_cache:
        if tag.kind != "definition" or tag.name != symbol_name:
            continue

        key = (tag.filepath, tag.line)
        if key in seen:
            continue
        seen.add(key)

        # Read the source line for a signature
        signature = _read_source_line(tag.filepath, tag.line)

        results.append(
            {
                "file": tag.filepath,
                "line": tag.line,
                "kind": tag.category,
                "signature": signature,
                "end_line": tag.end_line or tag.line,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Tool: find_references
# ---------------------------------------------------------------------------


def find_references(symbol_name: str) -> list[dict[str, Any]]:
    """Find all references to a symbol across the codebase.

    Uses text search (ripgrep or Python regex) to find all occurrences
    of the exact symbol name, then excludes definition lines. This works
    across all languages regardless of tree-sitter availability.

    Args:
        symbol_name: Exact name of the symbol.

    Returns:
        List of dicts with keys: file, line, context.
    """
    # Collect definition locations to exclude from results
    definition_lines: set[tuple[str, int]] = set()
    for tag in _tags_cache:
        if tag.kind == "definition" and tag.name == symbol_name:
            definition_lines.add((tag.filepath, tag.line))

    # Search for exact word-boundary matches of the symbol name
    # Use word-boundary regex to match whole identifiers only
    pattern = r"\b" + re.escape(symbol_name) + r"\b"

    raw_results = search_codebase(pattern, max_results=50)

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    for r in raw_results:
        key = (r["file"], r["line"])
        # Skip definitions — those are covered by find_definitions
        if key in definition_lines:
            continue
        if key in seen:
            continue
        seen.add(key)

        results.append(
            {
                "file": r["file"],
                "line": r["line"],
                "context": r["context"],
            }
        )

    return results


# ---------------------------------------------------------------------------
# Tool: search_codebase
# ---------------------------------------------------------------------------


def search_codebase(
    pattern: str,
    file_type: str | None = None,
    max_results: int = 20,
) -> list[dict[str, Any]]:
    """Search codebase with ripgrep (or Python regex fallback).

    Args:
        pattern: Regex or literal pattern to search for.
        file_type: Optional file type alias (e.g., "python").
        max_results: Maximum results to return (capped at 100).

    Returns:
        List of dicts with keys: file, line, match, context.
    """
    max_results = min(max(1, max_results), 100)

    rg_path = shutil.which("rg")
    if rg_path:
        return _search_with_rg(rg_path, pattern, file_type, max_results)
    return _search_with_re(pattern, file_type, max_results)


def _search_with_rg(
    rg_path: str,
    pattern: str,
    file_type: str | None,
    max_results: int,
) -> list[dict[str, Any]]:
    """Search using ripgrep with JSON output."""
    if _repo_path is None:
        return []

    cmd = [
        rg_path,
        "--json",
        "--max-count",
        str(max_results),
        "--color",
        "never",
        "--no-heading",
        "--line-number",
    ]

    if file_type:
        rg_type = _FILE_TYPE_MAP.get(file_type, file_type)
        cmd.extend(["--type", rg_type])

    cmd.extend([pattern, str(_repo_path)])

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(_repo_path),
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning(f"ripgrep search failed: {e}")
        return _search_with_re(pattern, file_type, max_results)

    results: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        if len(results) >= max_results:
            break
        try:
            entry = _parse_rg_json_line(line)
            if entry:
                results.append(entry)
        except (ValueError, KeyError):
            continue

    return results


def _parse_rg_json_line(line: str) -> dict[str, Any] | None:
    """Parse a single ripgrep JSON output line."""
    import json

    data = json.loads(line)
    if data.get("type") != "match":
        return None

    d = data.get("data", {})
    path = d.get("path", {}).get("text", "")
    line_number = d.get("line_number", 0)
    lines_text = d.get("lines", {}).get("text", "")

    # Make path relative to repo
    if _repo_path:
        try:
            path = str(Path(path).relative_to(_repo_path)).replace("\\", "/")
        except ValueError:
            pass

    # Extract the matched portion
    submatches = d.get("submatches", [])
    match_text = submatches[0]["match"]["text"] if submatches else ""

    return {
        "file": path,
        "line": line_number,
        "match": match_text,
        "context": lines_text.rstrip(),
    }


def _search_with_re(
    pattern: str,
    file_type: str | None,
    max_results: int,
) -> list[dict[str, Any]]:
    """Fallback search using Python regex when ripgrep is unavailable."""
    if _repo_path is None:
        return []

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error:
        return []

    # Map file type to extensions
    type_to_ext: dict[str, list[str]] = {
        "python": [".py"],
        "js": [".js", ".jsx", ".mjs"],
        "ts": [".ts", ".tsx"],
        "go": [".go"],
        "rust": [".rs"],
        "java": [".java"],
        "ruby": [".rb"],
    }
    allowed_ext: set[str] | None = None
    if file_type:
        allowed_ext = set(type_to_ext.get(file_type, []))

    results: list[dict[str, Any]] = []

    for root, dirs, filenames in os.walk(_repo_path):
        # Filter excluded directories in-place to avoid walking into
        # .git, node_modules, __pycache__, etc.
        dirs[:] = [
            d for d in sorted(dirs) if d not in EXCLUDE_DIRS and not d.startswith(".")
        ]

        for name in filenames:
            if len(results) >= max_results:
                break
            filepath = Path(root) / name

            if allowed_ext and filepath.suffix.lower() not in allowed_ext:
                continue

            try:
                text = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            rel_path = str(filepath.relative_to(_repo_path)).replace("\\", "/")

            for i, source_line in enumerate(text.splitlines(), 1):
                if len(results) >= max_results:
                    break
                m = regex.search(source_line)
                if m:
                    results.append(
                        {
                            "file": rel_path,
                            "line": i,
                            "match": m.group(0),
                            "context": source_line.rstrip(),
                        }
                    )

    return results


# ---------------------------------------------------------------------------
# Tool: read_file_summary
# ---------------------------------------------------------------------------


def read_file_summary(file_path: str, max_lines: int = 80) -> dict[str, Any]:
    """Read file header + all function/class signatures, skipping bodies.

    Uses tree-sitter for supported languages, falls back to regex.

    Args:
        file_path: Path relative to repo root.
        max_lines: Maximum lines in output (capped at 200).

    Returns:
        Dict with keys: file, language, docstring, imports, signatures, total_lines.
    """
    max_lines = min(max(1, max_lines), 200)

    resolved = _resolve_and_validate(file_path)
    if not resolved.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    ext = resolved.suffix.lower()
    language = EXTENSION_MAP.get(ext, "unknown")

    try:
        source_text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
        raise FileNotFoundError(f"Cannot read file: {file_path}")

    total_lines = len(source_text.splitlines())

    # Try tree-sitter for supported languages
    if language != "unknown":
        result = _read_summary_treesitter(resolved, source_text, language, max_lines)
        if result is not None:
            result["file"] = file_path.replace("\\", "/")
            result["language"] = language
            result["total_lines"] = total_lines
            return result

    # Fallback: regex-based summary
    return _read_summary_regex(file_path, source_text, language, max_lines, total_lines)


def _read_summary_treesitter(
    filepath: Path,
    source_text: str,
    language: str,
    max_lines: int,
) -> dict[str, Any] | None:
    """Extract file summary using tree-sitter AST."""
    tree, source_bytes = _get_or_parse(filepath)
    if tree is None or source_bytes is None:
        return None

    root = tree.root_node
    imports: list[str] = []
    signatures: list[dict[str, Any]] = []
    docstring: str | None = None

    if language == "python":
        docstring, imports, signatures = _extract_python_summary(
            root, source_bytes, max_lines
        )

    # Estimate output size
    result = {
        "docstring": docstring,
        "imports": imports[:20],  # Cap imports
        "signatures": signatures[:max_lines],
    }
    return result


def _extract_python_summary(
    root: Any,
    source: bytes,
    max_lines: int,
) -> tuple[str | None, list[str], list[dict[str, Any]]]:
    """Extract docstring, imports, and signatures from a Python AST."""
    docstring = None
    imports: list[str] = []
    signatures: list[dict[str, Any]] = []

    for child in root.children:
        node_type = child.type

        # Module docstring (first expression_statement containing a string)
        if node_type == "expression_statement" and docstring is None:
            for sub in child.children:
                if sub.type == "string":
                    text = source[sub.start_byte : sub.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    # Strip quotes
                    if len(text) > 6:
                        text = text.strip("\"' \n")
                    if len(text) > 2:
                        docstring = text[:500]
                    break

        elif node_type == "import_statement":
            line = source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
            imports.append(line.strip())

        elif node_type == "import_from_statement":
            line = source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
            imports.append(line.strip())

        elif node_type in ("function_definition", "class_definition"):
            if len(signatures) >= max_lines:
                break

            start_line = child.start_point[0] + 1
            sig_line = _read_bytes_line(source, child.start_point[0])

            # Include decorators
            sig_text = sig_line.strip()
            if (
                child.prev_named_sibling
                and child.prev_named_sibling.type == "decorator"
            ):
                dec_line = _read_bytes_line(
                    source, child.prev_named_sibling.start_point[0]
                )
                sig_text = f"{dec_line.strip()}\n{sig_text}"

            signatures.append(
                {
                    "name": _extract_def_name(child, source),
                    "kind": "class" if node_type == "class_definition" else "function",
                    "line": start_line,
                    "signature": sig_text[:200],
                    "end_line": child.end_point[0] + 1,
                }
            )

        elif node_type == "decorated_definition":
            # Get the actual definition inside
            for sub in child.children:
                if sub.type in ("function_definition", "class_definition"):
                    if len(signatures) >= max_lines:
                        break

                    start_line = child.start_point[0] + 1
                    # Decorator + first line of definition
                    dec_line = _read_bytes_line(source, child.start_point[0])
                    def_line = _read_bytes_line(source, sub.start_point[0])
                    sig_text = f"{dec_line.strip()}\n{def_line.strip()}"

                    signatures.append(
                        {
                            "name": _extract_def_name(sub, source),
                            "kind": (
                                "class"
                                if sub.type == "class_definition"
                                else "function"
                            ),
                            "line": start_line,
                            "signature": sig_text[:200],
                            "end_line": sub.end_point[0] + 1,
                        }
                    )

    return docstring, imports, signatures


def _extract_def_name(node: Any, source: bytes) -> str:
    """Extract the name from a function/class definition node."""
    for child in node.children:
        if child.type == "identifier":
            return source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return ""


def _read_bytes_line(source: bytes, line_index: int) -> str:
    """Read a specific line from source bytes by line index (0-based)."""
    lines = source.split(b"\n")
    if 0 <= line_index < len(lines):
        return lines[line_index].decode("utf-8", errors="replace")
    return ""


def _read_summary_regex(
    file_path: str,
    source_text: str,
    language: str,
    max_lines: int,
    total_lines: int,
) -> dict[str, Any]:
    """Fallback: extract summary using regex patterns."""
    lines = source_text.splitlines()

    # Detect language patterns
    patterns = _SUMMARY_REGEX_PATTERNS.get(
        language, _SUMMARY_REGEX_PATTERNS["_generic"]
    )

    # Extract docstring from first few lines
    docstring = None
    for line in lines[:5]:
        stripped = line.strip()
        if stripped.startswith('"""') or stripped.startswith("'''"):
            quote = stripped[:3]
            rest = stripped[3:]
            if rest.endswith(quote) and len(rest) > 3:
                docstring = rest[:-3].strip()
            else:
                docstring = rest.strip()
            if docstring:
                break

    # Extract signatures
    signatures: list[dict[str, Any]] = []
    for i, line in enumerate(lines, 1):
        if len(signatures) >= max_lines:
            break
        for kind, pattern in patterns:
            m = re.match(pattern, line)
            if m:
                signatures.append(
                    {
                        "name": m.group(1),
                        "kind": kind,
                        "line": i,
                        "signature": line.strip()[:200],
                        "end_line": i,
                    }
                )
                break

    # Extract imports (first 30 lines)
    imports: list[str] = []
    for line in lines[:30]:
        stripped = line.strip()
        if stripped.startswith(("import ", "from ", "require(", "use ")):
            imports.append(stripped)

    return {
        "file": file_path.replace("\\", "/"),
        "language": language,
        "docstring": docstring,
        "imports": imports[:20],
        "signatures": signatures,
        "total_lines": total_lines,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_source_line(filepath: str, line_number: int) -> str:
    """Read a single source line from the repo.

    Args:
        filepath: Relative path within the repo.
        line_number: 1-based line number.

    Returns:
        The source line text, stripped, or empty string.
    """
    if _repo_path is None:
        return ""

    try:
        full_path = _repo_path / filepath
        text = full_path.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if 1 <= line_number <= len(lines):
            return lines[line_number - 1].strip()
    except OSError:
        pass
    return ""
