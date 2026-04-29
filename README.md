<div align="center">

# Claude Code GitHub Agent

**Self-hosted GitHub agent that runs Claude SDK on any of 40+ webhook events — fully configurable via YAML workflows and plugins**

[![CI](https://github.com/GabsFranke/claude-code-github-agent/actions/workflows/test.yml/badge.svg)](https://github.com/GabsFranke/claude-code-github-agent/actions/workflows/test.yml) [![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/) [![Docker](https://img.shields.io/badge/docker-ready-blue.svg)](https://www.docker.com/) [![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

[Getting Started](#quick-start) · [Usage](#usage) · [Customization](#customization) · [Docs](#documentation) · [Contributing](#contributing)

</div>

---

## What It Does

A self-hosted GitHub agent that hooks into **40+ webhook events** and runs Claude SDK with full repository access — reading files, making changes, and interacting with GitHub via MCP. Everything is configured through **YAML workflows** and **plugins**:

```yaml
# workflows.yaml — add new behaviors without touching code
my-workflow:
  triggers:
    events:
      - event: pull_request.opened
      - event: issues.labeled
        filters:
          label.name: "bug"
    commands: [/my-command]
  prompt:
    template: "Analyze {repo} #{issue_number}"
```

**Built-in workflows** include PR review, CI failure auto-fix, issue triage, and a generic `/agent` command. **Plugins** add specialized agents (code reviewers, CI failure analyzers, retrospector). And the agent **remembers** your codebases across sessions through persistent memory and semantic search.

Runs on your infrastructure. Scales horizontally. Full observability via Langfuse.

## Key Features

### Event-Driven Engine

- **40+ GitHub events** — PRs, issues, comments, pushes, CI/CD, discussions, labels, releases, and more
- **YAML-driven workflows** — Define triggers, commands, filters, and prompts in `workflows.yaml`, no code changes needed
- **Slash commands** — `/review`, `/fix-ci`, `/triage`, `/agent <request>` in any issue or PR comment
- **Horizontal scaling** — Scale sandbox workers independently: `make up SANDBOX=10`

### Claude Code Integration

The agent runs Claude SDK with the full Claude Code feature set. Because `~/.claude/` is bind-mounted from your host into the container, anything you install with `--scope user` in Claude Code CLI is automatically available to the bot — no extra configuration needed.

- **Plugins** — Install with `/plugin` in Claude Code CLI (choose user scope). Built-in plugins include PR review, CI failure analysis, and retrospector. User-installed plugins in `~/.claude/plugins/` are picked up automatically.
- **Skills** — Any `~/.claude/skills/<skill-name>/SKILL.md` is discovered by the SDK and invokable via the `Skill` tool. Create your own or install from the community.
- **MCP servers** — Servers installed with `claude mcp add --scope user` are available inside Docker when `ALLOW_HOST_MCP=true` (default). The MCP proxy bridges them over SSE.
- **Memory** — Persistent per-repo knowledge across sessions. The `@memory-extractor` subagent reads session transcripts after each run and updates memory files.
- **Hooks** — Event-driven scripts on `Stop`, `SubagentStop`, and other lifecycle events. Used for Langfuse tracing and transcript persistence.
- **Subagents** — Delegate to specialized agents via the `Task` tool. Each plugin contributes its own agents (12 built in across 4 plugins).
- **CLAUDE.md** — Per-repo customization files read at session start. Define project conventions, constraints, and preferences.

### Code Intelligence

- **3-layer context** — File tree → AST code tools → semantic vector search
- **Structural awareness** — Aider-style repomap with tree-sitter + PageRank (10 languages), personalized per PR
- **5 MCP servers** — GitHub (HTTP), GitHub Actions, Memory, Codebase Tools, Semantic Search (proxied via SSE)
- **Self-improvement** — Retrospector analyzes past sessions and proposes instruction improvements via PRs

## Quick Start

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [Make](https://www.gnu.org/software/make/) (or use `docker compose` commands directly below)
- [GitHub App](https://github.com/settings/apps/new) (see setup below)
- Anthropic-compatible API key (Anthropic, Z.AI, Vertex AI, etc.)
- ngrok or similar tunnel (for local webhook testing)

### 1. Create a GitHub App

Go to **GitHub Settings → Developer settings → GitHub Apps → New GitHub App**:

| Field              | Value                                     |
| ------------------ | ----------------------------------------- |
| **Webhook URL**    | `https://your-ngrok-url.ngrok.io/webhook` |
| **Webhook secret** | A random string (save for `.env`)         |

**Repository permissions:**

| Permission    | Access       |
| ------------- | ------------ |
| Actions       | Read-only    |
| Contents      | Read & write |
| Issues        | Read & write |
| Pull requests | Read & write |

**Subscribe to events:** Choose which events GitHub sends to the webhook. For the built-in workflows, enable: Issue comment, Issues, Pull request, Pull request review, Pull request review comment, Pull request review thread, Push, Workflow job. You can subscribe to more or fewer events at any time — see the [full list of supported events](docs/WORKFLOWS.md#supported-events). The agent only acts on what you enable here and configure in [workflows.yaml](workflows.yaml).

After creating: note the **App ID**, generate a **private key** (.pem), install the app on your repos, and note the **Installation ID** from the URL.

### 2. Configure and Run

```bash
git clone https://github.com/GabsFranke/claude-code-github-agent.git
cd claude-code-github-agent
cp .env.example .env               # Edit .env with your credentials
cp repo-setup.example.yaml repo-setup.yaml  # Edit Per-repo dependency setup (optional)
```

```bash
# Build, start services, and open ngrok tunnel
make start

# Or step by step:
make build    # Build all Docker images
make up       # Start all services (detached)
make ngrok    # Open ngrok tunnel to webhook on port 10000

# Minimal setup (no Langfuse)
make up-minimal
```

Run `make help` to see all available targets. Service logs are written to `./logs/` per service — use `tail -f logs/webhook.log` or `make logs` to follow along.

<details>
<summary>Using docker compose directly</summary>

```bash
# Minimal setup
docker-compose -f docker-compose.minimal.yml up --build -d

# Full setup with Langfuse observability
docker-compose up --build -d
```

</details>

### Alternative AI Providers

The agent works with any Anthropic-compatible API. The easiest way to configure a provider is through `~/.claude/settings.json` — since `~/.claude/` is bind-mounted, the SDK picks it up automatically and it takes precedence over env vars:

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "your-token",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GLM-4.7-Flash",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "GLM-5.1",
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM-5.1"
  }
}
```

This works for Ollama, Vertex AI, Z.AI, or any Anthropic-compatible endpoint. Env vars in `.env` are still supported as a fallback but won't override `settings.json`.

<details>
<summary>Using .env instead (fallback)</summary>

```bash
# Z.AI (GLM models)
ANTHROPIC_BASE_URL=https://api.z.ai/api/anthropic
ANTHROPIC_DEFAULT_SONNET_MODEL=GLM-5.1

# Google Vertex AI
ANTHROPIC_VERTEX_PROJECT_ID=your-project
ANTHROPIC_VERTEX_REGION=global
```

</details>

## Usage

### Built-in Workflows

| Trigger                | What happens                                           |
| ---------------------- | ------------------------------------------------------ |
| PR opened              | Full code review with specialized agents               |
| CI job fails           | Analyzes logs, identifies root cause, pushes fix       |
| Issue opened           | Triages with priority, complexity, and type assessment |
| Issue labeled `triage` | Same triage, triggered by label                        |

### Slash Commands

Comment on any issue or PR:

```
/review                          # Full PR review
/fix-ci                          # Analyze and fix CI failures
/triage                          # Triage an issue
/agent review the auth logic     # Generic request with natural language
/agent find all uses of deprecated API
```

## Customization

### Add a Workflow

Edit `workflows.yaml` to define new triggers and behaviors — no code changes needed:

```yaml
workflows:
  my-workflow:
    triggers:
      events:
        - event: issues.opened
      commands: [/my-command]
    prompt:
      template: "Analyze {repo} #{issue_number}"
    context:
      repomap_budget: 2048
```

See [WORKFLOWS.md](docs/WORKFLOWS.md) for the full reference, and [CONFIGURATION.md](docs/CONFIGURATION.md) for environment variables.

### Add a Plugin

You can extend the agent with plugins in two ways:

**App-specific plugins** — Create a plugin directory under the repo's `plugins/` folder with a `.claude-plugin/plugin.json` manifest. Agents, commands, and MCP servers are auto-discovered at runtime. See [PLUGINS.md](docs/PLUGINS.md) for details.

**User-installed plugins** — Install with `/plugin` in the Claude Code CLI (choose `user` scope). Plugins are saved to `~/.claude/plugins/` and automatically picked up by the SDK inside the sandbox. See the [Anthropic plugin docs](https://docs.anthropic.com/en/docs/claude-code/plugins) for more.

### Per-Repository Instructions

Add a `CLAUDE.md` to any repo root. The agent reads it before every session and persists learned knowledge across sessions.

### Per-Repository Setup

Configure dependency installation and build commands per repo in `repo-setup.yaml` — lets the agent run tests, use language tools, and build the project. See [REPO_SETUP.md](docs/REPO_SETUP.md).

## Documentation

| Document                                 | Description                           |
| ---------------------------------------- | ------------------------------------- |
| [Architecture](docs/ARCHITECTURE.md)     | System design, components, data flows |
| [Development](docs/DEVELOPMENT.md)       | Testing, deployment, contributing     |
| [Workflows](docs/WORKFLOWS.md)           | Creating and managing workflows       |
| [Configuration](docs/CONFIGURATION.md)   | Environment variables reference       |
| [Plugins](docs/PLUGINS.md)               | Plugin system                         |
| [Subagents](docs/SUBAGENTS.md)           | Subagent system                       |
| [Repo Setup](docs/REPO_SETUP.md)         | Per-repository dependency setup       |
| [Langfuse Setup](docs/LANGFUSE_SETUP.md) | Observability configuration           |

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Make changes and add tests
4. Run quality checks: `bash ./check-code.sh`
5. Open a PR to `develop`

See [DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full guide.

## License

[MIT](LICENSE)
