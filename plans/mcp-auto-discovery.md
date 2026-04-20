# Plan: MCP Server Auto-Discovery

## Context

Adding a new MCP server today requires editing `shared/sdk_factory.py` (adding a `with_*_mcp()` method, updating toolsets) and touching multiple worker files. This is the only extensibility point in the project that still requires Python code changes — plugins, workflows, and repo setup are all declarative or convention-based.

Goal: Drop a new `mcp_servers/my_server/server.py`, rebuild Docker, done.

## Architecture Decision

**All MCP servers live in `mcp_servers/`.** No MCP servers inside plugin directories. Plugins declare which MCP tools they need via their own `allowed_tools` config (standard Claude Code plugin feature). This keeps a clean separation:
- `mcp_servers/` = shared infrastructure, auto-discovered
- `plugins/` = agents, commands, skills, hooks — they consume MCP tools but don't own them

All tools from all servers are available via `mcp__<name>__*` wildcard. If access needs to be restricted, handle it via instructions/prompts, not config.

## Approach

### 1. New file: `shared/mcp_discovery.py`

A small module that scans `mcp_servers/` and returns structured server configs.

**`MCPServerConfig`** (Pydantic model):
```python
class MCPServerConfig(BaseModel):
    name: str                    # directory name (e.g. "codebase_tools")
    server_path: str             # absolute path to server.py
    env: dict[str, str] = {}     # from mcp.json or defaults
```

**`discover_stdio_servers(base_path: str) -> list[MCPServerConfig]`**:
- List directories in `base_path/` (skip `__pycache__`, hidden dirs, `base.py`, `__init__.py`)
- For each dir containing `server.py`, build a config:
  - Read optional `mcp.json` for custom env vars
  - If no `mcp.json`, use defaults (see below)
- Path resolution: check `/app/mcp_servers/` first (Docker), then relative to `__file__` (local dev)

### 2. Convention: `mcp.json` (optional, per server — env vars only)

`mcp_servers/semantic_search/mcp.json`:
```json
{
  "env": {
    "QDRANT_URL": "${QDRANT_URL}",
    "GEMINI_API_KEY": "${GEMINI_API_KEY}"
  }
}
```

Servers without `mcp.json` get defaults:
- `PYTHONPATH` → app root
- `REPO_PATH` → worktree path (if provided)
- `GITHUB_REPOSITORY` → repo (if provided)

**Schema** (one optional field):
```python
class MCPManifest(BaseModel):
    env: dict[str, str] = {}
```

### 3. Move github-actions MCP to `mcp_servers/`

The github-actions MCP server currently lives in `plugins/ci-failure-toolkit/servers/`. Move it to `mcp_servers/github_actions/` to follow the new convention.

- Move: `plugins/ci-failure-toolkit/servers/github_actions_server.py` → `mcp_servers/github_actions/server.py`
- Move: `plugins/ci-failure-toolkit/tools/github_actions.py` → `mcp_servers/github_actions/tools.py`
- Create: `mcp_servers/github_actions/mcp.json` with env vars (`GITHUB_TOKEN`)
- Update imports inside the server and tools files
- Remove: `plugins/ci-failure-toolkit/servers/` directory
- Update Dockerfile if it references the old path

### 4. New method on `SDKOptionsBuilder`

**`with_auto_discovered_mcp_servers(repo, worktree_path)`**:
- Calls `discover_stdio_servers()`
- Reads `mcp_servers/http.json` for HTTP servers
- For each discovered server (stdio + HTTP):
  - Resolve env vars (merge defaults + `mcp.json` env + runtime values like `repo`, `worktree_path`)
  - Interpolate `${VAR}` patterns from environment
  - Skip servers whose required env vars are unresolved (e.g. `${GITHUB_TOKEN}` when token not set)
  - Register as `self._mcp_servers[name]`
- Store discovered server names for `with_full_toolset()` to use
- Return `self` for chaining

### 5. Update `with_full_toolset()`

Replace hardcoded MCP tool patterns with auto-generated wildcard for all discovered servers:

```python
def with_full_toolset(self) -> "SDKOptionsBuilder":
    tools = [
        "Task", "Skill", "Agent", "Bash",
        "Read", "Write", "Edit", "List", "Search", "Grep", "Glob",
    ]
    # All discovered MCP servers get wildcard access
    for name in self._discovered_server_names:
        tools.append(f"mcp__{name}__*")
    return self.with_tools(*tools)
```

### 6. HTTP MCP servers via `mcp_servers/http.json`

For HTTP MCP servers that can't be auto-discovered by directory convention:

```json
{
  "github": {
    "type": "http",
    "url": "https://api.githubcopilot.com/mcp",
    "headers": {
      "Authorization": "Bearer ${GITHUB_TOKEN}"
    }
  }
}
```

`with_auto_discovered_mcp_servers()` reads this file, interpolates `${VAR}` from env, and registers HTTP servers alongside stdio ones. If `GITHUB_TOKEN` isn't set, the github server is skipped.

This replaces the hardcoded `with_github_mcp()` method.

### 7. Naming: underscores not hyphens

Current server keys use hyphens (`codebase-tools`, `semantic-search`, `github-actions`). Auto-discovery derives names from directory names which use underscores. Changing to match:
- `codebase-tools` → `codebase_tools`
- `semantic-search` → `semantic_search`
- `github-actions` → `github_actions`

Tool patterns update accordingly: `mcp__codebase_tools__*`, `mcp__semantic_search__*`, `mcp__github_actions__*`

This affects `allowed_tools` strings and any prompts referencing specific tool names. Search for these references and update them.

### 8. Update workers

**Sandbox worker** (`services/sandbox_executor/sandbox_worker.py`):
```python
# Before (6 lines):
if github_token:
    builder.with_github_mcp(github_token).with_github_actions_mcp(github_token)
builder.with_memory_mcp(repo)
builder.with_codebase_tools(workspace)
builder.with_semantic_search(repo)

# After (1 line):
builder.with_auto_discovered_mcp_servers(repo=repo, worktree_path=workspace)
```

**Memory worker** — no change (only needs memory, `with_memory_mcp()` stays).
**Retrospector worker** — no change (only needs github + memory, individual methods stay).

### 9. Remove deprecated methods

After auto-discovery is wired up, these methods on `SDKOptionsBuilder` become unused by the sandbox worker:
- `with_memory_mcp()` — **keep** (used by memory and retrospector workers)
- `with_codebase_tools()` — **remove** (only used by sandbox)
- `with_semantic_search()` — **remove** (only used by sandbox)
- `with_github_mcp()` — **keep** (used by retrospector worker)
- `with_github_actions_mcp()` — **remove** (moved to `mcp_servers/`)
- `_resolve_indexing_config()` — **remove** (conditional logic now in auto-discovery)

## Files to Create/Modify

| File | Action |
|------|--------|
| `shared/mcp_discovery.py` | **Create** — discovery logic + Pydantic models |
| `shared/sdk_factory.py` | **Modify** — add `with_auto_discovered_mcp_servers()`, update `with_full_toolset()`, remove deprecated methods |
| `mcp_servers/semantic_search/mcp.json` | **Create** — env vars |
| `mcp_servers/github_actions/server.py` | **Create** — moved from plugin |
| `mcp_servers/github_actions/tools.py` | **Create** — moved from plugin |
| `mcp_servers/github_actions/__init__.py` | **Create** — module init |
| `mcp_servers/github_actions/mcp.json` | **Create** — env vars config |
| `mcp_servers/http.json` | **Create** — GitHub HTTP MCP config |
| `services/sandbox_executor/sandbox_worker.py` | **Modify** — use new auto-discovery method |
| `services/sandbox_executor/Dockerfile` | **Modify** — remove old plugin servers reference |
| `plugins/ci-failure-toolkit/servers/` | **Remove** — moved to `mcp_servers/` |
| Worker-specific toolsets in `sdk_factory.py` | **Modify** — update tool name patterns (hyphens → underscores) |

## Out of Scope

- `agent_worker` config (`mcp_config.py`, `claude_settings.py`) — separate config path
- Memory/retrospector worker changes — they use targeted subsets, individual methods are fine

## Verification

1. **Unit test**: `discover_stdio_servers()` returns all 4 servers, reads `mcp.json` correctly
2. **Unit test**: Missing env vars → server skipped (semantic search without Qdrant, github without token)
3. **Unit test**: `with_full_toolset()` generates wildcard patterns for all discovered servers
4. **Unit test**: `http.json` interpolation works, unresolved vars cause skip
5. **Integration**: Sandbox worker builds `ClaudeAgentOptions` with all MCP servers registered
6. **Manual**: `bash ./check-code.sh` passes

## How to Add a New MCP Server (After Implementation)

1. Create `mcp_servers/my_server/server.py` (use `mcp_servers/base.py` as the server loop)
2. Optionally create `mcp_servers/my_server/mcp.json` for custom env vars
3. Rebuild Docker — done. No code changes needed.
