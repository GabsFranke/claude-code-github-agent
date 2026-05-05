---
name: "codebase-context"
description: "Use this skill whenever you need to explore, search, or understand code — including code reviews, PR analysis, architecture review, finding definitions or references, tracing call graphs and dependencies, assessing the impact of changes, discovering API routes or MCP tools, searching by concept, or understanding file structure. Always trigger before reading files directly — the tools here (read_file_summary, get_file_overview, search_codebase, find_definitions, find_references, get_context, trace_flow, get_impact, detect_changes, get_routes_map, get_tools_map) are more efficient than sequential Read calls. Trigger for any task involving code exploration, context gathering, understanding how code is connected, or assessing change risk."
---

# Codebase Context Tools

You have three categories of tools available. Use them in order — start cheap, escalate only when needed.

## Structural Map (always available)

When your session starts, you receive a **repomap** — a compact structural overview of the entire codebase injected into your system prompt. It shows ranked definitions (classes, functions, methods) with line numbers, like a table of contents:

```
shared/sdk_factory.py:
  32│ class SDKOptionsBuilder
  58│ function build_options
  124│ method with_memory_mcp
  202│ method with_codebase_tools
services/sandbox_executor/sandbox_worker.py:
  25│ function main
  142│ function process_job
```

Generated via tree-sitter parsing and reference graph ranking, so the most important/referenced definitions appear first. Use it to:

- Quickly locate where things are defined before reading files
- Understand the overall shape of the codebase
- Know which files matter most for a given task

The repomap is personalized toward files relevant to your task (e.g., changed files in a PR review).

## Lookup Tools (always available)

These tools work immediately — no index beyond what the server builds on startup.

### `find_definitions(symbol_name)`

Find where a class, function, or method is defined. Returns file, line, kind, signature, and end line.

```
find_definitions(symbol_name="SDKOptionsBuilder")

# Returns: [{file, line, kind, signature, end_line}]
```

### `find_references(symbol_name)`

Find all references to a symbol across the codebase. Excludes definition lines. Returns file, line, and surrounding context.

```
find_references(symbol_name="generate_structural_context")

# Returns: [{file, line, context}]
```

### `search_codebase(pattern, file_type?, max_results?)`

Regex search via ripgrep. Supports file type filtering (python, js, ts, go, rust, java, ruby, c, cpp). Always available in text mode.

```
search_codebase(pattern="TODO|FIXME", file_type="python")
search_codebase(pattern="agent:\\w+:\\w+")
```

For semantic and hybrid search modes, see Discovery Tools below.

### `read_file_summary(file_path, max_lines?)`

Compact file overview: docstring, imports, and all class/function signatures with line numbers. Skips implementation bodies — typically 10-20% of original file size.

```
read_file_summary(file_path="shared/chunker.py")

# Returns: {file, language, docstring, imports, signatures, total_lines}
```

### `get_file_overview(file_path)`

All symbols, imports, and class structure for a file. More comprehensive than `read_file_summary` — includes what files import this one and class hierarchies.

```
get_file_overview(file_path="services/sandbox_executor/sandbox_worker.py")

# Returns: {definitions, imports, imported_by, classes}
```

## Graph Intelligence Tools (needs SurrealDB graph index)

These use the code graph (call edges, import edges, inheritance). If the index hasn't been built yet, they return empty or minimal results — fall back to Lookup Tools.

### `get_context(symbol_name, file_hint?)`

360-degree view of a symbol: where it's defined, what calls it, what it calls, its inheritance hierarchy, and enclosing scope. If the name is ambiguous (multiple definitions), returns a disambiguation list first. Use `file_hint` to resolve ambiguity.

```
get_context(symbol_name="process_job", file_hint="services/sandbox_executor/sandbox_worker.py")

# Returns: {definition, scope, calls, called_by, inherits_from, inherited_by}
```

### `trace_flow(entry_point, file_hint?, max_depth?)`

BFS traversal through the call graph from an entry point. Returns ordered list of reachable calls with depth markers and a nested call chain.

```
trace_flow(entry_point="Application.run", max_depth=5)

# Returns: {entry_point, entry_definition, steps, call_chain, total_steps, max_depth_reached}
```

### `get_impact(file_path, start_line?, end_line?, max_depth?, direction?)`

Blast radius analysis via BFS on the call graph. Includes risk assessment (low/medium/high).

- `max_depth`: 1-10 (default 3)
- `direction`: "upstream" (who depends on us), "downstream" (what we depend on), or "both" (default)

```
get_impact(file_path="shared/repomap.py", direction="upstream")

# Returns: {symbols_in_range, upstream_impact, downstream_impact, imported_by, risk_level, risk_summary}
```

### `detect_changes(scope?)`

Parses `git diff` (staged or unstaged), maps changed lines to symbols in the index, and runs impact analysis on each affected symbol. Use before committing or during code review.

```
detect_changes(scope="staged")

# Returns: {changed_files, summary, risk_level}
```

## Discovery Tools

### `search_codebase(pattern, search_type="semantic"|"hybrid", max_results?, file_type?, kind_filter?)`

Semantic mode embeds the query via Gemini and searches SurrealDB's HNSW vector index. Hybrid mode merges text + semantic results with deduplication. Needs `GEMINI_API_KEY` — returns error dict if unavailable.

Use for conceptual searches where you don't know exact names:

```
search_codebase(pattern="how does the queue handle retries", search_type="semantic")
search_codebase(pattern="authentication flow", file_type="python", search_type="semantic")
search_codebase(pattern="configuration settings", search_type="semantic", kind_filter="class")
search_codebase(pattern="retry", search_type="hybrid")
```

Good queries vs bad queries:

- Good: "error handling for embedding API rate limits"
- Good: "where is the job queue consumer logic"
- Bad: "code" (too vague)
- Bad: "process_job" (use `find_definitions` instead for exact names)

### `get_routes_map(framework?)`

Extracts API route definitions from FastAPI, Flask, and Django files. Returns path, HTTP method, handler function, framework, and file location.

```
get_routes_map(framework="fastapi")

# Returns: [{path, method, handler, filepath, line, framework, description}]
```

### `get_tools_map()`

Discovers all MCP tool definitions in the codebase. Returns tool names, descriptions, and required parameters from MCP server schemas. No parameters.

```
get_tools_map()

# Returns: [{name, description, server_file, server_name, required_params}]
```

## When to Use Each Tool

| Task | Tool | Notes |
|------|------|-------|
| Understand overall codebase shape | Repomap (system prompt) | Free, always available. Start here. |
| Find where a symbol is defined | `find_definitions` | Exact symbol name required. |
| Find all usages of a symbol | `find_references` | Excludes definition lines. |
| Understand a file's API surface | `read_file_summary` | Signatures only, skips bodies. |
| See all symbols/deps in a file | `get_file_overview` | Includes imports, cross-references, class hierarchies. |
| Search for a string or pattern | `search_codebase` (text) | Regex/ripgrep. Always available. |
| Find code by concept or behavior | `search_codebase` (semantic/hybrid) | Natural language. Needs GEMINI_API_KEY. |
| Understand a symbol's full role | `get_context` | 360 view: definition, callers, callees, inheritance. |
| Trace execution flow from entry | `trace_flow` | BFS call graph with depth markers. |
| Assess impact of a change | `get_impact` | Blast radius via BFS. Risk: low/medium/high. |
| See what pending changes affect | `detect_changes` | Git diff analysis. Staged or unstaged. |
| Discover API routes | `get_routes_map` | FastAPI, Flask, Django. Optional framework filter. |
| Discover MCP tools | `get_tools_map` | No parameters. Lists all tool definitions. |

## Recommended Workflow

**For an unfamiliar codebase or module:**

1. **Scan the repomap** in your system prompt to understand the overall structure
2. **`read_file_summary`** on files that look relevant to understand their API surface
3. **`get_file_overview`** for richer structural context when you need symbol-level detail
4. **`find_definitions`** / **`find_references`** to trace specific symbols and their usage
5. **`get_context`** for a 360-degree view of a symbol's role and relationships
6. **`trace_flow`** to understand execution paths before modifying a function
7. **`search_codebase`** (text for patterns, semantic for concepts) when you need to discover
8. **`Read`** full files only when implementation details are required

**Before committing or when reviewing changes:**

1. **`detect_changes`** to identify affected symbols
2. **`get_impact`** on affected files to assess downstream risk
3. **`get_context`** on high-risk symbols to understand their full dependency surface

**For API exploration:**

- **`get_routes_map`** to see the full API surface before drilling into specific handlers

This progression minimizes token usage — you only read full files when you know you need them.

## Technical Details

- **Tree-sitter support**: 10 languages (Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, Ruby) with regex fallback for others
- **Repomap ranking**: reference graph ranking with personalization toward task-relevant files
- **Code graph**: Relationships (calls, imports, inheritance) extracted from tree-sitter ASTs and stored as graph edges in SurrealDB
- **Embedding model**: `gemini-embedding-001` with 1024-dimensional vectors stored in SurrealDB HNSW vector index
- **Indexing**: Incremental via git diff with Redis embedding cache to avoid re-embedding unchanged content
