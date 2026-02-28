# Simple Claude Code GitHub Agent

AI-powered GitHub agent that automatically reviews pull requests and responds to commands using Claude Code CLI and GitHub's official MCP server.

> [!WARNING]
> This agent has full write access to your repositories and can autonomously create branches, commit changes, and open PRs. The current implementation auto-approves all GitHub MCP tool calls (configured with `autoApprove: ['*']`) to allow Claude Code to operate without manual confirmation. Fine-grained permission controls are not yet implemented. Use with caution and test in a sandbox repository first.

> [!IMPORTANT]
> This project is currently self-hosted only. You'll need to run it on your own infrastructure with Docker or manually. Cloud deployment options may be added in the future.

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
- GitHub App with appropriate permissions ([create one](https://github.com/settings/apps/new))
- Anthropic API Key ([get from console](https://console.anthropic.com/))
- ngrok (for local webhook testing)

### Setup (Self-Hosting)

1. **Get your ngrok URL:**

```bash
# Start ngrok (container doesn't need to be running yet)
ngrok http 10000
```

Copy the forwarding URL (e.g., `https://abc123.ngrok.io`)

2. **Create a GitHub App:**

Go to GitHub Settings → Developer settings → GitHub Apps → New GitHub App:
- **GitHub App name**: Choose a unique name (e.g., "My Claude Agent")
- **Homepage URL**: Your repository or any URL
- **Webhook URL**: `https://your-ngrok-url.ngrok.io/webhook`
- **Webhook secret**: Generate a random string (you'll use this as GITHUB_WEBHOOK_SECRET in .env)
- **Permissions**:
  - Repository permissions:
    - Actions: Read-only (optional, for workflow insights)
    - Contents: Read & write
    - Issues: Read & write
    - Pull requests: Read & write
    - Metadata: Read-only
- **Subscribe to events**: 
  - Discussion comment
  - Issue comment
  - Issues
  - Pull request
  - Pull request review
  - Pull request review comment
  - Pull request review thread
  - Push
  - Workflow job
- Click "Create GitHub App"
- Note your **App ID** from the app settings page
- Generate and download a **private key** (save the .pem file)
- Install the app on your repository (Install App → select repositories)
- Note your **Installation ID** from the URL after installation

> Note: GitHub Apps are required for the agent to review its own PRs and interact as a bot user. Personal Access Tokens are not supported.

3. **Configure environment:**

```bash
# Copy example config
cp .env.example .env

# Edit .env with your credentials:
ANTHROPIC_AUTH_TOKEN=(from Anthropic Console)
GITHUB_APP_ID=(from your GitHub App settings)
GITHUB_INSTALLATION_ID=(from installation URL)
GITHUB_PRIVATE_KEY=(contents of the .pem file)
GITHUB_WEBHOOK_SECRET=(the secret you set when creating the app)
```

4. **Run with Docker:**

```bash
# Option 1: Minimal setup (without Langfuse observability)
docker-compose -f docker-compose.minimal.yml up --build -d

# Option 2: Full setup with Langfuse (recommended for debugging)
docker-compose up --build -d

# Optional: Scale workers for parallel processing
docker-compose up --scale worker=2 -d
# or with minimal setup:
docker-compose -f docker-compose.minimal.yml up --scale worker=2 -d

# View logs
docker-compose logs -f

# Stop services
docker-compose down
```

5. **Access Langfuse (optional, only if using full setup):**

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
- **Message Queue** - Redis for job distribution
- **Claude Code CLI** - Autonomous coding agent with GitHub MCP access
- **Langfuse** - Optional self-hosted observability platform for tracing and debugging

## Configuration

### Environment Variables

**Required:**
- `ANTHROPIC_AUTH_TOKEN`: Your Anthropic API key
- `GITHUB_WEBHOOK_SECRET`: Secret for webhook signature verification
- `GITHUB_APP_ID`: Your GitHub App ID
- `GITHUB_INSTALLATION_ID`: Installation ID from the app installation URL
- `GITHUB_PRIVATE_KEY`: Contents of the private key .pem file

**Optional:**
- `ANTHROPIC_BASE_URL`: Override API endpoint for alternative providers
- `ANTHROPIC_DEFAULT_SONNET_MODEL`: Override model name
- `LANGFUSE_PUBLIC_KEY`: Langfuse API key (pre-configured for self-hosted setup)
- `LANGFUSE_SECRET_KEY`: Langfuse secret key (pre-configured for self-hosted setup)

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

### Troubleshooting

```bash
# View logs
docker-compose logs -f

# View specific service
docker-compose logs -f worker
docker-compose logs -f webhook

# Check status
docker-compose ps
```

## License

MIT
