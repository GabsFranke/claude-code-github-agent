"""Aider-style repomap for structural codebase understanding.

Generates a compact "table of contents" of a codebase using tree-sitter
parsing and PageRank ranking. Shows definitions, references, and their
relationships so agents start every job with structural awareness.

Falls back through three tiers:
  Tier 1: Full tree-sitter parsing with PageRank
  Tier 2: Regex-based tag extraction
  Tier 3: File-tree only (handled by caller)
"""

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .file_tree import EXCLUDE_DIRS, EXCLUDE_FILES, EXCLUDE_SUFFIXES, _load_ignore_spec

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tree-sitter setup (lazy-loaded)
# ---------------------------------------------------------------------------

_ts_languages: dict | None = None
_ts_parser_cache: dict[str, object] = {}


def _get_ts_languages() -> dict | None:
    """Lazily load tree-sitter language bundle."""
    global _ts_languages
    if _ts_languages is None:
        try:
            import tree_sitter_languages  # type: ignore[import-untyped]

            _ts_languages = tree_sitter_languages
        except ImportError:
            logger.warning("tree_sitter_languages not available, falling back to regex")
            _ts_languages = {}
    return _ts_languages


# Map file extensions to tree-sitter language names
LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    # Future:
    # ".ts": "typescript",
    # ".tsx": "tsx",
    # ".go": "go",
    # ".js": "javascript",
    # ".rs": "rust",
}

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class Tag:
    """A definition or reference extracted from source code."""

    filepath: str
    name: str
    kind: str  # "definition" | "reference"
    line: int
    category: str  # "function", "class", "method", "variable", "import", etc.
    end_line: int = 0  # For definitions, the last line


@dataclass
class _RankedTag:
    """Tag with its PageRank score for sorting."""

    tag: Tag
    score: float = 0.0


# ---------------------------------------------------------------------------
# Python tree-sitter queries
# ---------------------------------------------------------------------------

# S-expression queries for extracting definitions from Python AST.
# These capture the most important structural elements.
_PYTHON_DEFINITION_QUERIES = [
    # Class definitions
    ("class", "(class_definition name: (identifier) @name) @node"),
    # Function definitions
    ("function", "(function_definition name: (identifier) @name) @node"),
    # Top-level assignments (variables, constants)
    ("variable", "(assignment left: (identifier) @name) @node"),
    # Decorated definitions
    (
        "decorator",
        "(decorated_definition definition: (class_definition name: (identifier) @name)) @node",
    ),
    (
        "decorator",
        "(decorated_definition definition: (function_definition name: (identifier) @name)) @node",
    ),
]

_PYTHON_REFERENCE_QUERIES = [
    # All identifier references
    ("identifier", "(identifier) @name"),
]

# ---------------------------------------------------------------------------
# Regex fallback patterns (Tier 2)
# ---------------------------------------------------------------------------

_REGEX_PATTERNS: dict[str, list[tuple[str, str]]] = {
    "python": [
        ("class", r"^(\s*)class\s+(\w+)"),
        ("function", r"^(\s*)def\s+(\w+)"),
        ("variable", r"^(\w+)\s*=\s*"),
    ],
    # Generic patterns applied to all languages when no specific match
    "_generic": [
        ("function", r"^\s*(?:function|func|fn|def|sub)\s+(\w+)"),
        ("class", r"^\s*(?:class|struct|interface|type|enum)\s+(\w+)"),
        ("variable", r"^\s*(?:const|let|var|export\s+const|export\s+let)\s+(\w+)"),
    ],
}

# ---------------------------------------------------------------------------
# Token counting (lightweight, no external deps required)
# ---------------------------------------------------------------------------

_tiktoken: object | None = None


def _token_count(text: str) -> int:
    """Estimate token count. Uses tiktoken if available, else word-based heuristic."""
    global _tiktoken
    if _tiktoken is None:
        try:
            import tiktoken  # type: ignore[import-untyped]

            _tiktoken = tiktoken.encoding_for_model("claude-sonnet-4-20250514")
        except Exception:
            _tiktoken = False  # type: ignore[assignment]

    if _tiktoken:
        return len(_tiktoken.encode(text))  # type: ignore[union-attr,attr-defined]

    # Heuristic: ~1.3 tokens per word for code
    return max(1, int(len(text.split()) * 1.3))


# ---------------------------------------------------------------------------
# Core RepoMap class
# ---------------------------------------------------------------------------


class RepoMap:
    """Generate a compact structural map of a codebase."""

    def __init__(self, repo_path: Path):
        self.repo_path = Path(repo_path)
        self._tags: list[Tag] = []
        self._ignore_spec = _load_ignore_spec(self.repo_path)

    async def get_repo_map(
        self,
        mentioned_files: list[str] | None = None,
        mentioned_idents: list[str] | None = None,
        token_budget: int = 4096,
        include_test_files: bool = True,
    ) -> str:
        """Generate a ranked repomap within the token budget.

        Args:
            mentioned_files: Files to bias PageRank toward.
            mentioned_idents: Identifiers to bias PageRank toward.
            token_budget: Maximum tokens for the output.
            include_test_files: Whether to boost test files in ranking (default True).

        Returns:
            Compact text representation of codebase structure.
        """
        mentioned_files = mentioned_files or []
        mentioned_idents = mentioned_idents or []

        # Extract tags from all source files
        self._tags = self._extract_all_tags()
        if not self._tags:
            return ""

        # Build reference graph and rank
        ranked = self._rank_tags(
            self._tags,
            mentioned_files=mentioned_files,
            mentioned_idents=mentioned_idents,
            include_test_files=include_test_files,
        )

        # Render within budget
        return self._render_map(ranked, token_budget)

    # ------------------------------------------------------------------
    # Tag extraction
    # ------------------------------------------------------------------

    def _extract_all_tags(self) -> list[Tag]:
        """Extract tags from all source files in the repo."""
        tags: list[Tag] = []

        for filepath in self._iter_source_files():
            file_tags = self._get_tags(filepath)
            tags.extend(file_tags)

        logger.debug(f"Extracted {len(tags)} tags from {self.repo_path}")
        return tags

    def _iter_source_files(self) -> list[Path]:
        """Walk the repo yielding source files, respecting exclusions and ignore files."""
        files: list[Path] = []

        for root, dirs, filenames in os.walk(self.repo_path):
            rel_root = str(Path(root).relative_to(self.repo_path)).replace("\\", "/")
            if rel_root == ".":
                rel_root = ""

            # Filter excluded and ignored directories in-place
            filtered_dirs: list[str] = []
            for d in sorted(dirs):
                if d in EXCLUDE_DIRS or d.startswith("."):
                    continue
                rel = f"{rel_root}/{d}" if rel_root else d
                if self._ignore_spec and (
                    self._ignore_spec.match_file(rel + "/")
                    or self._ignore_spec.match_file(rel)
                ):
                    continue
                filtered_dirs.append(d)
            dirs[:] = filtered_dirs

            for name in sorted(filenames):
                if self._should_skip_file(name):
                    continue
                rel = f"{rel_root}/{name}" if rel_root else name
                if self._ignore_spec and self._ignore_spec.match_file(rel):
                    continue
                files.append(Path(root) / name)

        return files

    @staticmethod
    def _should_skip_file(name: str) -> bool:
        if name in EXCLUDE_FILES:
            return True
        if any(name.endswith(s) for s in EXCLUDE_SUFFIXES):
            return True
        # Skip very large files and binary-ish names
        if any(pat in name.lower() for pat in (".generated.", ".min.", ".bundle.")):
            return True
        return False

    def _get_tags(self, filepath: Path) -> list[Tag]:
        """Extract tags from a single file. Tries tree-sitter, falls back to regex."""
        ext = filepath.suffix.lower()
        rel = str(filepath.relative_to(self.repo_path)).replace("\\", "/")

        if ext not in LANGUAGE_MAP:
            # Try regex fallback for any text file
            return self._get_tags_regex(filepath, rel, "_generic")

        # Try tree-sitter first
        ts_lang = LANGUAGE_MAP[ext]
        tags = self._get_tags_treesitter(filepath, rel, ts_lang)
        if tags is not None:
            return tags

        # Fallback to regex for the specific language
        lang_patterns = _REGEX_PATTERNS.get(ts_lang)
        if lang_patterns:
            return self._get_tags_regex(filepath, rel, ts_lang)

        # Generic regex fallback
        return self._get_tags_regex(filepath, rel, "_generic")

    def _get_tags_treesitter(
        self, filepath: Path, rel_path: str, language: str
    ) -> list[Tag] | None:
        """Extract tags using tree-sitter. Returns None on failure."""
        try:
            ts_langs = _get_ts_languages()
            if not ts_langs:
                return None

            lang = ts_langs.get_language(language)  # type: ignore[attr-defined]
            if lang is None:
                return None

            source = filepath.read_bytes()

            # tree-sitter >= 0.22 API
            try:
                from tree_sitter import Parser  # type: ignore[import-untyped]

                parser = Parser(lang)
            except (ImportError, TypeError):
                # Older API
                import tree_sitter as ts  # type: ignore[import-untyped]

                parser = ts.Parser()
                try:
                    parser.set_language(lang)  # type: ignore[attr-defined]
                except Exception:
                    return None

            tree = parser.parse(source)  # type: ignore[unreachable]
            if tree is None:  # type: ignore[unreachable]
                return None  # type: ignore[unreachable]

            root = tree.root_node
            tags: list[Tag] = []

            # Extract definitions
            for category, query_str in _PYTHON_DEFINITION_QUERIES:
                if language != "python":
                    continue
                try:
                    matches = self._run_ts_query(root, query_str, source)
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

            # Extract references (identifiers)
            if language == "python":
                for category, query_str in _PYTHON_REFERENCE_QUERIES:
                    try:
                        matches = self._run_ts_query(root, query_str, source)
                        for match in matches:
                            name = match.get("name")
                            line = match.get("line")
                            if name and line is not None:
                                # Skip if already captured as a definition on same line
                                if any(
                                    t.name == name
                                    and t.line == line
                                    and t.kind == "definition"
                                    for t in tags
                                ):
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
            logger.debug(f"Tree-sitter failed for {rel_path}: {e}")
            return None

    @staticmethod
    def _run_ts_query(root_node, query_str: str, source: bytes) -> list[dict]:
        """Execute a tree-sitter query and return matched captures.

        Returns list of dicts with keys:
          - 'name': str value of the @name capture
          - 'node' | 'line': tuple (start_line, end_line) for @node, or int line for simple captures
        """
        # We need the Language object to create the query
        results: list[dict] = []
        try:
            # tree-sitter >= 0.22 API
            from tree_sitter import Query  # type: ignore[import-untyped]

            # Build query against the root's language
            lang = root_node.language if hasattr(root_node, "language") else None
            if lang is None:
                return results

            q = Query(lang, query_str)

            for match in q.matches(root_node):  # type: ignore[attr-defined]
                capture_dict: dict[str, list] = {}
                for node, name_list in match[
                    1
                ]:  # match[1] is list of (node, [capture_names])
                    for cap_name in name_list:
                        capture_dict.setdefault(cap_name, []).append(node)

                for name_node in capture_dict.get("name", []):
                    text = source[name_node.start_byte : name_node.end_byte].decode(
                        "utf-8", errors="replace"
                    )
                    entry: dict = {"name": text}

                    if "node" in capture_dict:
                        def_node = capture_dict["node"][0]
                        entry["node"] = (
                            def_node.start_point[0] + 1,
                            def_node.end_point[0] + 1,
                        )
                    else:
                        entry["line"] = name_node.start_point[0] + 1

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
                m = re.match(pattern, line)
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

    # ------------------------------------------------------------------
    # Ranking via PageRank (or simple reference counting)
    # ------------------------------------------------------------------

    def _rank_tags(
        self,
        tags: list[Tag],
        mentioned_files: list[str],
        mentioned_idents: list[str],
        include_test_files: bool = True,
    ) -> list[_RankedTag]:
        """Rank tags by importance using reference graph analysis."""
        definitions = [t for t in tags if t.kind == "definition"]
        if not definitions:
            return []

        # Build symbol index: name -> list of definition tags
        def_index: dict[str, list[Tag]] = {}
        for t in definitions:
            def_index.setdefault(t.name, []).append(t)

        # Count references per definition name
        ref_counts: dict[str, int] = {}
        for t in tags:
            if t.kind == "reference" and t.name in def_index:
                ref_counts[t.name] = ref_counts.get(t.name, 0) + 1

        # Try PageRank via networkx
        ranked_defs = self._pagerank_rank(
            definitions,
            ref_counts,
            mentioned_files,
            mentioned_idents,
            include_test_files,
        )

        if ranked_defs is None:
            # Fallback to simple scoring
            ranked_defs = self._simple_rank(
                definitions,
                ref_counts,
                mentioned_files,
                mentioned_idents,
                include_test_files,
            )

        return ranked_defs

    def _pagerank_rank(
        self,
        definitions: list[Tag],
        ref_counts: dict[str, int],
        mentioned_files: list[str],
        mentioned_idents: list[str],
        include_test_files: bool = True,
    ) -> list[_RankedTag] | None:
        """Rank definitions using PageRank on reference graph."""
        try:
            import networkx as nx  # type: ignore[import-untyped]
        except ImportError:
            return None

        G = nx.DiGraph()

        # Add nodes for each definition (unique by filepath:name:line)
        def_keys: dict[str, Tag] = {}
        for d in definitions:
            key = f"{d.filepath}:{d.name}:{d.line}"
            def_keys[key] = d
            G.add_node(key)

        # Build edges: for each reference, link to definitions of same name
        # Weight by identifier specificity (longer names = more specific)
        for d in definitions:
            src_key = f"{d.filepath}:{d.name}:{d.line}"
            # This definition refers to names in the same file
            # (simplified: cross-file refs via shared names)
            for other_d in definitions:
                if other_d.filepath == d.filepath and other_d.name == d.name:
                    continue
                if other_d.name in ref_counts:
                    dst_key = f"{other_d.filepath}:{other_d.name}:{other_d.line}"
                    # Weight: longer/more specific names get higher weight
                    weight = 1.0
                    if len(other_d.name) > 8:
                        weight = 2.0
                    if len(other_d.name) > 16:
                        weight = 4.0
                    # Penalize very common names
                    common = {
                        "get",
                        "set",
                        "run",
                        "do",
                        "main",
                        "init",
                        "setup",
                        "handle",
                    }
                    if other_d.name.lower() in common:
                        weight *= 0.1
                    G.add_edge(src_key, dst_key, weight=weight)

        if G.number_of_nodes() == 0:
            return None

        # Build personalization vector
        personalization: dict[str, float] = {}
        for key, d in def_keys.items():
            score = 1.0
            if d.filepath in mentioned_files:
                score += 10.0
            if d.name in mentioned_idents:
                score += 5.0
            # Boost by reference count
            score += ref_counts.get(d.name, 0) * 0.5
            # Test file handling
            if "test" in d.filepath.lower():
                if include_test_files:
                    score += 2.0  # Boost test files for review workflows
                else:
                    score *= 0.7  # Penalize for non-review workflows
            personalization[key] = score

        # Normalize personalization
        total = sum(personalization.values())
        if total > 0:
            personalization = {k: v / total for k, v in personalization.items()}

        try:
            # PageRank with timeout protection
            scores = nx.pagerank(
                G, personalization=personalization, max_iter=50, tol=1e-4
            )
        except Exception as e:
            logger.debug(f"PageRank failed: {e}")
            return None

        # Build ranked list
        ranked = []
        for key, score in scores.items():
            if key in def_keys:
                ranked.append(_RankedTag(tag=def_keys[key], score=score))

        ranked.sort(key=lambda r: -r.score)
        return ranked

    @staticmethod
    def _simple_rank(
        definitions: list[Tag],
        ref_counts: dict[str, int],
        mentioned_files: list[str],
        mentioned_idents: list[str],
        include_test_files: bool = True,
    ) -> list[_RankedTag]:
        """Simple scoring when PageRank is unavailable."""
        scored: list[_RankedTag] = []

        for d in definitions:
            score = 1.0

            # Referenced by other code
            score += ref_counts.get(d.name, 0) * 2.0

            # Personalization
            if d.filepath in mentioned_files:
                score += 10.0
            if d.name in mentioned_idents:
                score += 5.0

            # Category weighting
            if d.category == "class":
                score += 3.0
            elif d.category == "function":
                score += 2.0
            elif d.category == "method":
                score += 1.5

            # Test file handling
            if "test" in d.filepath.lower():
                if include_test_files:
                    score += 2.0  # Boost test files for review workflows
                else:
                    score *= 0.7  # Penalize for non-review workflows

            scored.append(_RankedTag(tag=d, score=score))

        scored.sort(key=lambda r: -r.score)
        return scored

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def _render_map(self, ranked_tags: list[_RankedTag], token_budget: int) -> str:
        """Render ranked tags into a compact text within token budget."""
        if not ranked_tags:
            return ""

        # Group by file
        file_entries: dict[str, list[str]] = {}
        for rt in ranked_tags:
            fp = rt.tag.filepath
            if fp not in file_entries:
                file_entries[fp] = []

            tag = rt.tag
            if tag.kind == "definition":
                if tag.end_line and tag.end_line > tag.line:
                    entry = f"{tag.line}-{tag.end_line}│ {tag.category} {tag.name}"
                else:
                    entry = f"{tag.line}│ {tag.category} {tag.name}"
            else:
                entry = f"{tag.line}│ {tag.name}"

            if entry not in file_entries[fp]:
                file_entries[fp].append(entry)

        # Render files in order of highest-scoring tag
        file_max_score: dict[str, float] = {}
        for rt in ranked_tags:
            fp = rt.tag.filepath
            if fp not in file_max_score or rt.score > file_max_score[fp]:
                file_max_score[fp] = rt.score

        sorted_files = sorted(
            file_entries.keys(), key=lambda f: -file_max_score.get(f, 0)
        )

        # Build output within budget
        lines: list[str] = []
        budget_remaining = token_budget

        for filepath in sorted_files:
            entries = file_entries[filepath]
            block = f"{filepath}:"
            for entry in entries:
                block += f"\n  {entry}"

            block_tokens = _token_count(block)
            if budget_remaining <= 0:
                break
            if block_tokens > budget_remaining:
                # Try to fit some entries
                partial = f"{filepath}:"
                for entry in entries:
                    candidate = partial + f"\n  {entry}"
                    if _token_count(candidate) > budget_remaining:
                        break
                    partial = candidate
                if _token_count(partial) > _token_count(f"{filepath}:") + 5:
                    lines.append(partial)
                break

            lines.append(block)
            budget_remaining -= block_tokens

        if not lines:
            return ""

        return "\n".join(lines)
