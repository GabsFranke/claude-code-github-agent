# Getting Started

Complete guide to installing and running the Claude Code GitHub Agent.

## Prerequisites

- Docker & Docker Compose (recommended) OR Python 3.11+
- GitHub App with appropriate permissions
- Anthropic API Key
- ngrok (for local webhook testing)

## Quick Start (5 minutes)

### 1. Get ngrok URL

```bash
ngrok http 10000
```

Copy the forwarding URL (e.g., `https://abc123.ngrok.io`)

### 2. Create GitHub App

Go to [GitHub Settings → Developer settings → GitHub Apps → New](https://github.com/settings/apps/new):

- **Webhook URL**: `https://your-ngrok-url.ngrok.io/webhook`
- **Webhook secret**: Generate random string (save for .env)
- **Permissions**:
  - Contents: Read & write
  - Issues: Read & write
  - Pull requests: Read & write
  - Metadata: Read-only
- **Events**: Issue comment, Pull request, Pull request review
- Generate private key (.pem file)
- Install on your test repository

Note your App ID and Installation ID.

### 3. Configure

```bash
git clone https://github.com/yourusername/claude-code-github-agent.git
cd claude-code-github-agent
cp .env.example .env
```

Edit `.env`:

```bash
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_APP_ID=123456
GITHUB_INSTALLATION_ID=789012
GITHUB_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----"
GITHUB_WEBHOOK_SECRET=your-webhook-secret
```

### 4. Run

```bash
# Minimal setup
docker-compose -f docker-compose.minimal.yml up --build -d

# Or with Langfuse observability
docker-compose up --build -d
```

### 5. Test

Open a PR in your test repository. The agent will automatically review it!

## Installation Options

### Option 1: Docker (Recommended)

```bash
# Start services
docker-compose up --build -d

# View logs
docker-compose logs -f

# Check health
curl http://localhost:10000/health
docker-compose exec worker cat /tmp/worker_health

# Stop
docker-compose down
```

### Option 2: Manual Installation

```bash
# Create and activate virtual environment
python -m venv venv

# Activate venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Start Redis
redis-server

# Terminal 1: Webhook
cd services/webhook
python main.py

# Terminal 2: Worker
cd services/agent_worker
python worker.py
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

## Scaling

```bash
# Scale sandbox workers for parallel processing
docker-compose up --scale sandbox_worker=10 -d

# Monitor queue
docker-compose exec redis redis-cli -a myredissecret LLEN agent:jobs:pending
```

## Troubleshooting

### Configuration Errors

```bash
# Check required fields
grep -E "GITHUB_|ANTHROPIC_" .env

# Validate PEM format
echo "$GITHUB_PRIVATE_KEY" | head -1
```

### Import Errors

```bash
# Install dependencies
pip install -r requirements-dev.txt

# Verify shared module is importable
python -c "import shared; print(shared.__file__)"
```

### Health Check Failures

```bash
docker-compose logs worker | tail -50
docker-compose restart worker
```

### Redis Connection

```bash
redis-cli ping  # Should return PONG
docker-compose logs redis
```

## Next Steps

- [Configuration Guide](CONFIGURATION.md) - All environment variables
- [Architecture Overview](ARCHITECTURE.md) - System design
- [Development Guide](DEVELOPMENT.md) - Testing and contributing
- [Langfuse Setup](LANGFUSE_SETUP.md) - Observability

## Alternative Providers

### Z.AI (GLM models)

```bash
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-4.7
```

### Vertex AI (Google Cloud)

```bash
gcloud auth application-default login
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id
ANTHROPIC_VERTEX_REGION=global
```

## Upgrading

```bash
git pull origin main
docker-compose build
docker-compose up -d
```

Configuration is backward compatible - no .env changes needed.
