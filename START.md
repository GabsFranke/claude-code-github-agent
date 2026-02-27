# Getting Started

## Prerequisites

- Docker & Docker Compose (recommended)
- OR Node.js 18+ and Python 3.11+ (for manual setup)
- GitHub Personal Access Token
- Anthropic API Key
- ngrok (for local testing)

## Quick Start with Docker

### 1. Get Your API Keys

**Anthropic API Key:**
1. Go to https://console.anthropic.com/
2. Sign up or log in
3. Navigate to API Keys
4. Create a new API key

**GitHub Personal Access Token:**
1. Go to https://github.com/settings/personal-access-tokens/new
2. Give it a name (e.g., "Claude Code Agent")
3. Select repository access (All repositories or specific ones)
4. Add permissions:
   - Contents: Read and write
   - Issues: Read and write
   - Pull requests: Read and write
   - Metadata: Read-only
5. Generate token and copy it

### 2. Configure Environment

```bash
# Copy example config
cp .env.example .env

# Edit .env and add your keys:
# - ANTHROPIC_AUTH_TOKEN=sk-ant-...
# - GITHUB_PAT=github_pat_...
# - GITHUB_WEBHOOK_SECRET=any_random_string_here
```

### 3. Start Services

```bash
# Build and start all services
docker-compose up --build -d

# View logs
docker-compose logs -f

# Check status
docker-compose ps
```

You should see:
- `redis` - Running and healthy (shared by agent queue and Langfuse)
- `langfuse-db` - PostgreSQL database for Langfuse
- `langfuse-clickhouse` - ClickHouse for storing traces/observations
- `langfuse` - Observability UI on http://localhost:7500
- `webhook` - Running on port 8080
- `worker` - Running and waiting for messages

### Step 4: Setup Langfuse (Optional but Recommended)

Langfuse provides visibility into your agent's prompts, tool calls, and responses.

With the new setup, Langfuse auto-creates everything on first start:

1. Open http://localhost:7500 in your browser
2. Login with:
   - Email: `admin@example.com`
   - Password: `admin123`
3. The project and API keys are already configured in `.env`:
   ```bash
   LANGFUSE_PUBLIC_KEY=lf_pk_github_agent_public
   LANGFUSE_SECRET_KEY=lf_sk_github_agent_secret
   ```
4. Restart worker to enable tracing: `docker-compose restart worker`

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for detailed instructions.

### 5. Expose Webhook with ngrok

```bash
# In a new terminal
ngrok http 8080
```

Copy the HTTPS URL (e.g., `https://abc123.ngrok.io`)

### 5. Configure GitHub Webhook

1. Go to your repository on GitHub
2. Settings → Webhooks → Add webhook
3. Set Payload URL: `https://your-ngrok-url.ngrok.io/webhook`
4. Set Content type: `application/json`
5. Set Secret: (same as GITHUB_WEBHOOK_SECRET in .env)
6. Select individual events:
   - ✓ Issue comments
   - ✓ Pull requests
7. Click "Add webhook"

### 6. Test It!

**Test Automatic PR Review:**
1. Create a new branch and make some changes
2. Open a pull request
3. Watch the agent automatically review it!

**Test Manual Command:**
1. Create an issue or comment on a PR
2. Comment: `/agent explain how this works`
3. Watch the agent respond!

Check logs: `docker-compose logs -f worker`

## Manual Setup (Without Docker)

### 1. Install Dependencies

**Install Node.js and Claude Code CLI:**
```bash
# Install Node.js 18+ from https://nodejs.org/
# Then install Claude Code CLI
npm install -g @anthropic-ai/claude-code
```

**Install Python dependencies:**
```bash
# Webhook service
cd services/webhook
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
cd ../..

# Worker service
cd services/agent-worker
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
cd ../..
```

**Install Redis:**
```bash
# macOS
brew install redis
brew services start redis

# Ubuntu/Debian
sudo apt-get install redis-server
sudo systemctl start redis

# Windows
# Download from https://redis.io/download
```

### 2. Configure Environment

```bash
cp .env.example .env
# Edit .env with your keys
```

### 3. Run Services

**Terminal 1 - Webhook:**
```bash
cd services/webhook
source venv/bin/activate
python main.py
```

**Terminal 2 - Worker:**
```bash
cd services/agent-worker
source venv/bin/activate
python worker.py
```

**Terminal 3 - ngrok:**
```bash
ngrok http 8080
```

### 4. Configure GitHub Webhook

Same as Docker setup above.

## Per-Repository Configuration

Add a `CLAUDE.md` file to your repository root with custom instructions:

```markdown
# Agent Instructions

When working on this project:

## Code Style
- Use TypeScript strict mode
- Follow existing naming conventions
- Add JSDoc comments for public APIs

## Testing
- Always run `npm test` before creating PRs
- Add tests for new features
- Maintain >80% code coverage

## Review Guidelines
- Focus on security and performance
- Check for proper error handling
- Verify accessibility compliance
```

The agent will automatically read and follow these instructions!

## Troubleshooting

### Worker fails to start

**Error: "ANTHROPIC_AUTH_TOKEN not set"**
- Make sure .env file has `ANTHROPIC_AUTH_TOKEN=sk-ant-...`
- Restart docker-compose: `docker-compose down && docker-compose up -d`

**Error: "GITHUB_PAT not set"**
- Make sure .env file has `GITHUB_PAT=github_pat_...`
- Token must have `repo` scope with proper permissions

### Claude Code not working

**Error: "Not logged in"**
- Worker automatically logs in using ANTHROPIC_AUTH_TOKEN
- Check logs: `docker-compose logs worker`

**Error: "GitHub MCP server not configured"**
- Worker automatically configures GitHub MCP on startup
- Check logs for configuration errors

### Webhook not receiving events

**Check ngrok:**
- Make sure ngrok is running
- URL should be HTTPS
- Check ngrok dashboard: http://localhost:4040

**Check GitHub webhook:**
- Go to repository Settings → Webhooks
- Click on your webhook
- Check "Recent Deliveries" for errors
- Response should be 200 OK

### Redis connection failed

**Docker:**
```bash
docker-compose logs redis
docker-compose restart redis
```

**Manual:**
```bash
redis-cli ping  # Should return PONG
```

### Agent not responding

**Check worker logs:**
```bash
docker-compose logs -f worker
```

**Common issues:**
- Claude Code timeout (10 min limit)
- GitHub API rate limit
- Invalid GITHUB_PAT permissions

## Using Alternative AI Providers

To use providers like z.ai instead of Anthropic:

```bash
# .env
ANTHROPIC_AUTH_TOKEN=your_zai_api_key
ANTHROPIC_BASE_URL=https://api.z.ai/v1
ANTHROPIC_DEFAULT_SONNET_MODEL=glm-4.7
```

The worker will automatically configure Claude Code with these settings on startup.

## Next Steps

- Read [ARCHITECTURE.md](ARCHITECTURE.md) for system design
- Check [PROGRESS.md](PROGRESS.md) for development status
- Deploy to cloud (Google Cloud Run + Pub/Sub)

## Stopping Services

```bash
# Docker
docker-compose down

# Manual
# Press Ctrl+C in each terminal
```
