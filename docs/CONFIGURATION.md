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

## Host Integration

| Variable | Default | Description |
|----------|---------|-------------|
| `ALLOW_HOST_MCP` | `true` | Auto-discover host MCP server names from `~/.claude.json` and add their tool permissions. Only grants tool access — does NOT bridge stdio servers through the MCP proxy. Host servers must be independently reachable from inside Docker (e.g., HTTP servers via `host.docker.internal`). |
| `WORKER_SESSION_PERSIST` | `true` | Persist conversation state so users can continue multi-turn sessions |
| `SESSION_PROXY_URL` | `http://localhost:10001` | URL for the session proxy WebSocket service. Used by worker and sandbox services to generate live-view links |

### Docker Networking

Containers cannot reach host `localhost` — use `host.docker.internal` instead. This applies to any service running on your host that the containers need to access (Ollama, oc-go-cc, OpenCode, etc.):

| Variable | Correct (container → host) | Wrong (container → itself) |
|----------|---------------------------|---------------------------|
| `ANTHROPIC_BASE_URL` | `http://host.docker.internal:3456` | `http://localhost:3456` |
| Any MCP server URL | `http://host.docker.internal:PORT` | `http://localhost:PORT` |

**Alternative**: Set `SOCAT_API_FORWARD=3456` in `.env` to add automatic socat forwarding (`localhost:3456` → `host.docker.internal:3456`) without changing your `settings.json` URL. This works for all three workers (sandbox, memory, retrospector).

When `ALLOW_HOST_MCP=true`, the SDK builder reads MCP server names from your host `~/.claude.json` and adds their tool patterns (`mcp__{name}__*`) to the agent's allowed tool list. This grants **tool permissions only** — it does not bridge those servers through the MCP proxy. For host MCP servers to actually work inside Docker, they must be independently reachable from the container. HTTP-based servers work if they're accessible via `host.docker.internal`, but stdio-based host servers will not work unless separately proxied.

The `~/.claude/` directory is bind-mounted read-write, so plugins and skills installed on the host via Claude Code CLI are also discovered automatically. On first run, built-in plugins and skills are seeded into `~/.claude/` (without overwriting existing files).

## Post-session Workers

Both post-session workers are **off by default**: each runs an additional Claude
session after every job. Turn them on explicitly when you want that cost.

They are configured by a replica count rather than a boolean, so one number is
both the switch and the scale. `docker compose` reads it as the service's
`scale:`, and at `0` no container is created at all: nothing to idle, nothing to
restart. `1` runs one, `2` runs two. `make up MEMORY=2` overrides the count for a
run without editing `.env`.

The same number gates the `Stop` hooks in `sandbox_worker`, so a worker with no
container also has no jobs queued for it. Running a worker module directly with
its count at `0` is the one case where a process exists anyway; it logs that it
is off and idles rather than consuming queued jobs.

Tier aliases resolve through the matching `ANTHROPIC_DEFAULT_*_MODEL` variable, so
model ids stay in one place.

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMORY_WORKER_REPLICAS` | `0` | `memory_worker` instances: extract and persist knowledge from session transcripts. `0` creates no container |
| `MEMORY_WORKER_MODEL` | `haiku` | Model tier for memory extraction: `opus`, `sonnet`, or `haiku` |
| `RETROSPECTOR_REPLICAS` | `0` | `retrospector_worker` instances: analyze sessions and propose instruction improvements. `0` creates no container |
| `RETROSPECTOR_MODEL` | `sonnet` | Model tier for retrospection: `opus`, `sonnet`, or `haiku` |
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
