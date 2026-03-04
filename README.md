# Simple Claude Code GitHub Agent

AI-powered GitHub agent that automatically reviews pull requests and responds to commands using Claude Agent SDK and GitHub's official MCP server.

> [!WARNING]
> This agent has full write access to your repositories and can autonomously create branches, commit changes, and open PRs. The current implementation auto-approves all GitHub MCP tool calls (configured with `autoApprove: ['*']`) to allow Claude Code to operate without manual confirmation. Fine-grained permission controls are not yet implemented. Use with caution.

> [!IMPORTANT]
> This project is currently self-hosted only. You'll need to run it on your own infrastructure with Docker or manually. Cloud deployment options may be added in the future.

## Features

- 🤖 **Automatic PR Reviews** - Reviews code quality, security, and best practices when PRs are opened
- 💬 **Command-based Interaction** - Respond to `/agent` commands in issues and PRs
- 🔧 **Code Analysis** - Answers questions about your codebase
- 🚀 **Autonomous Actions** - Can create branches, make changes, and open PRs
- 🤝 **Specialized Subagents** - Delegates tasks to focused agents (context-gathering, code review, bug investigation, test writing)
- 📝 **Per-repo Customization** - Support for CLAUDE.md configuration files
- 📊 **Full Observability** - Self-hosted Langfuse integration for tracing tool calls and reasoning

## Quick Start

### Prerequisites

- Docker & Docker Compose (recommended) OR Python 3.11+
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

# Optional: Scale sandbox workers for parallel processing
docker-compose up --scale sandbox_worker=5 -d
# or with minimal setup:
docker-compose -f docker-compose.minimal.yml up --scale sandbox_worker=5 -d

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

### Subagents

The agent uses specialized subagents for intelligent PR reviews:

**PR Review Subagents (used selectively based on changes):**

- **architecture-reviewer**: Evaluates design patterns, SOLID principles, and system architecture
- **security-reviewer**: Scans for vulnerabilities (SQL injection, XSS, auth issues, etc.)
- **bug-hunter**: Identifies potential bugs, edge cases, and error handling issues
- **code-quality-reviewer**: Reviews code style, readability, and maintainability

**General Purpose Subagents:**

- **context-gatherer**: Explores codebase to find relevant files
- **bug-investigator**: Traces bugs to root causes
- **test-writer**: Creates comprehensive test cases

The main agent intelligently decides which subagents to use based on the PR:

- Documentation changes → `code-quality-reviewer` only
- Bug fixes → `bug-hunter` + `code-quality-reviewer`
- New features → Multiple agents as needed
- Security-critical changes → All agents including `security-reviewer`
- Typo fixes → May skip agents entirely

The coordinator explains which agents were used and why in the review summary.

You can also request specific subagents manually:

```
/agent use security-reviewer to check for vulnerabilities
/agent have architecture-reviewer evaluate this design
/agent ask bug-hunter to find edge cases in the validation logic
```

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for detailed system design.

**High-level flow**:

```
GitHub Event → Webhook → Redis Queue → Worker → Job Queue → Sandbox Pool → GitHub MCP → GitHub API
```

**Key components**:

- **Webhook Service** - Receives GitHub events (FastAPI)
- **Worker** - Lightweight job coordinator
- **Sandbox Pool** - Executes Claude SDK in isolated workspaces
- **Result Poster** - Posts responses to GitHub
- **Redis** - Message queue and job queue
- **Claude Agent SDK** - Autonomous coding agent
- **GitHub MCP** - Official GitHub integration

**Scaling**: `docker-compose up --scale sandbox_worker=10 -d`

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) - Installation and setup
- [Architecture](docs/ARCHITECTURE.md) - System design and components
- [Configuration](docs/CONFIGURATION.md) - Environment variables
- [Development](docs/DEVELOPMENT.md) - Testing and contributing
- [Langfuse Setup](docs/LANGFUSE_SETUP.md) - Observability
- [PR Review Flow](docs/PR_REVIEW_FLOW.md) - Review workflow
- [Plugins](docs/PLUGINS.md) - Plugin system
- [Subagents](docs/SUBAGENTS.md) - Subagent system

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

You can use alternative providers instead of Anthropic's API:

**Option 1: Z.AI (GLM models via Anthropic-compatible API)**

```bash
# .env
ANTHROPIC_API_KEY=your_zai_api_key
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-4.7
ANTHROPIC_DEFAULT_HAIKU_MODEL=GLM-4.5-Air
ANTHROPIC_DEFAULT_OPUS_MODEL=GLM-4.7
```

**Option 2: Vertex AI (Claude models via Google Cloud)**

```bash
# 1. Authenticate with gcloud
gcloud auth application-default login

# 2. Configure .env
ANTHROPIC_API_KEY=sk-ant-unused
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id
ANTHROPIC_VERTEX_REGION=global

# 3. Recreate worker with new env vars
docker-compose up -d worker
```

The agent works identically with all providers - just toggle the environment variables and recreate the container.

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=shared --cov=services --cov-report=html
```

See [docs/TESTING.md](docs/TESTING.md) for the complete testing guide.

Tests run automatically on every PR via GitHub Actions.

## Development

### Project Structure

```
simple-claude-code-github-agent/
├── services/
│   ├── agent_worker/         # Claude Code worker
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
