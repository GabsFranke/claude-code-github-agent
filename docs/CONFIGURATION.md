# Configuration Reference

All configuration is loaded from environment variables or a `.env` file, validated at startup via Pydantic Settings.

**Loading order**: Environment variables > `.env` file > defaults in `shared/config.py`

> **Security Warning:** Default values like `changeme`, `admin123`, and `miniosecret` are for development ONLY. Change ALL secrets before deploying to production.

## Required

```bash
# Anthropic API (or set ANTHROPIC_AUTH_TOKEN)
ANTHROPIC_API_KEY=sk-ant-...

# GitHub App
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=789012
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

## Alternative Providers

```bash
# Z.AI (GLM models)
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-4.7
ANTHROPIC_DEFAULT_HAIKU_MODEL=GLM-4.5-Air
ANTHROPIC_DEFAULT_OPUS_MODEL=GLM-4.7

# Vertex AI
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
ANTHROPIC_VERTEX_REGION=us-central1
```

> **Note**: When using Vertex AI, `docker-compose.yml` bind-mounts gcloud credentials. On Windows, the path is `${APPDATA}/gcloud/application_default_credentials.json`. On Linux/macOS, use `~/.config/gcloud/application_default_credentials.json` instead.

| Variable | Default | Description |
|----------|---------|-------------|
| `GCLOUD_APPLICATION_CREDENTIALS` | — | Path to Google Cloud credentials. On Windows: `${APPDATA}/gcloud/application_default_credentials.json`. On Linux/macOS: `~/.config/gcloud/application_default_credentials.json` |

## Bot Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `BOT_USERNAME` | `Claude Code Agent` | Git `user.name` for commits |
| `BOT_USER_EMAIL` | `claude-code-agent[bot]@users.noreply.github.com` | Git `user.email` for commits |
| `WEBHOOK_BOT_USERNAME` | `claude-code-agent[bot]` | GitHub login — must match the App's username. Used for `skip_self` loop prevention |
| `BOT_REPO` | — | Bot's own repo (e.g. `owner/repo`). Used by retrospector to open improvement PRs |

## SDK Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_TURNS` | `50` | Maximum Claude SDK turns per session (1–200) |
| `SDK_EXECUTION_TIMEOUT` | `1800` | SDK execution timeout in seconds. The `WorkerConfig` model also exposes this as `sdk_timeout` (env var `SDK_TIMEOUT`), but `sdk_executor.py` reads `SDK_EXECUTION_TIMEOUT` directly |
| `SDK_MAX_RETRIES` | `3` | Retry attempts on transient SDK errors |
| `SDK_RETRY_BASE_DELAY` | `5.0` | Base delay (seconds) for exponential backoff |
| `SDK_DEBUG` | `false` | Verbose SDK logging (model, prompt, tools, messages) |

## Rate Limiting

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_RATE_LIMIT` | `5000` | GitHub API requests per hour |
| `ANTHROPIC_RATE_LIMIT` | `100` | Anthropic API requests per minute |

## Health Check

| Variable | Default | Description |
|----------|---------|-------------|
| `HEALTH_CHECK_INTERVAL` | `30` | Update interval in seconds |
| `HEALTH_CHECK_FILE` | `/tmp/worker_health` | Health status file path |
| `HEALTH_CHECK_MAX_IDLE` | `1800` | Seconds idle before marked unhealthy |

## Queue

| Variable | Default | Description |
|----------|---------|-------------|
| `QUEUE_TYPE` | `redis` | `redis` or `pubsub` |
| `REDIS_URL` | `redis://localhost:6379` | Redis connection URL |
| `REDIS_PASSWORD` | — | Redis password (**change from default in production**) |
| `QUEUE_NAME` | `agent-requests` | Default queue name |
| `GCP_PROJECT_ID` | — | GCP project (Pub/Sub only) |
| `PUBSUB_TOPIC_NAME` | `agent-requests` | Pub/Sub topic |
| `PUBSUB_SUBSCRIPTION_NAME` | `agent-requests-sub` | Pub/Sub subscription |

## Observability (Langfuse)

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_PUBLIC_KEY` | — | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | — | Langfuse secret key |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse server URL |
| `LANGFUSE_BASE_URL` | — | Alternative to `LANGFUSE_HOST` |
| `TRACE_TO_LANGFUSE` | — | Master switch for Langfuse tracing in hooks |
| `LANGFUSE_TRACE_LINKING` | `true` | Link parent/child spans for hierarchical traces |
| `LANGFUSE_HOOK_TIMEOUT` | `30` | Hook subprocess timeout in seconds |

> **Security**: The `.env.example` file and `docker-compose.yml` use default passwords (`changeme`, `clickhouse`, `miniosecret`) for MinIO, ClickHouse, and Langfuse services. **These must be changed for production deployments.**

## Semantic Code Search

| Variable | Default | Description |
|----------|---------|-------------|
| `INDEXING_ENABLED` | `false` | Enable the indexing worker. Note: `docker-compose.yml` may override this to `true` for the indexing_worker service |
| `GEMINI_API_KEY` | — | Required for Gemini embeddings |
| `SURREALDB_URL` | `ws://localhost:8000/rpc` | SurrealDB WebSocket URL |
| `SURREALDB_USER` | `root` | SurrealDB username |
| `SURREALDB_PASS` | `root` | SurrealDB password |
| `SURREALDB_NS` | `bot` | SurrealDB namespace |
| `SURREALDB_DB` | `codebase` | SurrealDB database name |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Gemini embedding model |
| `EMBEDDING_DIMENSION` | `1024` | Output vector dimensionality |
| `EMBEDDING_BATCH_SIZE` | `20` | Texts per embedding API call |

## Host Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_HOST_MCP` | `true` | Discover MCP servers from host's `~/.claude.json` inside Docker containers |
| `WORKER_SESSION_PERSIST` | `true` | Persist conversation state so users can continue multi-turn sessions |
| `SESSION_PROXY_URL` | `http://localhost:10001` | URL for the session proxy WebSocket service. Used by worker and sandbox services to generate live-view links |

When `ALLOW_HOST_MCP=true`, the sandbox worker reads MCP server definitions from your host `~/.claude.json` (the same file Claude Code CLI uses). This means any MCP server you install with `claude mcp add --scope user` on the host is automatically available to the agent inside Docker — no manual configuration needed.

The `~/.claude/` directory is bind-mounted read-write, so plugins and skills installed on the host via Claude Code CLI are also discovered automatically. On first run, built-in plugins and skills are seeded into `~/.claude/` (without overwriting existing files).

## Post-session Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_WORKER_ENABLED` | `true` | Extract and persist knowledge from session transcripts |
| `RETROSPECTOR_ENABLED` | `true` | Analyze sessions and propose instruction improvements |
| `REPO_SYNC_LOCK_TIMEOUT` | `300` | Lock timeout for repo sync operations (seconds) |
| `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` | `1` | Disable non-essential Claude Code SDK network traffic (telemetry, updates). Used by sandbox_worker, memory_worker, and retrospector_worker |

## Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Webhook service port |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

## See Also

- [Workflows](WORKFLOWS.md) - Workflow triggers and configuration
- [Development](DEVELOPMENT.md) - Testing, deployment, contributing
- [Architecture](ARCHITECTURE.md) - System design
