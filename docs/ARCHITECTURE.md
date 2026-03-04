# Architecture

Complete system architecture for the Simple Claude Code GitHub Agent.

## System Overview

```
┌─────────────────┐
│  GitHub Events  │
│  (PR, Comments) │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Webhook Service│
│    (FastAPI)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Redis Queue    │
│  (Messages)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Worker Service │
│  (Coordinator)  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Redis Job Queue│
└────────┬────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  Sandbox Workers (Scalable Pool)    │
│  ┌──────────┐ ┌──────────┐ ┌─────┐  │
│  │ Sandbox  │ │ Sandbox  │ │ ... │  │
│  │ Worker 1 │ │ Worker 2 │ │  N  │  │
│  │(Claude   │ │(Claude   │ │     │  │
│  │ SDK)     │ │ SDK)     │ │     │  │
│  └──────────┘ └──────────┘ └─────┘  │
└────────┬────────────────────────────┘
         │
         ▼
┌─────────────────┐
│  GitHub MCP     │
│  (Official)     │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   GitHub API    │
└─────────────────┘
```

## Core Components

### 1. Webhook Service

**Technology**: FastAPI (Python)
**Port**: 10000
**Purpose**: Receives GitHub webhook events

**Responsibilities**:

- Validates webhook signatures (HMAC)
- Parses GitHub events (issue_comment, pull_request)
- Extracts `/agent` commands from comments
- Publishes to Redis message queue
- Returns immediately (< 100ms)

**Key Files**: `services/webhook/main.py`

### 2. Worker Service (Coordinator)

**Technology**: Python
**Purpose**: Lightweight job coordinator

**Responsibilities**:

- Subscribes to Redis message queue
- Generates prompts via command registry
- Fetches CLAUDE.md from repositories
- Creates jobs in Redis job queue
- Returns immediately (non-blocking)

**Key Files**: `services/agent_worker/worker.py`

### 3. Sandbox Worker Pool

**Technology**: Python + Claude Agent SDK
**Purpose**: Executes agent requests in isolated workspaces

**Responsibilities**:

- Pulls jobs from Redis job queue
- Creates isolated temporary workspace per job
- Executes Claude Agent SDK
- Cleans up workspace after completion
- Publishes results to Redis
- **Scalable**: Run multiple instances independently

**Key Files**: `services/sandbox_executor/sandbox_worker.py`

**Workspace Isolation**:

```python
workspace = tempfile.mkdtemp(prefix=f"job_{job_id[:8]}_", dir="/tmp")
os.chdir(workspace)
# Execute SDK
shutil.rmtree(workspace)  # Cleanup
```

### 4. Result Poster Service

**Technology**: Python
**Purpose**: Posts agent responses to GitHub

**Responsibilities**:

- Listens for job completion via Redis pub/sub
- Posts responses to GitHub issues/PRs
- Handles success and error cases
- Single instance (pub/sub handles concurrency)

**Key Files**: `services/result_poster/main.py`

### 5. Claude Agent SDK

**Technology**: Python SDK by Anthropic
**Purpose**: Autonomous coding agent

**Capabilities**:

- Reads and analyzes code
- Creates branches and commits
- Opens pull requests
- Posts comments and reviews
- Executes bash commands
- Delegates to specialized subagents

**Configuration**: Programmatic via `ClaudeAgentOptions`

### 6. GitHub MCP Server

**Technology**: HTTP-based MCP server by GitHub
**Endpoint**: `https://api.githubcopilot.com/mcp`
**Authentication**: GitHub App installation token

**Tools**: read_file, list_files, create_branch, update_file, create_pull_request, get_issue, etc.

## Data Flow

### Automatic PR Review

1. Developer opens PR
2. GitHub sends `pull_request` webhook
3. Webhook validates signature and publishes to Redis
4. Worker picks up message, creates job
5. Sandbox worker executes Claude SDK
6. Claude SDK uses GitHub MCP to read PR
7. Claude SDK posts review via GitHub MCP
8. Result poster confirms completion

### Manual Command

1. Developer comments `/agent explain this function`
2. GitHub sends `issue_comment` webhook
3. Webhook parses command and publishes
4. Worker creates job with command
5. Sandbox worker executes
6. Claude SDK reads code and posts explanation
7. Developer sees response on GitHub

## Job Queue Architecture

### Redis Keys

**Message Queue**:

- `agent:requests` - Webhook messages

**Job Queue**:

- `agent:jobs:pending` - List of pending job IDs
- `agent:jobs:processing` - Set of currently processing job IDs
- `agent:job:data:{job_id}` - Job data (prompt, repo, etc.)
- `agent:job:status:{job_id}` - Job status (pending/processing/success/error)
- `agent:job:result:{job_id}` - Job result (response or error)

**Pub/Sub**:

- `agent:job:complete:{job_id}` - Job completion events

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
# Result poster stays at 1 (pub/sub handles concurrency)
```

### Performance

- **Webhook**: < 100ms response
- **Worker**: < 1s job creation
- **Sandbox**: 1-30 minutes execution
- **Result poster**: < 1s posting

### Monitoring

```bash
# Check queue depth
docker-compose exec redis redis-cli -a myredissecret LLEN agent:jobs:pending

# Check processing count
docker-compose exec redis redis-cli -a myredissecret SCARD agent:jobs:processing

# View logs
docker-compose logs -f sandbox_worker
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

Components: webhook + worker + sandbox_worker + Redis

### Full Setup

```bash
docker-compose up --build -d
```

Components: Minimal + Langfuse (PostgreSQL, ClickHouse, MinIO)

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
