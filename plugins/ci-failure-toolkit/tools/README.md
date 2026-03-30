# GitHub Actions Tools

Progressive tools for analyzing GitHub Actions workflow failures efficiently.

## Tools Overview

### 1. `get_workflow_run_summary(owner, repo, run_id)`

**Purpose:** High-level overview without logs
**Use when:** Starting analysis, identifying which jobs failed
**Returns:** Run metadata + job list with status/conclusion
**Token cost:** Low (~1-2KB)

### 2. `get_job_logs_raw(owner, repo, job_id, start_line=0, num_lines=500)`

**Purpose:** Paginated access to job logs
**Use when:** Reading logs in manageable chunks (primary method)
**Returns:** Slice of logs with timestamps stripped
**Token cost:** Low per call (~10-20KB for 500 lines)
**Note:** Call repeatedly to paginate through entire log

### 3. `search_job_logs(owner, repo, job_id, pattern, context_lines=5)`

**Purpose:** Find specific patterns in logs
**Use when:** Looking for specific errors in very long logs
**Returns:** Matching lines with context
**Token cost:** Low-Medium (depends on matches)

### 4. `get_failed_steps(owner, repo, job_id, log_lines_per_step=100)`

**Purpose:** Get metadata about which steps failed
**Use when:** Need to know which steps failed (metadata only)
**Returns:** Failed steps list + last N lines of full log
**Token cost:** Medium (~5-20KB depending on failures)

## Recommended Workflow

```python
# 1. Always start with summary (fast, no logs)
summary = await get_workflow_run_summary(owner, repo, run_id)

# 2. Identify failed jobs
failed_jobs = [j for j in summary["jobs"] if j["conclusion"] == "failure"]

# 3. Get logs with pagination (recommended)
# First call to check size
chunk1 = await get_job_logs_raw(owner, repo, failed_jobs[0]["id"], start_line=0, num_lines=500)
total_lines = chunk1["total_lines"]

# Get last 500 lines (where errors usually are)
last_chunk = await get_job_logs_raw(owner, repo, failed_jobs[0]["id"], start_line=total_lines-500, num_lines=500)

# 4. Only if needed: search for specific patterns
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
2. **Use `get_job_logs_raw` with pagination** - Read logs in 500-line chunks
3. **Start at the end** - Errors are usually in the last 500 lines
4. **Use `search_job_logs`** - For finding specific errors in huge logs

## Token Efficiency

| Tool                       | Typical Size | Use Case             |
| -------------------------- | ------------ | -------------------- |
| `get_workflow_run_summary` | 1-2KB        | Always start here    |
| `get_job_logs_raw`         | 10-20KB      | Per 500-line chunk   |
| `search_job_logs`          | 2-10KB       | Find specific errors |
| `get_failed_steps`         | 5-20KB       | Step metadata        |

## Examples

### Example 1: Quick Diagnosis with Pagination

```python
# Get summary
summary = await get_workflow_run_summary(owner, repo, run_id)
failed = [j for j in summary["jobs"] if j["conclusion"] == "failure"][0]

# Get first chunk to check size
chunk1 = await get_job_logs_raw(owner, repo, failed["id"], start_line=0, num_lines=500)
print(f"Total lines: {chunk1['total_lines']}")

# Get last 500 lines (where errors are)
last_chunk = await get_job_logs_raw(
    owner, repo, failed["id"],
    start_line=chunk1["total_lines"] - 500,
    num_lines=500
)
# Analyze last_chunk["lines"]
```

### Example 2: Read Entire Log

```python
# Paginate through entire log
job_id = failed["id"]
start = 0
num_lines = 500

while True:
    chunk = await get_job_logs_raw(owner, repo, job_id, start_line=start, num_lines=num_lines)
    print(chunk["lines"])

    if chunk["end_line"] >= chunk["total_lines"]:
        break

    start += num_lines
```

### Example 3: Search for Specific Errors

```python
# Search for patterns
errors = await search_job_logs(
    owner, repo, failed["id"],
    pattern="FAILED|ERROR|AssertionError",
    context_lines=10
)

for match in errors["matches"]:
    print(f"Line {match['line_number']}: {match['matched_line']}")
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
