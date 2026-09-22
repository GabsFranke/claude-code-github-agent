---
name: "codebase-context"
description: "Use this skill whenever you need to explore, search, or understand code — including code reviews, PR analysis, architecture review, finding definitions or references, tracing call graphs and dependencies, assessing the impact of changes, discovering API routes or MCP tools, searching by concept, or understanding file structure. Always trigger before reading files directly — CodeGraph tools (codegraph_search, codegraph_context, codegraph_trace, codegraph_callees, codegraph_callers, codegraph_impact, codegraph_node, codegraph_explore, codegraph_files, codegraph_status) and built-in tools (Grep, Glob, Read) are more efficient than sequential Read calls. Trigger for any task involving code exploration, context gathering, or understanding how code is connected."
---

# Codebase Context Tools

You have CodeGraph MCP tools and built-in Claude Code tools for code exploration. Use them in order — start cheap, escalate only when needed.

## Structural Map (always available)

When your session starts, you receive a **repomap** — a compact structural overview of the entire codebase injected into your system prompt. It shows ranked definitions (classes, functions, methods) with line numbers, like a table of contents:

```
shared/sdk_factory.py:
  32│ class SDKOptionsBuilder
  58│ function build_options
 124│ method with_memory_mcp
services/sandbox_executor/sandbox_worker.py:
  25│ function main
 142│ function process_job
```

Generated via tree-sitter parsing and reference graph ranking, so the most important/referenced definitions appear first. Use it to:

- Quickly locate where things are defined before reading files
- Understand the overall shape of the codebase
- Know which files matter most for a given task

The repomap is personalized toward files relevant to your task (e.g., changed files in a PR review).

## CodeGraph MCP Tools (needs `codegraph init -i`)

These 10+ tools use the code graph (call edges, import edges, inheritance) via the CodeGraph MCP server. They run as a separate stdio MCP server (`codegraph serve --mcp`) and query a local SQLite database. If CodeGraph is not installed, these tools are simply unavailable — no errors, just no graph intelligence.

### `codegraph_search`

Find symbols by name across the codebase. Returns kind, location, and signature.

### `codegraph_context`

**Primary discovery tool.** One call returns entry points, related symbols, and code snippets. Use this first for "how does X work?" questions.

```
codegraph_context(task="how does the queue handle retries")
```

### `codegraph_trace`

Trace the call path between two symbols ("how does X reach Y"). Each hop includes the source body inline. Follows dynamic-dispatch hops that grep can't.

```
codegraph_trace(from="process_job", to="execute_sdk_loop")
```

### `codegraph_callers` / `codegraph_callees`

Walk call flow one hop at a time. `callers` = who calls this symbol. `callees` = what does this symbol call.

### `codegraph_impact`

Analyze what code is affected by changing a symbol. Blast radius analysis with depth control.

```
codegraph_impact(symbol="SDKOptionsBuilder", depth=3)
```

### `codegraph_node`

Get details about a specific symbol — signature, source code, docstring.

### `codegraph_explore`

Return source for several related symbols grouped by file, plus a relationship map, in one budget-capped call. Prefer over many `codegraph_node` calls.

### `codegraph_files`

Get indexed file structure (faster than filesystem scanning).

### `codegraph_status`

Check index health and statistics.

## When to Use Each Tool

| Task | Tool | Notes |
|------|------|-------|
| Understand overall codebase shape | Repomap (system prompt) | Free, always available. Start here. |
| Find a string or pattern | `Grep` or `codegraph_search` | Grep for literal text; codegraph for symbol names |
| Understand a file's API surface | `Read` (file summary) or `codegraph_node` | Use node for a single symbol; Read for full file |
| See what pending changes affect | `Bash(git diff)` | Staged or unstaged. |
| Find where a symbol is defined | `codegraph_search` / `codegraph_node` | Requires CodeGraph index. |
| Find all usages of a symbol | `codegraph_callers` | Requires CodeGraph index. |
| Understand a symbol's full role | `codegraph_context` | 360 view. Requires CodeGraph index. |
| Trace execution flow | `codegraph_trace` | Call path with source. Requires CodeGraph index. |
| Assess impact of a change | `codegraph_impact` | Blast radius. Requires CodeGraph index. |
| Survey related symbols' source | `codegraph_explore` | Budget-capped. Requires CodeGraph index. |

## Recommended Workflow

**For an unfamiliar codebase or module:**

1. **Scan the repomap** in your system prompt to understand the overall structure
2. **`codegraph_context`** for a 360-degree view of a symbol's role and relationships (if CodeGraph available)
3. **`codegraph_trace`** to understand execution paths before modifying a function (if CodeGraph available)
4. **`Grep`** or **`codegraph_search`** when you need to discover by pattern
5. **`Read`** full files only when implementation details are required

**Before committing or when reviewing changes:**

1. **`Bash(git diff)`** to identify affected files and line ranges
2. **`codegraph_impact`** on affected symbols to assess downstream risk (if CodeGraph available)
3. **`codegraph_context`** on high-risk symbols to understand their full dependency surface (if CodeGraph available)

This progression minimizes token usage — you only read full files when you know you need them.

## Technical Details

- **Tree-sitter support**: 10 languages (Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, Ruby) with regex fallback for others
- **Repomap ranking**: reference graph ranking with personalization toward task-relevant files
- **CodeGraph**: Runs as a separate MCP server (`codegraph serve --mcp`), indexes repos via `codegraph init -i` — local SQLite, no database cluster or API keys needed
- **Optional**: If CodeGraph is not installed, graph-oriented tools are simply unavailable. Use Grep/Glob/Read as fallback.
