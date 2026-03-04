# Architecture

Complete system architecture for the Claude Code GitHub Agent.

## System Overview

```mermaid
flowchart LR
    GH[GitHub<br/>Events] --> WH[Webhook<br/>Service]

    WH --> RQ[(Redis<br/>Queues)]

    RQ --> W[Worker<br/>Coordinator]
    RQ --> RS[Repo Sync<br/>Service]

    W --> JQ[(Job<br/>Queue)]
    RS --> CACHE[(Bare Repo<br/>Cache)]

    JQ --> SW[Sandbox<br/>Workers]
    CACHE -.->|worktree| SW

    SW --> |Local Files| SW
    SW --> |GitHub API| MCP[GitHub<br/>MCP]

    MCP --> GH

    style GH fill:#e1f5ff
    style WH fill:#fff3e0
    style W fill:#fff3e0
    style RS fill:#f3e5f5
    style SW fill:#e8f5e9
    style CACHE fill:#fce4ec
    style MCP fill:#e0f2f1
```

**Architecture Flow:**

1. **GitHub** → Webhook events (PR, comments, push)
2. **Webhook Service** → Validates and publishes to Redis queues
3. **Worker** → Creates jobs from requests
4. **Repo Sync** → Maintains cached bare repositories
5. **Sandbox Workers** → Execute Claude SDK in isolated worktrees
6. **Claude SDK** → Local file operations + GitHub MCP for API calls
7. **Results** → Posted back to GitHub via MCP

## Core Components

### 1. Webhook Service

**Technology**: FastAPI (Python)
**Port**: 10000
**Purpose**: Receives GitHub webhook events

**Responsibilities**:

- Validates webhook signatures (HMAC)
- Parses GitHub events (issue_comment, pull_request, push)
- Extracts `/agent` commands from comments
- Publishes to Redis message queue (`agent:requests`)
- Publishes to Redis sync queue (`agent:sync:requests`) for proactive caching
- Returns immediately (< 100ms)

**Key Files**: `services/webhook/main.py`

### 2. Worker Service (Coordinator)

**Technology**: Python
**Purpose**: Lightweight job coordinator

**Responsibilities**:

- Subscribes to Redis message queue (`agent:requests`)
- Generates prompts via command registry
- Fetches CLAUDE.md from repositories
- Determines appropriate git ref (main, PR head, etc.)
- Creates jobs in Redis job queue with repo, ref, and prompt
- Returns immediately (non-blocking)

**Key Files**: `services/agent_worker/worker.py`

### 3. Repository Sync Service

**Technology**: Python + Git
**Purpose**: Manages cached bare repository clones

**Responsibilities**:

- Maintains warm bare repository clones in `/var/cache/repos/`
- Listens to sync requests on Redis queue (`agent:sync:requests`)
- Fetches updates for existing repositories
- Uses Redis locks to prevent concurrent syncs
- Provides fast repository access for sandbox workers
- Supports GitHub App authentication for private repos

**Key Files**: `services/repo_sync/sync_worker.py`

**Cache Structure**:

```
/var/cache/repos/
├── owner1/
│   └── repo1.git/  # Bare repository
└── owner2/
    └── repo2.git/  # Bare repository
```

**Sync Flow**:

```python
# Listen for sync requests
await sync_queue.subscribe(message_handler)

# On sync request
lock = redis.lock(f"agent:sync:lock:{repo}")
if not os.path.exists(repo_dir):
    # Initial clone
    git clone --bare https://github.com/{repo}.git {repo_dir}
else:
    # Update existing
    git --git-dir={repo_dir} fetch origin '+refs/heads/*:refs/heads/*'

# Signal completion
await redis.set(f"agent:sync:complete:{repo}:{ref}", "1", ex=300)
```

**Benefits**:

- Repository cloning: ~30s → ~2s (after first clone)
- Reduced GitHub API calls
- Shared cache across all sandbox workers
- Proactive cache warming on push events

### 4. Sandbox Worker Pool

**Technology**: Python + Claude Agent SDK
**Purpose**: Executes agent requests in isolated local workspaces

**Responsibilities**:

- Pulls jobs from Redis job queue
- Waits for repository sync completion (with fallback)
- Creates isolated git worktree per job from cached bare repo
- Executes Claude Agent SDK in local worktree with file system access
- Injects git credentials for pushing changes
- Cleans up workspace and worktree after completion
- Publishes results to Redis
- **Scalable**: Run multiple instances independently

**Key Files**: `services/sandbox_executor/sandbox_worker.py`

**Workspace Isolation**:

```python
# Wait for repo sync (with fallback)
repo_dir = await ensure_repo_synced(repo, ref, redis_client, github_token)

# Create worktree from bare repo
workspace = tempfile.mkdtemp(prefix=f"job_{job_id[:8]}_", dir="/tmp")
branch_name = f"job-{job_id[:8]}-{timestamp}"
git --git-dir={repo_dir} worktree add -b {branch_name} {workspace} heads/main

# Inject git credentials for pushing
git config credential.helper store
echo "https://x-access-token:{token}@github.com" > ~/.git-credentials

# Execute SDK with local file tools
os.chdir(workspace)
# ... SDK execution with Read, Write, Edit, List, Search, Bash tools ...

# Cleanup
git --git-dir={repo_dir} worktree remove --force {workspace}
```

**Worktree Benefits**:

- Fast creation (~1s vs ~30s clone)
- Isolated working directories per job
- Shared git database (bare repo)
- Automatic cleanup on container restart (tmpfs)
- Local file system access for Claude SDK

**Sync Coordination**:

- Waits up to 20 seconds for repo_sync service to complete
- Falls back to self-sync if repo_sync is slow or unavailable
- Uses Redis completion signals (`agent:sync:complete:{repo}:{ref}`)
- Prevents duplicate syncs with Redis locks

### 5. Claude Agent SDK

**Technology**: Python SDK by Anthropic
**Purpose**: Autonomous coding agent

**Capabilities**:

- Reads and analyzes code locally using Read, List, Search tools
- Writes and edits files locally using Write, Edit tools
- Executes bash commands using Bash tool
- Creates branches and commits via GitHub MCP
- Opens pull requests via GitHub MCP
- Posts comments and reviews via GitHub MCP
- Delegates to specialized subagents

**Configuration**: Programmatic via `ClaudeAgentOptions`

**Allowed Tools**:

- `Task` - Delegate to subagents
- `Bash` - Execute shell commands in worktree
- `Read` - Read local files from worktree
- `Write` - Create/overwrite local files in worktree
- `Edit` - Make targeted edits to local files
- `List` - List directory contents in worktree
- `Search` - Search for patterns in local files
- `mcp__github__*` - All GitHub MCP tools

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
- Automatic token refresh with expiration tracking
- JWT signing with RS256 algorithm
- Used by all services (webhook, worker, repo_sync, sandbox_worker)
- Async context manager support

**Usage**:

```python
from shared import GitHubAuthService, get_github_auth_service

# Option 1: Singleton instance
auth_service = await get_github_auth_service()
token = await auth_service.get_token()

# Option 2: Custom instance
async with GitHubAuthService(app_id, private_key, installation_id) as auth:
    token = await auth.get_token()
```

**Benefits**:

- Single source of truth for authentication
- Reduced code duplication
- Better token lifecycle management
- Shared across all services

## Data Flow

### Automatic PR Review

1. Developer opens PR
2. GitHub sends `pull_request` webhook
3. Webhook validates signature, publishes sync request and job to Redis
4. Repo sync service clones/updates bare repository
5. Worker picks up message, creates job with PR ref
6. Sandbox worker waits for sync, creates worktree from bare repo
7. Claude SDK executes in local worktree with file system access
8. Claude SDK uses local tools (Read, Write, Edit, List, Search, Bash)
9. Claude SDK uses GitHub MCP to post review
10. Job marked as complete in Redis

### Manual Command

1. Developer comments `/agent explain this function`
2. GitHub sends `issue_comment` webhook
3. Webhook parses command, publishes sync request and job
4. Repo sync service ensures repository is cached
5. Worker creates job with command
6. Sandbox worker creates worktree, executes SDK
7. Claude SDK reads code locally and posts explanation via GitHub MCP
8. Developer sees response on GitHub

### Push Event (Proactive Cache Warming)

1. Developer pushes to branch
2. GitHub sends `push` webhook
3. Webhook publishes sync request to `agent:sync:requests`
4. Repo sync service updates cached bare repository
5. Future jobs for this repo start faster (no sync wait)

## Job Queue Architecture

### Redis Keys

**Message Queue**:

- `agent:requests` - Webhook messages for worker
- `agent:sync:requests` - Repository sync requests

**Job Queue**:

- `agent:jobs:pending` - List of pending job IDs
- `agent:jobs:processing` - Set of currently processing job IDs
- `agent:job:data:{job_id}` - Job data (prompt, repo, ref, etc.)
- `agent:job:status:{job_id}` - Job status (pending/processing/success/error)
- `agent:job:result:{job_id}` - Job result (response or error)

**Repository Sync**:

- `agent:sync:lock:{repo}` - Lock for preventing concurrent syncs
- `agent:sync:complete:{repo}:{ref}` - Completion signal (TTL: 5 minutes)

### Benefits

1. **Workspace Isolation**: Each job in clean temporary directory
2. **Independent Scaling**: Scale sandbox workers separately
3. **Job Persistence**: Jobs survive worker crashes
4. **Observability**: Clear job states and metrics

## Scaling

### Horizontal Scaling

```bash
# Scale sandbox workers independently
docker-compose up --scale sandbox_worker=10 -d

# Worker stays at 1 (lightweight coordinator)
# Repo sync stays at 1 (manages shared cache)
```

### Performance

- **Webhook**: < 100ms response
- **Worker**: < 1s job creation
- **Repo Sync**: 2-30s initial clone, ~1s updates
- **Worktree Creation**: ~1s from cached bare repo
- **Sandbox**: 1-30 minutes execution (with local file access)
- **Result poster**: < 1s posting

### Performance Improvements

The repository caching and worktree architecture provides significant performance gains:

- **First job for a repo**: ~30s clone + execution time
- **Subsequent jobs**: ~1s worktree creation + execution time
- **Proactive caching**: Push events trigger background syncs, making future jobs instant
- **Shared cache**: All sandbox workers benefit from the same cached repositories
- **Local file operations**: Read/Write/Edit operations are instant (no GitHub API calls)

### Monitoring

```bash
# Check queue depth
docker-compose exec redis redis-cli -a myredissecret LLEN agent:jobs:pending

# Check processing count
docker-compose exec redis redis-cli -a myredissecret SCARD agent:jobs:processing

# Check sync queue depth
docker-compose exec redis redis-cli -a myredissecret LLEN agent:sync:requests

# Check if a repo is synced
docker-compose exec redis redis-cli -a myredissecret GET "agent:sync:complete:owner/repo:main"

# View logs
docker-compose logs -f sandbox_worker
docker-compose logs -f repo_sync
```

## Rate Limiting

### Overview

Uses token bucket algorithm with Redis-based distributed rate limiting:

- **GitHub API**: 5000 requests/hour (default)
- **Anthropic API**: 100 requests/minute (default)

### Configuration

```bash
# .env
GITHUB_RATE_LIMIT=5000  # Requests per hour
ANTHROPIC_RATE_LIMIT=100  # Requests per minute
```

### Implementation

**Distributed (Redis-based)**:

```python
from shared.rate_limiter import create_redis_rate_limiter_backend

redis_backend = await create_redis_rate_limiter_backend(
    redis_url="redis://localhost:6379",
    password="your_password"
)
rate_limiters = MultiRateLimiter(backend=redis_backend)
rate_limiters.add_limiter("github", max_requests=5000, time_window=3600)
```

**Benefits**:

- Shared across all workers
- Prevents API quota violations
- Automatic fallback to in-memory mode

### Adjusting Limits

Based on your API tier:

**Anthropic**:

- Tier 1: 50 req/min
- Tier 2: 100 req/min
- Tier 3: 200 req/min
- Tier 4: 400 req/min

## Health Monitoring

### Health Check System

**Location**: `/tmp/worker_health`

**Format**:

```
healthy=1
last_activity=1709123456
uptime=3600
processed=42
errors=2
message=Healthy: Last activity 15s ago
```

### Docker Health Check

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD test -f /tmp/worker_health && \
      [ $(( $(date +%s) - $(stat -c %Y /tmp/worker_health 2>/dev/null || echo 0) )) -lt 120 ] || exit 1
```

### Monitoring

```bash
# View health status
docker-compose exec worker cat /tmp/worker_health

# Check Docker health
docker-compose ps

# View health check logs
docker inspect --format='{{json .State.Health}}' <container-id> | jq
```

### Configuration

```bash
# .env
HEALTH_CHECK_INTERVAL=30  # Update interval in seconds
HEALTH_CHECK_FILE=/tmp/worker_health  # File path
```

## Security

### Authentication

- **GitHub**: GitHub App with installation token
- **Anthropic**: API key for Claude SDK
- **Webhooks**: HMAC signature verification

### Permissions

> **Warning**: Claude Agent SDK is configured for autonomous GitHub operations.

**Claude SDK Permissions**:

- allowed_tools: Task, mcp: **github**\* (GitHub MCP tools only)
- permission_mode: acceptEdits (auto-approve edits)

**GitHub MCP**:

- All GitHub MCP tools available
- Sequential review comments

**Security Implications**:

- Agent can create branches, commit, open PRs via GitHub MCP
- Agent can read any file in installed repositories via GitHub MCP
- All file operations go through GitHub MCP server
- Fine-grained controls not yet implemented

### Best Practices

- Test in sandbox repositories first
- Store secrets in environment variables
- Use webhook signature verification
- Install GitHub App only on required repos
- Use CLAUDE.md for repository-specific constraints
- Monitor logs and Langfuse traces

## Observability

### Langfuse Integration (Optional)

When using full Docker Compose setup:

- **Traces**: End-to-end execution flow
- **Generations**: Claude SDK invocations
- **Tool Calls**: GitHub MCP tool usage
- **Debugging**: Error tracking and performance

Access at: http://localhost:7500

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for details.

### Logs

```bash
# View all logs
docker-compose logs -f

# Specific service
docker-compose logs -f sandbox_worker
docker-compose logs -f worker
docker-compose logs -f webhook

# Langfuse hook logs (inside container only)
docker-compose exec sandbox_worker cat /root/.claude/state/langfuse_hook.log
```

## Deployment

### Minimal Setup

```bash
docker-compose -f docker-compose.minimal.yml up --build -d
```

Components: webhook + worker + sandbox_worker + repo_sync + Redis

Volumes: repo-cache (shared bare repositories)

### Full Setup

```bash
docker-compose up --build -d
```

Components: Minimal + Langfuse (PostgreSQL, ClickHouse, MinIO)

Volumes: repo-cache + langfuse-db-data + langfuse-clickhouse-data + langfuse-clickhouse-logs + langfuse-minio-data

### Scaling Strategy

Each sandbox worker processes one job at a time. Scale based on your expected activity:

**Low Activity (1-5 events/hour)**:

```bash
docker-compose up -d  # Default: 1 sandbox_worker
```

**Medium Activity (5-20 events/hour)**:

```bash
docker-compose up --scale sandbox_worker=5 -d
```

**High Activity (20+ events/hour)**:

```bash
docker-compose up --scale sandbox_worker=10 -d
```

**Very High Activity (50+ events/hour)**:

```bash
docker-compose up --scale sandbox_worker=20 -d
```

**Note**: Each worker handles 1 job at a time. Jobs typically take 2-10 minutes. Events include PR opens, issue comments, `/agent` commands, etc. Scale based on your peak activity, not average.

## Subagents

Specialized agents for focused tasks:

**PR Review Subagents**:

- architecture-reviewer - Design patterns and SOLID principles
- security-reviewer - Vulnerability scanning
- bug-hunter - Bug detection and edge cases
- code-quality-reviewer - Style and maintainability

**General Purpose**:

- context-gatherer - Codebase exploration
- bug-investigator - Root cause analysis
- test-writer - Test generation

See [SUBAGENTS.md](SUBAGENTS.md) for details.

## See Also

- [Getting Started](GETTING_STARTED.md) - Installation and setup
- [Configuration](CONFIGURATION.md) - Environment variables
- [Development](DEVELOPMENT.md) - Testing and contributing
- [PR Review Flow](PR_REVIEW_FLOW.md) - Review workflow details
- [Plugins](PLUGINS.md) - Plugin system
