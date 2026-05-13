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

## Automatic Environment Variables

The engine injects the following environment variables into every setup command automatically:

| Variable | Value | Description |
|----------|-------|-------------|
| `$FAST_CACHE` | `/var/cache/repos/fast_cache/<hash>/` | Per-worktree directory on the native Docker volume. Use this to keep heavy file I/O off the slow Docker Desktop bind mount. |

### $FAST_CACHE — Performance on Docker Desktop (Windows / macOS)

Docker Desktop on Windows and macOS bridges your host filesystem into its Linux VM via the 9P/virtiofs protocol. Writing thousands of small files across that bridge — such as `pip install`, `npm ci`, or `cargo build` — is orders of magnitude slower than a native Linux disk and can easily exhaust your timeout budget.

`$FAST_CACHE` points to a unique directory on the native Docker volume (`/var/cache/repos`), which has full ext4 speed. Use it by:

1. **Creating** your dependency directory inside `$FAST_CACHE` (real directory — no pre-existing symlink)
2. **Symlinking** it back into the worktree so tooling finds it at the expected path

```yaml
setup_commands:
  # 1. Create venv on the fast volume
  - "python -m venv $FAST_CACHE/.venv && ln -sfn $FAST_CACHE/.venv .venv"
  # 2. Symlink now exists — pip installs onto native ext4
  - ".venv/bin/pip install -r requirements.txt"
```

> **Why not symlink first?** Tools like `python -m venv` crash if they encounter a pre-existing directory symlink. Creating the real directory first, then symlinking it, avoids this.

On a native **Linux host**, Docker bind-mounts are native ext4 speed and the variable isn't needed — but it still works safely. The variable is silently omitted if `/var/cache/repos` doesn't exist (e.g. outside Docker).

## Using sudo

The container runs as non-root user `bot` with **passwordless sudo access**. This allows installing system packages and language runtimes:

```yaml
setup_commands:
  - "sudo apt-get update"
  - "sudo apt-get install -y nodejs npm"  # Always use -y flag
  - "npm ci"
```

## Execution Behavior

- Commands run **sequentially** in the workspace directory
- If `stop_on_failure: true` (default), remaining commands are skipped on first failure
- If setup fails entirely, the job **continues anyway** — the agent can still work with source code
- Timeout applies to **all commands combined**
- Installed packages **persist in the container** until restart (first job installs the runtime, subsequent jobs skip it)

## Getting Started

```bash
# Copy the example config
cp repo-setup.example.yaml repo-setup.yaml

# Edit for your repositories, then rebuild
# (config is baked into the image, not mounted)
docker compose build sandbox_worker
docker compose up -d sandbox_worker
```

For ready-to-use examples across Python, Node.js, Ruby, Go, Rust, Java, and fullstack projects — including `$FAST_CACHE` patterns — see [`repo-setup.example.yaml`](../repo-setup.example.yaml).

## Security

Setup commands have **full sudo access** and run in the repository's worktree. Only configure setup for repositories you trust. Don't put secrets in `repo-setup.yaml` — use environment variables instead.

## Troubleshooting

| Issue | Check |
|-------|-------|
| Setup not running | Verify repo name format (`owner/repo`), check YAML syntax |
| Commands failing | Use `-y` with `apt-get`, test in a `python:3.12-slim` container |
| Timeout on Windows/macOS | Use `$FAST_CACHE` to move heavy I/O off the bind mount |
| Timeout on Linux | Increase `timeout` value, combine `apt-get update` calls |
| Changes not applying | Rebuild the image: `docker compose build sandbox_worker` |

## See Also

- [Architecture](ARCHITECTURE.md) - System design
- [Configuration](CONFIGURATION.md) - Environment variables
