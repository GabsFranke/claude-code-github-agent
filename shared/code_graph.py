"""Queryable symbol index for code intelligence.

Extracts relationships (calls, imports, inheritance) from tree-sitter ASTs
and stores everything in SurrealDB — a multi-model database that handles
graph edges, vector search, and full-text search. The SymbolIndex class
provides a thin query layer over SurrealDB, replacing the in-memory lookup
dicts and JSON file cache from the first phase.
"""

import bisect
import hashlib
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .constants import sanitize_repo_key
from .file_tree import load_ignore_spec, walk_source_files
from .import_resolver import resolve_python_import, resolve_ts_import
from .repomap import RepoMap, Tag
from .surrealdb_client import (
    SCHEMA_VERSION,
    _raw_result_rows,
    apply_schema,
    is_initialized,
    query_surreal,
)
from .ts_languages import EXTENSION_MAP, get_language, get_language_config

logger = logging.getLogger(__name__)

RelationshipKind = Literal["calls", "imports", "inherits"]

# Whitelist of valid edge table names — guards against accidental injection
# surfaces when table names are interpolated into SurrealQL f-strings.
_VALID_EDGE_TABLES: frozenset[str] = frozenset(
    {"calls", "imports", "inherits", "contains_edge"}
)


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
    repo: str = ""
    _built: bool = field(default=False, repr=False)
    _edge_cache: dict[str, dict[str, set[str]]] | None = field(default=None, repr=False)
    _edge_cache_rev: dict[str, dict[str, set[str]]] | None = field(
        default=None, repr=False
    )
    _scope_cache: dict[tuple[str, int, str], str] | None = field(
        default=None, repr=False
    )
    _def_cache: dict[str, list[Tag]] | None = field(default=None, repr=False)

    def _load_edge_cache(self, repo: str) -> None:
        """Bulk-fetch all edge tables into memory."""
        self._edge_cache = {}
        self._edge_cache_rev = {}
        for table in ("calls", "imports", "inherits"):
            forward = _get_all_repo_edges(table, repo)
            self._edge_cache[table] = forward
            reverse: dict[str, set[str]] = {}
            for src, targets in forward.items():
                for tgt in targets:
                    reverse.setdefault(tgt, set()).add(src)
            self._edge_cache_rev[table] = reverse

    def _get_edge_cache(self, repo: str) -> tuple[dict, dict]:
        """Load forward+reverse edge caches. Returns (forward, reverse)."""
        if self._edge_cache is None:
            self._load_edge_cache(repo)
        return self._edge_cache, self._edge_cache_rev  # type: ignore[return-value]

    def _get_scope_cache(self, repo: str) -> dict:
        """Load scope containment cache. Returns {(filepath, line, name): parent}."""
        if self._scope_cache is None:
            self._scope_cache = _get_all_scope_edges(repo)
        return self._scope_cache  # type: ignore[return-value]

    def _get_def_cache(self, repo: str) -> dict[str, list[Tag]]:
        """Bulk-fetch all definition Tags into memory. Returns {name: [Tag, ...]}.

        Populated lazily on first call and invalidated by _invalidate_caches,
        so BFS traversals (which visit many symbols) avoid per-symbol DB queries.
        """
        if self._def_cache is None:
            try:
                result = query_surreal(
                    "SELECT name, kind, category, filepath, line, end_line"
                    " FROM symbol WHERE kind = 'definition' AND repo = $repo",
                    {"repo": repo},
                )
                cache: dict[str, list[Tag]] = {}
                for tag in _rows_to_tags(_raw_result_rows(result)):
                    cache.setdefault(tag.name, []).append(tag)
                self._def_cache = cache
            except Exception as e:
                logger.warning("Bulk definition fetch failed: %s", e)
                self._def_cache = {}
        return self._def_cache

    def _invalidate_caches(self) -> None:
        """Drop all in-memory caches (call after build/incremental build)."""
        self._edge_cache = None
        self._edge_cache_rev = None
        self._scope_cache = None
        self._def_cache = None

    def build(
        self,
        pre_extracted_tags: list[Tag] | None = None,
        pre_extracted_relationships: list[Relationship] | None = None,
        force: bool = False,
    ) -> None:
        """Build the symbol index by extracting tags and relationships.

        Stores everything in SurrealDB. If the current commit has already
        been indexed, skips the extraction and loads from the database.

        Args:
            pre_extracted_tags: Optional pre-extracted tags to use instead
                of performing a fresh extraction.
            pre_extracted_relationships: Optional pre-extracted relationships
                to use instead of calling _extract_relationships(). Ignored
                when pre_extracted_tags is None.
            force: If True, rebuild even if the commit is already indexed.
        """
        import time

        t0 = time.monotonic()

        if not is_initialized():
            logger.warning("SurrealDB not initialized, skipping build")
            return

        commit_hash = _get_head_commit(self.repo_path)

        # Skip if already indexed for this commit
        if (
            not force
            and not pre_extracted_tags
            and _is_commit_indexed(commit_hash, self.repo)
        ):
            logger.info(
                "Commit %s already indexed, reusing SurrealDB data", commit_hash
            )
            self._built = True
            return

        # Clear old data for this repo
        _clear_repo_data(self.repo)

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
        _upsert_symbols(tags, self.repo)
        logger.debug("Upserted %d symbols to SurrealDB", len(tags))

        # Extract and upsert relationships as graph edges
        if pre_extracted_relationships is not None:
            relationships = pre_extracted_relationships
        else:
            relationships = self._extract_relationships()
        logger.debug(
            "Extracted %d relationships, upserting to SurrealDB", len(relationships)
        )
        _upsert_relationships(relationships, self.repo)

        # Mark commit as indexed
        _mark_commit_indexed(commit_hash, self.repo)
        self._invalidate_caches()
        self._built = True
        logger.info(
            "SymbolIndex built: %d symbols, %d relationships, commit=%s (%.1fs)",
            len(tags),
            len(relationships),
            commit_hash,
            time.monotonic() - t0,
        )

    def build_incremental(self, changed_files: list[str]) -> None:
        """Re-index only the changed files, preserving existing data.

        Deletes symbols and edges for the changed files, then re-extracts
        and re-inserts only those files. Much faster than a full rebuild
        for small changes.

        Args:
            changed_files: Relative file paths that changed.
        """
        import time

        t0 = time.monotonic()

        if not is_initialized():
            logger.warning("SurrealDB not initialized, skipping incremental build")
            return

        if not changed_files:
            logger.info("No changed files, skipping incremental build")
            self._built = True
            return

        apply_schema()

        # Delete old symbols + edges for just these files
        _clear_repo_files(self.repo, changed_files)

        # Re-extract tags for changed files only
        rm = RepoMap(self.repo_path)
        tags = rm.extract_tags(only_files=changed_files)
        logger.debug(
            "Incremental: extracted %d tags from %d changed files",
            len(tags),
            len(changed_files),
        )

        if not tags:
            logger.warning("No tags extracted from changed files")
            self._built = True
            return

        # Upsert new symbols
        _upsert_symbols(tags, self.repo)
        logger.debug("Incremental: upserted %d symbols", len(tags))

        # Re-extract relationships for changed files only
        relationships = self._extract_relationships(only_files=changed_files)
        logger.debug(
            "Incremental: extracted %d relationships from changed files",
            len(relationships),
        )
        _upsert_relationships(relationships, self.repo)

        # Update commit hash
        commit_hash = _get_head_commit(self.repo_path)
        _mark_commit_indexed(commit_hash, self.repo)
        self._invalidate_caches()
        self._built = True
        logger.info(
            "Incremental build: %d symbols, %d relationships from %d files (%.1fs)",
            len(tags),
            len(relationships),
            len(changed_files),
            time.monotonic() - t0,
        )

    def _extract_relationships(
        self, only_files: list[str] | None = None
    ) -> list[Relationship]:
        """Extract call, import, and inheritance relationships from source files.

        Uses tree-sitter QueryCursor directly to capture @target / @node, then
        resolves the enclosing definition (source_name) by byte-range containment
        against a definition map built from the AST.

        Args:
            only_files: If provided, only extract relationships from these
                files (relative paths). Useful for incremental re-indexing.
        """
        relationships: list[Relationship] = []
        ignore_spec = load_ignore_spec(self.repo_path)
        source_files = list(walk_source_files(self.repo_path, ignore_spec))

        if only_files is not None:
            only_set = set(only_files)
            source_files = [
                f
                for f in source_files
                if str(f.relative_to(self.repo_path)).replace("\\", "/") in only_set
            ]

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

            # For multi-line parenthesised imports the name nodes appear on lines
            # *after* the module node (e.g. `from foo import (\n  bar,\n  baz\n)`).
            # Use a nearest-prior-line bisect fallback so bar/baz still resolve
            # to the correct module even though their line numbers don't match.
            sorted_module_lines = sorted(modules_by_line)
            for line, names in names_by_line.items():
                module = modules_by_line.get(line)
                if module is None and sorted_module_lines:
                    pos = bisect.bisect_right(sorted_module_lines, line) - 1
                    if pos >= 0 and line - sorted_module_lines[pos] <= 20:
                        module = modules_by_line[sorted_module_lines[pos]]
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

    def find_definitions(self, name: str, repo: str | None = None) -> list[Tag]:
        """Find all definitions of a symbol by name (deduplicated)."""
        scope = repo or self.repo
        result = query_surreal(
            "SELECT name, kind, category, filepath, line, end_line FROM symbol"
            " WHERE name = $name AND kind = 'definition' AND repo = $repo",
            {"name": name, "repo": scope},
        )
        tags = _rows_to_tags(_raw_result_rows(result))

        # Fallback: if no rows with kind='definition', try without the
        # kind filter and select definitions by category instead.  This
        # handles cases where the indexing path stored the symbol without
        # the expected kind value.
        if not tags:
            result = query_surreal(
                "SELECT name, kind, category, filepath, line, end_line FROM symbol"
                " WHERE name = $name AND repo = $repo",
                {"name": name, "repo": scope},
            )
            all_tags = _rows_to_tags(_raw_result_rows(result))
            def_categories = {"class", "function", "method", "variable", "decorator"}
            tags = [t for t in all_tags if t.category in def_categories]
            if tags:
                logger.info(
                    "find_definitions fallback for %r: found %d defs "
                    "(kind column may be inconsistent, categories: %s)",
                    name,
                    len(tags),
                    {t.category for t in tags},
                )

        logger.debug(
            "find_definitions(name=%r, repo=%r) → %d results",
            name,
            scope,
            len(tags),
        )
        seen: set[tuple[str, int, str]] = set()
        deduped: list[Tag] = []
        for t in tags:
            key = (t.filepath, t.line, t.category)
            if key not in seen:
                seen.add(key)
                deduped.append(t)
        return deduped

    def find_references(self, name: str, repo: str | None = None) -> list[Tag]:
        """Find all references to a symbol (both definitions and references)."""
        scope = repo or self.repo
        result = query_surreal(
            "SELECT name, kind, category, filepath, line, end_line FROM symbol"
            " WHERE name = $name AND repo = $repo",
            {"name": name, "repo": scope},
        )
        return _rows_to_tags(_raw_result_rows(result))

    def get_context(
        self, symbol_name: str, file_hint: str | None = None, repo: str | None = None
    ) -> dict:
        """Get a 360-degree view of a symbol: definition, callers, callees,
        inheritance, and scope.

        If the symbol name is ambiguous (multiple definitions), returns a
        disambiguation list so the agent can narrow down. Pass ``file_hint``
        to select a specific definition when the name exists in multiple files.
        """
        scope = repo or self.repo
        definitions = self.find_definitions(symbol_name, repo=scope)

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
                "scope": _resolve_scope(d, scope),
            }

        # Use SurrealQL graph traversal for relationships
        result["calls"] = sorted(_get_edge_targets(symbol_name, "calls", scope))
        result["called_by"] = sorted(_get_edge_sources(symbol_name, "calls", scope))
        result["inherits_from"] = sorted(
            _get_edge_targets(symbol_name, "inherits", scope)
        )
        result["inherited_by"] = sorted(
            _get_edge_sources(symbol_name, "inherits", scope)
        )

        return result

    def get_impact(
        self,
        file_path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_depth: int = 3,
        direction: str = "both",
        repo: str | None = None,
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
        scope = repo or self.repo

        if start_line is not None and end_line is not None:
            result = query_surreal(
                """SELECT name, kind, category, filepath, line, end_line FROM symbol
                   WHERE filepath = $fp AND kind = 'definition' AND repo = $repo
                   AND line >= $sl AND end_line <= $el""",
                {"fp": file_path, "sl": start_line, "el": end_line, "repo": scope},
            )
        elif start_line is not None:
            result = query_surreal(
                """SELECT name, kind, category, filepath, line, end_line FROM symbol
                   WHERE filepath = $fp AND kind = 'definition' AND repo = $repo
                   AND line >= $sl""",
                {"fp": file_path, "sl": start_line, "repo": scope},
            )
        else:
            result = query_surreal(
                """SELECT name, kind, category, filepath, line, end_line FROM symbol
                   WHERE filepath = $fp AND kind = 'definition' AND repo = $repo""",
                {"fp": file_path, "repo": scope},
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

        # Use edge cache for imports and BFS lookups
        edge_fwd, edge_rev = self._get_edge_cache(scope)
        imports_rev = edge_rev.get("imports", {})

        imported_by = sorted(imports_rev.get(file_path, set()))

        upstream = {}
        downstream = {}

        if direction in ("upstream", "both"):
            upstream = _bfs_upstream(
                self, affected_names, max_depth, scope, edge_rev=edge_rev
            )

        if direction in ("downstream", "both"):
            downstream = _bfs_downstream(
                self, affected_names, max_depth, scope, edge_fwd=edge_fwd
            )

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

    def get_file_overview(self, file_path: str, repo: str | None = None) -> dict:
        """Get all symbols, imports, and class structure for a file."""
        scope = repo or self.repo

        result = query_surreal(
            """SELECT name, kind, category, filepath, line, end_line FROM symbol
               WHERE filepath = $fp AND kind = 'definition' AND repo = $repo""",
            {"fp": file_path, "repo": scope},
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

        # Use bulk edge cache instead of per-symbol queries
        edge_fwd, edge_rev = self._get_edge_cache(scope)
        scope_cache = self._get_scope_cache(scope)

        imports_fwd = edge_fwd.get("imports", {})
        imports_rev = edge_rev.get("imports", {})
        inherits_fwd = edge_fwd.get("inherits", {})

        imports = sorted(imports_fwd.get(file_path, set()))
        imported_by = sorted(imports_rev.get(file_path, set()))

        # Build class structure
        classes: dict[str, dict] = {}
        for d in file_defs:
            if d.category == "class":
                classes[d.name] = {
                    "inherits_from": sorted(inherits_fwd.get(d.name, set())),
                    "methods": [],
                }

        # Assign methods to classes via scope resolution (cache lookup)
        for d in file_defs:
            parent = scope_cache.get((d.filepath, d.line, d.name))
            if parent and parent in classes:
                classes[parent]["methods"].append(d.name)

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

            diff_data = _parse_git_diff(result.stdout)
            changed_ranges = diff_data["changes"]
            stale_files = diff_data["stale_files"]
        except Exception as e:
            return {"changed_files": [], "error": f"Failed to parse diff: {e}"}

        # Clear data for deleted/renamed files to prevent stale symbols
        if stale_files:
            _clear_repo_files(self.repo, stale_files)

        # Load edge cache once for all changed files
        edge_fwd, edge_rev = self._get_edge_cache(self.repo)
        imports_rev = edge_rev.get("imports", {})

        changed_files = []
        for entry in changed_ranges:
            filepath = entry["file"]
            symbols: list[dict] = []
            for rng in entry["ranges"]:
                symbols.extend(
                    _get_symbols_in_range(filepath, rng[0], rng[1], self.repo)
                )

            if not symbols:
                continue

            affected_names = {s["name"] for s in symbols}
            imported_by = sorted(imports_rev.get(filepath, set()))
            upstream = _bfs_upstream(
                self,
                affected_names,
                max_depth=3,
                repo=self.repo,
                edge_rev=edge_rev,
            )
            downstream = _bfs_downstream(
                self,
                affected_names,
                max_depth=3,
                repo=self.repo,
                edge_fwd=edge_fwd,
            )
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
        repo: str | None = None,
    ) -> dict:
        """Trace the execution flow from an entry-point symbol.

        Uses BFS graph traversal through call edges to build an ordered
        flow of function/method calls with depth markers.

        Args:
            entry_point: Name of the symbol to start tracing from.
            file_hint: Optional file path to disambiguate when the
                symbol exists in multiple files.
            max_depth: Maximum traversal depth (1-50, default 20).
            repo: Optional repo scope to avoid cross-repo ambiguity.

        Returns:
            Dict with entry_point, entry_definition, steps, call_chain,
            total_steps, max_depth_reached.
        """
        scope = repo or self.repo

        # Resolve entry point
        definitions = self.find_definitions(entry_point, repo=scope)
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

        # Load edge cache for O(1) callees lookup; def cache for O(1) def lookup.
        edge_fwd, _ = self._get_edge_cache(scope)
        calls_fwd = edge_fwd.get("calls", {})
        def_cache = self._get_def_cache(scope)

        # BFS traversal to build call chain
        visited: set[str] = {entry_point}
        steps: list[dict] = []
        frontier: deque[tuple[str, int]] = deque([(entry_point, 0)])

        while frontier:
            current_name, depth = frontier.popleft()
            if depth >= max_depth:
                continue

            callees = sorted(calls_fwd.get(current_name, set()))
            for callee in callees:
                if callee in visited:
                    continue
                visited.add(callee)
                callee_defs = def_cache.get(callee, [])
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

        call_chain = _build_call_chain(
            entry_point, visited, max_depth, calls_fwd=calls_fwd
        )

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


def _assert_valid_table(table: str) -> None:
    """Guard against unexpected edge-table names before SurrealQL interpolation.

    All callers use constants from *_VALID_EDGE_TABLES*, but this check makes
    the constraint explicit so future refactoring can't accidentally open an
    injection surface without the linter noticing.
    """
    if table not in _VALID_EDGE_TABLES:
        raise ValueError(
            f"Unknown edge table {table!r}. Must be one of: {sorted(_VALID_EDGE_TABLES)}"
        )


def _query_with_retry(
    query: str,
    params: dict,
    retries: int = 3,
    backoff: float = 0.5,
):
    """Execute a SurrealDB query with exponential back-off on transient failures.

    Retries up to *retries* times, doubling the sleep after each attempt.
    Does NOT retry 401/authentication errors — query_surreal already
    handles those with re-authentication, so persistent 401s indicate
    a problem that retrying won't fix.
    Raises the last exception if all retries are exhausted.
    """
    import time as _time

    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            return query_surreal(query, params)
        except RuntimeError:
            # Circuit breaker tripped or init error — retrying won't help
            raise
        except Exception as exc:
            last_exc = exc
            err_msg = str(exc)
            # Don't retry auth errors — query_surreal already tried re-auth
            if (
                "401" in err_msg
                or "Unauthorized" in err_msg
                or "circuit breaker" in err_msg.lower()
            ):
                raise
            if attempt < retries - 1:
                _time.sleep(backoff * (2**attempt))
            else:
                logger.warning(
                    "Query failed after %d attempt(s): %s — %s",
                    retries,
                    query[:80],
                    exc,
                )
    raise last_exc  # type: ignore[misc]


def _build_call_chain(
    name: str,
    visited: set[str],
    max_depth: int,
    depth: int = 0,
    path_visited: set[str] | None = None,
    repo: str = "",
    calls_fwd: dict[str, set[str]] | None = None,
) -> dict:
    """Recursively build a nested call chain for trace_flow.

    *path_visited* tracks the current recursion path to prevent cycles
    within a single branch and deduplicate shared callees.
    """
    if path_visited is None:
        path_visited = set()
    if depth >= max_depth or name in path_visited:
        return {"name": name, "callees": [], "truncated": True}

    path_visited.add(name)
    if calls_fwd is not None:
        callees = sorted(calls_fwd.get(name, set()))
    else:
        callees = sorted(_get_edge_targets(name, "calls", repo))
    result = {
        "name": name,
        "callees": [
            _build_call_chain(
                c,
                visited,
                max_depth,
                depth + 1,
                path_visited,
                repo,
                calls_fwd=calls_fwd,
            )
            for c in callees
            if c in visited
        ],
    }
    path_visited.discard(name)
    return result


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
    depth: int = 0,
    max_depth: int = 200,
) -> None:
    """Recursively walk AST looking for named definition nodes.

    Depth-limited to avoid hitting Python's recursion limit on deeply
    nested source files.
    """
    if depth >= max_depth:
        logger.warning(
            "_walk_def_nodes reached max depth %d, truncating AST walk", max_depth
        )
        return
    if node.type in definable_types:
        name = _extract_node_name(node, source)
        if name:
            entries.append((node.start_byte, node.end_byte, name))
    for child in node.children:
        _walk_def_nodes(child, source, definable_types, entries, depth + 1, max_depth)


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
    """Find the name of the definition that encloses *node* using bisect-based
    lookup with backward linear scan.

    *def_map* is sorted by start_byte.  Returns the tightest enclosing definition
    (smallest byte range) that contains *node*.
    """
    node_start = node.start_byte
    node_end = node.end_byte

    # Binary search to find the rightmost candidate whose start ≤ node_start
    starts = [e[0] for e in def_map]
    idx = bisect.bisect_right(starts, node_start) - 1

    best_name: str | None = None
    best_size: int | None = None

    # Scan backwards — enclosing defs must have start ≤ node_start. A def with
    # an earlier start byte could still span a larger range, so we cannot break
    # early: we keep decrementing until idx < 0.
    while idx >= 0:
        def_start, def_end, name = def_map[idx]
        if def_start <= node_start and node_end <= def_end:
            size = def_end - def_start
            if best_size is None or size < best_size:
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


def _upsert_symbols(tags: list[Tag], repo: str = "") -> None:
    """Upsert symbol records into SurrealDB, with in-batch dedup.

    Uses UPSERT with deterministic record IDs so re-indexing the same
    commit updates existing rows instead of creating duplicates.
    """
    seen: set[tuple[str, str, str, str, int]] = set()
    batch: list[dict] = []
    for t in tags:
        key = (t.name, t.kind, t.category, t.filepath, t.line)
        if key in seen:
            continue
        seen.add(key)

        # Deterministic ID: SHA-256 of the composite key guarantees uniqueness
        # even for paths that differ only in separators (e.g. src/foo.py vs
        # src_foo_py). The first 40 hex chars give 160-bit collision resistance.
        # DO NOT revert to string-replacement: it silently collides on paths
        # that share a common prefix after separator normalisation.
        raw_key = f"{repo}:{t.filepath}:{t.kind}:{t.name}:{t.line}"
        record_id = hashlib.sha256(raw_key.encode()).hexdigest()[:40]
        batch.append(
            {
                "id": record_id,
                "name": t.name,
                "kind": t.kind,
                "category": t.category,
                "filepath": t.filepath,
                "line": t.line,
                "end_line": t.end_line or t.line,
                "language": _lang_from_filepath(t.filepath),
                "repo": repo,
                "content": "",
            }
        )
        if len(batch) >= 500:
            try:
                _query_with_retry(
                    "INSERT INTO symbol $records ON DUPLICATE KEY UPDATE "
                    "name = $input.name, kind = $input.kind, "
                    "category = $input.category, filepath = $input.filepath, "
                    "line = $input.line, end_line = $input.end_line, "
                    "language = $input.language, repo = $input.repo, "
                    "content = $input.content",
                    {"records": batch},
                )
            except Exception as e:
                logger.error("Batch upsert of %d symbols failed: %s", len(batch), e)
            batch.clear()

    if batch:
        try:
            _query_with_retry(
                "INSERT INTO symbol $records ON DUPLICATE KEY UPDATE "
                "name = $input.name, kind = $input.kind, "
                "category = $input.category, filepath = $input.filepath, "
                "line = $input.line, end_line = $input.end_line, "
                "language = $input.language, repo = $input.repo, "
                "content = $input.content",
                {"records": batch},
            )
        except Exception as e:
            logger.error("Final batch upsert of %d symbols failed: %s", len(batch), e)


def _upsert_relationships(relationships: list[Relationship], repo: str = "") -> None:
    """Batch upsert relationships as SurrealDB graph edges.

    Fetches all definition IDs in a single query, then resolves edges
    in-memory and inserts them in batches.
    """
    import time

    if not relationships:
        return

    t0 = time.monotonic()

    # -- 1. Fetch all definitions for this repo in one query ---------------
    t1 = time.monotonic()
    try:
        result = query_surreal(
            "SELECT id, name, filepath FROM symbol "
            "WHERE kind = 'definition' AND repo = $repo",
            {"repo": repo},
        )
        rows = _raw_result_rows(result)
    except Exception as e:
        logger.warning("Failed to fetch definitions for edge resolution: %s", e)
        return
    logger.debug("Fetched %d definitions in %.1fs", len(rows), time.monotonic() - t1)

    # Build lookup: (name, filepath) -> id  and  name -> id (first match)
    by_name_file: dict[tuple[str, str], str] = {}
    by_name_only: dict[str, str] = {}
    for row in rows:
        rid = row.get("id")
        name = row.get("name", "")
        filepath = row.get("filepath", "")
        if not rid or not name:
            continue
        by_name_file[(name, filepath)] = rid
        if name not in by_name_only:
            by_name_only[name] = rid

    # -- 2. Resolve edges and group by kind --------------------------------
    by_kind: dict[str, list[dict]] = {}
    skipped_no_src = 0
    skipped_no_tgt = 0
    skipped_self = 0
    for rel in relationships:
        src_key = (rel.source_name, rel.source_file)
        src_id = by_name_file.get(src_key)
        if not src_id:
            skipped_no_src += 1
            continue

        tgt_id: str | None = None
        if rel.target_file:
            tgt_id = by_name_file.get((rel.target_name, rel.target_file))
        if tgt_id is None:
            tgt_id = by_name_only.get(rel.target_name)

        if not tgt_id:
            skipped_no_tgt += 1
            continue
        if src_id == tgt_id:
            skipped_self += 1
            continue

        edge = {"in": src_id, "out": tgt_id, "source_line": rel.source_line}
        by_kind.setdefault(rel.kind, []).append(edge)

    total_edges = sum(len(v) for v in by_kind.values())
    logger.debug(
        "Resolved %d edges (%d no-src, %d no-tgt, %d self-loop) in %.1fs",
        total_edges,
        skipped_no_src,
        skipped_no_tgt,
        skipped_self,
        time.monotonic() - t0,
    )

    # -- 3. Batch insert edges per kind ------------------------------------
    t2 = time.monotonic()
    batch_size = 500
    for kind, edges in by_kind.items():
        _assert_valid_table(kind)
        for i in range(0, len(edges), batch_size):
            batch = edges[i : i + batch_size]
            try:
                _query_with_retry(
                    f"INSERT RELATION INTO {kind} $records",
                    {"records": batch},
                )
            except Exception as e:
                logger.warning(
                    "Batch insert of %d %s edges failed: %s. Falling back to individual inserts.",
                    len(batch),
                    kind,
                    e,
                )
                for edge in batch:
                    try:
                        _query_with_retry(
                            f"RELATE $src->{kind}->$tgt SET source_line = $line",
                            {
                                "src": edge["in"],
                                "tgt": edge["out"],
                                "line": edge["source_line"],
                            },
                        )
                    except Exception as e2:
                        logger.warning("Individual edge upsert failed: %s", e2)

    logger.info(
        "Upserted %d edges in %.1fs (total: %.1fs)",
        total_edges,
        time.monotonic() - t2,
        time.monotonic() - t0,
    )


def _get_edge_targets(name: str, edge_table: str, repo: str = "") -> set[str]:
    """Get target names from a graph edge table, scoped by repo."""
    _assert_valid_table(edge_table)
    try:
        result = query_surreal(
            f"SELECT out.name AS target FROM {edge_table} WHERE in.name = $name AND in.repo = $repo",  # nosec B608
            {"name": name, "repo": repo},
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


def _get_edge_sources(name: str, edge_table: str, repo: str = "") -> set[str]:
    """Get source names from a graph edge table, scoped by repo."""
    _assert_valid_table(edge_table)
    try:
        result = query_surreal(
            f"SELECT in.name AS source FROM {edge_table} WHERE out.name = $name AND out.repo = $repo",  # nosec B608
            {"name": name, "repo": repo},
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


def _get_all_repo_edges(edge_table: str, repo: str) -> dict[str, set[str]]:
    """Bulk-fetch ALL edges for an edge table scoped to a repo.

    Returns a forward mapping: {source_name: {target_name, ...}}.
    Build the reverse mapping in-memory from the same data.
    """
    _assert_valid_table(edge_table)
    try:
        result = query_surreal(
            f"SELECT in.name AS src, out.name AS tgt FROM {edge_table}"  # nosec B608
            " WHERE in.repo = $repo",
            {"repo": repo},
        )
        rows = _raw_result_rows(result)
        forward: dict[str, set[str]] = {}
        for r in rows:
            src = r.get("src", "")
            tgt = r.get("tgt", "")
            if src and tgt:
                forward.setdefault(src, set()).add(tgt)
        return forward
    except Exception as e:
        logger.warning("Bulk edge fetch failed for '%s': %s", edge_table, e)
        return {}


def _get_all_scope_edges(repo: str) -> dict[tuple[str, int, str], str]:
    """Bulk-fetch ALL contains_edge records for a repo.

    Returns {(filepath, line, name): parent_name}.
    """
    try:
        result = query_surreal(
            "SELECT in.name AS child, in.filepath AS fp, in.line AS line,"
            " out.name AS parent FROM contains_edge WHERE in.repo = $repo",
            {"repo": repo},
        )
        rows = _raw_result_rows(result)
        scope_map: dict[tuple[str, int, str], str] = {}
        for r in rows:
            child = r.get("child", "")
            fp = r.get("fp", "")
            line = r.get("line")
            parent = r.get("parent", "")
            if child and fp and line is not None and parent:
                scope_map[(fp, int(line), child)] = parent
        return scope_map
    except Exception as e:
        logger.warning("Bulk scope fetch failed: %s", e)
        return {}


def _resolve_scope(tag: Tag, repo: str = "") -> str | None:
    """Resolve the enclosing class scope for a symbol using SurrealDB."""
    try:
        result = query_surreal(
            """SELECT out.name AS parent FROM contains_edge
               WHERE in.filepath = $fp AND in.line = $line
               AND in.name = $name AND in.repo = $repo""",
            {"fp": tag.filepath, "line": tag.line, "name": tag.name, "repo": repo},
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


def _bfs_upstream(
    symbol_index,
    seed_names: set[str],
    max_depth: int,
    repo: str = "",
    edge_rev: dict[str, dict[str, set[str]]] | None = None,
) -> dict:
    """BFS traversal upstream — who depends on us (callers)."""
    if edge_rev is None:
        edge_rev = symbol_index._get_edge_cache(repo)[1]
    callers_rev = edge_rev.get("calls", {})

    impact: dict[int, list[dict]] = {}
    visited: set[str] = set(seed_names)
    current = set(seed_names)
    def_cache = symbol_index._get_def_cache(repo)

    for depth in range(1, max_depth + 1):
        next_level: set[str] = set()
        level_items: list[dict] = []

        for name in current:
            for source in callers_rev.get(name, set()):
                if source not in visited:
                    visited.add(source)
                    next_level.add(source)
                    for d in def_cache.get(source, []):
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


def _bfs_downstream(
    symbol_index,
    seed_names: set[str],
    max_depth: int,
    repo: str = "",
    edge_fwd: dict[str, dict[str, set[str]]] | None = None,
) -> dict:
    """BFS traversal downstream — what we depend on (callees)."""
    if edge_fwd is None:
        edge_fwd = symbol_index._get_edge_cache(repo)[0]
    calls_fwd = edge_fwd.get("calls", {})

    impact: dict[int, list[dict]] = {}
    visited: set[str] = set(seed_names)
    current = set(seed_names)
    def_cache = symbol_index._get_def_cache(repo)

    for depth in range(1, max_depth + 1):
        next_level: set[str] = set()
        level_items: list[dict] = []

        for name in current:
            for target in calls_fwd.get(name, set()):
                if target not in visited:
                    visited.add(target)
                    next_level.add(target)
                    for d in def_cache.get(target, []):
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


def _is_commit_indexed(commit_hash: str, repo: str = "") -> bool:
    """Check if the given commit is already indexed for this repo."""
    if not commit_hash or not repo:
        return False
    try:
        result = query_surreal(
            "SELECT repo_commit FROM _schema_meta WHERE repo_commit = $hash LIMIT 1",
            {"hash": commit_hash},
        )
        rows = _raw_result_rows(result)
        return bool(rows and rows[0].get("repo_commit") == commit_hash)
    except Exception as e:
        logger.debug("Commit index check failed for %s: %s", commit_hash, e)
        return False


def _mark_commit_indexed(commit_hash: str, repo: str = "") -> None:
    """Record the indexed commit hash in a repo-specific metadata record."""
    if not commit_hash or not repo:
        return
    try:
        # Deterministic record ID — one row per repo, upserted on re-index.
        record_id = repo.replace("/", "_").replace(".", "_").replace("-", "_")
        _query_with_retry(
            "INSERT INTO _schema_meta {id: $rid, version: $ver, "
            "repo_commit: $hash}"
            " ON DUPLICATE KEY UPDATE repo_commit = $hash, version = $ver",
            {"rid": record_id, "ver": SCHEMA_VERSION, "hash": commit_hash},
        )
    except Exception as e:
        logger.warning("Failed to mark commit indexed: %s", e)


def _clear_repo_data(repo: str = "") -> None:
    """Remove code intelligence data for a specific repo.

    Deletes all graph edges and symbol definitions scoped to the given repo.
    """
    import time

    t0 = time.monotonic()

    # Clear edges using dot-notation (fast on SurrealDB v2).
    edge_tables = ["calls", "imports", "inherits", "contains_edge"]
    for table in edge_tables:
        _assert_valid_table(table)
        try:
            result = _query_with_retry(
                f"DELETE FROM {table} WHERE in.repo = $repo",  # nosec B608
                {"repo": repo},
            )
            rows = _raw_result_rows(result)
            count = len(rows)
            logger.debug("Cleared %d edges from %s for repo %s", count, table, repo)
        except Exception as e:
            logger.warning("Failed to clear table '%s' during re-index: %s", table, e)

    # Clear ALL symbols for this repo
    try:
        result = _query_with_retry(
            "DELETE FROM symbol WHERE repo = $repo",
            {"repo": repo},
        )
        rows = _raw_result_rows(result)
        count = len(rows)
        logger.debug("Cleared %d symbols for repo %s", count, repo)
    except Exception as e:
        logger.warning("Failed to clear symbols during re-index: %s", e)

    logger.info("Cleared repo data for %s in %.1fs", repo, time.monotonic() - t0)


def _clear_repo_files(repo: str, files: list[str]) -> None:
    """Remove code intelligence data for specific files within a repo.

    Used for incremental re-indexing: only deletes data for the changed
    files, leaving everything else intact.
    """
    edge_tables = ["calls", "imports", "inherits", "contains_edge"]
    for filepath in files:
        # Delete edges where the source symbol is in this file
        for table in edge_tables:
            _assert_valid_table(table)
            try:
                _query_with_retry(
                    f"DELETE FROM {table} WHERE in.filepath = $fp AND in.repo = $repo",  # nosec B608
                    {"fp": filepath, "repo": repo},
                )
            except Exception as e:
                logger.debug(
                    "Edge delete from %s for file %s failed: %s", table, filepath, e
                )

        # Delete symbol definitions for this file (no embedding)
        try:
            _query_with_retry(
                "DELETE FROM symbol WHERE filepath = $fp AND repo = $repo",
                {"fp": filepath, "repo": repo},
            )
        except Exception as e:
            logger.debug("Symbol delete for file %s failed: %s", filepath, e)

    logger.info("Cleared %d files for repo %s", len(files), repo)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_head_commit(repo_path: Path) -> str:
    """Get the HEAD commit hash from a git worktree.

    Tries ``git rev-parse HEAD`` first (correct for worktrees, shallow clones,
    and packed refs) and falls back to direct file-reading only when the git
    subprocess is unavailable.
    """
    import subprocess

    # Fast, always-correct path: ask git directly
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_path),
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception as e:
        logger.debug("git rev-parse HEAD unavailable, falling back to file read: %s", e)

    # Fallback: read git internals directly (fast but fragile on worktrees,
    # shallow clones, and repos with packed refs)
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

    logger.warning(
        "Could not resolve HEAD commit hash — returning 'unknown'."
        " This may trigger unnecessary full re-indexing."
    )
    return "unknown"


def _safe_repo_name(repo: str) -> str:
    """Convert a repo slug to a safe key for cache/index naming."""
    return sanitize_repo_key(repo)


def _parse_git_diff(diff_output: str) -> dict:
    """Parse git diff --unified=0 output to extract changed file + line ranges.

    Also detects deleted or renamed files by tracking '--- a/' vs '+++ b/'.
    Returns:
        dict: {
            "changes": [{"file": str, "ranges": [(int, int), ...]}],
            "stale_files": [str, ...]
        }
    """
    changes: list[dict] = []
    stale_files: list[str] = []
    current_src_file: str | None = None
    current_dst_file: str | None = None
    ranges: list[tuple[int, int]] = []

    def flush():
        nonlocal current_dst_file, ranges
        if current_dst_file and ranges:
            changes.append({"file": current_dst_file, "ranges": ranges})
        current_dst_file = None
        ranges = []

    for line in diff_output.splitlines():
        if line.startswith("--- a/"):
            flush()
            current_src_file = line[6:]
        elif line.startswith("+++ b/"):
            current_dst_file = line[6:]
            # If the source file differs from destination, the source is stale
            if current_src_file and current_src_file != current_dst_file:
                stale_files.append(current_src_file)
            current_src_file = None
        elif line.startswith("@@") and current_dst_file:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                ranges.append((start, start + count - 1))

    flush()
    # Handle the case where a file was deleted but not renamed (no +++ b/ line)
    if current_src_file and not current_dst_file:
        stale_files.append(current_src_file)

    return {"changes": changes, "stale_files": stale_files}


def _get_symbols_in_range(
    filepath: str, start_line: int, end_line: int, repo: str = ""
) -> list[dict]:
    """Get symbol definitions that overlap with a line range."""
    try:
        result = query_surreal(
            """SELECT name, line, end_line, category FROM symbol
               WHERE filepath = $fp AND kind = 'definition'
               AND line <= $el AND end_line >= $sl AND repo = $repo""",
            {"fp": filepath, "sl": start_line, "el": end_line, "repo": repo},
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
