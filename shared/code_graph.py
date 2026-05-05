"""Queryable symbol index for code intelligence.

Extracts relationships (calls, imports, inheritance) from tree-sitter ASTs
and stores everything in SurrealDB — a multi-model database that handles
graph edges, vector search, and full-text search. The SymbolIndex class
provides a thin query layer over SurrealDB, replacing the in-memory lookup
dicts and JSON file cache from the first phase.
"""

import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .file_tree import load_ignore_spec, walk_source_files
from .import_resolver import resolve_python_import, resolve_ts_import
from .repomap import RepoMap, Tag
from .surrealdb_client import (
    SCHEMA_VERSION,
    _raw_result_rows,
    apply_schema,
    get_surreal,
    is_initialized,
)
from .ts_languages import EXTENSION_MAP, get_language, get_language_config

logger = logging.getLogger(__name__)

RelationshipKind = Literal["calls", "imports", "inherits"]


@dataclass
class Relationship:
    """A relationship between two symbols in the codebase."""

    source_file: str
    source_line: int
    source_name: str
    target_name: str
    target_file: str | None = None
    kind: RelationshipKind = "calls"


@dataclass
class SymbolIndex:
    """Queryable index of definitions, references, and relationships.

    Backed by SurrealDB for persistence. Extracts tags and relationships
    via tree-sitter, stores them as documents and graph edges, and exposes
    the same query API the MCP tools expect.
    """

    repo_path: Path = field(default_factory=Path)
    _built: bool = field(default=False, repr=False)

    def build(
        self, pre_extracted_tags: list[Tag] | None = None, force: bool = False
    ) -> None:
        """Build the symbol index by extracting tags and relationships.

        Stores everything in SurrealDB. If the current commit has already
        been indexed, skips the extraction and loads from the database.

        Args:
            pre_extracted_tags: Optional pre-extracted tags to use instead
                of performing a fresh extraction.
            force: If True, rebuild even if the commit is already indexed.
        """
        if not is_initialized():
            logger.warning("SurrealDB not initialized, skipping build")
            return

        db = get_surreal()
        apply_schema(db)

        commit_hash = _get_head_commit(self.repo_path)

        # Skip if already indexed for this commit
        if not force and not pre_extracted_tags and _is_commit_indexed(db, commit_hash):
            logger.info(
                "Commit %s already indexed, reusing SurrealDB data", commit_hash
            )
            self._built = True
            return

        # Clear old data for this repo
        _clear_repo_data(db)

        # Extract tags
        rm = RepoMap(self.repo_path)
        if pre_extracted_tags is not None:
            tags = pre_extracted_tags
        else:
            tags = rm.extract_tags()

        if not tags:
            logger.warning("No tags extracted from %s", self.repo_path)
            self._built = True
            return

        # Upsert symbols
        _upsert_symbols(db, tags)
        logger.debug("Upserted %d symbols to SurrealDB", len(tags))

        # Extract and upsert relationships as graph edges
        relationships = self._extract_relationships()
        logger.debug(
            "Extracted %d relationships, upserting to SurrealDB", len(relationships)
        )
        _upsert_relationships(db, relationships)

        # Mark commit as indexed
        _mark_commit_indexed(db, commit_hash)
        self._built = True
        logger.info(
            "SymbolIndex built: %d symbols, %d relationships, commit=%s",
            len(tags),
            len(relationships),
            commit_hash,
        )

    def _extract_relationships(self) -> list[Relationship]:
        """Extract call, import, and inheritance relationships from source files.

        Uses tree-sitter QueryCursor directly to capture @target / @node, then
        resolves the enclosing definition (source_name) by byte-range containment
        against a definition map built from the AST.
        """
        relationships: list[Relationship] = []
        ignore_spec = load_ignore_spec(self.repo_path)
        source_files = list(walk_source_files(self.repo_path, ignore_spec))

        for filepath in source_files:
            rel_path = str(filepath.relative_to(self.repo_path)).replace("\\", "/")
            ext = filepath.suffix.lower()
            lang_name = EXTENSION_MAP.get(ext)
            if not lang_name:
                continue

            config = get_language_config(lang_name)
            if not config:
                continue

            lang = get_language(lang_name)
            if lang is None:
                continue

            source = filepath.read_bytes()
            from tree_sitter import Parser  # type: ignore[import-untyped]

            try:
                parser = Parser(lang)  # type: ignore[arg-type]
                tree = parser.parse(source)
                if tree is None:
                    continue  # type: ignore[unreachable]
                root = tree.root_node
            except Exception as e:
                logger.warning("Tree-sitter parse failed for %s: %s", rel_path, e)
                continue

            # Build definition map: (start_byte, end_byte) → name for every named
            # definition in the file.  Used to resolve which function / class / method
            # encloses a call, import, or inheritance clause.
            def_map = _build_definition_map(
                root,
                source,
                config.function_types,
                config.class_types,
                config.method_types,
                config.definition_queries,
                lang,
            )
            if not def_map:
                continue

            # --- Call relationships ---
            for _category, query_str in config.call_queries:
                try:
                    for target_text, node in _run_rel_query(
                        lang, root, query_str, source
                    ):
                        source_name = _find_enclosing_def(def_map, node)
                        if not source_name:
                            continue
                        relationships.append(
                            Relationship(
                                source_file=rel_path,
                                source_line=node.start_point[0] + 1,  # type: ignore[attr-defined]
                                source_name=source_name,
                                target_name=target_text,
                                kind="calls",
                            )
                        )
                except Exception as e:
                    logger.warning("Call query failed for %s: %s", rel_path, e)

            # --- Import relationships ---
            import_matches: list[tuple[str, str, int, str | None]] = []
            for _category, query_str in config.import_queries:
                try:
                    for target_text, node in _run_rel_query(
                        lang, root, query_str, source
                    ):
                        line = node.start_point[0] + 1  # type: ignore[attr-defined]
                        source_name = _find_enclosing_def(def_map, node)
                        import_matches.append(
                            (_category, target_text, line, source_name)
                        )
                except Exception as e:
                    logger.warning("Import query failed for %s: %s", rel_path, e)

            # Group by line for from-import pairing (module + name on same line)
            modules_by_line: dict[int, str] = {}
            names_by_line: dict[int, list[tuple[str, str | None]]] = {}

            for category, target, line, source_name in import_matches:
                if category == "import_source":
                    modules_by_line[line] = target
                elif category in ("import_from_module",):
                    modules_by_line[line] = target
                elif category in ("import_from_name",):
                    names_by_line.setdefault(line, []).append((target, source_name))
                else:
                    resolved = _resolve_module(
                        target, rel_path, lang_name, self.repo_path
                    )
                    relationships.append(
                        Relationship(
                            source_file=rel_path,
                            source_line=line,
                            source_name=source_name or target,
                            target_name=target,
                            target_file=resolved,
                            kind="imports",
                        )
                    )

            for line, names in names_by_line.items():
                module = modules_by_line.get(line)
                resolved_file = None
                if module:
                    resolved_file = _resolve_module(
                        module, rel_path, lang_name, self.repo_path
                    )
                for target, source_name in names:
                    relationships.append(
                        Relationship(
                            source_file=rel_path,
                            source_line=line,
                            source_name=source_name or target,
                            target_name=target,
                            target_file=resolved_file,
                            kind="imports",
                        )
                    )

            # --- Inheritance relationships ---
            for _category, query_str in config.inheritance_queries:
                try:
                    for target_text, node in _run_rel_query(
                        lang, root, query_str, source
                    ):
                        source_name = _find_enclosing_def(def_map, node)
                        if not source_name:
                            continue
                        relationships.append(
                            Relationship(
                                source_file=rel_path,
                                source_line=node.start_point[0] + 1,  # type: ignore[attr-defined]
                                source_name=source_name,
                                target_name=target_text,
                                kind="inherits",
                            )
                        )
                except Exception as e:
                    logger.warning("Inheritance query failed for %s: %s", rel_path, e)

        return relationships

    # ------------------------------------------------------------------
    # Query methods
    # ------------------------------------------------------------------

    def find_definitions(self, name: str) -> list[Tag]:
        """Find all definitions of a symbol by name (deduplicated)."""
        db = get_surreal()
        result = db.query(
            "SELECT * FROM symbol WHERE name = $name AND kind = 'definition'",
            {"name": name},
        )
        tags = _rows_to_tags(_raw_result_rows(result))
        seen: set[tuple[str, int, str]] = set()
        deduped: list[Tag] = []
        for t in tags:
            key = (t.filepath, t.line, t.category)
            if key not in seen:
                seen.add(key)
                deduped.append(t)
        return deduped

    def find_references(self, name: str) -> list[Tag]:
        """Find all references to a symbol (both definitions and references)."""
        db = get_surreal()
        result = db.query("SELECT * FROM symbol WHERE name = $name", {"name": name})
        return _rows_to_tags(_raw_result_rows(result))

    def get_context(self, symbol_name: str, file_hint: str | None = None) -> dict:
        """Get a 360-degree view of a symbol: definition, callers, callees,
        inheritance, and scope.

        If the symbol name is ambiguous (multiple definitions), returns a
        disambiguation list so the agent can narrow down. Pass ``file_hint``
        to select a specific definition when the name exists in multiple files.
        """
        db = get_surreal()
        definitions = self.find_definitions(symbol_name)

        if not definitions:
            return {"symbol": symbol_name, "error": "Symbol not found"}

        # Apply file_hint for disambiguation
        selected = None
        if file_hint and len(definitions) > 1:
            matches = [d for d in definitions if d.filepath == file_hint]
            if len(matches) == 1:
                selected = matches[0]
                definitions = matches

        if len(definitions) > 1 and selected is None:
            disambiguation = [
                {
                    "file": d.filepath,
                    "line": d.line,
                    "kind": d.category,
                    "end_line": d.end_line,
                }
                for d in definitions
            ]
            result: dict = {
                "symbol": symbol_name,
                "ambiguous": True,
                "definitions": disambiguation,
            }
        else:
            d = selected if selected else definitions[0]
            result = {
                "symbol": symbol_name,
                "definitions": [
                    {
                        "file": d.filepath,
                        "line": d.line,
                        "kind": d.category,
                        "end_line": d.end_line,
                    }
                ],
                "scope": _resolve_scope(db, d),
            }

        # Use SurrealQL graph traversal for relationships
        result["calls"] = sorted(_get_edge_targets(db, symbol_name, "calls"))
        result["called_by"] = sorted(_get_edge_sources(db, symbol_name, "calls"))
        result["inherits_from"] = sorted(_get_edge_targets(db, symbol_name, "inherits"))
        result["inherited_by"] = sorted(_get_edge_sources(db, symbol_name, "inherits"))

        return result

    def get_impact(
        self,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_depth: int = 3,
        direction: str = "both",
    ) -> dict:
        """Get the blast radius of changes to a file or line range.

        Uses BFS graph traversal through call edges to find upstream
        (who depends on us) and downstream (what we depend on) impact.

        Args:
            file_path: Path relative to repo root.
            start_line: Optional start line to narrow the impact range.
            end_line: Optional end line to narrow the impact range.
            max_depth: Maximum BFS depth (1-10, default 3).
            direction: "upstream" (who depends on us), "downstream"
                (what we depend on), or "both" (default).

        Returns:
            Dict with symbols_in_range, upstream_impact, downstream_impact,
            imported_by, risk_level, risk_summary.
        """
        max_depth = min(max(1, max_depth), 10)
        db = get_surreal()

        if start_line is not None and end_line is not None:
            result = db.query(
                """SELECT * FROM symbol
                   WHERE filepath = $fp AND kind = 'definition'
                   AND line >= $sl AND end_line <= $el""",
                {"fp": file_path, "sl": start_line, "el": end_line},
            )
        elif start_line is not None:
            result = db.query(
                """SELECT * FROM symbol
                   WHERE filepath = $fp AND kind = 'definition'
                   AND line >= $sl""",
                {"fp": file_path, "sl": start_line},
            )
        else:
            result = db.query(
                """SELECT * FROM symbol
                   WHERE filepath = $fp AND kind = 'definition'""",
                {"fp": file_path},
            )

        file_defs = _rows_to_tags(_raw_result_rows(result))

        symbols_in_range = [
            {
                "name": d.name,
                "line": d.line,
                "kind": d.category,
                "end_line": d.end_line,
            }
            for d in file_defs
        ]

        affected_names = {d.name for d in file_defs}

        imported_by = sorted(_get_edge_sources(db, file_path, "imports"))

        upstream = {}
        downstream = {}

        if direction in ("upstream", "both"):
            upstream = _bfs_upstream(self, db, affected_names, max_depth)

        if direction in ("downstream", "both"):
            downstream = _bfs_downstream(self, db, affected_names, max_depth)

        risk_level, risk_summary = _assess_risk(upstream, downstream, imported_by)

        return {
            "file": file_path,
            "symbols_in_range": symbols_in_range,
            "upstream_impact": upstream,
            "downstream_impact": downstream,
            "imported_by": imported_by,
            "risk_level": risk_level,
            "risk_summary": risk_summary,
        }

    def get_file_overview(self, file_path: str) -> dict:
        """Get all symbols, imports, and class structure for a file."""
        db = get_surreal()

        result = db.query(
            """SELECT * FROM symbol
               WHERE filepath = $fp AND kind = 'definition'""",
            {"fp": file_path},
        )
        file_defs = _rows_to_tags(_raw_result_rows(result))

        definitions = [
            {
                "name": d.name,
                "kind": d.category,
                "line": d.line,
                "end_line": d.end_line,
            }
            for d in file_defs
        ]

        imports = sorted(_get_edge_targets(db, file_path, "imports"))
        imported_by = sorted(_get_edge_sources(db, file_path, "imports"))

        # Build class structure
        classes: dict[str, dict] = {}
        for d in file_defs:
            if d.category == "class":
                classes[d.name] = {
                    "inherits_from": sorted(_get_edge_targets(db, d.name, "inherits")),
                    "methods": [],
                }

        # Assign methods to classes via scope resolution
        for d in file_defs:
            scope = _resolve_scope(db, d)
            if scope and scope in classes:
                classes[scope]["methods"].append(d.name)

        return {
            "file": file_path,
            "definitions": definitions,
            "imports": imports,
            "imported_by": imported_by,
            "classes": classes,
        }

    def detect_changes_from_diff(self, scope: str = "staged") -> dict:
        """Detect symbols affected by git changes and analyze impact.

        Args:
            scope: "staged" (default) for staged changes, "unstaged" for
                working tree changes.

        Returns:
            Dict with changed_files, summary, risk_level.
        """
        import subprocess

        cmd = ["git", "diff", "--unified=0"]
        if scope == "staged":
            cmd.append("--staged")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.repo_path),
                timeout=10,
            )
            if result.returncode != 0:
                return {
                    "changed_files": [],
                    "error": f"git diff failed: {result.stderr}",
                }
        except Exception as e:
            return {"changed_files": [], "error": str(e)}

        changed_ranges = _parse_git_diff(result.stdout)
        db = get_surreal()

        changed_files = []
        for entry in changed_ranges:
            filepath = entry["file"]
            symbols: list[dict] = []
            for rng in entry["ranges"]:
                symbols.extend(_get_symbols_in_range(db, filepath, rng[0], rng[1]))

            if not symbols:
                continue

            affected_names = {s["name"] for s in symbols}
            imported_by = sorted(_get_edge_sources(db, filepath, "imports"))
            upstream = _bfs_upstream(self, db, affected_names, max_depth=3)
            downstream = _bfs_downstream(self, db, affected_names, max_depth=3)
            risk_level, risk_summary = _assess_risk(upstream, downstream, imported_by)

            changed_files.append(
                {
                    "file": filepath,
                    "line_ranges": entry["ranges"],
                    "affected_symbols": symbols,
                    "upstream_impact": upstream,
                    "downstream_impact": downstream,
                    "risk_level": risk_level,
                    "risk_summary": risk_summary,
                }
            )

        return {
            "changed_files": changed_files,
            "summary": _summarize_changes(changed_files),
            "risk_level": _overall_risk(changed_files),
        }

    def trace_flow(
        self,
        entry_point: str,
        file_hint: str | None = None,
        max_depth: int = 20,
    ) -> dict:
        """Trace the execution flow from an entry-point symbol.

        Uses BFS graph traversal through call edges to build an ordered
        flow of function/method calls with depth markers.

        Args:
            entry_point: Name of the symbol to start tracing from.
            file_hint: Optional file path to disambiguate when the
                symbol exists in multiple files.
            max_depth: Maximum traversal depth (1-50, default 20).

        Returns:
            Dict with entry_point, entry_definition, steps, call_chain,
            total_steps, max_depth_reached.
        """
        db = get_surreal()

        # Resolve entry point
        definitions = self.find_definitions(entry_point)
        if not definitions:
            return {"entry_point": entry_point, "error": "Symbol not found"}

        if file_hint:
            definitions = [d for d in definitions if d.filepath == file_hint]

        if len(definitions) > 1:
            return {
                "entry_point": entry_point,
                "ambiguous": True,
                "definitions": [
                    {"file": d.filepath, "line": d.line, "kind": d.category}
                    for d in definitions
                ],
            }

        d = definitions[0]
        entry_def = {
            "name": d.name,
            "file": d.filepath,
            "line": d.line,
            "kind": d.category,
        }

        max_depth = max(1, min(max_depth, 50))

        # BFS traversal to build call chain
        visited: set[str] = {entry_point}
        steps: list[dict] = []
        frontier: deque[tuple[str, int]] = deque([(entry_point, 0)])

        while frontier:
            current_name, depth = frontier.popleft()
            if depth >= max_depth:
                continue

            callees = sorted(_get_edge_targets(db, current_name, "calls"))
            for callee in callees:
                if callee in visited:
                    continue
                visited.add(callee)
                callee_defs = self.find_definitions(callee)
                if callee_defs:
                    cd = callee_defs[0]
                    steps.append(
                        {
                            "name": callee,
                            "caller": current_name,
                            "file": cd.filepath,
                            "line": cd.line,
                            "kind": cd.category,
                            "depth": depth + 1,
                        }
                    )
                else:
                    steps.append(
                        {
                            "name": callee,
                            "caller": current_name,
                            "file": None,
                            "line": None,
                            "kind": "unknown",
                            "depth": depth + 1,
                        }
                    )
                frontier.append((callee, depth + 1))

        call_chain = _build_call_chain(db, entry_point, visited, max_depth)

        return {
            "entry_point": entry_point,
            "entry_definition": entry_def,
            "steps": steps,
            "total_steps": len(steps),
            "max_depth_reached": any(s["depth"] == max_depth for s in steps),
            "call_chain": call_chain,
        }


# ---------------------------------------------------------------------------
# SurrealDB helpers
# ---------------------------------------------------------------------------


def _build_call_chain(
    db,
    name: str,
    visited: set[str],
    max_depth: int,
    depth: int = 0,
) -> dict:
    """Recursively build a nested call chain for trace_flow."""
    if depth >= max_depth:
        return {"name": name, "callees": [], "truncated": True}

    callees = sorted(_get_edge_targets(db, name, "calls"))
    return {
        "name": name,
        "callees": [
            _build_call_chain(db, c, visited, max_depth, depth + 1)
            for c in callees
            if c in visited
        ],
    }


def _build_definition_map(
    root_node,
    source: bytes,
    function_types: frozenset[str],
    class_types: frozenset[str],
    method_types: frozenset[str],
    definition_queries: tuple[tuple[str, str], ...],
    lang,
) -> list[tuple[int, int, str]]:
    """Build a list of (start_byte, end_byte, name) for every named definition.

    Uses both structural node-type matching and tree-sitter definition queries
    to capture function / class / method names with their byte ranges.
    """
    entries: list[tuple[int, int, str]] = []

    # Collect names from definition queries (these already have @name captures)
    from tree_sitter import Query, QueryCursor  # type: ignore[import-untyped]

    for _category, query_str in definition_queries:
        try:
            q = Query(lang, query_str)
            cursor = QueryCursor(q)
            captures = cursor.captures(root_node)
            if not captures or not isinstance(captures, dict):
                continue
            name_nodes = captures.get("name", [])
            node_nodes = captures.get("node", [])
            for name_node in name_nodes:
                name_text = source[name_node.start_byte : name_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                # Use the containing @node's range or the name itself
                enclosing = name_node
                for node in node_nodes:
                    if (
                        node.start_byte <= name_node.start_byte
                        and name_node.end_byte <= node.end_byte
                    ):
                        enclosing = node
                        break
                entries.append((enclosing.start_byte, enclosing.end_byte, name_text))
        except Exception as e:
            logger.warning("Definition map building failed: %s", e)

    # Also walk direct children for any named function/class/method nodes that
    # the definition queries might have missed (e.g. arrow functions, exported
    # declarations).
    definable_types = function_types | class_types | method_types
    if definable_types:
        _walk_def_nodes(root_node, source, definable_types, entries)

    # Sort by start_byte for binary search, and de-duplicate by (start, end, name)
    seen: set[tuple[int, int, str]] = set()
    unique: list[tuple[int, int, str]] = []
    for start, end, name in sorted(entries, key=lambda e: e[0]):
        key = (start, end, name)
        if key not in seen:
            seen.add(key)
            unique.append((start, end, name))
    return unique


def _walk_def_nodes(
    node,
    source: bytes,
    definable_types: frozenset[str],
    entries: list[tuple[int, int, str]],
) -> None:
    """Recursively walk AST looking for named definition nodes."""
    if node.type in definable_types:
        name = _extract_node_name(node, source)
        if name:
            entries.append((node.start_byte, node.end_byte, name))
    for child in node.children:
        _walk_def_nodes(child, source, definable_types, entries)


def _extract_node_name(node, source: bytes) -> str | None:
    """Extract the name from a definition-like AST node."""
    # Most languages use a 'name' field on definition nodes
    name_child = node.child_by_field_name("name")
    if name_child is not None:
        return source[name_child.start_byte : name_child.end_byte].decode(
            "utf-8", errors="replace"
        )
    # Fallback: look for an identifier or type_identifier child
    for child in node.children:
        if child.type in ("identifier", "type_identifier", "property_identifier"):
            return source[child.start_byte : child.end_byte].decode(
                "utf-8", errors="replace"
            )
    return None


def _run_rel_query(
    lang, root_node, query_str: str, source: bytes
) -> list[tuple[str, object]]:
    """Run a relationship query and return (target_text, enclosing_node) pairs.

    Unlike _run_ts_query, this does NOT require @name — it works with just
    @target and @node captures.  Returns a list of (target_text, node) tuples
    where *node* is the enclosing @node (falling back to the target node itself).
    """
    results: list[tuple[str, object]] = []
    from tree_sitter import Query, QueryCursor  # type: ignore[import-untyped]

    q = Query(lang, query_str)
    cursor = QueryCursor(q)
    captures = cursor.captures(root_node)
    if not captures or not isinstance(captures, dict):
        return results

    target_nodes = captures.get("target", [])
    node_nodes = captures.get("node", [])

    for target_node in target_nodes:
        target_text = source[target_node.start_byte : target_node.end_byte].decode(
            "utf-8", errors="replace"
        )

        # Pick the tightest enclosing @node (smallest byte range)
        best_node = target_node
        best_size = target_node.end_byte - target_node.start_byte
        for node in node_nodes:
            if (
                node.start_byte <= target_node.start_byte
                and target_node.end_byte <= node.end_byte
            ):
                size = node.end_byte - node.start_byte
                if size < best_size:
                    best_node = node
                    best_size = size

        results.append((target_text, best_node))

    return results


def _find_enclosing_def(
    def_map: list[tuple[int, int, str]],
    node,
) -> str | None:
    """Find the name of the definition that encloses *node* using binary search.

    *def_map* is sorted by start_byte.  Returns the tightest enclosing definition
    (smallest byte range) that contains *node*.
    """
    node_start = node.start_byte
    node_end = node.end_byte

    # Binary search to find the first candidate whose start ≤ node_start
    import bisect

    starts = [e[0] for e in def_map]
    idx = bisect.bisect_right(starts, node_start) - 1

    best_name = None
    best_size = None

    # Scan backwards — enclosing defs must have start ≤ node_start
    while idx >= 0:
        def_start, def_end, name = def_map[idx]
        if def_end < node_end:
            # No further candidate can contain us (starts are monotonic, but
            # a def with an earlier start could still have a later end).  We
            # must keep scanning.
            idx -= 1
            continue
        if def_start <= node_start and node_end <= def_end:
            size = def_end - def_start
            if best_size is None:
                best_size = size
                best_name = name
            elif size < best_size:  # type: ignore[unreachable]
                best_size = size
                best_name = name
        idx -= 1

    return best_name


def _rows_to_tags(rows: list[dict]) -> list[Tag]:
    """Convert SurrealDB result rows back to Tag objects."""
    tags = []
    for row in rows:
        try:
            tags.append(
                Tag(
                    filepath=row.get("filepath", ""),
                    name=row.get("name", ""),
                    kind=row.get("kind", "reference"),
                    line=row.get("line", 0),
                    category=row.get("category", ""),
                    end_line=row.get("end_line", 0),
                )
            )
        except Exception as e:
            logger.warning("Failed to convert SurrealDB row to Tag: %s", e)
            continue
    return tags


def _lang_from_filepath(filepath: str) -> str:
    """Derive programming language from file extension."""
    ext = filepath.rsplit(".", 1)[-1].lower() if "." in filepath else ""
    return {
        "py": "python",
        "js": "javascript",
        "mjs": "javascript",
        "ts": "typescript",
        "tsx": "typescript",
        "jsx": "javascript",
        "go": "go",
        "rs": "rust",
        "java": "java",
    }.get(ext, "")


def _upsert_symbols(db, tags: list[Tag]) -> None:
    """Batch upsert symbol records into SurrealDB, with in-batch dedup."""
    seen: set[tuple[str, str, str, str, int]] = set()
    batch: list[dict] = []
    for t in tags:
        key = (t.name, t.kind, t.category, t.filepath, t.line)
        if key in seen:
            continue
        seen.add(key)

        batch.append(
            {
                "name": t.name,
                "kind": t.kind,
                "category": t.category,
                "filepath": t.filepath,
                "line": t.line,
                "end_line": t.end_line or t.line,
                "language": _lang_from_filepath(t.filepath),
                "content": "",
            }
        )
        if len(batch) >= 500:
            try:
                db.query("INSERT INTO symbol $records", {"records": batch})
            except Exception as e:
                logger.error(
                    "Batch insert of %d symbols failed: %s. Data loss — these symbols will be missing from the index.",
                    len(batch),
                    e,
                )
            batch.clear()

    if batch:
        try:
            db.query("INSERT INTO symbol $records", {"records": batch})
        except Exception as e:
            logger.error(
                "Final batch insert of %d symbols failed: %s. Data loss — these symbols will be missing from the index.",
                len(batch),
                e,
            )


def _upsert_relationships(db, relationships: list[Relationship]) -> None:
    """Batch upsert relationships as SurrealDB graph edges.

    Performs deduplicated batch lookups for sources and targets before
    issuing RELATE statements, avoiding the previous N+1 query pattern.
    """
    if not relationships:
        return

    # -- 1. Batch source lookup -------------------------------------------
    src_keys: list[tuple[str, str]] = []
    src_set: set[tuple[str, str]] = set()
    for rel in relationships:
        key = (rel.source_name, rel.source_file)
        if key not in src_set:
            src_set.add(key)
            src_keys.append(key)

    src_map: dict[tuple[str, str], str] = {}
    for key in src_keys:
        name, fp = key
        try:
            result = db.query(
                "SELECT id FROM symbol WHERE name = $name AND filepath = $fp "
                "AND kind = 'definition' LIMIT 1",
                {"name": name, "fp": fp},
            )
            rows = _raw_result_rows(result)
            if rows:
                src_map[key] = rows[0]["id"]
        except Exception as e:
            logger.warning("Source lookup failed for %s: %s", key, e)

    # -- 2. Batch target lookup -------------------------------------------
    tgt_with_file: set[tuple[str, str]] = set()
    tgt_no_file: set[str] = set()
    for rel in relationships:
        key = (rel.source_name, rel.source_file)
        if key not in src_map:
            continue  # skip edges whose source was never found
        if rel.target_file:
            tgt_with_file.add((rel.target_name, rel.target_file))
        tgt_no_file.add(rel.target_name)

    tgt_map: dict[tuple[str, str], str] = {}
    for name, fp in tgt_with_file:
        try:
            result = db.query(
                "SELECT id FROM symbol WHERE name = $name AND kind = 'definition' "
                "AND filepath = $fp LIMIT 1",
                {"name": name, "fp": fp},
            )
            rows = _raw_result_rows(result)
            if rows:
                tgt_map[(name, fp)] = rows[0]["id"]
        except Exception as e:
            logger.warning("Target (file) lookup failed for %s:%s: %s", name, fp, e)
    for name in tgt_no_file:
        try:
            result = db.query(
                "SELECT id FROM symbol WHERE name = $name AND kind = 'definition' LIMIT 1",
                {"name": name},
            )
            rows = _raw_result_rows(result)
            if rows:
                tgt_map[(name, "")] = rows[0]["id"]
        except Exception as e:
            logger.warning("Target (name) lookup failed for %s: %s", name, e)

    # -- 3. Issue RELATE statements --------------------------------------
    for rel in relationships:
        src_key = (rel.source_name, rel.source_file)
        src_id = src_map.get(src_key)
        if not src_id:
            continue

        tgt_id: str | None = None
        if rel.target_file:
            tgt_id = tgt_map.get((rel.target_name, rel.target_file))
        if tgt_id is None:
            tgt_id = tgt_map.get((rel.target_name, ""))

        if not tgt_id or src_id == tgt_id:
            continue

        try:
            db.query(
                f"RELATE $src->{rel.kind}->$tgt SET source_line = $line",
                {"src": src_id, "tgt": tgt_id, "line": rel.source_line},
            )
        except Exception as e:
            logger.warning("Edge upsert failed: %s", e)


def _get_edge_targets(db, name: str, edge_table: str) -> set[str]:
    """Get target names from a graph edge table."""
    try:
        result = db.query(
            f"SELECT out.name AS target FROM {edge_table} WHERE in.name = $name",  # nosec B608
            {"name": name},
        )
        rows = _raw_result_rows(result)
        return {r.get("target", "") for r in rows if r.get("target")}
    except Exception as e:
        logger.warning(
            "Edge target query failed for table '%s', name '%s': %s",
            edge_table,
            name,
            e,
        )
        return set()


def _get_edge_sources(db, name: str, edge_table: str) -> set[str]:
    """Get source names from a graph edge table."""
    try:
        result = db.query(
            f"SELECT in.name AS source FROM {edge_table} WHERE out.name = $name",  # nosec B608
            {"name": name},
        )
        rows = _raw_result_rows(result)
        return {r.get("source", "") for r in rows if r.get("source")}
    except Exception as e:
        logger.warning(
            "Edge source query failed for table '%s', name '%s': %s",
            edge_table,
            name,
            e,
        )
        return set()


def _resolve_scope(db, tag: Tag) -> str | None:
    """Resolve the enclosing class scope for a symbol using SurrealDB."""
    try:
        result = db.query(
            """SELECT out.name AS parent FROM contains_edge
               WHERE in.filepath = $fp AND in.line = $line
               AND in.name = $name""",
            {"fp": tag.filepath, "line": tag.line, "name": tag.name},
        )
        rows = _raw_result_rows(result)
        if rows:
            parent: object = rows[0].get("parent")
            return str(parent) if parent else None
    except Exception as e:
        logger.debug(
            "Scope resolution failed for %s:%s:%s: %s",
            tag.filepath,
            tag.line,
            tag.name,
            e,
        )
    return None


# ---------------------------------------------------------------------------
# BFS impact traversal helpers
# ---------------------------------------------------------------------------


def _bfs_upstream(symbol_index, db, seed_names: set[str], max_depth: int) -> dict:
    """BFS traversal upstream — who depends on us (callers)."""
    impact: dict[int, list[dict]] = {}
    visited: set[str] = set(seed_names)
    current = set(seed_names)

    for depth in range(1, max_depth + 1):
        next_level: set[str] = set()
        level_items: list[dict] = []

        for name in current:
            for source in _get_edge_sources(db, name, "calls"):
                if source not in visited:
                    visited.add(source)
                    next_level.add(source)
                    for d in symbol_index.find_definitions(source):
                        level_items.append(
                            {
                                "name": source,
                                "file": d.filepath,
                                "line": d.line,
                                "kind": d.category,
                            }
                        )

        if level_items:
            impact[depth] = level_items
        current = next_level
        if not current:
            break

    return impact


def _bfs_downstream(symbol_index, db, seed_names: set[str], max_depth: int) -> dict:
    """BFS traversal downstream — what we depend on (callees)."""
    impact: dict[int, list[dict]] = {}
    visited: set[str] = set(seed_names)
    current = set(seed_names)

    for depth in range(1, max_depth + 1):
        next_level: set[str] = set()
        level_items: list[dict] = []

        for name in current:
            for target in _get_edge_targets(db, name, "calls"):
                if target not in visited:
                    visited.add(target)
                    next_level.add(target)
                    for d in symbol_index.find_definitions(target):
                        level_items.append(
                            {
                                "name": target,
                                "file": d.filepath,
                                "line": d.line,
                                "kind": d.category,
                            }
                        )

        if level_items:
            impact[depth] = level_items
        current = next_level
        if not current:
            break

    return impact


def _assess_risk(
    upstream: dict,
    downstream: dict,
    imported_by: list[str],
) -> tuple[str, str]:
    """Assess risk level from BFS impact data."""
    # Collect unique affected files
    upstream_files: set[str] = set()
    for items in upstream.values():
        for item in items:
            upstream_files.add(item["file"])

    downstream_files: set[str] = set()
    for items in downstream.values():
        for item in items:
            downstream_files.add(item["file"])

    total_files = len(upstream_files | downstream_files | set(imported_by))
    total_symbols = sum(len(items) for items in upstream.values())
    total_symbols += sum(len(items) for items in downstream.values())

    has_tests = any("test" in str(f).lower() for f in upstream_files | downstream_files)

    if total_files > 10 or total_symbols > 20:
        level = "high"
        summary = f"High risk: affects {total_files} files and {total_symbols} symbols"
    elif total_files > 3 or total_symbols > 5:
        level = "medium"
        summary = (
            f"Medium risk: affects {total_files} files and {total_symbols} symbols"
        )
    else:
        level = "low"
        summary = f"Low risk: affects {total_files} files and {total_symbols} symbols"

    if has_tests:
        summary += " (includes test files)"

    return level, summary


# ---------------------------------------------------------------------------
# Commit tracking
# ---------------------------------------------------------------------------


def _is_commit_indexed(db, commit_hash: str) -> bool:
    """Check if the given commit is already indexed with the current schema."""
    try:
        result = db.query(
            "SELECT version, repo_commit FROM _schema_meta WHERE repo_commit = $hash LIMIT 1",
            {"hash": commit_hash},
        )
        rows = _raw_result_rows(result)
        if rows:
            row = rows[0]
            return bool(row.get("version") == SCHEMA_VERSION)
        return False
    except Exception as e:
        logger.warning("Failed to check if commit %s is indexed: %s", commit_hash, e)
        return False


def _mark_commit_indexed(db, commit_hash: str) -> None:
    """Record the indexed commit hash and schema version in metadata."""
    try:
        db.query(
            "UPDATE _schema_meta SET repo_commit = $hash, version = $ver",
            {"hash": commit_hash, "ver": SCHEMA_VERSION},
        )
    except Exception as e:
        logger.warning("Failed to mark commit indexed: %s", e)


def _clear_repo_data(db) -> None:
    """Remove all code intelligence data for a fresh re-index.

    Only deletes graph definitions (records without embeddings) and graph
    edges. Search chunks with embeddings are preserved — they're owned by
    the indexing worker.
    """
    tables = ["calls", "imports", "inherits", "contains_edge"]
    for table in tables:
        try:
            db.query(f"DELETE FROM {table}")  # nosec B608
        except Exception as e:
            logger.warning("Failed to clear table '%s' during re-index: %s", table, e)
    # Only clear symbol records that are graph definitions (no embedding)
    try:
        db.query("DELETE FROM symbol WHERE embedding IS NULL")
    except Exception as e:
        logger.warning("Failed to clear symbol definitions during re-index: %s", e)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_head_commit(repo_path: Path) -> str:
    """Get the HEAD commit hash from a git worktree."""
    head_file = repo_path / ".git" / "HEAD"
    try:
        if head_file.is_file():
            head_content = head_file.read_text().strip()
            if head_content.startswith("ref:"):
                ref_path = head_content.split(":", 1)[1].strip()
                git_file = repo_path / ".git"
                if git_file.is_file():
                    gitdir = git_file.read_text().strip()
                    if gitdir.startswith("gitdir:"):
                        gitdir = gitdir.split(":", 1)[1].strip()
                        ref_full = Path(gitdir) / ref_path
                        if ref_full.exists():
                            return ref_full.read_text().strip()
                ref_full = repo_path / ".git" / ref_path
                if ref_full.exists():
                    return ref_full.read_text().strip()
            else:
                return head_content
    except (OSError, ValueError):
        pass

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
    except Exception as e:
        logger.warning("Failed to resolve HEAD commit via git command: %s", e)

    logger.warning(
        "Could not resolve HEAD commit hash — returning 'unknown'. This may trigger unnecessary full re-indexing."
    )
    return "unknown"


def _safe_repo_name(repo: str) -> str:
    """Convert a repo slug to a safe key for cache/index naming."""
    return repo.replace("/", "--")


def _parse_git_diff(diff_output: str) -> list[dict]:
    """Parse git diff --unified=0 output to extract changed file + line ranges."""
    changes: list[dict] = []
    current_file: str | None = None
    ranges: list[tuple[int, int]] = []

    for line in diff_output.splitlines():
        if line.startswith("+++ b/"):
            if current_file and ranges:
                changes.append({"file": current_file, "ranges": ranges})
            current_file = line[6:]
            ranges = []
        elif line.startswith("@@") and current_file:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                ranges.append((start, start + count - 1))

    if current_file and ranges:
        changes.append({"file": current_file, "ranges": ranges})

    return changes


def _get_symbols_in_range(
    db, filepath: str, start_line: int, end_line: int
) -> list[dict]:
    """Get symbol definitions that overlap with a line range."""
    try:
        result = db.query(
            """SELECT name, line, end_line, category FROM symbol
               WHERE filepath = $fp AND kind = 'definition'
               AND line <= $el AND end_line >= $sl""",
            {"fp": filepath, "sl": start_line, "el": end_line},
        )
        rows = _raw_result_rows(result)
        return [
            {
                "name": r.get("name", ""),
                "line": r.get("line", 0),
                "end_line": r.get("end_line", 0),
                "kind": r.get("category", ""),
            }
            for r in rows
        ]
    except Exception as e:
        logger.warning(
            "Symbol-in-range query failed for %s lines %d-%d: %s",
            filepath,
            start_line,
            end_line,
            e,
        )
        return []


def _summarize_changes(changed_files: list[dict]) -> str:
    """Generate a human-readable summary of detected changes."""
    if not changed_files:
        return "No symbol-affecting changes detected."

    total_symbols = sum(len(f["affected_symbols"]) for f in changed_files)
    high_risk = [f["file"] for f in changed_files if f["risk_level"] == "high"]

    summary = (
        f"{len(changed_files)} file(s) changed, " f"{total_symbols} symbol(s) affected."
    )
    if high_risk:
        summary += (
            f" {len(high_risk)} file(s) at high risk: " f"{', '.join(high_risk[:3])}"
        )

    return summary


def _overall_risk(changed_files: list[dict]) -> str:
    """Determine the overall risk level across all changed files."""
    if any(f.get("risk_level") == "high" for f in changed_files):
        return "high"
    if any(f.get("risk_level") == "medium" for f in changed_files):
        return "medium"
    return "low"


def _resolve_module(
    module_name: str,
    from_file: str,
    lang_name: str,
    repo_path: Path,
) -> str | None:
    """Resolve an import module name to a file path within the repo."""
    if lang_name == "python":
        return resolve_python_import(module_name, from_file, repo_path)
    if lang_name in ("typescript", "tsx", "javascript"):
        return resolve_ts_import(module_name, from_file, repo_path)
    return None
