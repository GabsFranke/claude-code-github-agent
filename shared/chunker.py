"""Tree-sitter-based semantic code chunker for embedding pipelines.

Splits source files into meaningful units (functions, classes, methods
with their docstrings) suitable for embedding. Supports 10+ languages
via the shared tree-sitter language registry in ts_languages.py.

Reuses exclusion logic from shared/file_tree.py.
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .file_tree import EXCLUDE_DIRS, EXCLUDE_FILES, EXCLUDE_SUFFIXES, _load_ignore_spec
from .ts_languages import (
    EXTENSION_MAP,
    LanguageConfig,
    get_language,
    get_language_config,
)

logger = logging.getLogger(__name__)

# Max lines per chunk before splitting at child boundaries
DEFAULT_MAX_LINES = 150

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Chunk:
    """A semantic unit of source code produced by the chunker."""

    filepath: str  # Relative path from repo root, forward slashes
    name: str  # Symbol name (function/class/method)
    kind: str  # "function", "class", "method", "module_docstring"
    language: str  # Tree-sitter language name or "unknown"
    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    content: str  # Full source text of the chunk
    parent: str | None = None  # Enclosing class name for methods


# ---------------------------------------------------------------------------
# Regex fallback patterns
# ---------------------------------------------------------------------------

_GENERIC_CHUNK_PATTERNS = [
    ("function", re.compile(r"^\s*(?:async\s+)?(?:def|function|func|fn|sub)\s+(\w+)")),
    ("class", re.compile(r"^\s*(?:class|struct|interface|type|enum)\s+(\w+)")),
]

_PYTHON_CHUNK_PATTERNS = [
    ("class", re.compile(r"^(\s*)class\s+(\w+)")),
    ("function", re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)")),
]

# Language-specific regex patterns keyed by language name
_REGEX_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    "python": _PYTHON_CHUNK_PATTERNS,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def chunk_file(filepath: Path, repo_root: Path) -> list[Chunk]:
    """Chunk a single source file into semantic units.

    Uses tree-sitter for languages with installed packages; falls back to
    regex-based splitting for other languages.

    Args:
        filepath: Absolute path to the source file.
        repo_root: Absolute path to the repo root (for relative paths).

    Returns:
        List of Chunk dataclass instances.
    """
    rel_path = str(filepath.relative_to(repo_root)).replace("\\", "/")
    ext = filepath.suffix.lower()

    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    if not source.strip():
        return []

    # Try tree-sitter for registered languages
    lang_name = EXTENSION_MAP.get(ext)
    if lang_name:
        config = get_language_config(lang_name)
        if config:
            chunks = _chunk_with_treesitter(filepath, source, rel_path, config)
            if chunks is not None:
                return chunks

    # Regex fallback
    patterns = _REGEX_PATTERNS.get(lang_name or "", _GENERIC_CHUNK_PATTERNS)
    return _chunk_with_regex(source, rel_path, lang_name or "unknown", patterns)


def chunk_repo(
    repo_path: Path,
    changed_files: list[str] | None = None,
) -> list[Chunk]:
    """Walk repo and chunk all source files.

    Args:
        repo_path: Absolute path to the git worktree.
        changed_files: Optional list of relative paths to re-chunk.
            If None, all source files are chunked (full build).

    Returns:
        List of Chunk instances.
    """
    repo_path = Path(repo_path)
    ignore_spec = _load_ignore_spec(repo_path)

    if changed_files is not None and len(changed_files) == 0:
        return []

    if changed_files:
        chunks: list[Chunk] = []
        for rel in changed_files:
            filepath = repo_path / rel
            if filepath.is_file():
                chunks.extend(chunk_file(filepath, repo_path))
        return chunks

    # Full scan
    chunks = []
    for root, dirs, filenames in os.walk(repo_path):
        rel_root = str(Path(root).relative_to(repo_path)).replace("\\", "/")
        if rel_root == ".":
            rel_root = ""

        # Filter excluded and ignored directories in-place
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
            if _should_skip(name):
                continue
            rel = f"{rel_root}/{name}" if rel_root else name
            if ignore_spec and ignore_spec.match_file(rel):
                continue
            filepath = Path(root) / name
            chunks.extend(chunk_file(filepath, repo_path))

    return chunks


# ---------------------------------------------------------------------------
# Tree-sitter chunking (generic, multi-language)
# ---------------------------------------------------------------------------


def _chunk_with_treesitter(
    filepath: Path,
    source: str,
    rel_path: str,
    config: LanguageConfig,
) -> list[Chunk] | None:
    """Chunk using tree-sitter AST with language-agnostic node type mappings.

    Returns None on failure (triggers regex fallback).
    """
    lang = get_language(config.name)
    if lang is None:
        return None

    try:
        from tree_sitter import Parser

        source_bytes = source.encode("utf-8")
        parser = Parser(lang)
        tree = parser.parse(source_bytes)
        if tree is None:
            return None  # type: ignore[unreachable]

        root = tree.root_node
        chunks = _extract_chunks(root, source_bytes, rel_path, config)
        return chunks if chunks else None
    except Exception as e:
        logger.debug(f"Tree-sitter chunking failed for {rel_path}: {e}")
        return None


def _extract_chunks(
    root: Any,
    source: bytes,
    rel_path: str,
    config: LanguageConfig,
) -> list[Chunk]:
    """Extract chunks from an AST using language-configured node type mappings.

    Walks top-level children of the root node. For each child:
    - If it's a decorator/wrapper: unwrap to find the actual definition
    - If it's a function-like node: extract as a function chunk
    - If it's a class-like node: extract as a class chunk (split if large)
    """
    chunks: list[Chunk] = []

    for child in root.children:
        if child.type in config.decorator_types:
            # Unwrap decorator to find actual definition
            for sub in child.children:
                if sub.type in config.function_types:
                    chunks.append(
                        _make_chunk(sub, source, rel_path, config.name, wrapper=child)
                    )
                elif sub.type in config.class_types:
                    chunks.extend(
                        _make_class_chunks(sub, source, rel_path, config, wrapper=child)
                    )
        elif child.type in config.function_types:
            chunks.append(_make_chunk(child, source, rel_path, config.name))
        elif child.type in config.class_types:
            chunks.extend(_make_class_chunks(child, source, rel_path, config))

    return chunks


def _make_class_chunks(
    node: Any,
    source: bytes,
    rel_path: str,
    config: LanguageConfig,
    wrapper: Any | None = None,
) -> list[Chunk]:
    """Extract a class chunk. If the class is large, split into methods."""
    start_node = wrapper if wrapper else node
    start_line = start_node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    span = end_line - start_line + 1

    class_name = _extract_name(node, source)
    content = _node_text(source, start_node if wrapper else node)

    chunks: list[Chunk] = []

    if span <= DEFAULT_MAX_LINES:
        # Small class — return as single chunk
        chunks.append(
            Chunk(
                filepath=rel_path,
                name=class_name,
                kind="class",
                language=config.name,
                start_line=start_line,
                end_line=end_line,
                content=content,
            )
        )
    else:
        # Large class — split into class header + methods
        lines = source.split(b"\n")
        header_end = _find_header_end(node, config.method_types)
        if header_end > start_line:
            header_text = b"\n".join(lines[start_line - 1 : header_end]).decode(
                "utf-8", errors="replace"
            )
            chunks.append(
                Chunk(
                    filepath=rel_path,
                    name=class_name,
                    kind="class",
                    language=config.name,
                    start_line=start_line,
                    end_line=header_end,
                    content=header_text,
                )
            )

        # Extract each method as its own chunk
        for child in node.children:
            if child.type in config.method_types:
                method_name = _extract_name(child, source)
                chunks.append(
                    Chunk(
                        filepath=rel_path,
                        name=method_name,
                        kind="method",
                        language=config.name,
                        start_line=child.start_point[0] + 1,
                        end_line=child.end_point[0] + 1,
                        content=_node_text(source, child),
                        parent=class_name,
                    )
                )

    return chunks


def _make_chunk(
    node: Any,
    source: bytes,
    rel_path: str,
    language: str,
    wrapper: Any | None = None,
) -> Chunk:
    """Extract a function/definition chunk."""
    start_node = wrapper if wrapper else node
    start_line = start_node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    func_name = _extract_name(node, source)
    content = _node_text(source, start_node if wrapper else node)

    return Chunk(
        filepath=rel_path,
        name=func_name,
        kind="function",
        language=language,
        start_line=start_line,
        end_line=end_line,
        content=content,
    )


def _find_header_end(node: Any, method_types: frozenset[str]) -> int:
    """Find the end of the class header (before the first method).

    Returns the line number after the last header element.
    """
    for child in node.children:
        if child.type in method_types:
            return int(child.start_point[0]) + 1

    # No methods — return midpoint as header
    mid = (int(node.start_point[0]) + int(node.end_point[0])) // 2
    return min(mid + 1, int(node.end_point[0]) + 1)


# ---------------------------------------------------------------------------
# Regex fallback chunking
# ---------------------------------------------------------------------------


def _chunk_with_regex(
    source: str,
    rel_path: str,
    language: str,
    patterns: list[tuple[str, re.Pattern]],
) -> list[Chunk]:
    """Fallback chunking using regex patterns + blank-line splitting."""
    lines = source.splitlines()
    if not lines:
        return []

    # Find definition start lines
    def_starts: list[tuple[int, str, str]] = []  # (line_idx, kind, name)
    for i, line in enumerate(lines):
        for kind, pattern in patterns:
            m = pattern.match(line)
            if m:
                name = m.group(m.lastindex) if m.lastindex else "unknown"
                def_starts.append((i, kind, name))
                break

    if not def_starts:
        # No definitions found — chunk the whole file as one unit
        return [
            Chunk(
                filepath=rel_path,
                name=Path(rel_path).stem,
                kind="module",
                language=language,
                start_line=1,
                end_line=len(lines),
                content=source,
            )
        ]

    chunks: list[Chunk] = []
    for idx, (start_idx, kind, name) in enumerate(def_starts):
        if idx + 1 < len(def_starts):
            end_idx = def_starts[idx + 1][0] - 1
        else:
            end_idx = len(lines) - 1

        # Trim trailing blank lines
        while end_idx > start_idx and not lines[end_idx].strip():
            end_idx -= 1

        content = "\n".join(lines[start_idx : end_idx + 1])
        chunks.append(
            Chunk(
                filepath=rel_path,
                name=name,
                kind=kind,
                language=language,
                start_line=start_idx + 1,
                end_line=end_idx + 1,
                content=content,
            )
        )

    return chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_skip(name: str) -> bool:
    """Check if a file should be skipped."""
    if name in EXCLUDE_FILES:
        return True
    # Skip .git file (worktrees create a .git file pointing to the bare repo)
    if name == ".git":
        return True
    if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
        return True
    if any(pat in name.lower() for pat in (".generated.", ".min.", ".bundle.")):
        return True
    return False


def _node_text(source: bytes, node: Any) -> str:
    """Extract text from a tree-sitter node."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _extract_name(node: Any, source: bytes) -> str:
    """Extract the name identifier from a definition node.

    Tries common name node types in order: identifier, property_identifier,
    type_identifier, field_identifier, constant.
    """
    for child in node.children:
        if child.type in (
            "identifier",
            "property_identifier",
            "type_identifier",
            "field_identifier",
            "constant",
        ):
            return source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return "unknown"
