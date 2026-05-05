"""Tree-sitter-based semantic code chunker for embedding pipelines.

Splits source files into meaningful units (functions, classes, methods
with their docstrings) suitable for embedding. Supports 10+ languages
via the shared tree-sitter language registry in ts_languages.py.

Reuses exclusion logic from shared/file_tree.py.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .file_tree import load_ignore_spec, walk_source_files
from .ts_languages import (
    EXTENSION_MAP,
    LanguageConfig,
    get_language,
    get_language_config,
)

logger = logging.getLogger(__name__)

# Max lines per chunk before splitting at child boundaries
DEFAULT_MAX_LINES = 150

# Max lines per function before splitting at nested block boundaries
MAX_FUNCTION_LINES = 200

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


ChunkKind = Literal["function", "class", "method", "module"]


@dataclass
class Chunk:
    """A semantic unit of source code produced by the chunker."""

    filepath: str  # Relative path from repo root, forward slashes
    name: str  # Symbol name (function/class/method)
    kind: ChunkKind  # "function", "class", "method", "module"
    language: str  # Tree-sitter language name or "unknown"
    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    content: str  # Full source text of the chunk
    parent: str | None = None  # Enclosing class name for methods

    @property
    def embed_text(self) -> str:
        """Content with structural context header for embedding.

        The header gives the embedding model awareness of where this chunk
        lives in the codebase structure, improving retrieval quality.
        Stored content in SurrealDB remains raw source (via self.content).
        """
        header = f"# {self.filepath}"
        if self.parent:
            header += f" | {self.kind}: {self.parent}.{self.name}"
        else:
            header += f" | {self.kind}: {self.name}"
        return f"{header}\n{self.content}"


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
    ignore_spec = load_ignore_spec(repo_path)

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
    for filepath in walk_source_files(repo_path, ignore_spec):
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
                    chunks.extend(
                        _make_function_chunks(
                            sub, source, rel_path, config.name, wrapper=child
                        )
                    )
                elif sub.type in config.class_types:
                    chunks.extend(
                        _make_class_chunks(sub, source, rel_path, config, wrapper=child)
                    )
        elif child.type in config.function_types:
            chunks.extend(_make_function_chunks(child, source, rel_path, config.name))
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


def _make_function_chunks(
    node: Any,
    source: bytes,
    rel_path: str,
    language: str,
    wrapper: Any | None = None,
) -> list[Chunk]:
    """Extract a function chunk, splitting oversized functions at block boundaries."""
    start_node = wrapper if wrapper else node
    start_line = start_node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    span = end_line - start_line + 1

    if span <= MAX_FUNCTION_LINES:
        return [_make_chunk(node, source, rel_path, language, wrapper=wrapper)]

    return _split_oversized_function(node, source, rel_path, language, wrapper=wrapper)


# Block types that represent meaningful split points across languages
_SPLIT_BLOCK_TYPES = frozenset(
    {
        "try_statement",
        "if_statement",
        "for_statement",
        "while_statement",
        "with_statement",
        "match_statement",
    }
)


def _split_oversized_function(
    node: Any,
    source: bytes,
    rel_path: str,
    language: str,
    wrapper: Any | None = None,
) -> list[Chunk]:
    """Split an oversized function at nested block boundaries.

    Looks for top-level block statements (try, if, for, while, with, match)
    within the function body and splits at those boundaries. Each part
    includes the function signature for context.

    Falls back to a single chunk if there aren't enough split points.
    """
    start_node = wrapper if wrapper else node
    start_line = start_node.start_point[0] + 1
    end_line = node.end_point[0] + 1

    func_name = _extract_name(node, source)

    # Find direct children that are block statements
    block_starts: list[tuple[int, int]] = []  # (start_line, end_line)
    for child in node.children:
        if child.type in _SPLIT_BLOCK_TYPES:
            block_starts.append((child.start_point[0] + 1, child.end_point[0] + 1))

    if len(block_starts) < 2:
        # Not enough structure to split meaningfully
        return [_make_chunk(node, source, rel_path, language, wrapper=wrapper)]

    # Find the function body start (first line after signature)
    # The first non-trivia child after the function name gives us the body
    body_start = start_line
    for child in node.children:
        if child.type in ("block", "function_body", "statement_block"):
            body_start = child.start_point[0] + 1
            break

    # Build split boundaries: [body_start, block1.start, block2.start, ..., end_line+1]
    boundaries = [body_start]
    for block_start, _ in block_starts:
        if block_start > boundaries[-1]:
            boundaries.append(block_start)
    boundaries.append(end_line + 1)

    lines = source.split(b"\n")

    # Include the function signature as a prefix for each part
    sig_lines = lines[start_line - 1 : body_start - 1]
    sig_text = b"\n".join(sig_lines).decode("utf-8", errors="replace") + "\n"

    chunks: list[Chunk] = []
    for i in range(len(boundaries) - 1):
        part_start = boundaries[i]
        part_end = boundaries[i + 1] - 1

        if part_start > part_end:
            continue

        # Each part includes the function signature for context
        part_text = b"\n".join(lines[part_start - 1 : part_end]).decode(
            "utf-8", errors="replace"
        )
        content = sig_text + part_text

        part_num = i + 1
        chunks.append(
            Chunk(
                filepath=rel_path,
                name=f"{func_name} (part {part_num})",
                kind="function",
                language=language,
                start_line=part_start,
                end_line=part_end,
                content=content,
            )
        )

    return (
        chunks
        if chunks
        else [_make_chunk(node, source, rel_path, language, wrapper=wrapper)]
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
                kind=kind,  # type: ignore[arg-type]
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
