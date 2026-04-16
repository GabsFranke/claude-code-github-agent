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

This is a self-hosted GitHub bot that uses Claude Agent SDK to autonomously review PRs and respond to commands. The system has several key components.

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

The project has a bash script for quality checks. Always invoke it with `bash` (not executable in git):

```bash
# Check code quality (read-only)
bash ./check-code.sh

# Auto-fix formatting and imports
bash ./check-code.sh --fix

# Fast mode (skip mypy)
bash ./check-code.sh --fast
```

**Manual auto-fix sequence** (run in this exact order when fixing lint failures):

```bash
black services/ shared/ subagents/ hooks/ plugins/ tests/
isort services/ shared/ subagents/ hooks/ plugins/ tests/
ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/
bash ./check-code.sh  # verify
```

Configuration files: `pyproject.toml` (black, isort, mypy, pytest, ruff), `.flake8`, `check-code.sh`, `check-code.ps1`.

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

## Getting Help

- Check documentation in `docs/` directory
- Review existing subagents in `subagents/` for examples
- Look at plugin implementations in `plugins/`
- Check workflow definitions in `workflows.yaml`
- Review test cases in `tests/` for usage examples

## Memory

You have access to a persistent memory system scoped to the current repository. Memory survives across sessions and is shared with the memory extraction worker that runs after each session.

### Reading Memory

Memory context from previous sessions is injected into your prompt at the start of each job inside `<memory>` tags. Read it — it contains architecture notes, known issues, commands, and other facts learned from prior sessions.

You can also query memory directly during a session:

```
mcp__memory__memory_read()                          # List all memory files
mcp__memory__memory_read(file_path="index.md")      # Read the index
mcp__memory__memory_read(file_path="issues/foo.md") # Read a specific file
```

### Writing Memory

You do not write memory directly. After every session, a background worker automatically runs the `@memory-extractor` subagent against your full transcript — it handles deduplication, organization, and quality control.

Your goal is to help developers by reviewing code, fixing issues, and automating workflows while following best practices and maintaining code quality.
