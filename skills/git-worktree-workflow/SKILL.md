---
name: "git-worktree-workflow"
description: "Essential knowledge for working in git worktrees: understanding the environment, file operations, and git commands"
---

# Git Worktree Workflow Skill

This skill provides essential knowledge for agents working in isolated git worktrees. Use this to understand your workspace and how to work with files and git.

## Your Workspace Environment

### CRITICAL: You Are Already in a Git Worktree

**DO NOT clone the repository!** You are already working in an isolated git worktree.

- Your current directory IS the repository
- All files are already available locally
- Git credentials are pre-configured
- The repository is ready for you to work

### What is a Git Worktree?

A worktree is an isolated working directory linked to a shared git repository. Multiple worktrees can exist simultaneously without interfering with each other.

**Key characteristics:**

- Isolated file system (your changes don't affect other jobs)
- Shared git history (commits are visible across worktrees)
- Pre-configured credentials (git push works automatically)

### Check Your Current State

```bash
# Check if you're on a branch or in detached HEAD
git status

# If on a branch, see which one
git branch --show-current

# See recent commits
git log -1 --oneline
```

## File Operations

### Reading and Analyzing Files

Use these tools to explore the codebase:

- **Read** - Read file contents
- **List** - List directory contents
- **Search** - Search for files by name
- **Grep** - Search for text patterns in files
- **Bash** - Run shell commands for complex operations

```bash
# Find all Python test files
find tests/ -name "test_*.py"

# Search for specific error patterns
grep -r "import requests" src/

# Check current directory
pwd
ls -la
```

### Modifying Files

Use these tools to implement fixes:

- **Write** - Create new files or overwrite existing ones
- **Edit** - Make targeted changes to existing files
- **Bash** - Run commands that modify files (formatters, etc.)

```bash
# Run auto-formatters (use virtual environment)
.venv/bin/black src/
.venv/bin/isort src/
.venv/bin/ruff check --fix .

# Run tests locally (use virtual environment)
.venv/bin/python -m pytest tests/
npm test
```

## Git Workflow

### Before Committing: Code Quality

**CRITICAL: Always run code quality checks before committing Python code.**

This project has strict code quality standards. Run these commands before every commit:

```bash
# Auto-fix formatting and linting (run in this order, use virtual environment)
.venv/bin/black services/ shared/ subagents/ hooks/ plugins/ tests/
.venv/bin/isort services/ shared/ subagents/ hooks/ plugins/ tests/
.venv/bin/ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/

# Verify all checks pass
./check-code.sh
```

**Why this matters:**

- CI will fail if code doesn't pass these checks
- Auto-fixing prevents manual formatting work
- Consistent code style across the project

**When to run:**

- After making any Python code changes
- Before running `git commit`
- Even for small fixes (one line changes need formatting too)

### Committing Changes

After implementing fixes and running code quality checks, commit your changes:

```bash
# Verify you're on a branch (not detached HEAD)
git branch --show-current

# Stage all changes
git add .

# Or stage specific files
git add src/api.py tests/test_api.py

# Commit with descriptive message
git commit -m "fix: resolve CI failure - [brief description]

- Root cause: [explanation]
- Changes: [what was fixed]
- Tested: [how it was verified]"
```

**Commit message format:**

- Use conventional commits: `fix:`, `feat:`, `test:`, `refactor:`, `docs:`
- First line: brief summary (50 chars max)
- Blank line
- Bullet points with details
- Reference issue/PR/run numbers if applicable

### Pushing Changes

Push your changes to the remote repository:

```bash
# Push to current branch
git push origin HEAD

# If this is the first push for this branch
git push -u origin HEAD
```

**Important:**

- Always push to `HEAD` (current branch)
- Never push to `main` or `master` directly
- Credentials are pre-configured (no authentication needed)

## GitHub Integration

### Using GitHub MCP Tools

**CRITICAL: The `gh` CLI is NOT available in this environment.**

All GitHub interactions must use MCP tools with the `mcp__github__` prefix:

```python
# Get workflow run details
mcp__github__list_workflow_run_jobs({
    "owner": "owner-name",
    "repo": "repo-name",
    "run_id": 12345678
})

# Download logs
mcp__github__download_workflow_run_logs({
    "owner": "owner-name",
    "repo": "repo-name",
    "run_id": 12345678
})

# Post comment to PR or issue
mcp__github__add_issue_comment({
    "owner": "owner-name",
    "repo": "repo-name",
    "issue_number": 456,
    "body": "## Analysis Results\n\n..."
})
```

## Testing Your Fixes

Always verify fixes locally before pushing:

```bash
# Python projects (use virtual environment)
.venv/bin/python -m pytest tests/
.venv/bin/python -m pytest tests/test_specific.py -v
.venv/bin/mypy src/

# Node projects
npm test
npm run lint
npm run build

# Docker projects
docker build -t test-build .

# Run specific commands from project
cat package.json | grep -A 10 "scripts"
make test
```

## Error Handling

### Detached HEAD State

```bash
# If you see "HEAD detached at <commit>"
# You need to be on a branch to commit
# Check with your invoking agent - they should have created a branch

git status
# Output: HEAD detached at abc1234

# This shouldn't happen if the main agent set things up correctly
# Report this in your results
```

### Git Push Failures

```bash
# If push fails due to remote changes
git pull --rebase origin $(git branch --show-current)
git push origin HEAD

# If push fails because branch doesn't exist remotely yet
git push -u origin HEAD
```

### File Permission Issues

```bash
# If files are not writable
chmod +w file.txt

# If directory is not accessible
chmod +x directory/
```

## Best Practices

1. **Always test locally** - Run tests/linters before committing
2. **Commit atomically** - One logical change per commit
3. **Write clear messages** - Explain what and why
4. **Push to HEAD** - Never hardcode branch names
5. **Use MCP tools** - Never use `gh` CLI
6. **Verify before pushing** - Ensure fixes work

## What NOT to Do

❌ **DO NOT** run `git clone` - you're already in a worktree
❌ **DO NOT** commit in detached HEAD - ensure you're on a branch first
❌ **DO NOT** push to `main` directly - push to your current branch
❌ **DO NOT** use `gh` CLI - use MCP tools
❌ **DO NOT** hardcode branch names - use `HEAD` or `$(git branch --show-current)`
❌ **DO NOT** commit without running code quality checks - CI will fail
❌ **DO NOT** skip `./check-code.sh` - it must pass before pushing

## Common Scenarios

### Scenario 1: You're a Subagent

The main agent has already set up a branch for you:

```bash
# 1. Verify you're on a branch
git branch --show-current
# Output: fix/ci-failure-run-12345

# 2. Implement your fixes
# (use Read/Write/Edit/Bash tools)

# 3. Test locally (use virtual environment)
.venv/bin/python -m pytest tests/

# 4. Run code quality checks (see python-code-quality skill)

# 5. Verify checks pass
./check-code.sh

# 6. Commit
git add .
git commit -m "fix: resolve test failures"

# 7. Push
git push origin HEAD

# 8. Return results to main agent
```

### Scenario 2: Working with Multiple Commits

For complex fixes:

```bash
# 1. Implement first fix
# (edit files)

# 2. Run code quality checks (see python-code-quality skill)

# 3. Commit first fix
git add src/api.py
git commit -m "fix: resolve import error"

# 4. Implement second fix
# (edit more files)

# 5. Run code quality checks again

# 6. Commit second fix
git add tests/test_api.py
git commit -m "fix: update test expectations"

# 7. Push all commits
git push origin HEAD
```

## Summary

You are working in an isolated git worktree with:

- All repository files available locally
- Pre-configured git credentials
- Direct file system access

Your typical workflow:

1. Verify you're on a branch: `git branch --show-current`
2. Implement fixes using file tools
3. Test locally with Bash (pytest, npm test, etc.)
4. Run code quality checks (see `python-code-quality` skill)
5. Commit with clear messages
6. Push to current branch: `git push origin HEAD`

Remember: You're in a worktree, not a fresh clone. Don't clone, don't create branches (unless instructed), just work!
