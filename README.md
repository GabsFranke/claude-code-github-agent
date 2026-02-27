# Simple Claude Code GitHub Agent

AI-powered GitHub agent that automatically reviews pull requests and responds to commands using Claude Code CLI and GitHub's official MCP server.

## Features

- 🤖 **Automatic PR Reviews** - Reviews code quality, security, and best practices when PRs are opened
- 💬 **Command-based Interaction** - Respond to `/agent` commands in issues and PRs
- 🔧 **Code Analysis** - Answers questions about your codebase
- 🚀 **Autonomous Actions** - Can create branches, make changes, and open PRs
- 📝 **Per-repo Customization** - Support for CLAUDE.md configuration files
- 📊 **Full Observability** - Self-hosted Langfuse integration for tracing tool calls and reasoning

## Quick Start

### Prerequisites

- Docker & Docker Compose (recommended) OR Node.js 18+ & Python 3.11+
- GitHub Personal Access Token with `repo` scope ([create one](https://github.com/settings/personal-access-tokens/new))
- Anthropic API Key ([get from console](https://console.anthropic.com/))
- ngrok (for local testing) or cloud hosting

### Setup

1. **Configure environment:**

```bash
# Copy example config
cp .env.example .env

# Edit .env with your credentials:
# - ANTHROPIC_AUTH_TOKEN (from Anthropic Console)
# - GITHUB_PAT (Personal Access Token with repo scope)
# - GITHUB_WEBHOOK_SECRET (any random string)
```

2. **Run with Docker:**

```bash
# Start all services
docker-compose up --build -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

3. **Expose webhook with ngrok:**

```bash
ngrok http 8080
```

4. **Configure GitHub webhook:**

Go to your repository Settings → Webhooks → Add webhook:
- Payload URL: `https://your-ngrok-url.ngrok.io/webhook`
- Content type: `application/json`
- Secret: (same as GITHUB_WEBHOOK_SECRET in .env)
- Events: Select "Issue comments" and "Pull requests"

5. **Access Langfuse (optional):**

View agent traces and debug tool calls at http://localhost:7500
- Email: `admin@example.com`
- Password: `admin123`

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for details.

## Usage

### Automatic PR Reviews

When you open a PR, the agent automatically reviews it and posts:
- General code review summary
- Inline comments on specific lines
- Suggestions for improvements

### Manual Commands

Comment on any issue or PR with `/agent` followed by your request:

```
/agent explain how authentication works
/agent review the security of this code
/agent create a PR to add error handling
/agent what does this function do?
```

### Per-Repository Configuration

Add a `CLAUDE.md` file to your repository root with custom instructions:

```markdown
# Agent Instructions

When working on this project:
- Always run tests before creating PRs
- Follow the existing code style
- Update documentation if you change APIs
```

## Architecture

```
GitHub Event → Webhook → Redis Queue → Worker → Claude Code CLI
                                                      ↓
                                              GitHub MCP Server
                                                   (Official)
                                                      ↓
                                                 GitHub API
```

**Components:**
- **Webhook Service** - Receives GitHub events (FastAPI)
- **Worker** - Spawns Claude Code CLI instances to process requests
- **Message Queue** - Redis (self-hosted) or Google Pub/Sub (cloud)
- **Claude Code CLI** - Autonomous coding agent with GitHub MCP access
- **Langfuse** - Self-hosted observability platform for tracing and debugging

## Configuration

### Environment Variables

**Required:**
- `ANTHROPIC_AUTH_TOKEN`: Your Anthropic API key
- `GITHUB_PAT`: GitHub Personal Access Token with `repo` scope
- `GITHUB_WEBHOOK_SECRET`: Secret for webhook signature verification

**Optional:**
- `ANTHROPIC_BASE_URL`: Override API endpoint for alternative providers
- `ANTHROPIC_DEFAULT_SONNET_MODEL`: Override model name
- `QUEUE_TYPE`: `redis` (self-hosted) or `pubsub` (cloud)
- `LANGFUSE_PUBLIC_KEY`: Langfuse API key (pre-configured for self-hosted)
- `LANGFUSE_SECRET_KEY`: Langfuse secret key (pre-configured for self-hosted)

### Using Alternative AI Providers

You can use alternative Anthropic-compatible providers like [z.ai](https://z.ai):

```bash
# .env
ANTHROPIC_AUTH_TOKEN=your_zai_api_key
ANTHROPIC_BASE_URL=https://api.z.ai/v1
ANTHROPIC_DEFAULT_SONNET_MODEL=glm-4.7
```

## Development

### Project Structure
```
simple-claude-code-github-agent/
├── services/
│   ├── agent-worker/         # Claude Code worker
│   └── webhook/              # Webhook receiver
├── shared/
│   └── queue.py             # Message queue abstraction
├── docker-compose.yml       # Docker Compose config
└── docs/                    # Documentation
```

### Manual Setup (without Docker)

See [START.md](START.md) for detailed instructions.

### Testing

```bash
# View logs
docker-compose logs -f

# View specific service
docker-compose logs -f worker
docker-compose logs -f webhook

# Check status
docker-compose ps
```

## Deployment

### Self-Hosted (Development)
Uses Docker Compose with Redis:
```bash
docker-compose up -d
```

### Cloud (Production)
Deploy to Google Cloud Run with Pub/Sub. See [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## Documentation

- **[START.md](START.md)** - Detailed setup guide
- **[ARCHITECTURE.md](ARCHITECTURE.md)** - Technical architecture
- **[LANGFUSE_SETUP.md](LANGFUSE_SETUP.md)** - Observability and tracing setup

## License

MIT
