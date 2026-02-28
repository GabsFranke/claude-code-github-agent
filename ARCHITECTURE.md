# Architecture

## Overview

Simple Claude Code GitHub Agent is a lightweight system that uses Claude Code CLI with GitHub's official MCP server to provide automated code reviews and respond to developer commands.

## System Architecture

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
│  Message Queue  │
│  Redis/Pub/Sub  │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Worker Service │
│  Spawns Claude  │
│   Code CLI      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  Claude Code    │
│      CLI        │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│  GitHub MCP     │
│  Server (HTTP)  │
│   (Official)    │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   GitHub API    │
└─────────────────┘
```

## Components

### 1. Webhook Service

**Technology:** FastAPI (Python)  
**Port:** 8080  
**Purpose:** Receives GitHub webhook events

**Responsibilities:**
- Validates webhook signatures
- Parses GitHub events (issue_comment, pull_request)
- Extracts `/agent` commands from comments
- Publishes requests to message queue
- Triggers automatic PR reviews

**Key Files:**
- `services/webhook/main.py`

### 2. Message Queue

**Technology:** Redis (self-hosted)  
**Purpose:** Decouples webhook from worker for async processing

**Benefits:**
- Handles webhook timeouts (GitHub expects response in 10s)
- Enables horizontal scaling of workers
- Provides retry mechanism
- Allows long-running agent tasks

**Key Files:**
- `shared/queue.py` - Abstraction layer

### 3. Worker Service

**Technology:** Python + Claude Code CLI (Node.js)  
**Purpose:** Processes agent requests

**Responsibilities:**
- Subscribes to message queue
- Configures Claude Code settings from environment
- Fetches CLAUDE.md from repositories (if present)
- Spawns Claude Code CLI with appropriate prompts
- Handles both manual commands and automatic reviews

**Key Files:**
- `services/agent-worker/worker.py`

### 4. Claude Code CLI

**Technology:** Node.js CLI tool by Anthropic  
**Purpose:** Autonomous coding agent

**Capabilities:**
- Reads and analyzes code
- Creates branches and commits
- Opens pull requests
- Posts comments and reviews
- Executes bash commands
- Iterates on feedback

**Configuration:**
- `~/.claude/settings.json` - Permissions and model settings
- `~/.claude/mcp.json` - MCP server configurations

### 5. GitHub MCP Server

**Technology:** HTTP-based MCP server by GitHub  
**Endpoint:** `https://api.githubcopilot.com/mcp`  
**Authentication:** GitHub App installation token (recommended) or Personal Access Token

**Tools Provided:**
- `read_file` - Read file contents
- `list_files` - List directory contents
- `create_branch` - Create new branches
- `update_file` - Create/update files
- `create_pull_request` - Open PRs
- `get_issue` - Get issue details
- And more...

## Data Flow

### Automatic PR Review

1. Developer opens PR
2. GitHub sends `pull_request` webhook
3. Webhook service receives event
4. Webhook publishes to queue: `{repo, pr_number, command: "Review this PR", auto_review: true}`
5. Worker picks up message
6. Worker spawns Claude Code with review prompt
7. Claude Code uses GitHub MCP to read PR diff
8. Claude Code posts review comments
9. Developer sees review on GitHub

### Manual Command

1. Developer comments `/agent explain this function`
2. GitHub sends `issue_comment` webhook
3. Webhook service parses command
4. Webhook publishes to queue: `{repo, issue_number, command: "explain this function"}`
5. Worker picks up message
6. Worker checks for CLAUDE.md in repo
7. Worker spawns Claude Code with command
8. Claude Code uses GitHub MCP to read code
9. Claude Code posts explanation as comment
10. Developer sees response on GitHub

## Deployment

See [README.md](README.md) for setup instructions.

**Current Status:** Self-hosted only with Docker Compose and Redis.

**Architecture:**
- Minimal setup: webhook + worker + Redis
- Full setup: Adds Langfuse observability stack (PostgreSQL, ClickHouse, MinIO)
- Scaling: `docker-compose up --scale worker=N`


## Security

### Authentication

- **GitHub:** GitHub App (recommended) with installation token, or Personal Access Token with `repo` scope
- **Anthropic:** API key for Claude Code
- **Webhooks:** HMAC signature verification using `GITHUB_WEBHOOK_SECRET`

### Permissions

> [!WARNING]
> Claude Code is configured with broad permissions to enable autonomous operation.

Claude Code permissions configured in `~/.claude/settings.json`:
- **Allow:** Read, Write, Edit, Bash, MCP tools (including `mcp__github`)
- **Deny:** Empty
- **Ask:** Empty (auto-approve all allowed tools)

GitHub MCP server configured in `~/.claude/mcp.json`:
- **autoApprove:** `['*']` (all GitHub MCP tools auto-approved)
- **sequentialReviewComments:** `true` (prevents parallel review comment issues)

**Security Implications:**
- Agent can create branches, commit code, and open PRs without confirmation
- Agent can read any file in repositories where the GitHub App is installed
- Agent can execute bash commands within the worker container
- Fine-grained permission controls are not yet implemented

### Best Practices

- **Test in sandbox repositories first**
- Store all secrets in environment variables
- Use webhook signature verification
- Install GitHub App only on required repositories
- Use CLAUDE.md to set repository-specific rules and constraints
- Monitor logs and Langfuse traces for unexpected behavior

## Scalability

### Horizontal Scaling

- Multiple worker instances can subscribe to same queue
- Each worker processes messages independently
- Redis/Pub/Sub handles distribution

### Performance

- Webhook responds immediately (< 100ms)
- Worker processes in background (1-10 minutes)
- Claude Code timeout: 10 minutes per request

### Limits

- GitHub API rate limits: 5000 requests/hour (with GitHub App or PAT)
- Anthropic API rate limits: Varies by plan and model
- Claude Code: One request at a time per worker instance
- Worker scaling: Use `docker-compose up --scale worker=N` for parallel processing

## Monitoring & Observability

### Langfuse Integration (Optional)

When using the full Docker Compose setup, Langfuse provides complete observability:
- **Traces:** End-to-end execution flow
- **Generations:** Claude Code CLI invocations
- **Tool Calls:** GitHub MCP tool usage
- **Debugging:** Error tracking and performance analysis

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for details.

### Logs

Standard Docker Compose logging available for all services. See README for log commands.

## Future Enhancements

- [ ] Fine-grained permission controls for MCP tools
- [ ] Cloud deployment options (Google Cloud Run, AWS Lambda)
- [ ] Custom review rules per repository (beyond CLAUDE.md)
- [ ] Integration with CI/CD pipelines
- [ ] Rate limiting and cost controls
