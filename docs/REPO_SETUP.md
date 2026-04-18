# Repository Setup

Automatically install dependencies and run setup commands before the agent starts working on a repository. This lets the agent run tests, build commands, and use language-specific tools.

Setup commands run after the git worktree is created but before the Claude SDK executes. Failures are non-fatal — the agent can still work with source code.

## Configuration

Define setup commands in `repo-setup.yaml` at the project root:

```yaml
repositories:
  owner/repo-name:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"
      - "npm ci"
    timeout: 300
    stop_on_failure: true
    env:
      NODE_ENV: "development"

default:
  enabled: false
  setup_commands: []
  timeout: 300
```

### Options

| Field | Default | Description |
|-------|---------|-------------|
| `setup_commands` | `[]` | Shell commands to run sequentially in the workspace directory |
| `timeout` | `300` | Timeout in seconds for all commands combined |
| `stop_on_failure` | `true` | Stop running remaining commands if one fails |
| `env` | — | Custom environment variables merged with system env |

Set `default.enabled: false` (recommended) to only run setup for explicitly configured repositories.

## Using sudo

The container runs as non-root user `bot` with **passwordless sudo access**. This allows installing system packages and language runtimes:

```yaml
setup_commands:
  - "sudo apt-get update"
  - "sudo apt-get install -y nodejs npm"  # Always use -y flag
  - "npm ci"
```

## Examples

```yaml
# Python with venv (recommended — isolated per job)
repositories:
  myorg/python-api:
    setup_commands:
      - "python -m venv .venv"
      - |
        .venv/bin/pip install -r requirements.txt
        .venv/bin/pip install -r requirements-dev.txt
    timeout: 300
    env:
      PYTHONPATH: "/workspace/src"

# Node.js
repositories:
  myorg/frontend:
    setup_commands:
      - |
        sudo apt-get update
        sudo apt-get install -y nodejs npm
      - "npm ci"
    timeout: 300

# Rust — rustup and cargo share the same shell so `source` takes effect
repositories:
  myorg/rust-cli:
    setup_commands:
      - |
        curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
        source $HOME/.cargo/env
        cargo fetch
        cargo build --release
    timeout: 900
```

Use a plain string for single commands. Use `|` (block scalar) for multi-line scripts that need a shared shell environment (`source`, `export`, venv activation, etc.).

For more examples (Go, Ruby, Java, monorepos, fullstack), see `repo-setup.example.yaml`.

## Getting Started

```bash
# Copy the example config
cp repo-setup.example.yaml repo-setup.yaml

# Add your repositories
# Then rebuild (config is baked into the image, not mounted)
docker-compose build sandbox_worker
docker-compose up -d sandbox_worker
```

## Execution Behavior

- Commands run **sequentially** in the workspace directory
- If `stop_on_failure: true` (default), remaining commands are skipped on first failure
- If setup fails entirely, the job **continues anyway** — the agent can still work with source code
- Timeout applies to **all commands combined**
- Installed packages **persist in the container** until restart (first job installs the runtime, subsequent jobs skip it)

## Security

Setup commands have **full sudo access** and run in the repository's worktree. Only configure setup for repositories you trust. Don't put secrets in `repo-setup.yaml` — use environment variables instead.

## Troubleshooting

| Issue | Check |
|-------|-------|
| Setup not running | Verify repo name format (`owner/repo`), check YAML syntax |
| Commands failing | Use `-y` with `apt-get`, test in a `python:3.12-slim` container |
| Timeout | Increase `timeout` value, combine `apt-get update` calls |
| Changes not applying | Rebuild the image: `docker-compose build sandbox_worker` |

## See Also

- [Architecture](ARCHITECTURE.md) - System design
- [Configuration](CONFIGURATION.md) - Environment variables
