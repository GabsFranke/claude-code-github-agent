# Configuration Reference

All configuration is loaded from environment variables or a `.env` file, validated at startup via Pydantic Settings.

**Loading order**: Environment variables > `.env` file > defaults in `shared/config.py`

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
| `SDK_TIMEOUT` | `1800` | Overall SDK execution timeout in seconds |
| `SDK_EXECUTION_TIMEOUT` | `1800` | Per-invocation SDK timeout in seconds |
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
| `REDIS_PASSWORD` | — | Redis password |
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

## Semantic Code Search

| Variable | Default | Description |
|----------|---------|-------------|
| `INDEXING_ENABLED` | `false` | Enable the indexing worker |
| `GEMINI_API_KEY` | — | Required for Gemini embeddings |
| `QDRANT_URL` | `http://localhost:6333` | Qdrant instance URL |
| `QDRANT_API_KEY` | — | Qdrant API key (if auth enabled) |
| `EMBEDDING_MODEL` | `gemini-embedding-001` | Gemini embedding model |
| `EMBEDDING_DIMENSION` | `1024` | Output vector dimensionality |
| `EMBEDDING_BATCH_SIZE` | `20` | Texts per embedding API call |

## Post-session Workers

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_WORKER_ENABLED` | `true` | Extract and persist knowledge from session transcripts |
| `RETROSPECTOR_ENABLED` | `true` | Analyze sessions and propose instruction improvements |
| `REPO_SYNC_LOCK_TIMEOUT` | `300` | Lock timeout for repo sync operations (seconds) |

## Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `PORT` | `8080` | Webhook service port |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |

## See Also

- [Workflows](WORKFLOWS.md) - Workflow triggers and configuration
- [Development](DEVELOPMENT.md) - Testing, deployment, contributing
- [Architecture](ARCHITECTURE.md) - System design
