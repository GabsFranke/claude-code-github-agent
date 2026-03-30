# Repository Setup Configuration

The repository setup system allows you to automatically install dependencies and run setup commands when preparing a workspace for each repository. This enables the agent to run tests, execute build commands, and use language-specific tools.

## Overview

When a sandbox worker creates a git worktree for a job, it can optionally run setup commands before executing the Claude SDK. This is useful for:

- Installing dependencies (pip, npm, bundle, etc.)
- Installing language runtimes (Node.js, Ruby, Go, etc.) via sudo
- Building the project
- Setting up test databases
- Configuring development environment
- Running pre-commit hooks

The sandbox container runs as a non-root user but has **passwordless sudo access**, allowing you to install any system packages or language runtimes needed for your repositories.

## Configuration

Setup commands are defined in `repo-setup.yaml` in the project root. This file follows the same pattern as `workflows.yaml` - a single YAML file that defines setup for all repositories.

### Basic Structure

```yaml
repositories:
  owner/repo-name:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"
      - "npm install"
    timeout: 300 # seconds
    stop_on_failure: true # Stop if any command fails (default)
    env: # Optional custom environment variables
      NODE_ENV: "development"

default:
  enabled: false # Don't run setup by default
  setup_commands: []
  timeout: 300
  stop_on_failure: true
```

### Example Configurations

#### Python Project (No sudo needed)

Python is pre-installed in the container, so you can use pip directly:

```yaml
repositories:
  myorg/python-api:
    setup_commands:
      - "pip install -r requirements.txt"
      - "pip install -r requirements-dev.txt"
    timeout: 300
    env:
      PYTHONPATH: "/workspace/src"
      ENVIRONMENT: "test"
```

#### Node.js Project

Install Node.js first, then install dependencies:

```yaml
repositories:
  myorg/frontend:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"
      - "npm ci"
      - "npm run build:dev"
    timeout: 300
```

#### Ruby Project

Install Ruby first, then use bundle:

```yaml
repositories:
  myorg/rails-app:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y ruby-full"
      - "bundle install --jobs=4"
      - "bundle exec rails db:setup"
    timeout: 600
    env:
      RAILS_ENV: "test"
```

#### Go Project

Install Go compiler first:

```yaml
repositories:
  myorg/go-service:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y golang-go"
      - "go mod download"
      - "go build ./..."
    timeout: 300
```

#### Rust Project

Install Rust via rustup (no sudo needed for user installation):

```yaml
repositories:
  myorg/rust-cli:
    setup_commands:
      - "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
      - "source $HOME/.cargo/env"
      - "cargo fetch"
      - "cargo build --release"
    timeout: 900
```

#### Monorepo

Install Node.js and yarn, then build multiple workspaces:

```yaml
repositories:
  myorg/monorepo:
    setup_commands:
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"
      - "sudo npm install -g yarn"
      - "yarn install --frozen-lockfile"
      - "yarn workspace @myorg/api build"
      - "yarn workspace @myorg/web build"
    timeout: 600
```

#### Multiple Languages

Combine Python and Node.js in one project:

```yaml
repositories:
  myorg/fullstack:
    setup_commands:
      - "pip install -r backend/requirements.txt"
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"
      - "npm ci --prefix frontend"
      - "npm run build --prefix frontend"
    timeout: 400
```

## Configuration Options

### Per-Repository Options

- **setup_commands** (required): List of shell commands to run sequentially
- **timeout** (optional, default: 300): Timeout in seconds for all commands combined
- **stop_on_failure** (optional, default: true): Stop executing remaining commands if one fails
- **env** (optional): Dictionary of custom environment variables

### Default Configuration

The `default` section applies to repositories not explicitly configured:

```yaml
default:
  enabled: true # Enable for all repos
  setup_commands:
    - "echo 'Running default setup'"
  timeout: 300
```

Set `enabled: false` (recommended) to only run setup for explicitly configured repositories.

## Using sudo

The container runs as a non-root user (`bot`) but has **passwordless sudo access**. This allows you to:

- Install system packages: `sudo apt-get install -y <package>`
- Install language runtimes: Node.js, Ruby, Go, Java, etc.
- Modify system configuration if needed
- Install global tools: `sudo npm install -g <tool>`

**Important:** Always use the `-y` flag with `apt-get install` to avoid interactive prompts:

```bash
# Good
sudo apt-get install -y nodejs

# Bad (will hang waiting for confirmation)
sudo apt-get install nodejs
```

## Execution Behavior

### Command Execution

1. Commands run **sequentially** in the order specified
2. Commands run in the **workspace directory** (git worktree root)
3. If a command **fails** (non-zero exit code) and `stop_on_failure: true` (default), remaining commands are **skipped**
4. If `stop_on_failure: false`, all commands run regardless of failures
5. If setup **fails**, the job **continues** anyway (agent can still work with source code)
6. All output (stdout/stderr) is **logged** for debugging

### Timeout Behavior

- Timeout applies to **all commands combined**, not per-command
- If timeout is reached, the process is **killed** and remaining commands are **skipped**
- Job continues even if setup times out

### Environment Variables

- Custom env vars are **merged** with system environment
- System env vars take precedence if conflicts exist
- Common env vars available: `PATH`, `HOME`, `USER`, etc.

### Caching

- Installed packages **persist in the container** until restart
- First job for a repo: installs language runtime (~1-3 minutes)
- Subsequent jobs: runtime already installed (0 seconds)
- Container restart: need to reinstall everything

## Setup Flow

```
1. Webhook receives GitHub event
2. Worker routes to workflow
3. Repo sync clones/updates bare repository
4. Sandbox worker creates git worktree
5. Sandbox worker injects git credentials
6. → Sandbox worker runs setup commands (NEW)
7. Sandbox worker executes Claude SDK
8. Results posted to GitHub
```

## Getting Started

### 1. Create Configuration

```bash
# Copy example file
cp repo-setup.yaml.example repo-setup.yaml

# Edit with your repositories
nano repo-setup.yaml
```

### 2. Add Your Repositories

```yaml
repositories:
  yourorg/yourrepo:
    setup_commands:
      - "pip install -r requirements.txt"
    timeout: 300
```

### 3. Test Commands Locally (Optional)

Test your setup commands in a similar environment:

```bash
# Start a Python 3.11 container
docker run -it --rm python:3.11-slim bash

# Install sudo
apt-get update && apt-get install -y sudo

# Test your commands
sudo apt-get install -y nodejs npm
npm --version
```

### 4. Deploy

```bash
# Rebuild sandbox worker with new Dockerfile
docker-compose up --build -d sandbox_worker

# Or restart all services
docker-compose restart
```

The `repo-setup.yaml` file is mounted into the sandbox worker container, so changes take effect on container restart.

## Monitoring

### View Setup Logs

```bash
# Watch sandbox worker logs
docker-compose logs -f sandbox_worker

# Look for setup-related messages:
# - "Found setup configuration for owner/repo"
# - "Running 3 setup command(s) for owner/repo"
# - "[1/3] Running: sudo apt-get install -y nodejs"
# - "✓ Setup completed successfully for owner/repo in 45.2s"
```

### Debug Setup Failures

If setup fails, check logs for:

```
✗ Setup completed with failures for owner/repo in 12.3s
Failed command: npm install - Command 'npm install' returned non-zero exit status 1
```

Common issues:

- Forgot `-y` flag with apt-get (hangs waiting for confirmation)
- Network issues downloading packages
- Timeout too short for large installations
- Missing system dependencies (e.g., build tools for native modules)

## Performance Considerations

### Installation Times

First job for a repository (cold start):

- **Python packages**: 30s - 2min
- **Node.js installation + packages**: 1-3min
- **Ruby installation + gems**: 2-5min
- **Go installation + modules**: 1-2min
- **Rust installation + build**: 10-20min

Subsequent jobs (warm start):

- Language runtime already installed: 0s
- Only package installation time

### Optimization Tips

1. **Use lock files** for reproducible installs:
   - `npm ci` instead of `npm install`
   - `yarn install --frozen-lockfile`
   - `pip install -r requirements.txt` (with pinned versions)

2. **Combine apt-get commands**:

   ```bash
   # Good (one update)
   sudo apt-get update && sudo apt-get install -y nodejs ruby-full

   # Bad (multiple updates)
   sudo apt-get update && sudo apt-get install -y nodejs
   sudo apt-get update && sudo apt-get install -y ruby-full
   ```

3. **Keep setup minimal**:
   - Only install what the agent needs
   - Skip optional dependencies
   - Avoid running tests during setup

### Timeout Guidelines

- **Python**: 2-5 minutes
- **Node.js**: 2-4 minutes (includes installation)
- **Ruby**: 3-10 minutes (includes installation)
- **Go**: 2-5 minutes (includes installation)
- **Rust**: 10-20 minutes (cargo build is slow)
- **Monorepos**: 5-15 minutes

Set timeouts generously to avoid failures on slow networks.

## Security Considerations

> [!WARNING]
> Repository setup commands have **full sudo access** and run with **no sandboxing restrictions**. They can install any system package, modify system configuration, and execute arbitrary code. Only configure setup for repositories you **fully trust**. Malicious setup commands could compromise the sandbox container or exfiltrate secrets.

### sudo Access

- Container runs as non-root user with passwordless sudo
- Allows installing any system package
- Commands run in isolated tmpfs workspace
- Workspace deleted after job completes
- Only affects explicitly configured repositories

### Command Injection

- Commands run in the **repository's worktree** with the repository's code
- Only configure repositories you **trust**
- Commands have **full access** to the workspace and system (via sudo)
- Be careful with user-provided input in commands
- **Never** use untrusted input in setup commands (e.g., branch names, PR titles)

### Secrets

- Don't put secrets in `repo-setup.yaml` (it's version controlled)
- Use environment variables for sensitive data
- Configure secrets in `.env` file or Docker secrets
- Setup commands have access to all environment variables (including secrets)

### Isolation

- Each job runs in an **isolated tmpfs workspace**
- Workspace is **deleted** after job completes
- Setup commands **cannot** affect other jobs
- Setup commands **cannot** access host filesystem (except mounted volumes)
- However, setup commands **can** install system packages that persist in the container

## Troubleshooting

### Setup Not Running

Check:

1. Is `repo-setup.yaml` present in project root?
2. Is repository name correct (format: `owner/repo`)?
3. Are there any YAML syntax errors?
4. Check logs: `docker-compose logs sandbox_worker`

### Commands Failing

Check:

1. Do commands work locally in a similar container?
2. Did you use `-y` flag with apt-get?
3. Is the timeout long enough?
4. Are there network issues?
5. Check command output in logs

### Timeout Issues

If setup times out:

1. Increase timeout value
2. Optimize commands (combine apt-get updates)
3. Remove unnecessary commands
4. Check network speed

### Container Issues

If container won't start:

1. Check YAML syntax: `docker-compose config`
2. Check logs: `docker-compose logs sandbox_worker`
3. Rebuild: `docker-compose up --build -d sandbox_worker`

## Advanced Usage

### Conditional Setup

Use shell conditionals in commands:

```yaml
repositories:
  myorg/repo:
    setup_commands:
      - "[ -f requirements.txt ] && pip install -r requirements.txt || echo 'No requirements.txt'"
      - "[ -f package.json ] && (sudo apt-get update && sudo apt-get install -y nodejs npm && npm ci) || echo 'No package.json'"
```

### Multi-Stage Setup

Break setup into logical stages:

```yaml
repositories:
  myorg/repo:
    setup_commands:
      # Stage 1: Install runtimes
      - "sudo apt-get update"
      - "sudo apt-get install -y nodejs npm"

      # Stage 2: Install dependencies
      - "pip install -r requirements.txt"
      - "npm ci"

      # Stage 3: Build
      - "npm run build"

      # Stage 4: Setup database
      - "python manage.py migrate"
```

### Environment-Specific Setup

Use environment variables to control behavior:

```yaml
repositories:
  myorg/repo:
    setup_commands:
      - "pip install -r requirements-${ENVIRONMENT:-dev}.txt"
    env:
      ENVIRONMENT: "test"
```

## Best Practices

1. **Start simple**: Begin with just dependency installation
2. **Test locally**: Verify commands work in a Python 3.11 container
3. **Use lock files**: Ensure reproducible installs
4. **Set generous timeouts**: Avoid false failures
5. **Monitor logs**: Watch for setup issues
6. **Keep it fast**: Only install what's needed
7. **Document commands**: Add comments in YAML
8. **Version control**: Commit `repo-setup.yaml.example`, not `repo-setup.yaml`
9. **Use -y flag**: Always use `-y` with apt-get to avoid prompts
10. **Combine updates**: Run `apt-get update` once, install multiple packages

## Examples

See `repo-setup.yaml.example` for comprehensive examples covering:

- Python (pip)
- Node.js (npm, yarn)
- Ruby (bundle)
- Go (go mod)
- Rust (cargo)
- Java (maven, gradle)
- Monorepos
- Multi-language projects

## See Also

- [Architecture](ARCHITECTURE.md) - System design
- [Configuration](CONFIGURATION.md) - Environment variables
- [Development](DEVELOPMENT.md) - Testing and contributing
