# GitHub Actions Tools

Progressive tools for analyzing GitHub Actions workflow failures efficiently.

## Tools Overview

### 1. `get_workflow_run_summary(owner, repo, run_id)`

**Purpose:** High-level overview without logs
**Use when:** Starting analysis, identifying which jobs failed
**Returns:** Run metadata + job list with status/conclusion
**Token cost:** Low (~1-2KB)

### 2. `get_failed_steps(owner, repo, job_id, log_lines_per_step=100)`

**Purpose:** Extract only failed steps with log excerpts
**Use when:** Diagnosing failures (most common use case)
**Returns:** Failed steps with last N lines of logs
**Token cost:** Medium (~5-20KB depending on failures)

### 3. `get_job_logs(owner, repo, job_id, max_lines=None)`

**Purpose:** Full logs for specific job
**Use when:** Need complete context beyond failed steps
**Returns:** Complete job logs (or file path if >10KB)
**Token cost:** High (can be 100KB+)
**Note:** Automatically writes to `.ci-logs/` directory if >10KB

### 4. `search_job_logs(owner, repo, job_id, pattern, context_lines=5)`

**Purpose:** Find specific patterns in logs
**Use when:** Looking for specific errors in very long logs
**Returns:** Matching lines with context
**Token cost:** Low-Medium (depends on matches)

## Recommended Workflow

```python
# 1. Always start with summary (fast, no logs)
summary = await get_workflow_run_summary(owner, repo, run_id)

# 2. Identify failed jobs
failed_jobs = [j for j in summary["jobs"] if j["conclusion"] == "failure"]

# 3. Get failed steps (usually sufficient)
failed_steps = await get_failed_steps(owner, repo, failed_jobs[0]["id"])

# 4. Only if needed: get full logs or search
# logs = await get_job_logs(owner, repo, job_id, max_lines=500)
# matches = await search_job_logs(owner, repo, job_id, "error|exception")
```

## Manual Testing

### Prerequisites

1. Set GitHub token:

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

2. Install dependencies (if testing outside Docker):

```bash
pip install httpx
```

### Run Test Script

```bash
# Basic usage
python plugins/ci-failure-toolkit/tools/test_github_actions.py <owner> <repo> <run_id>

# Example with real workflow run
python plugins/ci-failure-toolkit/tools/test_github_actions.py \
  GabsFranke \
  claude-code-github-agent \
  23327718957
```

### Expected Output

```
================================================================================
Testing GitHub Actions Tools
Repository: GabsFranke/claude-code-github-agent
Run ID: 23327718957
================================================================================

📊 Step 1: Getting workflow run summary...
--------------------------------------------------------------------------------
✅ Run: CI
   Status: completed
   Conclusion: failure
   URL: https://github.com/GabsFranke/claude-code-github-agent/actions/runs/23327718957

   Jobs (3):
   ✅ build - success
   ❌ test - failure
   ✅ lint - success

🔍 Step 2: Getting failed steps for job 'test'...
--------------------------------------------------------------------------------
✅ Job: test
   Conclusion: failure
   Failed steps: 1

   ❌ Step 3: Run tests
      Status: completed / failure
      Log excerpt (last 100 lines):
      ----------------------------------------------------------------------
      FAILED tests/test_integration.py::test_webhook_signature
      ...

🔎 Step 3: Searching for 'error' patterns in logs...
--------------------------------------------------------------------------------
✅ Found 5 matches
   Showing first 5 matches:

   Match 1 (line 234):
   AssertionError: Expected signature to match

📄 Step 4: Getting full job logs...
--------------------------------------------------------------------------------
   (Limiting to last 200 lines to avoid huge output)
✅ Job: test
   Status: completed / failure
   Original size: 45678 chars
   Truncated: True
   📁 Logs written to file: .ci-logs/job_12345_logs_67890.log

================================================================================
✅ All tests completed successfully!
================================================================================
```

## File Output

When logs exceed 10KB, they're automatically written to:

```
.ci-logs/
├── job_12345_logs_67890.log
├── job_12346_logs_67891.log
└── ...
```

The tool returns:

- `content`: Preview (first 1000 chars)
- `file_path`: Full path to log file
- `full_size`: Original size in characters
- `truncated`: True
- `note`: Human-readable message

## Integration with Agent

The agent can use these tools directly:

```python
from tools.github_actions import get_workflow_run_summary, get_failed_steps

# In agent code
summary = await get_workflow_run_summary(owner, repo, run_id)
failed_jobs = [j for j in summary["jobs"] if j["conclusion"] == "failure"]

for job in failed_jobs:
    failed_steps = await get_failed_steps(owner, repo, job["id"])
    # Analyze failed_steps and implement fixes
```

## Error Handling

All tools raise exceptions on errors:

- `ValueError`: Missing GITHUB_TOKEN
- `httpx.HTTPStatusError`: GitHub API errors (401, 404, etc.)
- `httpx.TimeoutException`: Request timeout

Example:

```python
try:
    summary = await get_workflow_run_summary(owner, repo, run_id)
except ValueError as e:
    print(f"Missing token: {e}")
except httpx.HTTPStatusError as e:
    print(f"GitHub API error: {e.response.status_code}")
```

## Performance Tips

1. **Always start with summary** - It's fast and identifies failed jobs
2. **Use `get_failed_steps` first** - Usually sufficient for diagnosis
3. **Limit `max_lines`** - When using `get_job_logs`, limit to last N lines
4. **Use `search_job_logs`** - For finding specific errors in huge logs
5. **Check file output** - Large logs are written to files automatically

## Token Efficiency

| Tool                       | Typical Size | Use Case             |
| -------------------------- | ------------ | -------------------- |
| `get_workflow_run_summary` | 1-2KB        | Always start here    |
| `get_failed_steps`         | 5-20KB       | Primary diagnosis    |
| `get_job_logs` (limited)   | 10-50KB      | Need more context    |
| `get_job_logs` (full)      | 50KB-1MB+    | Rare, writes to file |
| `search_job_logs`          | 2-10KB       | Find specific errors |

## Examples

### Example 1: Quick Diagnosis

```python
# Get summary and failed steps only
summary = await get_workflow_run_summary(owner, repo, run_id)
failed = [j for j in summary["jobs"] if j["conclusion"] == "failure"][0]
steps = await get_failed_steps(owner, repo, failed["id"])

# Analyze steps["failed_steps"] - usually enough to fix
```

### Example 2: Deep Investigation

```python
# When failed steps aren't enough
summary = await get_workflow_run_summary(owner, repo, run_id)
failed = [j for j in summary["jobs"] if j["conclusion"] == "failure"][0]

# Get last 500 lines of full logs
logs = await get_job_logs(owner, repo, failed["id"], max_lines=500)

# Or search for specific patterns
errors = await search_job_logs(
    owner, repo, failed["id"],
    pattern="error|exception|failed",
    context_lines=10
)
```

### Example 3: Multiple Failed Jobs

```python
summary = await get_workflow_run_summary(owner, repo, run_id)
failed_jobs = [j for j in summary["jobs"] if j["conclusion"] == "failure"]

for job in failed_jobs:
    print(f"Analyzing {job['name']}...")
    steps = await get_failed_steps(owner, repo, job["id"])
    # Process each job's failures
```

## Troubleshooting

### "GITHUB_TOKEN not available"

```bash
export GITHUB_TOKEN=ghp_your_token_here
```

### "404 Not Found"

- Check run_id is correct
- Verify token has access to repository
- Ensure repository name format is `owner/repo`

### "Logs too large"

- Use `max_lines` parameter to limit output
- Tool automatically writes to file if >10KB
- Check `.ci-logs/` directory for full logs

### Import errors in test script

```bash
# Make sure you're in the project root
cd /path/to/claude-code-github-agent

# Run from project root
python plugins/ci-failure-toolkit/tools/test_github_actions.py ...
```
