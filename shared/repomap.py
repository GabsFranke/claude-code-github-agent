"""Tag extraction for structural codebase understanding.

Extracts definition and reference tags from source code using tree-sitter
parsing with regex fallback. Tags are used by the SymbolIndex in
shared.code_graph for on-demand code intelligence queries.
"""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .file_tree import load_ignore_spec, walk_source_files
from .ts_languages import (
    EXTENSION_MAP,
    LanguageConfig,
    get_language,
    get_language_config,
)

logger = logging.getLogger(__name__)

# Track which file extensions have already logged a tree-sitter warning
# to avoid spamming logs for every file of an unsupported language.
_warned_ts_failures: set[str] = set()


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


TagKind = Literal["definition", "reference"]


@dataclass
class Tag:
    """A definition or reference extracted from source code."""

    filepath: str
    name: str
    kind: TagKind
    line: int
    category: str  # "function", "class", "method", "variable", "import", etc.
    end_line: int = 0  # For definitions, the last line


# ---------------------------------------------------------------------------
# Regex fallback patterns (Tier 2)
# ---------------------------------------------------------------------------

_REGEX_PATTERNS: dict[str, list[tuple[str, re.Pattern]]] = {
    "python": [
        ("class", re.compile(r"^(\s*)class\s+(\w+)")),
        ("function", re.compile(r"^(\s*)def\s+(\w+)")),
        ("variable", re.compile(r"^(\w+)\s*=\s*")),
    ],
    "_generic": [
        ("function", re.compile(r"^\s*(?:function|func|fn|def|sub)\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:class|struct|interface|type|enum)\s+(\w+)")),
        (
            "variable",
            re.compile(r"^\s*(?:const|let|var|export\s+const|export\s+let)\s+(\w+)"),
        ),
    ],
}

# ---------------------------------------------------------------------------
# Core RepoMap class
# ---------------------------------------------------------------------------


class RepoMap:
    """Extract definition and reference tags from source code."""

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path)
        self._tags: list[Tag] = []
        self._ignore_spec = load_ignore_spec(self.repo_path)

    # ------------------------------------------------------------------
    # Tag extraction
    # ------------------------------------------------------------------

    def extract_tags(self) -> list[Tag]:
        """Extract tags from all source files in the repo.

        Public API for callers that need the raw tag list (e.g.,
        codebase_tools MCP server for find_definitions/find_references).
        """
        return self._extract_all_tags()

    def _extract_all_tags(self, priority_files: list[str] | None = None) -> list[Tag]:
        """Extract tags from source files, processing priority files first.

        Priority files (e.g., mentioned/changed files) are processed in full,
        then background files are processed.
        """
        priority_files = priority_files or []
        all_files = self._iter_source_files()

        # Split into priority and background files
        priority_paths: list[Path] = []
        background_paths: list[Path] = []
        for filepath in all_files:
            rel = str(filepath.relative_to(self.repo_path)).replace("\\", "/")
            if rel in priority_files:
                priority_paths.append(filepath)
            else:
                background_paths.append(filepath)

        tags: list[Tag] = []

        # Priority pass: process mentioned/changed files in full
        for filepath in priority_paths:
            file_tags = self._get_tags(filepath)
            tags.extend(file_tags)

        # Background pass: process remaining files
        for filepath in background_paths:
            file_tags = self._get_tags(filepath)
            tags.extend(file_tags)

        logger.debug(f"Extracted {len(tags)} tags from {self.repo_path}")
        return tags

    def _iter_source_files(self) -> list[Path]:
        """Walk the repo yielding source files, respecting exclusions and ignore files."""
        return list(walk_source_files(self.repo_path, self._ignore_spec))

    def _get_tags(self, filepath: Path) -> list[Tag]:
        """Extract tags from a single file. Tries tree-sitter, falls back to regex."""
        ext = filepath.suffix.lower()
        rel = str(filepath.relative_to(self.repo_path)).replace("\\", "/")

        lang_name = EXTENSION_MAP.get(ext)
        if lang_name:
            config = get_language_config(lang_name)
            if config:
                tags = self._get_tags_treesitter(filepath, rel, config)
                if tags is not None:
                    return tags

                # Fallback to regex for the specific language
                lang_patterns = _REGEX_PATTERNS.get(lang_name)
                if lang_patterns:
                    return self._get_tags_regex(filepath, rel, lang_name)

        # Generic regex fallback
        return self._get_tags_regex(filepath, rel, "_generic")

    def _get_tags_treesitter(
        self, filepath: Path, rel_path: str, config: LanguageConfig
    ) -> list[Tag] | None:
        """Extract tags using tree-sitter. Returns None on failure."""
        try:
            lang = get_language(config.name)
            if lang is None:
                return None

            source = filepath.read_bytes()

            from tree_sitter import Parser  # type: ignore[import-untyped]

            parser = Parser(lang)  # type: ignore[arg-type]

            tree = parser.parse(source)
            if tree is None:
                return None  # type: ignore[unreachable]

            root = tree.root_node
            tags: list[Tag] = []

            # Extract definitions using per-language queries from config
            for category, query_str in config.definition_queries:
                try:
                    matches = self._run_ts_query(lang, root, query_str, source)
                    for match in matches:
                        name_bytes = match.get("name")
                        node_bytes = match.get("node")
                        if name_bytes and node_bytes is not None:
                            name = name_bytes
                            start_line = node_bytes[0]
                            end_line = node_bytes[1]
                            tags.append(
                                Tag(
                                    filepath=rel_path,
                                    name=name,
                                    kind="definition",
                                    line=start_line,
                                    category=category,
                                    end_line=end_line,
                                )
                            )
                except Exception as e:
                    logger.debug(f"Query failed for {rel_path}: {e}")
                    continue

            # Deduplicate definitions: overlapping queries (e.g., bare
            # function_declaration AND export_statement wrapping it) can
            # produce multiple Tags for the same symbol.  Keep the one
            # with the widest byte range per (name, category, filepath).
            deduped: dict[tuple[str, str, str], Tag] = {}
            for tag in tags:
                key = (tag.name, tag.category, tag.filepath)
                existing = deduped.get(key)
                if existing is None:
                    deduped[key] = tag
                else:
                    tag_span = tag.end_line - tag.line
                    existing_span = existing.end_line - existing.line
                    if tag_span > existing_span:
                        deduped[key] = tag
            tags = list(deduped.values())

            defn_set = {(t.name, t.line) for t in tags if t.kind == "definition"}

            # Extract references using per-language queries from config
            for category, query_str in config.reference_queries:
                try:
                    matches = self._run_ts_query(lang, root, query_str, source)
                    for match in matches:
                        name = match.get("name")
                        line = match.get("line")
                        if name and line is not None:
                            if (name, line) in defn_set:
                                continue
                            tags.append(
                                Tag(
                                    filepath=rel_path,
                                    name=name,
                                    kind="reference",
                                    line=line,
                                    category=category,
                                )
                            )
                except Exception as e:
                    logger.debug(f"Reference query failed for {rel_path}: {e}")

            return tags if tags else None

        except Exception as e:
            # Log first failure per extension at WARNING, subsequent at DEBUG
            ext = filepath.suffix.lower()
            if ext not in _warned_ts_failures:
                _warned_ts_failures.add(ext)
                logger.warning(
                    f"Tree-sitter parsing failed for {ext} file "
                    f"({rel_path}): {e}. Subsequent failures for {ext} "
                    f"files will be logged at DEBUG."
                )
            else:
                logger.debug(f"Tree-sitter failed for {rel_path}: {e}")
            return None

    @staticmethod
    def _run_ts_query(lang, root_node, query_str: str, source: bytes) -> list[dict]:
        """Execute a tree-sitter query and return matched captures.

        Args:
            lang: tree-sitter Language object (passed from caller).
            root_node: Root node of the parsed tree.
            query_str: Tree-sitter query string.
            source: Original source bytes.

        Returns list of dicts with keys:
          - 'name': str value of the @name capture
          - 'node' | 'line': tuple (start_line, end_line) for @node, or int line for simple captures
          - 'target': str value of the @target capture (optional, for relationship queries)
        """
        results: list[dict] = []
        try:
            from tree_sitter import Query, QueryCursor  # type: ignore[import-untyped]

            q = Query(lang, query_str)
            cursor = QueryCursor(q)

            # cursor.captures() returns dict[str, list[Node]]
            # e.g. {'name': [Node, ...], 'node': [Node, ...], 'target': [Node, ...]}
            capture_dict = cursor.captures(root_node)

            # Type guard: ensure we got a dict back
            if not capture_dict or not isinstance(capture_dict, dict):
                return results

            name_nodes = capture_dict.get("name", [])
            node_nodes = capture_dict.get("node", [])
            target_nodes = capture_dict.get("target", [])

            # Pair each @name capture with its corresponding @node by
            # checking byte-range containment: the name identifier is a
            # descendant of the definition node, so its byte range falls
            # inside the node's range.  Index-based pairing is unreliable
            # because tree-sitter may return captures in different orders.
            node_pool = list(node_nodes)  # copy so we can consume
            target_pool = list(target_nodes)  # copy so we can consume

            for name_node in name_nodes:
                text = source[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                entry: dict = {"name": text}

                # Find the @node whose byte range contains this @name
                matched_node = None
                for j, cand in enumerate(node_pool):
                    if (
                        cand.start_byte <= name_node.start_byte
                        and name_node.end_byte <= cand.end_byte
                    ):
                        matched_node = cand
                        node_pool.pop(j)
                        break

                if matched_node is not None:
                    entry["node"] = (
                        matched_node.start_point[0] + 1,
                        matched_node.end_point[0] + 1,
                    )
                else:
                    entry["line"] = name_node.start_point[0] + 1

                # Find the @target whose byte range is contained within
                # the same @node as this @name (or is the @name itself
                # for simple captures). For relationship queries, @target
                # identifies the symbol being called/imported/inherited.
                matched_target = None
                for k, tcand in enumerate(target_pool):
                    # @target must be inside the same @node as @name,
                    # or if there's no @node, inside the @name's byte range
                    if matched_node is not None:
                        if (
                            tcand.start_byte >= matched_node.start_byte
                            and tcand.end_byte <= matched_node.end_byte
                        ):
                            matched_target = tcand
                            target_pool.pop(k)
                            break
                    else:
                        if (
                            tcand.start_byte >= name_node.start_byte
                            and tcand.end_byte <= name_node.end_byte
                        ):
                            matched_target = tcand
                            target_pool.pop(k)
                            break

                if matched_target is not None:
                    entry["target"] = source[
                        matched_target.start_byte : matched_target.end_byte
                    ].decode("utf-8", errors="replace")

                results.append(entry)
        except Exception as e:
            logger.debug(f"Query execution failed: {e}")

        return results

    def _get_tags_regex(self, filepath: Path, rel_path: str, lang: str) -> list[Tag]:
        """Fallback: extract tags using regex patterns."""
        patterns = _REGEX_PATTERNS.get(lang, _REGEX_PATTERNS["_generic"])
        tags: list[Tag] = []

        try:
            text = filepath.read_text(encoding="utf-8", errors="replace")
        except (OSError, UnicodeDecodeError):
            return tags

        for category, pattern in patterns:
            for i, line in enumerate(text.splitlines(), 1):
                m = pattern.match(line)
                if m:
                    # Extract name from regex match
                    name = (
                        m.group(2) if m.lastindex and m.lastindex >= 2 else m.group(1)
                    )
                    if name and name.isidentifier():
                        tags.append(
                            Tag(
                                filepath=rel_path,
                                name=name,
                                kind="definition",
                                line=i,
                                category=category,
                            )
                        )

        return tags
