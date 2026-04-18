# Development Guide

Complete guide for developers working on the Claude Code GitHub Agent.

## Table of Contents

- [Setup](#setup)
  - [Install Dependencies](#install-dependencies)
  - [Configure Environment](#configure-environment)
  - [Start Services](#start-services)
- [Project Structure](#project-structure)
- [Code Quality](#code-quality)
- [Testing](#testing)
  - [Run Tests](#run-tests)
  - [Test Structure](#test-structure)
  - [Writing Tests](#writing-tests)
- [Deployment](#deployment)
  - [Minimal vs Full Setup](#minimal-vs-full-setup)
  - [Docker Images](#docker-images)
  - [Scaling Strategy](#scaling-strategy)
  - [Manual Installation (without Docker)](#manual-installation-without-docker)
  - [Performance Characteristics](#performance-characteristics)
- [Monitoring and Operations](#monitoring-and-operations)
  - [Queue and Job Monitoring](#queue-and-job-monitoring)
  - [Health Monitoring](#health-monitoring)
  - [Observability (Langfuse)](#observability-langfuse)
  - [Rate Limiting](#rate-limiting)
- [Common Tasks](#common-tasks)
  - [Add New Workflow](#add-new-workflow)
  - [Add New Subagent](#add-new-subagent)
  - [Add New Plugin](#add-new-plugin)
  - [Modify Configuration](#modify-configuration)
- [Troubleshooting](#troubleshooting)
- [Verification](#verification)
- [Upgrading](#upgrading)
- [Contributing](#contributing)
- [See Also](#see-also)

## Setup

### Install Dependencies

```bash
# Clone repository
git clone https://github.com/GabsFranke/claude-code-github-agent.git
cd claude-code-github-agent

# Create and activate virtual environment
python -m venv venv

# Activate venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install all dependencies (services + dev tools)
pip install -r requirements-dev.txt
```

### Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

See [CONFIGURATION.md](CONFIGURATION.md) for all options.

### Start Services

```bash
# Docker (recommended)
docker-compose up --build -d

# Or minimal setup (no Langfuse observability)
docker-compose -f docker-compose.minimal.yml up --build -d
```

Manual setup is not recommended. The system requires 7+ services running simultaneously (webhook, worker, sandbox_worker, repo_sync, memory_worker, retrospector_worker, indexing_worker, Redis, and optionally Qdrant/Langfuse).

## Project Structure

```
claude-code-github-agent/
├── services/
│   ├── webhook/              # FastAPI webhook receiver
│   ├── agent_worker/         # Job coordinator + context enrichment
│   ├── sandbox_executor/     # Claude SDK execution in isolated worktrees
│   ├── repo_sync/            # Bare repository cache management
│   ├── memory_worker/        # Memory extraction from session transcripts
│   ├── retrospector_worker/  # Self-improvement: analyzes sessions, opens PRs
│   └── indexing_worker/      # Semantic code indexing (Gemini + Qdrant)
├── shared/                   # Shared utilities and infrastructure
│   ├── config.py             # Pydantic Settings models
│   ├── sdk_factory.py        # SDKOptionsBuilder (composable SDK config)
│   ├── sdk_executor.py       # Centralized SDK execution with retry
│   ├── post_processing.py    # Transcript staging + job enqueueing
│   ├── queue.py              # Redis/PubSub queue abstraction
│   ├── job_queue.py          # Redis job lifecycle management
│   ├── dlq.py                # Dead-letter queue utilities
│   ├── rate_limiter.py       # Token bucket rate limiting (Redis-backed)
│   ├── github_auth.py        # GitHub App authentication
│   ├── chunker.py            # Tree-sitter code chunker
│   ├── repomap.py            # Aider-style repomap (tree-sitter + PageRank)
│   ├── context_builder.py    # Structural context generation with caching
│   ├── ts_languages.py       # 10-language tree-sitter registry
│   ├── file_tree.py          # File tree generation + Qdrant collection naming
│   ├── transcript_parser.py  # JSONL transcript parsing
│   ├── health.py             # Health checker
│   ├── exceptions.py         # Custom exception hierarchy (15 classes)
│   └── ...                   # retry, signals, git_utils, http_client, etc.
├── mcp_servers/              # MCP server implementations
│   ├── base.py               # Shared stdio JSON-RPC 2.0 server loop
│   ├── memory/               # memory_read / memory_write tools
│   ├── codebase_tools/       # find_definitions, find_references, search_codebase, read_file_summary
│   └── semantic_search/      # semantic_search tool (Qdrant + Gemini)
├── plugins/                  # Claude Code plugins
│   ├── pr-review-toolkit/    # PR review workflow (7 agents, review-pr command)
│   ├── ci-failure-toolkit/   # CI failure analysis (4 agents, GitHub Actions MCP)
│   ├── test-toolkit/         # Generic task testing
│   └── retrospector/         # Self-improvement analysis
├── subagents/                # Core subagent definitions
│   ├── architecture_reviewer.py
│   └── memory_extractor.py
├── workflows/                # Workflow engine
│   └── engine.py             # WorkflowEngine (loads YAML, routes events)
├── prompts/                  # System context templates
│   ├── review.md
│   ├── triage.md
│   └── generic.md
├── hooks/                    # SDK event hooks
│   └── langfuse_hook.py      # Langfuse observability (Stop/SubagentStop)
├── skills/                   # Claude Code skills
├── repo_setup/               # Repository setup engine + config
├── workflows.yaml            # Workflow definitions (single source of truth)
├── repo-setup.yaml           # Repository setup commands
├── tests/                    # Test suite (~550+ tests across 45 files)
└── docs/                     # Documentation
```

## Code Quality

### Run All Checks

```bash
# Windows
bash ./check-code.sh        # or .\check-code.ps1

# Auto-fix formatting and imports
bash ./check-code.sh --fix

# Fast mode (skip mypy)
bash ./check-code.sh --fast
```

The script runs 5 checks sequentially: **black** (formatting) → **isort** (imports) → **flake8** (linting) → **mypy** (types) → **ruff** (linting).

### Manual Auto-Fix

```bash
black services/ shared/ subagents/ hooks/ plugins/ tests/
isort services/ shared/ subagents/ hooks/ plugins/ tests/
ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/
bash ./check-code.sh  # verify
```

### Configuration

- `pyproject.toml` — black, isort, mypy, pytest, ruff, pylint
- `.flake8` — Flake8 configuration
- `.pylintrc` — Pylint configuration

## Testing

### Run Tests

```bash
# All unit tests
pytest tests/ -v -m "not slow"

# With coverage
pytest --cov=services --cov=shared --cov-report=html

# Specific test file
pytest tests/shared/test_config.py

# Pattern matching
pytest -k "rate_limiter"

# Stop on first failure
pytest -x

# Drop into debugger on failure
pytest --pdb

# Show print statements
pytest -s
```

### Test Structure

```
tests/
├── conftest.py                          # Root fixtures (mock_redis, mock_httpx, payloads)
├── fixtures/
│   └── github_payloads.py               # Parametric payload generators
├── shared/                              # Tests for shared/ modules
│   ├── test_config.py                   # Pydantic configuration
│   ├── test_context_builder.py          # File tree + repomap + caching
│   ├── test_dlq.py                      # Dead letter queue utilities
│   ├── test_github_auth.py              # JWT, token caching, validation
│   ├── test_job_queue.py                # Redis job queue lifecycle
│   ├── test_rate_limiter.py             # Token bucket rate limiting
│   ├── test_repomap.py                  # Tag extraction + PageRank ranking
│   ├── test_ts_languages.py             # 10-language registry
│   └── ...                              # chunker, file_tree, retry, health, etc.
├── unit/                                # Cross-cutting unit tests
│   ├── test_sdk_executor.py             # SDK execution + retry
│   ├── test_sdk_factory_retro_dedup.py  # Post-processing dedup
│   ├── test_langfuse_hooks.py           # Langfuse hook subprocess
│   ├── test_transcript_parser.py        # JSONL transcript parsing
│   └── ...
├── agent_worker/                        # Worker service tests
├── webhook/                             # Webhook service tests (payload extraction)
├── sandbox_executor/                    # Sandbox execution tests
├── repo_sync/                           # Repo sync tests
├── retrospector_worker/                 # Retrospector tests
├── services/indexing_worker/            # Indexing pipeline tests
├── mcp_servers/                         # MCP server tests
│   ├── test_base.py                     # JSON-RPC protocol
│   ├── codebase_tools/                  # find_definitions, search, etc.
│   ├── memory/                          # memory_read/write + security
│   └── semantic_search/                 # Embedding + Qdrant filtering
├── plugins/                             # Plugin tests (GitHub Actions tools)
├── workflows/                           # Workflow engine tests (routing, filters, skip_self)
├── integration/                         # Integration tests (require live Redis)
│   ├── test_queue_integration.py
│   └── test_webhook_handlers.py
└── test_chunker.py, test_repomap_queries.py  # Root-level chunker/repomap tests
```

### Writing Tests

**Async test**:

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_queue_publish(mock_redis):
    queue = RedisQueue(redis_client=mock_redis, queue_name="test")
    await queue.publish({"event": "test"})
    mock_redis.rpush.assert_called_once()
```

**Using fixtures** (defined in `tests/conftest.py`):

- `mock_redis` — Mock Redis client
- `mock_httpx_client` — Mock HTTP client
- `redis_client` — Real Redis client (integration tests, auto-skipped if unavailable)
- `sample_github_webhook_payload` — GitHub PR opened payload
- `sample_issue_comment_payload` — GitHub comment payload

**Markers**: `@pytest.mark.asyncio` (auto-mode enabled), `@pytest.mark.integration`, `@pytest.mark.slow`

## Deployment

### Minimal vs Full Setup

**Minimal** (no observability stack):

```bash
docker-compose -f docker-compose.minimal.yml up --build -d
```

Services: webhook, worker, sandbox_worker, repo_sync, memory_worker, retrospector_worker, Redis, Qdrant, indexing_worker

Volumes: repo-cache, agent-memory, transcripts, qdrant-storage

**Full** (with Langfuse observability):

```bash
docker-compose up --build -d
```

Services: Minimal + Langfuse (PostgreSQL, ClickHouse, MinIO, Worker, Web UI at http://localhost:7500)

Volumes: Minimal + langfuse-db-data, langfuse-clickhouse-data, langfuse-clickhouse-logs, langfuse-minio-data

**Semantic search**: Set `INDEXING_ENABLED=true` and `GEMINI_API_KEY` to activate the indexing worker.

### Docker Images

| Service | Base Image | Notable |
|---------|-----------|---------|
| webhook | python:3.12-slim | FastAPI |
| worker | python:3.12-slim | Healthcheck |
| sandbox_worker | python:3.12-slim | Non-root `bot` user, OS tools (git, jq, ripgrep), plugins + skills |
| repo_sync | python:3.12-slim | Non-root `bot` user, git |
| memory_worker | python:3.12-slim | Non-root `bot` user |
| retrospector_worker | python:3.12-slim | Non-root `bot` user, git |
| indexing_worker | python:3.12-slim | Non-root `bot` user, git |

### Scaling Strategy

Each sandbox worker processes one job at a time. Scale based on your expected activity:

```bash
# Low activity (1-5 events/hour)
docker-compose up -d  # Default: 1 sandbox_worker

# Medium activity (5-20 events/hour)
docker-compose up --scale sandbox_worker=5 -d

# High activity (20+ events/hour)
docker-compose up --scale sandbox_worker=10 -d

# Very high activity (50+ events/hour)
docker-compose up --scale sandbox_worker=20 -d
```

Jobs typically take 2-10 minutes. Scale based on your peak activity, not average. Other workers (memory, retrospector, indexing) stay at 1 each.

### Manual Installation (without Docker)

Not recommended — the system requires 7+ services running simultaneously. For development:

```bash
# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
source venv/bin/activate     # Linux/Mac

# Install dependencies
pip install -r requirements-dev.txt

# Start Redis
redis-server

# Terminal 1: Webhook
cd services/webhook && python main.py

# Terminal 2: Worker
cd services/agent_worker && python worker.py
```

### Performance Characteristics

| Component | Typical Time |
|-----------|-------------|
| Webhook | < 100ms |
| Worker (job creation) | < 1s |
| Repo Sync (initial clone) | 2-30s |
| Repo Sync (update) | ~1s |
| Worktree creation | ~1s from cached bare repo |
| Sandbox execution | 1-30 min |
| Memory extraction | 30-60s (Haiku) |
| Retrospector | 2-5 min (Sonnet) |
| Indexing | 1-5 min (depends on repo size) |

First job for a repo takes ~30s (clone). Subsequent jobs take ~1s (worktree from cached bare repo). Repomap is cached by commit hash + personalization.

## Monitoring and Operations

### Queue and Job Monitoring

```bash
# Check pending jobs
docker-compose exec redis redis-cli -a myredissecret LLEN agent:jobs:pending

# Check currently processing jobs
docker-compose exec redis redis-cli -a myredissecret SCARD agent:jobs:processing

# Check sync queue depth
docker-compose exec redis redis-cli -a myredissecret LLEN agent:sync:requests

# Check if a repo is synced
docker-compose exec redis redis-cli -a myredissecret GET "agent:sync:complete:owner/repo:main"

# Check DLQ counts (dead letter queues)
docker-compose exec redis redis-cli -a myredissecret LLEN agent:jobs:dead_letter
docker-compose exec redis redis-cli -a myredissecret LLEN agent:memory:dead_letter
docker-compose exec redis redis-cli -a myredissecret LLEN agent:retrospector:dead_letter

# View service logs
docker-compose logs -f sandbox_worker
docker-compose logs -f repo_sync
docker-compose logs -f memory_worker
docker-compose logs -f retrospector_worker
docker-compose logs -f indexing_worker
```

### Health Monitoring

Workers write health status to `/tmp/worker_health`:

```
healthy=1
last_activity=1709123456
uptime=3600
processed=42
errors=2
message=Healthy: Last activity 15s ago
```

**Docker health check** (configured in Dockerfile):

```dockerfile
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD test -f /tmp/worker_health && \
      [ $(( $(date +%s) - $(stat -c %Y /tmp/worker_health 2>/dev/null || echo 0) )) -lt 120 ] || exit 1
```

**Configuration**:

```bash
HEALTH_CHECK_INTERVAL=30    # Update interval in seconds
HEALTH_CHECK_FILE=/tmp/worker_health
HEALTH_CHECK_MAX_IDLE=1800  # Max idle seconds before unhealthy
```

### Observability (Langfuse)

When using full Docker Compose setup, Langfuse provides:

- **Traces**: End-to-end execution flow
- **Generations**: Claude SDK invocations
- **Tool Calls**: GitHub MCP tool usage
- **Trace Linking**: Parent span IDs connect worker → sandbox → post-processing

Access at: http://localhost:7500

Langfuse hook logs (inside container only):

```bash
docker-compose exec sandbox_worker cat /root/.claude/state/langfuse_hook.log
```

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for setup details.

### Rate Limiting

Uses token bucket algorithm with Redis-based distributed rate limiting:

- **GitHub API**: 5000 requests/hour (default)
- **Anthropic API**: 100 requests/minute (default)

```bash
# .env
GITHUB_RATE_LIMIT=5000   # Requests per hour
ANTHROPIC_RATE_LIMIT=100 # Requests per minute
```

**Anthropic API tiers**:

| Tier | Limit |
|------|-------|
| Tier 1 | 50 req/min |
| Tier 2 | 100 req/min |
| Tier 3 | 200 req/min |
| Tier 4 | 400 req/min |

**Adjusting limits**:

```bash
# Check rate limit logs
docker-compose logs worker | grep "Rate limit"

# Update .env and restart
docker-compose restart worker
```

## Common Tasks

### Add New Workflow

Edit `workflows.yaml` — the single source of truth for all workflows:

```yaml
workflows:
  my-workflow:
    triggers:
      events:
        - issues.opened
      commands:
        - /my-command
    prompt:
      template: "Do something with {repo} #{issue_number}"
      system_context: "my-context.md"  # Optional, loaded from prompts/
    context:
      repomap_budget: 2048
      personalized: false
    skip_self: true  # Default: true
```

The `WorkflowEngine` auto-loads the config. Place system context files in `prompts/`.

Template placeholders: `{repo}`, `{issue_number}`, `{user_query}`.

Filters use dot-path resolution against the webhook payload:

```yaml
filters:
  workflow_job.conclusion: "failure"  # Exact match
  label.name: "triage"                # Exact match
```

### Add New Subagent

1. Create file in `subagents/`:

```python
# subagents/my_specialist.py
from claude_agent_sdk import AgentDefinition

MY_SPECIALIST = AgentDefinition(
    description="Brief description and when to use proactively",
    prompt="""System prompt with instructions...""",
    model="inherit"
)
```

2. Export in `subagents/__init__.py`:

```python
from .my_specialist import MY_SPECIALIST

AGENTS = {
    "my-specialist": MY_SPECIALIST,
    # ... other agents
}
```

3. Rebuild and restart:

```bash
docker-compose build sandbox_worker  # subagents run in sandbox
docker-compose up -d sandbox_worker
```

See [SUBAGENTS.md](SUBAGENTS.md) for details.

### Add New Plugin

1. Create plugin directory with `.claude-plugin/plugin.json`:

```bash
mkdir -p plugins/my-plugin/.claude-plugin
mkdir -p plugins/my-plugin/agents
mkdir -p plugins/my-plugin/commands
```

2. Add agents, commands, and optionally MCP servers.

3. Plugins are auto-discovered from `~/.claude/plugins/` (copied during Docker build). No code changes needed — just rebuild:

```bash
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

See [PLUGINS.md](PLUGINS.md) for details.

### Modify Configuration

1. Edit `shared/config.py` to add new Pydantic settings
2. Add validation if needed
3. Update `.env.example` with new variables
4. Update [CONFIGURATION.md](CONFIGURATION.md)
5. Add tests in `tests/shared/test_config.py`

## Troubleshooting

### Configuration Errors

```bash
# Check required fields
grep -E "GITHUB_|ANTHROPIC_" .env

# Test configuration loading
python -c "from shared.config import get_worker_config; print(get_worker_config())"
```

### Import Errors

```bash
# Verify installation
python -c "import shared; print(shared.__file__)"
```

### Health Check Failures

```bash
# Check health file
docker-compose exec worker cat /tmp/worker_health

# Check logs
docker-compose logs worker | tail -50

# Restart worker
docker-compose restart worker
```

### Redis Connection

```bash
# Check Redis is running
docker-compose exec redis redis-cli -a myredissecret ping  # Should return PONG

# Check connection logs
docker-compose logs redis
```

### Rate Limiting Issues

```bash
# Check rate limit logs
docker-compose logs worker | grep "Rate limit"

# Adjust limits in .env
GITHUB_RATE_LIMIT=4000
ANTHROPIC_RATE_LIMIT=80

# Restart
docker-compose restart worker
```

## Verification

### Check Configuration

```bash
python -c "from shared.config import get_worker_config; print(get_worker_config())"
```

### Test Webhook

```bash
curl -X POST http://localhost:10000/webhook \
  -H "Content-Type: application/json" \
  -H "X-GitHub-Event: ping" \
  -d '{"zen": "test"}'
```

### View Logs

```bash
docker-compose logs -f worker
docker-compose logs -f webhook
```

## Upgrading

```bash
git pull origin main
docker-compose build
docker-compose up -d
```

Configuration is backward compatible — no `.env` changes needed between versions.

## Contributing

### Workflow

1. Fork repository
2. Create feature branch: `git checkout -b feature/my-feature`
3. Make changes
4. Run tests: `pytest`
5. Run linters: `bash ./check-code.sh`
6. Commit: `git commit -m "Add my feature"`
7. Push: `git push origin feature/my-feature`
8. Open Pull Request to `develop`

### Code Style

- Follow PEP 8 with **black** formatting (88 char line length)
- Use type hints throughout (Python 3.12+ syntax)
- Use **Pydantic** for configuration and validation
- Write async functions for all I/O operations
- Use custom exceptions from `shared/exceptions.py`
- Add tests for new features

### Commit Messages

- Use present tense: "Add feature" not "Added feature"
- Be descriptive but concise
- Reference issues: "Fix #123: Add rate limiting"

### CI

Tests run automatically on every PR via `.github/workflows/test.yml`:

- Unit tests (Python 3.12)
- Integration tests (with Redis service container)
- Linting (black, isort, ruff)
- Coverage reports (uploaded as artifacts)

## See Also

- [Architecture](ARCHITECTURE.md) - System design and component details
- [Configuration](CONFIGURATION.md) - Environment variables
- [Workflows](WORKFLOWS.md) - Creating and managing workflows
- [Subagents](SUBAGENTS.md) - Subagent system
- [Plugins](PLUGINS.md) - Plugin system
