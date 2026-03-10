# CI Failure Toolkit

Comprehensive CI/CD failure analysis and fix toolkit for GitHub Actions workflows. Automatically diagnoses and fixes build failures, test failures, linting issues, and deployment problems.

## Features

- **Automatic Failure Detection**: Triggered automatically when GitHub Actions workflows fail
- **Specialized Agents**: Four specialized agents for different failure types
- **Root Cause Analysis**: Identifies underlying issues, not just symptoms
- **Automated Fixes**: Implements fixes and verifies them locally
- **GitHub Integration**: Posts results and creates PRs with fixes

## Commands

### `/ci-failure-toolkit:fix-ci`

Analyze and fix CI/CD failures.

**Usage:**

```
/ci-failure-toolkit:fix-ci [owner/repo] [run-id-or-pr-number] [failure-type]
```

**Arguments:**

- `owner/repo` - Repository (required)
- `run-id-or-pr-number` - Workflow run ID or PR number (required)
- `failure-type` - Optional filter: build, test, lint, deploy, all

**Examples:**

```
# Auto-triggered on workflow failure
/ci-failure-toolkit:fix-ci owner/repo 12345

# Manual trigger with PR number
/ci-failure-toolkit:fix-ci owner/repo 456

# Specific failure type
/ci-failure-toolkit:fix-ci owner/repo 12345 test
```

## Agents

### build-failure-analyzer

Diagnoses and fixes:

- Compilation errors
- Dependency conflicts
- Missing environment variables
- Configuration issues

### test-failure-analyzer

Diagnoses and fixes:

- Unit test failures
- Integration test failures
- Flaky tests
- Timeout issues

### lint-failure-analyzer

Diagnoses and fixes:

- Code style violations
- Type errors
- Import issues
- Formatting problems

### deploy-failure-analyzer

Diagnoses and fixes:

- Docker build failures
- Container startup issues
- Health check failures
- Resource constraints

## Workflow Integration

### Automatic Trigger

Add to your `workflows.yaml`:

```yaml
fix-ci:
  triggers:
    events:
      - workflow_job.completed
    commands:
      - /fix-ci
      - /fix-build
      - /fix-tests
  prompt:
    template: "/ci-failure-toolkit:fix-ci {repo} {issue_number}"
    system_context: "fix-ci.md"
  description: "Analyze and fix CI/CD failures"
```

### Manual Trigger

Comment on any issue or PR:

```
/fix-ci owner/repo 12345
```

## How It Works

1. **Detect Failure**: Webhook receives `workflow_job.completed` with `conclusion=failure`
2. **Fetch Logs**: Agent retrieves workflow run logs via GitHub MCP
3. **Analyze**: Identifies failure type and root cause
4. **Delegate**: Routes to specialized agent (build, test, lint, deploy)
5. **Fix**: Agent implements targeted fixes in local worktree
6. **Verify**: Tests fixes locally before committing
7. **Commit**: Pushes changes with descriptive commit message
8. **Report**: Posts summary to GitHub

## Output Format

The agent posts a comprehensive summary:

```markdown
# CI Failure Analysis - Run #12345

## Failure Type

Test failures

## Root Cause

Test expected old API response format after recent API changes

## Changes Made

- Updated test assertions to match new API format
- Updated test fixtures with current response structure
- Fixed 3 related tests in test_api.py

## Files Modified

- `tests/test_api.py` - Updated assertions for new API format
- `tests/fixtures/api_responses.json` - Updated fixture data

## Verification

All tests pass after fixes (ran 10 times to check for flakiness)

## Prevention

- Add API contract tests
- Update fixtures when API changes
- Use schema validation in tests

---

🤖 Analyzed and fixed by CI Failure Toolkit
```

## Configuration

### Webhook Setup

The webhook service must be configured to handle `workflow_job.completed` events:

```python
# In services/webhook/main.py
@app.post("/webhook")
async def webhook(request: Request):
    event_type = request.headers.get("X-GitHub-Event")

    if event_type == "workflow_job":
        payload = await request.json()
        if payload.get("action") == "completed":
            conclusion = payload["workflow_job"]["conclusion"]
            if conclusion == "failure":
                # Trigger fix-ci workflow
                await queue.publish({
                    "event_type": "workflow_job",
                    "action": "completed",
                    "repo": payload["repository"]["full_name"],
                    "run_id": payload["workflow_job"]["run_id"],
                    # ... other data
                })
```

### Worker Configuration

Add the plugin to your worker configuration:

```python
# In services/sandbox_executor/executor.py
options = ClaudeAgentOptions(
    agents=AGENTS,
    plugins=[
        {"type": "local", "path": "/app/plugins/pr-review-toolkit"},
        {"type": "local", "path": "/app/plugins/ci-failure-toolkit"},
    ],
    allowed_tools=["Task", "Bash", "Read", "Write", "Edit", "List", "Search", "Grep", "Glob", "mcp__github__*"],
)
```

## Best Practices

1. **Test in Sandbox**: Always test in a sandbox repository first
2. **Review Fixes**: Review automated fixes before merging
3. **Monitor Patterns**: Track common failure types
4. **Improve CI**: Use prevention suggestions to improve CI pipeline
5. **Document Issues**: Keep track of recurring problems

## Limitations

- Requires GitHub Actions workflow logs to be accessible
- Cannot fix issues requiring external service changes
- May need manual intervention for complex failures
- Auto-commits to the same branch (configure branch protection accordingly)

## License

Same as parent project.
