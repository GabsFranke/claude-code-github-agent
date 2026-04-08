# Configuration Reference

Complete reference for all environment variables and configuration options.

## Quick Reference

**Required**:

```bash
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=789012
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

**Optional** (with defaults):

```bash
LOG_LEVEL=INFO
GITHUB_RATE_LIMIT=5000
ANTHROPIC_RATE_LIMIT=100
MAX_TURNS=50
SDK_TIMEOUT=1800
```

## Configuration System

Uses Pydantic Settings for type-safe configuration with automatic validation.

**Loading order**:

1. Environment variables (highest priority)
2. `.env` file
3. Default values in `shared/config.py`

**Validation**: All configuration is validated at startup. Invalid config fails fast with clear error messages.

## Worker Configuration

### Required Settings

```bash
# GitHub App credentials
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=789012
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret

# Anthropic API
ANTHROPIC_API_KEY=sk-ant-...
# Note: ANTHROPIC_AUTH_TOKEN is also supported as an alternative
```

### Optional Settings

```bash
# Bot Identity (for git commits)
BOT_USERNAME="Claude Code Agent"  # Git user.name for commits
BOT_USER_EMAIL="claude-code-agent[bot]@users.noreply.github.com"  # Git user.email

# Logging
LOG_LEVEL=INFO  # DEBUG, INFO, WARNING, ERROR, CRITICAL

# Claude SDK
MAX_TURNS=50  # Maximum turns for Claude SDK (1-200)
SDK_TIMEOUT=1800  # SDK execution timeout in seconds (min: 60)

# Health Check
HEALTH_CHECK_INTERVAL=30  # Update interval in seconds (min: 5)
HEALTH_CHECK_FILE=/tmp/worker_health  # Health check file path

# Rate Limiting
GITHUB_RATE_LIMIT=5000  # Requests per hour (default: 5000)
ANTHROPIC_RATE_LIMIT=100  # Requests per minute (default: 100)

# Anthropic API (Alternative providers)
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-4.7
ANTHROPIC_DEFAULT_HAIKU_MODEL=GLM-4.5-Air
ANTHROPIC_DEFAULT_OPUS_MODEL=GLM-4.7

# Vertex AI
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
ANTHROPIC_VERTEX_REGION=us-central1

# Langfuse (Observability)
LANGFUSE_PUBLIC_KEY=lf_pk_...
LANGFUSE_SECRET_KEY=lf_sk_...
LANGFUSE_HOST=http://langfuse:3000

# Queue
QUEUE_TYPE=redis  # redis or pubsub
REDIS_URL=redis://localhost:6379
REDIS_PASSWORD=myredissecret

# Google Pub/Sub (if QUEUE_TYPE=pubsub)
GCP_PROJECT_ID=your-project
PUBSUB_TOPIC_NAME=agent-requests
PUBSUB_SUBSCRIPTION_NAME=agent-requests-sub
```

## Webhook Configuration

### Memory Worker Settings

The memory worker uses the same Anthropic API settings as the sandbox worker. It also supports:

```bash
# Memory Worker (optional - uses Haiku for cost efficiency)
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5-20251001  # Model for memory extraction
```

The memory worker listens on the `agent:memory:requests` Redis queue and uses the Memory MCP server to read/write persistent knowledge files.

### Required Settings

```bash
# GitHub App credentials
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=789012
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

### Optional Settings

```bash
# Server
PORT=8080  # Webhook service port (1-65535)
LOG_LEVEL=INFO

# Bot Identity (for preventing infinite loops)
WEBHOOK_BOT_USERNAME=claude-code-agent[bot]  # GitHub username of the bot (must match GitHub App username)

# Queue
QUEUE_TYPE=redis
REDIS_URL=redis://localhost:6379
REDIS_PASSWORD=myredissecret
```

## Configuration Classes

The configuration is organized into logical groups:

- `GitHubConfig` - GitHub App credentials
- `AnthropicConfig` - Anthropic API settings
- `LangfuseConfig` - Observability settings
- `QueueConfig` - Message queue settings
- `WebhookConfig` - Webhook service settings
- `WorkerConfig` - Worker service settings

## Validation Examples

### Valid Configuration

```python
from shared.config import get_worker_config

try:
    config = get_worker_config()
    print(f"Configuration loaded: {config.github.github_app_id}")
except Exception as e:
    print(f"Configuration error: {e}")
```

### Invalid Configuration

If you're missing required fields:

```
FATAL: Configuration validation failed: 1 validation error for WorkerConfig
github -> github_app_id
  Field required [type=missing, input_value={}, input_type=dict]
```

If you have invalid values:

```
FATAL: Configuration validation failed: 1 validation error for WorkerConfig
github -> github_private_key
  Value error, GitHub private key must be in PEM format
```

## Environment Variable Precedence

1. Explicit environment variables (highest priority)
2. `.env` file
3. Default values in code (lowest priority)

## Type Safety

All configuration values are type-checked:

```python
config.port  # int (validated: 1-65535)
config.log_level  # str (validated: DEBUG, INFO, WARNING, ERROR, CRITICAL)
config.github_rate_limit  # int (validated: >= 1)
config.langfuse.is_enabled  # bool (computed property)
```

## Accessing Configuration

```python
# In worker
from shared.config import get_worker_config
config = get_worker_config()

# In webhook
from shared.config import get_webhook_config
config = get_webhook_config()

# Access nested config
github_app_id = config.github.github_app_id
api_key = config.anthropic.get_api_key_or_raise()
is_langfuse_enabled = config.langfuse.is_enabled
```
