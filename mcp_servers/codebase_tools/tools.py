"""Codebase search tools for structured code exploration.

Provides find_definitions, find_references, search_codebase,
read_file_summary, get_context, get_impact, and get_file_overview tools
that agents can use for targeted lookups without burning tokens on raw
Bash/Grep exploration.

Uses SymbolIndex from shared.code_graph for relationship-aware queries
(call graph, import graph, inheritance).
"""

import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from shared.code_graph import SymbolIndex
from shared.file_tree import EXCLUDE_DIRS
from shared.surrealdb_client import (
    _raw_result_rows,
    get_surreal,
    init_surrealdb,
    is_initialized as _sdb_is_initialized,
)
from shared.ts_languages import EXTENSION_MAP, get_language

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state (initialized once by init_repo)
# ---------------------------------------------------------------------------

_repo_path: Path | None = None
_symbol_index: SymbolIndex | None = None
_ast_cache: dict[tuple[str, float], tuple[Any, bytes]] = {}
_AST_CACHE_MAX_SIZE = 500
_index_ready = threading.Event()

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
    global _repo_path, _symbol_index

    _repo_path = Path(repo_path).resolve()

    if not _repo_path.is_dir():
        raise ValueError(f"Repo path does not exist: {_repo_path}")

    # Initialize SurrealDB connection (idempotent)
    surrealdb_url = os.getenv("SURREALDB_URL", "ws://localhost:8000/rpc")
    init_surrealdb(surrealdb_url)

    # Build the symbol index (SurrealDB-backed, skips if already indexed)
    _symbol_index = SymbolIndex(repo_path=_repo_path)
    _symbol_index.build()
    _index_ready.set()
    logger.info("Symbol index ready for %s", _repo_path)


def is_ready() -> bool:
    """Check whether the symbol index has finished building."""
    return _index_ready.is_set()


def warmup_surrealdb() -> None:
    """Pre-load SurrealDB HNSW index pages into memory.

    Running a lightweight query forces SurrealDB to load index segments
    into the page cache, avoiding a cold-start scenario where the first
    k-NN search returns 0 rows or mutates internal structures during
    iteration.

    Safe to call before init — it is a no-op if SurrealDB is not
    connected.
    """
    try:
        if not _sdb_is_initialized():
            return
        db = get_surreal()
        db.query("SELECT count() FROM symbol LIMIT 1")
        logger.info("SurrealDB warmup complete")
    except Exception as e:
        logger.warning("SurrealDB warmup query failed: %s", e)


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


def find_definitions(symbol_name: str) -> dict[str, Any]:
    """Find where a symbol (class, function, method) is defined.

    Uses the cached tag index from SymbolIndex. Supports
    tree-sitter and regex-extracted tags.

    Args:
        symbol_name: Exact name of the symbol to find.

    Returns:
        Dict with keys: results (list of defs), partial (bool), total (int).
    """
    if _symbol_index is None:
        return {"results": [], "partial": False, "total": 0}

    MAX_DEFINITIONS = 50

    tags = _symbol_index.find_definitions(symbol_name)
    seen: set[tuple[str, int, str]] = set()
    results: list[dict[str, Any]] = []

    for tag in tags:
        key = (tag.filepath, tag.line, tag.category)
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
        if len(results) >= MAX_DEFINITIONS:
            break

    return {
        "results": results,
        "partial": len(results) < len(tags),
        "total": len(tags),
    }


# ---------------------------------------------------------------------------
# Tool: find_references
# ---------------------------------------------------------------------------


def find_references(symbol_name: str) -> list[dict[str, Any]]:
    """Find all references to a symbol across the codebase using text search.

    Uses ripgrep or Python regex for word-boundary matching across the codebase,
    excluding definition lines. Does NOT use graph-based reference tracking.

    Args:
        symbol_name: Exact name of the symbol.

    Returns:
        List of dicts with keys: file, line, context.
    """
    # Collect definition locations to exclude from results
    definition_lines: set[tuple[str, int]] = set()
    if _symbol_index is not None:
        for tag in _symbol_index.find_definitions(symbol_name):
            definition_lines.add((tag.filepath, tag.line))

    # Search for exact word-boundary matches of the symbol name
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
    search_type: str = "text",
    kind_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Search codebase with text, semantic, or hybrid search.

    Args:
        pattern: For text/hybrid: regex pattern. For semantic: natural language query.
        file_type: Optional file type filter (e.g., "python", "js", "ts").
        max_results: Maximum results to return (capped at 100).
        search_type: "text" (default), "semantic", or "hybrid".
        kind_filter: For semantic/hybrid: filter by symbol kind
            ("function", "class", "method", "variable").

    Returns:
        List of result dicts. Format varies by search_type:
        - text: {file, line, match, context}
        - semantic: {file, name, kind, line, end_line, content, score}
        - hybrid: Same as semantic/text with "source" field added.
    """
    max_results = min(max(1, max_results), 100)

    if search_type == "semantic":
        return _semantic_search(pattern, file_type, max_results, kind_filter)
    elif search_type == "hybrid":
        return _hybrid_search(pattern, file_type, max_results, kind_filter)
    else:
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


# File type aliases for language names (used in SurrealDB queries)
_FILE_TYPE_TO_LANGUAGE: dict[str, str] = {
    "python": "python",
    "js": "javascript",
    "ts": "typescript",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "ruby": "ruby",
    "c": "c",
    "cpp": "cpp",
}


def _semantic_search(
    query: str,
    file_type: str | None,
    max_results: int,
    kind_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Semantic search via Gemini embedding + SurrealDB HNSW vector index.

    Falls back to text search if Gemini or SurrealDB are unavailable.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        logger.warning("google-genai not installed. Falling back to text search.")
        return _fallback_text_search(query, file_type, max_results)

    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        logger.warning("GEMINI_API_KEY not set. Falling back to text search.")
        return _fallback_text_search(query, file_type, max_results)

    embedding_dim = int(os.getenv("EMBEDDING_DIMENSION", "1024"))

    try:
        client = genai.Client(api_key=api_key)
        response = client.models.embed_content(
            model="gemini-embedding-001",
            contents=[query],
            config=types.EmbedContentConfig(
                output_dimensionality=embedding_dim,
                task_type="RETRIEVAL_QUERY",
            ),
        )

        if not response.embeddings or not response.embeddings[0].values:
            logger.warning("Gemini returned empty embedding for query: %s", query[:100])
            return []

        query_embedding = response.embeddings[0].values

        if not _sdb_is_initialized():
            init_surrealdb()
        db = get_surreal()

        # Warm up SurrealDB — first query after startup may need to load
        # HNSW index pages into memory. Without this, the k-NN below can
        # return 0 rows even though the index has matching embeddings.
        try:
            db.query("SELECT count() FROM symbol LIMIT 1")
        except Exception:
            pass

        params: dict[str, object] = {
            "qvec": query_embedding,
        }

        conditions: list[str] = []

        if file_type:
            lang = _FILE_TYPE_TO_LANGUAGE.get(file_type)
            if lang:
                conditions.append("language = $lang")
                params["lang"] = lang

        if kind_filter:
            conditions.append("kind = $kind")
            params["kind"] = kind_filter

        kind_condition = ""
        if conditions:
            kind_condition = "AND " + " AND ".join(conditions)

        result = db.query(
            f"""SELECT name, kind, filepath, line, end_line, content,
                       vector::distance::knn() AS score
                FROM symbol
                WHERE embedding IS NOT NULL {kind_condition}
                AND embedding <|{max_results},128|> $qvec
                ORDER BY score
                LIMIT {max_results}""",
            params,
        )

        rows = _raw_result_rows(result)

        if not rows:
            logger.debug(
                "Semantic search returned 0 rows for query: %s (dim=%d)",
                query[:100],
                len(query_embedding),
            )

        output: list[dict[str, Any]] = []
        for row in rows:
            content = row.get("content", "")
            content_truncated = False
            if content and len(content) > 2000:
                content = content[:2000] + "\n... [truncated]"
                content_truncated = True
            output.append(
                {
                    "file": row.get("filepath", ""),
                    "name": row.get("name", ""),
                    "kind": row.get("kind", ""),
                    "line": row.get("line", 0),
                    "end_line": row.get("end_line", 0),
                    "content": content,
                    "score": row.get("score"),
                }
            )
            if content_truncated:
                output[-1]["content_truncated"] = True
            if len(output) >= max_results:
                break

        return output

    except Exception as e:
        logger.error("Semantic search failed: %s", e, exc_info=True)
        return _fallback_text_search(query, file_type, max_results)


def _hybrid_search(
    pattern: str,
    file_type: str | None,
    max_results: int,
    kind_filter: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid search combining text + semantic with deduplication.

    Runs both search modes independently and merges results. Semantic results
    are prioritized since they capture meaning, not just literal matches.
    Deduplicates by (file, line) key.
    """
    text_results = _fallback_text_search(pattern, file_type, max_results)
    semantic_results = _semantic_search(pattern, file_type, max_results, kind_filter)

    seen: set[tuple[str, int]] = set()
    merged: list[dict[str, Any]] = []

    # Add semantic results first (ranked by relevance)
    for r in semantic_results:
        key = (r["file"], r["line"])
        if key not in seen:
            seen.add(key)
            r["source"] = "semantic"
            merged.append(r)

    # Add non-overlapping text results
    for r in text_results:
        key = (r["file"], r["line"])
        if key not in seen:
            seen.add(key)
            r["source"] = "text"
            merged.append(r)

    return merged[:max_results]


def _fallback_text_search(
    pattern: str,
    file_type: str | None,
    max_results: int,
) -> list[dict[str, Any]]:
    """Text search fallback used when semantic search is unavailable."""
    rg_path = shutil.which("rg")
    if rg_path:
        return _search_with_rg(rg_path, pattern, file_type, max_results)
    return _search_with_re(pattern, file_type, max_results)


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
# Tool: get_context
# ---------------------------------------------------------------------------


def get_context(symbol_name: str, file_hint: str | None = None) -> dict[str, Any]:
    """Get a 360-degree view of a symbol: definition, callers, callees, inheritance.

    If the symbol name is ambiguous (multiple definitions), returns a
    disambiguation list so the agent can narrow down.

    Args:
        symbol_name: Exact name of the symbol.
        file_hint: Optional file path to disambiguate when the symbol exists
            in multiple files. Example: 'shared/repomap.py'.

    Returns:
        Dict with definition, scope, calls, called_by, inherits_from, inherited_by.
    """
    if _symbol_index is None:
        return {"symbol": symbol_name, "error": "Symbol index not initialized"}

    result = _symbol_index.get_context(symbol_name, file_hint=file_hint)

    # Cap unbounded call graph sets
    for field in ("calls", "called_by"):
        items = result.get(field)
        if isinstance(items, list) and len(items) > 100:
            result[field] = sorted(items)[:100]
            result[f"{field}_partial"] = True
            result[f"{field}_total"] = len(items)

    return result


# ---------------------------------------------------------------------------
# Tool: get_impact
# ---------------------------------------------------------------------------


def get_impact(
    file_path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    max_depth: int = 3,
    direction: str = "both",
) -> dict[str, Any]:
    """Get the blast radius of changes to a file or line range.

    Uses BFS graph traversal to find upstream (who depends on us) and
    downstream (what we depend on) impact through the call graph.

    Args:
        file_path: Path relative to repo root.
        start_line: Optional start line to narrow the impact range.
        end_line: Optional end line to narrow the impact range.
        max_depth: Maximum BFS traversal depth (1-10, default 3).
        direction: "upstream" (who depends on us), "downstream"
            (what we depend on), or "both" (default).

    Returns:
        Dict with symbols_in_range, upstream_impact, downstream_impact,
        imported_by, risk_level, risk_summary.
    """
    if _symbol_index is None:
        return {"file": file_path, "error": "Symbol index not initialized"}

    # Validate the file path
    _resolve_and_validate(file_path)

    result = _symbol_index.get_impact(
        file_path,
        start_line,
        end_line,
        max_depth=max_depth,
        direction=direction,
    )

    # Cap items per depth level in upstream/downstream impact
    for direction_key in ("upstream_impact", "downstream_impact"):
        impact = result.get(direction_key)
        if isinstance(impact, dict):
            for depth_key, items in impact.items():
                if isinstance(items, list) and len(items) > 50:
                    impact[depth_key] = items[:50]
                    impact[f"{depth_key}_partial"] = True
                    impact[f"{depth_key}_total"] = len(items)

    return result


# ---------------------------------------------------------------------------
# Tool: get_file_overview
# ---------------------------------------------------------------------------


def get_file_overview(file_path: str) -> dict[str, Any]:
    """Get all symbols, imports, and class structure for a file.

    Args:
        file_path: Path relative to repo root.

    Returns:
        Dict with definitions, imports, imported_by, classes.
    """
    if _symbol_index is None:
        return {"file": file_path, "error": "Symbol index not initialized"}

    # Validate the file path
    _resolve_and_validate(file_path)

    result = _symbol_index.get_file_overview(file_path)

    imported_by = result.get("imported_by")
    if isinstance(imported_by, list) and len(imported_by) > 50:
        result["imported_by"] = imported_by[:50]
        result["imported_by_partial"] = True
        result["imported_by_total"] = len(imported_by)

    return result


# ---------------------------------------------------------------------------
# Tool: detect_changes
# ---------------------------------------------------------------------------


def detect_changes(scope: str = "staged") -> dict[str, Any]:
    """Detect symbols affected by git changes and analyze impact.

    Parses git diff output to identify which symbols are affected by
    changes, then runs impact analysis on those symbols.

    Args:
        scope: "staged" (default) for staged changes or "unstaged" for
            working tree changes.

    Returns:
        Dict with changed_files, summary, risk_level.
    """
    if _symbol_index is None:
        return {"changed_files": [], "error": "Symbol index not initialized"}

    result = _symbol_index.detect_changes_from_diff(scope=scope)

    changed_files = result.get("changed_files")
    if isinstance(changed_files, list) and len(changed_files) > 30:
        result["changed_files"] = changed_files[:30]
        result["changed_files_partial"] = True
        result["changed_files_total"] = len(changed_files)

    return result


# ---------------------------------------------------------------------------
# Tool: trace_flow
# ---------------------------------------------------------------------------


def trace_flow(
    entry_point: str,
    file_hint: str | None = None,
    max_depth: int = 20,
) -> dict[str, Any]:
    """Trace the execution flow starting from a symbol.

    Uses BFS graph traversal through call edges to build an ordered
    flow of function/method calls, showing the full call chain.

    Args:
        entry_point: Name of the symbol to start tracing from.
            Example: 'handle_request', 'Application.run'.
        file_hint: Optional file path to disambiguate when the symbol
            exists in multiple files.
        max_depth: Maximum traversal depth (1-50, default 20).

    Returns:
        Dict with entry_point, entry_definition, steps, call_chain,
        total_steps, max_depth_reached.
    """
    if _symbol_index is None:
        return {"entry_point": entry_point, "error": "Symbol index not initialized"}

    result = _symbol_index.trace_flow(
        entry_point,
        file_hint=file_hint,
        max_depth=max_depth,
    )

    steps = result.get("steps")
    if isinstance(steps, list) and len(steps) > 200:
        result["steps"] = steps[:200]
        result["steps_partial"] = True
        result["steps_total"] = len(steps)

    return result


# ---------------------------------------------------------------------------
# Tool: get_routes_map
# ---------------------------------------------------------------------------


def get_routes_map(framework: str | None = None) -> list[dict[str, Any]]:
    """Get all API route definitions in the codebase.

    Extracts FastAPI, Flask, and Django route decorators from Python files
    and caches them in SurrealDB for subsequent queries.

    Args:
        framework: Optional filter. One of "fastapi", "flask", "django".

    Returns:
        List of route dicts with path, method, handler, filepath, line,
        framework, description.
    """
    from shared.route_maps import get_routes_map as _get_routes_map

    return _get_routes_map(repo_path=_repo_path, framework=framework)


# ---------------------------------------------------------------------------
# Tool: get_tools_map
# ---------------------------------------------------------------------------


def get_tools_map() -> list[dict[str, Any]]:
    """Get all MCP tool definitions in the codebase.

    Extracts tool names, descriptions, and parameters from MCP server
    JSON schema definitions and caches them in SurrealDB.

    Returns:
        List of tool dicts with name, description, server_file,
        server_name, required_params.
    """
    from shared.route_maps import get_tools_map as _get_tools_map

    return _get_tools_map(repo_path=_repo_path)


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
