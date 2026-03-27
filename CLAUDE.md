# Agent Instructions for Claude Code GitHub Agent

## Where You Are

You are working in a **git worktree** - an isolated workspace created from a cached bare repository. This is NOT the main repository clone.

**Important Context:**

- Your working directory is a temporary worktree (e.g., `/tmp/job_abc12345_123456/`)
- The worktree is created from a cached bare repository at `/var/cache/repos/{owner}/{repo}.git`
- You have a unique branch name like `job-abc12345-123456`
- Git credentials are pre-configured for pushing changes
- The worktree will be automatically cleaned up after job completion
- All your file operations (Read, Write, Edit, List, Search, Bash) work on local files in this worktree
- GitHub API operations (creating PRs, posting comments, reading PR metadata) use GitHub MCP tools

## Architecture Overview

This is a self-hosted GitHub bot that uses Claude Agent SDK to autonomously review PRs and respond to commands. The system has several key components:

**Your Environment:**

- You run in a Docker container (`sandbox_worker`)
- You have access to local files via Read/Write/Edit/List/Search/Bash tools
- You interact with GitHub via MCP tools (all tools prefixed with `mcp__github__`)
- You can delegate to specialized subagents via the Task tool
- You can use plugins (pr-review-toolkit, ci-failure-toolkit)

## Technology Stack

- **Python 3.11+** with async/await patterns
- **FastAPI** for webhook service
- **Redis** for message queues and job coordination
- **Claude Agent SDK** for autonomous operations
- **GitHub MCP** for GitHub API interactions
- **Git worktrees** for isolated workspaces

## Code Quality Standards

When making changes to this codebase:

### Formatting & Style

- Use **black** for code formatting (line length: 88)
- Use **isort** for import organization (black profile)
- Follow PEP 8 conventions
- Use type hints throughout (Python 3.11+ syntax)
- Use Pydantic for configuration and validation

### Code Quality

- Write async functions for all I/O operations
- Use proper error handling with custom exceptions from `shared/exceptions.py`
- Add logging at appropriate levels (DEBUG, INFO, WARNING, ERROR)
- Use context managers for resource cleanup
- Implement graceful shutdown handlers for services

### Testing

- Write tests using **pytest**
- Use markers: `@pytest.mark.unit`, `@pytest.mark.integration`
- Aim for high coverage on critical paths
- Mock external dependencies (GitHub API, Redis, etc.)

### Running Quality Checks

```bash
# Run all checks (formatting, linting, type checking)
./check-code.sh

# Auto-fix formatting and imports
./check-code.sh --fix

# Fast mode (skip mypy)
./check-code.sh --fast
```

## Project Structure

```
claude-code-github-agent/
├── services/
│   ├── webhook/              # Receives GitHub webhooks
│   ├── agent_worker/         # Coordinates jobs (workflow routing)
│   ├── repo_sync/            # Manages bare repository cache
│   └── sandbox_executor/     # Executes jobs in worktrees (YOU ARE HERE)
├── shared/                   # Shared utilities (importable package)
│   ├── config.py            # Pydantic configuration
│   ├── queue.py             # Message queue abstraction
│   ├── job_queue.py         # Job queue abstraction
│   ├── github_auth.py       # GitHub App authentication
│   └── exceptions.py        # Custom exceptions
├── workflows/               # Workflow engine
│   ├── engine.py           # YAML-based routing
│   └── workflows.yaml      # Single source of truth for workflows
├── prompts/                # System context for workflows
├── subagents/              # Specialized subagent definitions
├── plugins/                # Claude Code plugins
│   ├── pr-review-toolkit/  # PR review commands & agents
│   └── ci-failure-toolkit/ # CI failure analysis
├── hooks/                  # Agent hooks
└── tests/                  # Test suite
```

## Working with Files

### Local File Operations

Use these tools for reading/writing files in your worktree:

- **Read** - Read file contents
- **Write** - Create or overwrite files
- **Edit** - Make targeted edits
- **List** - List directory contents
- **Search** - Search for patterns in files
- **Bash** - Execute shell commands

### GitHub API Operations

Use GitHub MCP tools for API interactions:

- `get_pull_request` - Get PR details
- `list_pull_request_files` - List changed files
- `get_pull_request_diff` - Get PR diff
- `add_issue_comment` - Post comments
- `pull_request_review_write` - Post review with inline comments
- `create_branch` - Create branches
- `update_file` - Update files via API (use local Write instead when possible)
- `create_pull_request` - Open PRs

**Prefer local file operations over GitHub API when possible** - they're faster and don't count against rate limits.

## Common Workflows

### Reviewing a Pull Request

1. Use `get_pull_request` to get PR details
2. Use `list_pull_request_files` to see changed files
3. Use `get_pull_request_diff` to analyze changes
4. Read relevant files locally using Read tool
5. Delegate to specialized subagents as needed
6. Post summary via `add_issue_comment`
7. Post inline comments via `pull_request_review_write`

### Making Code Changes

1. Read files locally using Read tool
2. Make changes using Write or Edit tools
3. Test changes using Bash tool (run tests, linters)
4. Commit changes using Bash tool (`git add`, `git commit`)
5. Push to remote using Bash tool (`git push origin HEAD`)
6. Create PR using `create_pull_request`

### Analyzing CI Failures

1. Use `get_workflow_run` to get workflow details
2. Use `get_workflow_run_logs` to get logs
3. Analyze logs to identify failure cause
4. Make fixes locally using Write/Edit tools
5. Test fixes using Bash tool
6. Commit and push changes

## Git Operations

Git credentials are pre-configured. You can use git commands directly:

```bash
# Check status
git status

# Stage changes
git add .

# Commit
git commit -m "Fix: description"

# Push to remote (credentials are already configured)
git push origin HEAD

# Create a new branch
git checkout -b feature-branch
```

## Environment Variables

Key environment variables available to you:

- `GITHUB_APP_ID` - GitHub App ID
- `GITHUB_INSTALLATION_ID` - Installation ID
- `GITHUB_PRIVATE_KEY` - Private key for authentication
- `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` - Observability (optional)
- `CLAUDE_TEMP_DIR` - Set to your workspace directory
- `TMPDIR` - Set to your workspace directory

## Security Considerations

**You have full write access to repositories:**

- You can create branches, commit changes, and open PRs
- All GitHub MCP tools are auto-approved (no manual confirmation)
- Be careful with destructive operations
- Always test changes before pushing
- Follow repository-specific guidelines in CLAUDE.md files

**Best Practices:**

- Validate inputs before making changes
- Use descriptive commit messages
- Create feature branches for changes (don't push to main)
- Open PRs to `develop` for review rather than direct commits
- Check for existing CLAUDE.md in repositories for custom guidelines

## Debugging

If something goes wrong:

- Check logs: Your output is logged by the sandbox worker
- Use Bash tool to inspect the environment
- Check git status: `git status`, `git log`, `git remote -v`
- Verify GitHub token: `git config credential.helper`
- Check worktree: `git worktree list`

## Common Pitfalls

1. **Don't use GitHub API to read files you can read locally** - Use Read tool instead
2. **Don't forget to commit before pushing** - Git won't push uncommitted changes
3. **Don't assume you're in the main repository** - You're in a temporary worktree
4. **Don't use absolute paths** - Use relative paths from your working directory
5. **Don't skip error handling** - Always check command exit codes
6. **Don't make changes without testing** - Run tests/linters before committing

## Getting Help

- Check documentation in `docs/` directory
- Review existing subagents in `subagents/` for examples
- Look at plugin implementations in `plugins/`
- Check workflow definitions in `workflows.yaml`
- Review test cases in `tests/` for usage examples

## Summary

You are an autonomous agent working in an isolated git worktree with:

- Local file access via Read/Write/Edit/List/Search/Bash tools
- GitHub API access via MCP tools
- Ability to delegate to specialized subagents
- Access to plugins for common workflows
- Pre-configured git credentials for pushing changes
- Full write access to repositories (use responsibly)

Your goal is to help developers by reviewing code, fixing issues, and automating workflows while following best practices and maintaining code quality.
