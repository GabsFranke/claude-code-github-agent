# Architecture

Complete system architecture for the Claude Code GitHub Agent.

## Table of Contents

- [System Overview](#system-overview)
- [Workflow System](#workflow-system)
  - [YAML-Driven Configuration](#yaml-driven-configuration)
  - [Workflow Routing](#workflow-routing)
- [Core Components](#core-components)
  - [1. Webhook Service](#1-webhook-service)
  - [2. Worker Service (Coordinator)](#2-worker-service-coordinator)
  - [3. Repository Sync Service](#3-repository-sync-service)
  - [4. Sandbox Worker Pool](#4-sandbox-worker-pool)
  - [5. Claude Agent SDK](#5-claude-agent-sdk)
  - [6. GitHub MCP Server](#6-github-mcp-server)
  - [7. Shared Authentication Service](#7-shared-authentication-service)
  - [8. Memory Worker](#8-memory-worker)
  - [9. Retrospector Worker](#9-retrospector-worker)
  - [10. Indexing Worker](#10-indexing-worker)
  - [11. Codebase Context System](#11-codebase-context-system)
  - [12. Plugin System](#12-plugin-system)
  - [13. MCP Proxy Service](#13-mcp-proxy-service)
- [Shared Module Infrastructure](#shared-module-infrastructure)
- [Data Flow](#data-flow)
- [Job Queue Architecture](#job-queue-architecture)
- [Security](#security)
- [Subagents](#subagents)
- [See Also](#see-also)

## System Overview

```mermaid
flowchart LR
    GH[GitHub<br/>Events] --> WH[Webhook<br/>Service]

    WH --> RQ[(Redis<br/>Queues)]
    WH --> |push| SYNCQ[(Sync<br/>Queue)]

    RQ --> W[Worker<br/>Coordinator]
    SYNCQ --> RS[Repo Sync<br/>Service]

    W --> JQ[(Job<br/>Queue)]
    RS --> CACHE[(Bare Repo<br/>Cache)]

    JQ --> SW[Sandbox<br/>Workers]
    CACHE -.->|worktree| SW
    CACHE -.->|worktree| IDX
    CACHE -.->|worktree| RETRO

    SW --> |1. setup| PREP[Repo Setup +<br/>Context Gen]
    PREP --> |2. execute| AGENT[Claude SDK<br/>Execution]

    AGENT --> |GitHub API| MCP[GitHub<br/>MCP]
    AGENT --> |transcript| PP[(Post-Processing<br/>Pipeline)]
    AGENT --> MEM_MCP[Memory<br/>MCP]
    AGENT --> |SSE| MCPPROXY[MCP Proxy<br/>:18000]
    MCPPROXY --> LOCAL_MCP[Local MCP<br/>Servers]
    MCPPROXY --> |host ~/.claude.json| HOST_MCP[Host MCP<br/>Servers]
    AGENT -.->|semantic search| QDRANT

    PP --> MEMQ[(Memory<br/>Queue)]
    PP --> RETROQ[(Retrospector<br/>Queue)]
    PP --> IDXQ[(Indexing<br/>Queue)]

    MEMQ --> MW[Memory<br/>Worker]
    MW --> |memory_read/write| MEM_MCP
    MW --> MEM_VOL[(Agent<br/>Memory)]

    RETROQ --> RETRO[Retrospector<br/>Worker]
    RETRO --> |PR to bot repo| MCP

    RS --> |sync complete| IDX[Indexing<br/>Worker]
    IDXQ --> IDX
    IDX --> |embeddings| QDRANT[(Qdrant<br/>Vector DB)]

    MCP --> GH

    style GH fill:#e1f5ff
    style WH fill:#fff3e0
    style W fill:#fff3e0
    style RS fill:#f3e5f5
    style SW fill:#e8f5e9
    style PREP fill:#e0f7fa
    style AGENT fill:#e8f5e9
    style CACHE fill:#fce4ec
    style MCP fill:#e0f2f1
    style MW fill:#fff9c4
    style MEM_MCP fill:#fff9c4
    style MEM_VOL fill:#fff9c4
    style MEMQ fill:#fff9c4
    style RETRO fill:#ffe0b2
    style RETROQ fill:#ffe0b2
    style PP fill:#f3e5f5
    style IDX fill:#e8eaf6
    style QDRANT fill:#e8eaf6
    style IDXQ fill:#e8eaf6
    style LOCAL_MCP fill:#e0f2f1
```

**Architecture Flow:**

1. **GitHub** → Webhook events (PR, comments, push, CI/CD, issues, discussions)
2. **Webhook Service** → Validates signatures, matches workflows, publishes to Redis queues
3. **Worker** → Enriches events with repository context, creates jobs for sandbox
4. **Repo Sync** → Maintains cached bare repositories (proactive on push)
5. **Sandbox Workers** → Create isolated worktrees from cached bare repos
6. **Pre-Processing** → Run repo setup commands (`repo-setup.yaml`) + generate structural context (file tree + repomap)
7. **Claude SDK** → Executes with 5 MCP servers (GitHub, GitHub Actions, Memory, Codebase Tools, Semantic Search)
8. **Results** → Posted back to GitHub via MCP
9. **Post-Processing** → Transcript staging, enqueues memory/retrospector/indexing jobs
10. **Memory Worker** → Extracts knowledge from session transcripts via `@memory-extractor` subagent
11. **Retrospector Worker** → Analyzes sessions, opens improvement PRs on the bot's own repo
12. **Indexing Worker** → Chunks repos, generates embeddings, stores in Qdrant for semantic search

## Workflow System

### YAML-Driven Configuration

The system uses a declarative YAML configuration (`workflows.yaml`) as the single source of truth for all workflows. Each workflow defines triggers (events and/or commands), prompt templates, context profiles, and optional filters.

**Structure:**

```
claude-code-github-agent/
├── workflows.yaml           # Single config file - defines all workflows
├── workflows/
│   ├── __init__.py
│   └── engine.py           # WorkflowEngine - loads YAML and routes
├── prompts/
│   ├── review.md           # System context for PR reviews
│   ├── triage.md           # System context for issue triage
│   └── generic.md          # System context for generic requests
└── services/
    ├── webhook/
    │   ├── main.py              # FastAPI webhook handler
    │   ├── payload_extractor.py # Declarative payload extraction
    │   └── extraction_rules.py  # 40+ GitHub event type rules
    └── agent_worker/
        ├── worker.py                         # Receives events, enriches context
        └── processors/
            ├── request_processor.py          # Creates jobs for sandbox
            └── repository_context_loader.py  # Fetches CLAUDE.md + memory index
```

**Current workflows.yaml:**

```yaml
workflows:
  review-pr:
    triggers:
      events:
        - event: pull_request.opened
        - event: pull_request.labeled
          filters:
            label.name: ["review", "pr-review", "review-pr"]
      commands: [/review, /pr-review, /review-pr]
    prompt:
      template: "/pr-review-toolkit:review-pr {repo} {issue_number}"
    context:
      repomap_budget: 4096
      personalized: true
      include_test_files: true

  triage-issue:
    triggers:
      events:
        - event: issues.opened
        - event: issues.labeled
          filters:
            label.name: "triage"
      commands: [/triage, /triage-issue]
    prompt:
      template: "Triage issue #{issue_number} in {repo}"
      system_context: "triage.md"

  fix-ci:
    triggers:
      events:
        - event: workflow_job.completed
          filters:
            workflow_job.conclusion: "failure"
      commands: [/fix-ci, /fix-build, /fix-tests]
    prompt:
      template: "/ci-failure-toolkit:fix-ci {repo} {issue_number}"
    context:
      repomap_budget: 4096
      personalized: true
      priority_focus: ["build_system", "test_structure"]

  test-toolkit:
    triggers:
      commands: [/test]
    prompt:
      template: "/test-toolkit:test {user_query}"
    skip_self: false

  generic:
    triggers:
      commands: [/agent]
    prompt:
      template: "{user_query}"
      system_context: "generic.md"
    skip_self: false

  fix-review:
    triggers:
      events:
        - event: pull_request.labeled
          filters:
            label.name: ["fix-review", "fix-it", "pr-fix"]
      commands: [/fix-it]
    prompt:
      template: "/pr-fix:fix-review {repo} {issue_number}"
    context:
      repomap_budget: 4096
      personalized: true
      include_test_files: true
    skip_self: true
```

### Workflow Routing

**Webhook Service** (matches + routes):

- Receives GitHub events
- Extracts structured data via `PayloadExtractor` + `EXTRACTION_RULES` (40+ event types)
- Matches events/commands to workflows via `WorkflowEngine`
- Applies declarative payload filters (e.g., `workflow_job.conclusion: "failure"`)
- Enforces `skip_self` to avoid bot triggering itself
- Publishes matched jobs with `workflow_name` to Redis queue
- Push events go directly to sync queue for proactive cache warming

**Agent Worker** (enriches + dispatches):

- Receives matched events from queue
- Validates `workflow_name` against `WorkflowEngine`
- Fetches repository context (CLAUDE.md from GitHub API + memory index from local volume)
- Triggers repo sync and builds the final prompt
- Creates rich jobs in the `JobQueue` with all context

See [WORKFLOWS.md](WORKFLOWS.md) for details on creating and managing workflows.

## Core Components

### 1. Webhook Service

**Technology**: FastAPI (Python 3.12)
**Port**: 10000 (mapped from internal 8080)
**Purpose**: Receives GitHub webhook events and routes to workflows

**Responsibilities**:

- Validates webhook signatures (HMAC-SHA256)
- Parses GitHub events using declarative extraction rules (40+ event types)
- Extracts `/command` patterns from comments
- Uses `PayloadExtractor` to extract standardized fields (`issue_number`, `ref`, `user`, `extra`)
- Matches events/commands to workflows via `WorkflowEngine`
- Applies declarative payload filters and `skip_self` logic
- Publishes matched jobs to Redis queue (`agent-requests`) with pre-resolved `workflow_name`
- Publishes push events to sync queue (`agent:sync:requests`) for proactive caching
- Returns immediately (< 100ms)

**Key Files**:

- `services/webhook/main.py` — FastAPI application
- `services/webhook/payload_extractor.py` — Declarative field extraction
- `services/webhook/extraction_rules.py` — 40+ event type configurations
- `services/webhook/validators/signature_validator.py` — HMAC verification

### 2. Worker Service (Coordinator)

**Technology**: Python 3.12
**Purpose**: Lightweight job coordinator that enriches events with context

**Responsibilities**:

- Subscribes to Redis message queue (`agent-requests`)
- Validates pre-resolved `workflow_name` from webhook
- Fetches repository context (CLAUDE.md + memory index.md) via `RepositoryContextLoader`
- Triggers repo sync for the target ref
- Builds prompts from workflow templates + system context + CI failure context
- Creates jobs in `JobQueue` with full context (prompt, repo, ref, CLAUDE.md, memory, GitHub token, Langfuse span)
- Manages distributed rate limiting (GitHub/Anthropic) via Redis-backed `MultiRateLimiter`
- Maintains health checks and Langfuse observability traces

**Key Files**:

- `services/agent_worker/worker.py` — Main worker loop
- `services/agent_worker/processors/request_processor.py` — Job creation pipeline
- `services/agent_worker/processors/repository_context_loader.py` — CLAUDE.md + memory fetching
- `services/agent_worker/config/claude_settings.py` — Claude SDK settings
- `services/agent_worker/config/mcp_config.py` — MCP server configuration

### 3. Repository Sync Service

**Technology**: Python + Git
**Purpose**: Manages cached bare repository clones

**Responsibilities**:

- Maintains warm bare repository clones in `/var/cache/repos/`
- Listens to sync requests on Redis queue (`agent:sync:requests`)
- Clones new repos with full refspec (branches, tags, PR refs)
- Fetches updates for existing repositories
- Uses Redis locks to prevent concurrent syncs (configurable timeout, default 300s)
- Publishes completion/error events to `agent:sync:events` pub/sub
- Sets completion key `agent:sync:complete:{repo}:{ref}` with 1-hour TTL
- Supports GitHub App authentication for private repos

**Key Files**: `services/repo_sync/sync_worker.py`

**Cache Structure**:

```
/var/cache/repos/
└── owner/repo.git/  # Bare repository (flat structure)
```

**Sync Flow**:

```python
# Listen for sync requests
await sync_queue.subscribe(message_handler)

# On sync request
lock = redis.lock(f"agent:sync:lock:{repo}", timeout=300)
if not os.path.exists(repo_dir):
    # Initial clone
    git clone --bare https://github.com/{repo}.git {repo_dir}
    # Configure refspec for PR refs
    git --git-dir={repo_dir} config remote.origin.fetch '+refs/pull/*/head:refs/pull/*/head'
    git --git-dir={repo_dir} fetch origin
else:
    # Update existing
    git --git-dir={repo_dir} fetch origin '+refs/heads/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*' '+refs/pull/*/head:refs/pull/*/head'

# Signal completion
await redis.set(f"agent:sync:complete:{repo}:{ref}", "1", ex=3600)
await redis.publish("agent:sync:events", json.dumps({"repo": repo, "ref": ref, "status": "complete"}))
```

**Benefits**:

- Repository cloning: ~30s → ~2s (after first clone)
- Reduced GitHub API calls
- Shared cache across all workers
- Proactive cache warming on push events

### 4. Sandbox Worker Pool

**Technology**: Python 3.12 + Claude Agent SDK
**Purpose**: Executes agent requests in isolated local workspaces

**Responsibilities**:

- Pulls jobs from `JobQueue` (Redis-backed)
- Waits for repository sync completion via `wait_for_repo_sync()` (pub/sub + fast-path cache check)
- Creates isolated git worktree per job from cached bare repo (detached HEAD mode)
- Handles multiple ref formats: `refs/pull/N/head`, `refs/tags/*`, `refs/remotes/origin/*`
- Runs repository setup commands via `RepoSetupEngine` (from `repo-setup.yaml`)
- Generates structural context (file tree + personalized repomap with PageRank)
- Builds `ClaudeAgentOptions` via composable `SDKOptionsBuilder`
- Executes Claude Agent SDK with retry (configurable, default 3 attempts)
- Flushes buffered post-processing jobs (memory, retrospector, indexing)
- Cleans up workspace, worktree, and credentials

**Key Files**: `services/sandbox_executor/sandbox_worker.py`

**Workspace Isolation**:

```python
# Wait for repo sync
await wait_for_repo_sync(repo, ref, redis_client)

# Create worktree from bare repo (detached HEAD)
workspace = tempfile.mkdtemp(prefix=f"job_{job_id[:8]}_", dir="/tmp")
git --git-dir={repo_dir} worktree add --detach {workspace} {ref}

# Inject git credentials
git config credential.helper store
echo "https://x-access-token:{token}@github.com" > ~/.git-credentials
git config user.name "Claude Code Agent"
git config user.email "claude-code-agent[bot]@users.noreply.github.com"

# Generate structural context
file_tree, repomap = await generate_structural_context(
    workspace, repo, mentioned_files, mentioned_idents,
    token_budget=context_profile.repomap_budget
)

# Build SDK options and execute
builder = SDKOptionsBuilder(cwd=workspace)
options = (builder.with_model(model)
    .with_github_mcp(github_token)
    .with_memory_mcp(repo)
    .with_codebase_tools(workspace)
    .with_semantic_search(repo)
    .with_auto_discovered_plugins()
    .with_full_toolset()
    .with_structural_context(file_tree, repomap)
    .with_repository_context(claude_md, memory_index)
    .build())

result = await execute_sdk(prompt, options)

# Cleanup
git --git-dir={repo_dir} worktree remove --force {workspace}
```

**MCP Servers Available**:

| Server | Type | Purpose |
|--------|------|---------|
| GitHub MCP | HTTP (`api.githubcopilot.com/mcp`) | PR/issue/comment operations |
| GitHub Actions MCP | SSE (via mcp_proxy) | CI/CD workflow analysis |
| Memory MCP | stdio | Repository memory read/write |
| Codebase Tools MCP | SSE (via mcp_proxy) | AST-based code search and file summaries |
| Semantic Search MCP | SSE (via mcp_proxy) | Embedding-based code search via Qdrant |

**Sync Coordination**:

- Fast path: checks `agent:sync:complete:{repo}:{ref}` Redis key
- Slow path: subscribes to `agent:sync:events` pub/sub for completion notification
- 5-minute timeout for large repositories (fails job if repo_sync is down)

### 5. Claude Agent SDK

**Technology**: Python SDK by Anthropic
**Purpose**: Autonomous coding agent

**Capabilities**:

- Reads and analyzes code locally using Read, List, Search, Grep, Glob tools
- Writes and edits files locally using Write, Edit tools
- Executes bash commands using Bash tool
- Delegates to specialized subagents via Task tool
- Creates branches and commits via GitHub MCP
- Opens pull requests via GitHub MCP
- Posts comments and reviews via GitHub MCP
- Searches codebase structurally via Codebase Tools MCP
- Searches code semantically via Semantic Search MCP
- Accesses repository memory via Memory MCP

**Configuration**: Composable via `SDKOptionsBuilder` in `shared/sdk_factory.py`

**Full Toolset** (sandbox worker):

- `Task`, `Skill` — Delegate to subagents and invoke skills
- `Bash` — Execute shell commands in worktree
- `Read`, `Write`, `Edit` — File operations in worktree
- `List`, `Search`, `Grep`, `Glob` — Code exploration in worktree
- `mcp__github__*` — All GitHub MCP tools
- `mcp__github_actions__*` — CI/CD workflow tools
- `mcp__memory__*` — Repository memory tools
- `mcp__codebase_tools__*` — AST-based code analysis
- `mcp__semantic_search__*` — Embedding-based code search

**Local vs Remote Operations**:

The agent operates in a hybrid mode:

- **Local operations**: File reading, writing, editing, searching, and bash commands execute directly in the git worktree
- **Remote operations**: GitHub interactions (creating PRs, posting comments, reading PR metadata) use GitHub MCP
- **Benefits**: Fast local file access, reduced GitHub API calls, ability to test changes before pushing

### 6. GitHub MCP Server

**Technology**: HTTP-based MCP server by GitHub
**Endpoint**: `https://api.githubcopilot.com/mcp`
**Authentication**: GitHub App installation token

**Tools**: read_file, list_files, create_branch, update_file, create_pull_request, get_issue, etc.

### 7. Shared Authentication Service

**Location**: `shared/github_auth.py`
**Purpose**: Centralized GitHub App authentication

**Features**:

- Singleton pattern for shared token management
- Automatic token refresh with 540s cache (9 min, 60s pre-expiry buffer)
- JWT signing with RS256 algorithm
- Retry with exponential backoff (3 attempts)
- Used by all services (webhook, worker, repo_sync, sandbox_worker, retrospector_worker)
- Async context manager support

### 8. Memory Worker

**Technology**: Python + Claude Agent SDK (Haiku)
**Purpose**: Extracts persistent knowledge from session transcripts

**Responsibilities**:

- Listens for memory extraction jobs on Redis queue (`agent:memory:requests`)
- Reads persisted session transcripts from shared `transcripts` volume
- Parses transcript into clean conversation text via `shared/transcript_parser.py`
- Invokes the `@memory-extractor` subagent (runs on Haiku for cost efficiency)
- Updates memory files (index.md + detailed files) via Memory MCP server
- DLQ support: transient errors retried (3 attempts), non-transient go to dead letter queue

**Key Files**:

- `services/memory_worker/memory_worker.py`
- `subagents/memory_extractor.py`
- `mcp_servers/memory/server.py`
- `mcp_servers/memory/tools.py`

**Memory Storage**:

```
/home/bot/agent-memory/
└── owner/repo/
    └── memory/                # Persistent knowledge
        ├── index.md           # Table of contents (100 lines max)
        ├── architecture/      # System design notes
        ├── issues/            # Known bugs and workarounds
        ├── workflows/         # Development workflows
        ├── commands.md        # Operational commands
        └── decisions.md       # Architectural decision records
```

**Memory Extraction Flow**:

1. Sandbox worker session completes (Stop/SubagentStop hook fires)
2. Transcript staged to shared `transcripts` volume (`/home/bot/transcripts/{repo}/`)
3. Memory extraction job enqueued to `agent:memory:requests` Redis queue
4. Memory worker picks up job from queue
5. Transcript parsed into clean conversation text (strips metadata, thinking blocks)
6. `@memory-extractor` subagent invoked with conversation text + existing memory context
7. Subagent reads existing `index.md`, extracts new facts, updates memory files via Memory MCP
8. Future sessions receive memory context at startup via prompt injection

**Memory MCP Server**:

A lightweight stdio-based MCP server that provides two tools:

- `memory_read(file_path?)` - List or read memory files (scoped per repo)
- `memory_write(file_path, content)` - Create or update memory files (scoped per repo)

Both tools validate paths to prevent directory traversal attacks.

### 9. Retrospector Worker

**Technology**: Python + Claude Agent SDK (Sonnet)
**Purpose**: Analyzes session transcripts and proposes improvements to the bot's own instructions

**Responsibilities**:

- Listens for retrospector jobs on Redis queue (`agent:retrospector:requests`)
- Syncs the bot's own repository (not the target repo) into bare repo cache
- Creates isolated git worktree of the bot repo
- Extracts a structured transcript summary (to stay under SDK JSON buffer limits)
- Invokes `/retrospector:retrospect` command via Claude Agent SDK (Sonnet model)
- Opens PRs to the bot repo's `develop` branch with proposed instruction improvements
- Handles both main sessions (`Stop` hook) and subagent sessions (`SubagentStop` hook)
- DLQ support: transient errors retried, non-transient go to dead letter queue

**Key Files**:

- `services/retrospector_worker/retrospector_worker.py`
- `plugins/retrospector/` — Retrospector plugin

**Architecture Significance**: The retrospector is a self-improvement mechanism — after each agent session, it analyzes what happened and proposes changes to the bot's own instructions, workflows, and configuration.

### 10. Indexing Worker

**Technology**: Python + Gemini Embedding API + Qdrant
**Purpose**: Background worker that chunks repos, generates embeddings, and stores in Qdrant for semantic code search

**Responsibilities**:

- Dual trigger: subscribes to `agent:sync:events` pub/sub (auto-trigger on sync) + processes `agent:indexing:requests` list
- Chunks source files into semantic units (functions, classes, methods) using tree-sitter
- Generates embeddings via Google Gemini (`gemini-embedding-001`, 1024 dimensions)
- Stores vectors in Qdrant with metadata (filepath, name, kind, language, line numbers, commit hash)
- Supports incremental indexing via git diff — only re-embeds changed files
- Caches embeddings in Redis to avoid re-embedding unchanged content
- Cleans up stale points from previous commits
- Controlled by `INDEXING_ENABLED` env var and `IndexingConfig`

**Key Files**:

- `services/indexing_worker/indexing_worker.py` — Main worker loop and indexing pipeline
- `shared/chunker.py` — Tree-sitter-based semantic code chunker (10 languages)
- `shared/ts_languages.py` — Language registry with dynamic loading

**Supported Languages**:

Python, JavaScript, TypeScript, TSX, Go, Rust, Java, C, C++, Ruby — via per-language tree-sitter packages with regex fallback.

**Indexing Flow**:

1. Repo sync completes → event published to `agent:sync:events`
2. Indexing worker receives event → enqueues indexing job
3. Creates worktree from bare repo cache
4. Checks previous commit for incremental diff (or full index if first time)
5. Chunks changed files via tree-sitter (or regex fallback)
6. Checks Redis embedding cache — only embeds new/changed chunks via Gemini API
7. Upserts vectors into per-repo Qdrant collection
8. Cleans up stale points from previous commits
9. Updates indexing metadata in Redis

### 11. Codebase Context System

**Purpose**: Three-layer context system that gives agents structural awareness of codebases

**Layer 1 — Structural Context (Repomap)**:

- `shared/repomap.py` — Aider-style repomap using tree-sitter + PageRank
- `shared/context_builder.py` — Async wrapper with commit-based caching and personalization
- Generates compact "table of contents" of a codebase within a token budget
- Ranks definitions by importance using reference graph analysis
- Three-tier fallback: full tree-sitter → regex → file tree only
- Personalized ranking based on mentioned files, identifiers, and priority focus areas
- Configurable per-workflow via `context` profiles in `workflows.yaml`

**Layer 2 — Code Tools MCP**:

- `mcp_servers/codebase_tools/` — MCP server for AST-based code search and file summaries
- Provides `search_codebase(pattern, file_type?)` for regex-based code search across the worktree
- Provides `read_file_summary(file_path)` for structured file analysis
- Provides `find_definitions(symbol_name)` to locate symbol definitions
- Provides `find_references(symbol_name)` to find all references to a symbol
- Available to agents as registered MCP tools during sandbox execution

**Layer 3 — Semantic Search**:

- `mcp_servers/semantic_search/` — MCP server for embedding-based code search
- Provides `semantic_search(query, file_filter?, kind_filter?)` for natural language queries against indexed code
- Connects to Qdrant vector database with Gemini embeddings
- Supports file and kind filtering (e.g., search only functions in a specific path)
- Requires prior indexing by the Indexing Worker
- Conditionally registered: only when `INDEXING_ENABLED=true` + `QDRANT_URL` + `GEMINI_API_KEY`

**Language Support**:

All layers share `shared/ts_languages.py` — a central language registry with dynamic loading:

- 10 languages with full tree-sitter support
- Per-language node type mappings for generic AST walking
- Per-language tree-sitter queries for definition and reference extraction
- Dynamic loading — only installed language packages are used
- Regex fallback for languages without tree-sitter packages

### 12. Plugin System

**Location**: `plugins/`
**Purpose**: Extensible plugin architecture for specialized workflows

Each plugin follows the Claude Code plugin structure (`.claude-plugin/plugin.json`) with commands, agents, and optionally MCP servers.

**Plugins**:

| Plugin | Purpose | Agents | Commands |
|--------|---------|--------|----------|
| `pr-review-toolkit` | PR review workflow | code-reviewer, code-architecture-reviewer, code-simplifier, comment-analyzer, pr-test-analyzer, silent-failure-hunter, type-design-analyzer | `review-pr` |
| `ci-failure-toolkit` | CI failure analysis | deploy-failure-analyzer, test-failure-analyzer, build-failure-analyzer, lint-failure-analyzer | `fix-ci` |
| `test-toolkit` | Generic task testing | generic-worker | `test` |
| `pr-fix` | PR review feedback fixes | — | `fix-review` |
| `retrospector` | Self-improvement analysis | — | `retrospect` |

Plugins are auto-discovered from `~/.claude/plugins/` at SDK build time via `SDKOptionsBuilder.with_auto_discovered_plugins()`.

### 13. MCP Proxy Service

**Location**: `services/mcp_proxy/`
**Port**: 18000
**Purpose**: Bridges stdio MCP servers to HTTP/SSE so Docker containers can access them

The MCP proxy wraps each stdio MCP server (codebase_tools, github_actions, semantic_search) as an SSE endpoint at `http://mcp_proxy:18000/mcp/{server_name}/sse`. Workers connect via `socat` port forwarding (`localhost:18000 → mcp_proxy:18000`).

It also bridges host services into the Docker network:
- `localhost:11434` → host Ollama (via `host.docker.internal:11434`)
- `localhost:6333` → Qdrant

When `ALLOW_HOST_MCP=true`, MCP server definitions from the host's `~/.claude.json` are also available through the proxy, so any server installed with `claude mcp add --scope user` works inside Docker without extra configuration.

## Shared Module Infrastructure

**Location**: `shared/`
**Purpose**: Common utilities, configuration, and infrastructure shared across all services

### Configuration

| Module | Purpose |
|--------|---------|
| `config.py` | Pydantic Settings models: `WebhookConfig`, `WorkerConfig`, `AnthropicConfig`, `LangfuseConfig`, `QueueConfig`, `IndexingConfig`, `GitHubConfig` |

### SDK Infrastructure

| Module | Purpose |
|--------|---------|
| `sdk_factory.py` | `SDKOptionsBuilder` — composable builder for `ClaudeAgentOptions` with fluent API, system prompt budget enforcement (12K tokens), and MCP server wiring |
| `sdk_executor.py` | `execute_sdk()` — centralized SDK execution with retry, timeout, and error categorization |
| `post_processing.py` | Transcript staging, Redis enqueue for memory/retrospector/indexing jobs, flush with deduplication |
| `langfuse_hooks.py` | Langfuse observability hook setup (Stop/SubagentStop events) |

### Queue and Job Management

| Module | Purpose |
|--------|---------|
| `queue.py` | `RedisQueue` / `PubSubQueue` abstraction + `wait_for_repo_sync()` |
| `job_queue.py` | `JobQueue` — Redis-based job lifecycle with DLQ support |
| `dlq.py` | Dead-letter queue utilities: transient vs permanent error classification, retry with attempt tracking |
| `rate_limiter.py` | Token bucket rate limiting with Redis backend for distributed mode |

### Code Analysis

| Module | Purpose |
|--------|---------|
| `chunker.py` | Tree-sitter-based semantic code chunker (functions, classes, methods) |
| `repomap.py` | Aider-style repomap using tree-sitter + PageRank for structural context |
| `context_builder.py` | Async structural context generation with commit-based caching |
| `ts_languages.py` | Language registry (10 languages) with dynamic tree-sitter loading |
| `file_tree.py` | File tree generation with exclusion rules and Qdrant collection naming |

### Cross-Cutting Concerns

| Module | Purpose |
|--------|---------|
| `github_auth.py` | GitHub App authentication (singleton, JWT, token refresh) |
| `transcript_parser.py` | JSONL transcript parsing (conversation extraction + retrospector summaries) |
| `exceptions.py` | Custom exception hierarchy (15 exception classes) |
| `health.py` | Health checker with file-based status reporting |
| `git_utils.py` | Async git command execution wrapper |
| `http_client.py` | Shared async HTTP client (httpx) |
| `retry.py` | Async retry decorator with exponential backoff |
| `signals.py` | Graceful shutdown signal handlers |
| `logging_utils.py` | Logging configuration with noisy logger silencing |
| `models.py` | `AgentRequest` / `AgentResponse` Pydantic models |
| `utils.py` | General utilities (dot-path dict resolution) |

### MCP Server Base

| Module | Purpose |
|--------|---------|
| `mcp_servers/base.py` | Shared stdio JSON-RPC 2.0 server loop for all custom MCP servers |

## Data Flow

### Automatic PR Review

1. Developer opens PR
2. GitHub sends `pull_request.opened` webhook
3. Webhook validates signature, extracts payload, matches to `review-pr` workflow via `WorkflowEngine`
4. Webhook publishes matched job with `workflow_name` to Redis (`agent-requests`)
5. Worker validates workflow, fetches CLAUDE.md + memory index, triggers repo sync
6. Worker creates rich job in `JobQueue`
7. Repo sync service clones/updates bare repository
8. Sandbox worker waits for sync, creates worktree from bare repo (detached HEAD)
9. Structural context generated (file tree + personalized repomap with PR changed files)
10. Claude SDK executes with 5 MCP servers (GitHub, GitHub Actions, Memory, Codebase Tools, Semantic Search)
11. Claude SDK posts review to GitHub via MCP
12. Post-processing: transcript staged, memory/retrospector/indexing jobs enqueued
13. Job marked as complete in Redis

### CI Failure Fix

1. CI job fails, GitHub sends `workflow_job.completed` webhook with `conclusion: failure`
2. Webhook matches to `fix-ci` workflow (filter: `workflow_job.conclusion: "failure"`)
3. Worker enriches with CI context (run_id, workflow_name, job_name, conclusion, head_branch)
4. Sandbox worker executes CI failure analysis with `ci-failure-toolkit` plugin
5. Agent analyzes CI logs via GitHub Actions MCP, identifies root cause
6. Agent creates branch, pushes fix, opens PR

### Review Feedback Fix

1. Developer comments `/fix-it` or adds `fix-review` label on a reviewed PR
2. Webhook matches to `fix-review` workflow (command or `pull_request.labeled` + label filter)
3. Sandbox worker creates worktree from PR head ref
4. Agent reads all review feedback (reviews, inline comments, conversation) via GitHub MCP
5. Agent parses findings, deduplicates, builds fix plan
6. Agent delegates fix implementation to subagents via Agent tool
7. Agent creates PR targeting the original PR's feature branch
8. Agent posts summary comment on the original PR

### Manual Command

1. Developer comments `/review check auth logic`
2. GitHub sends `issue_comment.created` webhook
3. Webhook parses command, matches to `review-pr` workflow
4. Webhook publishes matched job to Redis
5. Worker enriches with repository context, creates job
6. Sandbox worker creates worktree, executes SDK with user query
7. Claude SDK reads code locally and posts review via GitHub MCP
8. Developer sees response on GitHub

### Push Event (Proactive Cache Warming)

1. Developer pushes to branch
2. GitHub sends `push` webhook
3. Webhook publishes sync request to `agent:sync:requests`
4. Repo sync service updates cached bare repository
5. Sync completion event triggers indexing worker
6. Future jobs for this repo start faster (no sync wait)

### Post-Processing Pipeline (Automatic)

1. Sandbox worker session completes (Stop/SubagentStop hook fires)
2. Transcript staged to shared `transcripts` volume (`/home/bot/transcripts/{repo}/`)
3. `flush_pending_post_jobs()` deduplicates and enqueues:
   - Memory job → `agent:memory:requests`
   - Retrospector job → `agent:retrospector:requests`
   - Indexing job → `agent:indexing:requests`
4. Memory worker extracts facts from transcript via `@memory-extractor` (Haiku)
5. Retrospector worker analyzes session, opens improvement PR on bot repo (Sonnet)
6. Indexing worker re-indexes changed files if applicable

### Unhandled Event

1. GitHub sends `issue_comment.deleted` webhook
2. Webhook finds no matching workflow in `WorkflowEngine`
3. Event not published to any queue (efficient — no Redis write)
4. Event gracefully ignored

## Job Queue Architecture

### Redis Keys

**Message Queue**:

- `agent-requests` - Webhook messages for worker (with pre-resolved workflow_name)
- `agent:sync:requests` - Repository sync requests

**Job Queue**:

- `agent:jobs:pending` - List of pending job IDs
- `agent:jobs:processing` - Set of currently processing job IDs
- `agent:job:data:{job_id}` - Job data (prompt, repo, ref, context, etc.) — TTL: 1 hour
- `agent:job:status:{job_id}` - Job status (pending/processing/success/error) — TTL: 1 hour
- `agent:job:result:{job_id}` - Job result (response or error) — TTL: 1 hour
- `agent:jobs:dead_letter` - Failed/corrupted jobs

**Repository Sync**:

- `agent:sync:lock:{repo}` - Lock for preventing concurrent syncs (TTL: 300s)
- `agent:sync:complete:{repo}:{ref}` - Completion signal (TTL: 1 hour)
- `agent:sync:events` - Pub/sub channel for sync completion/error events

**Memory Extraction**:

- `agent:memory:requests` - Memory extraction job queue
- `agent:memory:dead_letter` - Failed memory jobs

**Retrospector**:

- `agent:retrospector:requests` - Retrospector job queue
- `agent:retrospector:dead_letter` - Failed retrospector jobs

**Indexing**:

- `agent:indexing:requests` - Indexing job queue (repo, ref, trigger)
- `agent:indexing:dead_letter` - Failed indexing jobs
- `agent:indexing:meta:{repo}` - Hash: field=ref, value=JSON (indexed_commit, chunk_count, collection_name)
- `agent:indexing:cache:{model}` - Hash: field=content_hash, value=JSON (embedding vector cache)

**Rate Limiting**:

- `rate_limit:{name}` - Sorted set for sliding window rate limiting

### Benefits

1. **Workspace Isolation**: Each job in clean temporary directory
2. **Independent Scaling**: Scale sandbox workers separately
3. **Job Persistence**: Jobs survive worker crashes
4. **Observability**: Clear job states and metrics
5. **Persistent Memory**: Knowledge extracted across sessions via shared volumes
6. **Self-Improvement**: Retrospector analyzes sessions and proposes bot improvements
7. **DLQ Support**: Failed jobs are classified (transient vs permanent) and routed appropriately

## Security

### Authentication

- **GitHub**: GitHub App with installation token
- **Anthropic**: API key for Claude SDK
- **Webhooks**: HMAC-SHA256 signature verification

### Permissions

> **Warning**: Claude Agent SDK is configured for autonomous GitHub operations.

**Sandbox Worker Permissions**:

- permission_mode: `acceptEdits` (auto-approve edits)
- Full toolset: Task, Skill, Bash, Read, Write, Edit, List, Search, Grep, Glob
- All MCP tool wildcards: `mcp__github__*`, `mcp__github_actions__*`, `mcp__memory__*`, etc.

**Worker-Specific Toolsets**:

| Worker | Tools |
|--------|-------|
| Sandbox | Full toolset + all MCP servers |
| Memory Worker | `mcp__memory__*` only (read + write) |
| Retrospector | Skill, Bash, Glob, Grep, Read, Write, Edit + `mcp__github__*` |

### Best Practices

- Test in sandbox repositories first
- Store secrets in environment variables (see `.env.example`)
- Use webhook signature verification
- Install GitHub App only on required repos
- Use CLAUDE.md for repository-specific constraints
- Monitor logs and Langfuse traces

## Subagents

**Core Subagents** (in `subagents/`):

- `architecture-reviewer` — Design patterns and SOLID principles review
- `memory-extractor` — Extracts facts from session transcripts to build repository knowledge

**Plugin Agents** (in `plugins/*/agents/`):

- `code-reviewer`, `code-architecture-reviewer`, `code-simplifier`, `comment-analyzer`, `pr-test-analyzer`, `silent-failure-hunter`, `type-design-analyzer` — PR review agents in `pr-review-toolkit`
- `deploy-failure-analyzer`, `test-failure-analyzer`, `build-failure-analyzer`, `lint-failure-analyzer` — CI failure agents in `ci-failure-toolkit`
- `generic-worker` — Generic task agent in `test-toolkit`

See [SUBAGENTS.md](SUBAGENTS.md) for details.

## See Also

- [Development](DEVELOPMENT.md) - Setup, testing, deployment
- [Configuration](CONFIGURATION.md) - Environment variables
- [PR Review Flow](PR_REVIEW_FLOW.md) - Review workflow details
- [Plugins](PLUGINS.md) - Plugin system
