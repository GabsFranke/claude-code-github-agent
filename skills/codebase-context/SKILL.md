---
name: "codebase-context"
description: "Use this skill whenever you need to explore, search, or understand code — including code reviews, PR analysis, architecture review, finding definitions or references, tracing dependencies, checking for existing utilities, or understanding file structure. Always trigger before reading files directly — the tools here (read_file_summary, find_references, semantic_search, search_codebase, find_definitions) are more efficient than sequential Read calls. Trigger for any task involving code exploration, context gathering, or understanding how code is connected."
---

# Codebase Context Tools

You have three layers of codebase context available. Use them in order — start cheap, escalate only when needed.

## Layer 1: Structural Map (always available)

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

This is generated via tree-sitter parsing and PageRank ranking, so the most important/referenced definitions appear first. Use it to:

- Quickly locate where things are defined before reading files
- Understand the overall shape of the codebase
- Know which files matter most for a given task

The repomap is personalized toward files relevant to your task (e.g., changed files in a PR review), so definitions in those files are ranked higher.

## Layer 2: Code Tools MCP (always available)

The `codebase_tools` MCP server provides four tools for structured code exploration. These are more token-efficient than raw Bash grep because they return structured, minimal results.

### `find_definitions(symbol_name)`

Find where a class, function, or method is defined. Returns file, line, kind (class/function/method), signature, and end line.

Use when you need to locate a symbol but only know its name.

```
# Find where SDKOptionsBuilder is defined
find_definitions(symbol_name="SDKOptionsBuilder")

# Returns:
# [{"file": "shared/sdk_factory.py", "line": 32, "kind": "class",
#   "signature": "class SDKOptionsBuilder:", "end_line": 280}]
```

### `find_references(symbol_name)`

Find all references to a symbol across the codebase. Excludes definition lines. Returns file, line, and surrounding context.

Use to trace how a symbol is used, what depends on it, or understand its role in the system.

```
# Find everywhere generate_structural_context is called
find_references(symbol_name="generate_structural_context")

# Returns:
# [{"file": "services/sandbox_executor/sandbox_worker.py", "line": 343,
#   "context": "file_tree_text, repomap_text = await generate_structural_context("}]
```

### `search_codebase(pattern, file_type?, max_results?)`

Regex search across the codebase. Returns structured results with file, line, matched text, and context. Supports file type filtering (python, js, ts, go, rust, java, ruby, c, cpp).

Use when you need to search for patterns, error strings, config keys, or anything regex-based.

```
# Find all TODO comments in Python files
search_codebase(pattern="TODO", file_type="python", max_results=10)

# Find Redis key patterns
search_codebase(pattern="agent:\\w+:\\w+")
```

### `read_file_summary(file_path, max_lines?)`

Read a compact summary of a file: docstring, imports, and all class/function signatures with line numbers. Skips implementation bodies — typically 10-20% of original file size.

Use to quickly understand a file's API surface without reading the full content. Good for deciding whether a file is relevant before committing tokens to read it fully.

```
# Get an overview of the chunker module
read_file_summary(file_path="shared/chunker.py")

# Returns: docstring, imports list, and all function/class signatures
# {"docstring": "Tree-sitter-based semantic code chunker...",
#  "imports": ["import logging", "from pathlib import Path", ...],
#  "signatures": [{"name": "Chunk", "kind": "class", "line": 36, ...}, ...]}
```

## Layer 3: Semantic/Hybrid Search (conditional)

`search_codebase` with `search_type="semantic"` or `search_type="hybrid"` provides embedding-based search via Gemini + SurrealDB HNSW vector index. It understands natural language queries and finds semantically similar code, even when exact keywords don't match.

**Only available when the indexing worker has indexed the repository** (requires `GEMINI_API_KEY`). If calls return empty results, the repo hasn't been indexed yet — fall back to Layers 1 and 2.

### `search_codebase(pattern, search_type="semantic"|"hybrid", max_results?, file_type?, kind_filter?)`

Semantic mode embeds the query and searches SurrealDB's vector index. Hybrid mode combines both text + semantic with deduplication.

Use for conceptual searches where you don't know exact names:

```
# Find how retries are handled
search_codebase(pattern="how does the queue handle retries", search_type="semantic")

# Find authentication-related code, only in Python
search_codebase(pattern="authentication flow", file_type="python", search_type="semantic")

# Find only class definitions related to configuration
search_codebase(pattern="configuration settings", search_type="semantic", kind_filter="class")

# Hybrid: regex + semantic
search_codebase(pattern="retry", search_type="hybrid")
```

Good queries vs bad queries:

- Good: "how does the indexing worker create worktrees from bare repos"
- Good: "error handling for embedding API rate limits"
- Good: "where is the job queue consumer logic"
- Bad: "code" (too vague)
- Bad: "process_job" (use `find_definitions` instead for exact names)

## When to Use Each Layer

| Task | Layer | Tool |
|------|-------|------|
| Quick overview of a file | 2 | `read_file_summary` |
| Find where something is defined | 2 | `find_definitions` |
| Find all usages of a symbol | 2 | `find_references` |
| Search for a string or pattern | 2 | `search_codebase` (text mode) |
| Find code by concept/behavior | 3 | `search_codebase` (semantic/hybrid) |
| Understand codebase structure | 1 | Read the repomap in system prompt |
| Explore an unfamiliar module | 1 → 2 | Repomap → `read_file_summary` on interesting files |

## Recommended Workflow

For an unfamiliar codebase or module:

1. **Scan the repomap** in your system prompt to understand the overall structure
2. **`read_file_summary`** on files that look relevant to understand their API surface
3. **`find_definitions`** / **`find_references`** to trace specific symbols and dependencies
4. **`search_codebase` with `search_type="semantic"`** for conceptual exploration when you don't know exact names
5. **`Read`** the full file only when you need implementation details

This progression minimizes token usage — you only read full files when you know you need them.

## Technical Details

- **Tree-sitter support**: 10 languages (Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, Ruby) with regex fallback for others
- **Repomap ranking**: PageRank on reference graph with personalization toward task-relevant files
- **Embedding model**: `gemini-embedding-001` with 1024-dimensional vectors stored in SurrealDB
- **Indexing**: Incremental via git diff with Redis embedding cache to avoid re-embedding unchanged content
