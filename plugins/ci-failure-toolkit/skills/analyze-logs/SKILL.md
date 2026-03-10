---
name: "analyze-logs"
description: "Parse CI/CD logs to identify failure type and extract error context"
---

# Analyze CI/CD Logs Skill

This skill parses GitHub Actions workflow logs to identify failure types and extract relevant error information.

## Purpose

Extract structured information from raw CI/CD logs to enable targeted failure analysis.

## Inputs

- `logs` - Raw workflow run logs (string)
- `workflow_name` - Name of the failed workflow (optional)
- `job_name` - Name of the failed job (optional)

## Process

### 1. Parse Log Structure

Identify log sections:

- Setup steps
- Build steps
- Test steps
- Deployment steps
- Teardown steps

### 2. Identify Failure Point

Find where the failure occurred:

- Failed step name
- Exit code
- Timestamp
- Duration

### 3. Extract Error Context

Capture relevant error information:

- Error messages
- Stack traces
- Failed assertions
- Compilation errors
- Dependency issues

### 4. Classify Failure Type

Determine failure category:

**Build Failure Indicators:**

- "compilation error", "build failed"
- "cannot find module", "import error"
- "dependency resolution failed"
- Exit codes: 1, 2

**Test Failure Indicators:**

- "test failed", "assertion error"
- "expected X but got Y"
- "FAILED tests/"
- Exit codes: 1

**Lint Failure Indicators:**

- "lint error", "style violation"
- "type error", "mypy error"
- "formatting error"
- Exit codes: 1

**Deploy Failure Indicators:**

- "docker build failed"
- "deployment failed"
- "health check failed"
- Exit codes: 1, 125, 126, 127

### 5. Extract Key Errors

Parse specific error patterns:

**Python:**

```
Traceback (most recent call last):
  File "...", line X, in <module>
    ...
ErrorType: Error message
```

**JavaScript/TypeScript:**

```
Error: Error message
    at function (file:line:col)
    at ...
```

**Docker:**

```
ERROR [stage X/Y] RUN command
failed to solve: process "/bin/sh -c command" did not complete successfully: exit code: 1
```

**Test Failures:**

```
FAILED tests/test_file.py::test_name - AssertionError: message
```

## Output

Return structured JSON:

```json
{
  "failure_type": "test|build|lint|deploy|unknown",
  "failed_step": "Step name",
  "exit_code": 1,
  "error_summary": "Brief description of the error",
  "error_details": {
    "message": "Full error message",
    "stack_trace": "Stack trace if available",
    "file": "File where error occurred",
    "line": 42,
    "context": "Additional context"
  },
  "failed_tests": [
    {
      "test": "test_name",
      "file": "tests/test_file.py",
      "error": "AssertionError: message"
    }
  ],
  "recommendations": [
    "Check dependency versions",
    "Review recent changes to affected files"
  ]
}
```

## Usage Example

```python
# In fix-ci command
logs = get_workflow_run_logs(owner, repo, run_id)

# Use skill to parse logs
result = use_skill(
    "analyze-logs",
    inputs={
        "logs": logs,
        "workflow_name": "CI",
        "job_name": "test"
    }
)

# Route to appropriate agent based on failure_type
if result["failure_type"] == "test":
    delegate_to_agent("test-failure-analyzer", result)
elif result["failure_type"] == "build":
    delegate_to_agent("build-failure-analyzer", result)
# ... etc
```

## Error Patterns

### Python Build Errors

```
ModuleNotFoundError: No module named 'requests'
ImportError: cannot import name 'X' from 'Y'
SyntaxError: invalid syntax
```

### Python Test Errors

```
AssertionError: assert X == Y
FAILED tests/test_file.py::test_name
E       AssertionError: message
```

### JavaScript Build Errors

```
Cannot find module 'module-name'
Module not found: Error: Can't resolve 'module'
SyntaxError: Unexpected token
```

### JavaScript Test Errors

```
FAIL tests/test.js
  ● Test suite failed to run
Expected X to equal Y
```

### Docker Errors

```
ERROR [stage 2/5] RUN pip install -r requirements.txt
failed to solve: process "/bin/sh -c ..." did not complete successfully
```

### Linting Errors

```
error: line too long (88 > 79 characters)
error: unused import 'os'
error: missing return type annotation
```

## Notes

- Handles multi-line error messages
- Preserves stack traces
- Identifies multiple errors if present
- Provides context for debugging
- Suggests next steps based on error type
