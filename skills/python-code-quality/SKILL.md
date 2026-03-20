---
name: "python-code-quality"
description: "Python code quality standards and automated fixing tools for this project"
---

# Python Code Quality Skill

This skill documents the Python code quality standards for this project and how to use automated tools to fix issues.

## Project Standards

This project follows strict Python code quality standards:

- **Black** - Code formatter (line length: 88)
- **isort** - Import organizer (black profile)
- **Flake8** - Linter
- **Mypy** - Type checker (Python 3.11+)
- **Ruff** - Fast linter

All code must pass these checks before being committed.

## Quick Fix Command

The project has a PowerShell script for code quality checks:

```bash
# Check code quality (read-only)
./check-code.ps1

# Auto-fix formatting and imports
./check-code.ps1 -Fix

# Fast mode (skip mypy)
./check-code.ps1 -Fast

# Skip type checking
./check-code.ps1 -SkipMypy

# Verbose output
./check-code.ps1 -Verbose
```

**For CI fixes, always run with `-Fix` to auto-fix issues before committing.**

## Automated Fixing

### Step 1: Run Auto-Fixers

These tools automatically fix most issues:

```bash
# Format code with Black (line length 88)
black services/ shared/ subagents/ hooks/ plugins/ tests/

# Organize imports with isort (black profile)
isort services/ shared/ subagents/ hooks/ plugins/ tests/

# Fix linting issues with Ruff
ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/
```

**CRITICAL:** Always run these three commands in this order when fixing lint failures.

### Step 2: Verify Fixes

After auto-fixing, verify all checks pass:

```bash
# Run all checks
./check-code.ps1

# Or manually:
black --check services/ shared/ subagents/ hooks/ plugins/ tests/
isort --check-only services/ shared/ subagents/ hooks/ plugins/ tests/
flake8 services/ shared/ subagents/ hooks/ plugins/ tests/
ruff check services/ shared/ subagents/ hooks/ plugins/ tests/
```

### Step 3: Fix Remaining Issues Manually

Some issues require manual fixes:

**Type errors (mypy):**

- Add missing type annotations
- Fix type mismatches
- Add `# type: ignore` comments only as last resort

**Flake8 errors that can't be auto-fixed:**

- Line too long (split into multiple lines)
- Unused variables (remove or prefix with `_`)
- Complex expressions (simplify)

## Common Fixes

### Black Formatting

Black automatically fixes:

- Line length (max 88 characters)
- Indentation (4 spaces)
- Quote style (double quotes)
- Trailing commas
- Whitespace

**No configuration needed** - Black is opinionated.

### isort Import Organization

isort automatically organizes imports into groups:

1. Standard library imports
2. Third-party imports
3. Local application imports

```python
# Before
from myapp import models
import sys
from typing import Dict
import os

# After (isort --fix)
import os
import sys
from typing import Dict

from myapp import models
```

### Ruff Auto-Fixes

Ruff can fix many issues automatically:

- Unused imports
- Unused variables
- Missing trailing commas
- Unnecessary parentheses
- And many more

```bash
# See what Ruff can fix
ruff check services/

# Apply fixes
ruff check --fix services/
```

### Type Annotations

Add type hints for mypy:

```python
# Before (mypy error)
def process_data(data):
    return {"result": data}

# After
from typing import Dict, Any

def process_data(data: Dict[str, Any]) -> Dict[str, Any]:
    return {"result": data}
```

## Project-Specific Patterns

### Async Functions

All I/O operations use async/await:

```python
# Good
async def fetch_data(url: str) -> dict:
    async with httpx.AsyncClient() as client:
        response = await client.get(url)
        return response.json()

# Bad
def fetch_data(url: str) -> dict:
    response = requests.get(url)
    return response.json()
```

### Type Safety

Use Pydantic for configuration and validation:

```python
from pydantic import BaseModel, Field

class Config(BaseModel):
    api_key: str = Field(..., min_length=1)
    timeout: int = Field(default=30, gt=0)
```

### Error Handling

Use custom exceptions from `shared.exceptions`:

```python
from shared import SDKError, WorktreeCreationError

try:
    result = await execute_sdk()
except Exception as e:
    raise SDKError(f"Failed to execute: {e}") from e
```

## CI Failure Workflow

When fixing CI lint failures:

1. **Run auto-fixers:**

   ```bash
   black services/ shared/ subagents/ hooks/ plugins/ tests/
   isort services/ shared/ subagents/ hooks/ plugins/ tests/
   ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/
   ```

2. **Verify fixes:**

   ```bash
   ./check-code.ps1
   ```

3. **Fix remaining issues manually** (if any)

4. **Commit with clear message:**

   ```bash
   git add .
   git commit -m "fix: resolve linting issues

   - Applied black formatting
   - Organized imports with isort
   - Fixed ruff violations
   - Added missing type annotations"
   ```

5. **Push:**
   ```bash
   git push origin HEAD
   ```

## Configuration Files

The project uses these configuration files:

- **pyproject.toml** - Black, isort, mypy, pytest, ruff configuration
- **.flake8** - Flake8 configuration
- **check-code.ps1** - Automated quality check script

**Do not modify these files** unless you're updating project-wide standards.

## Common Errors and Fixes

### "would reformat X files"

```bash
# Error from Black
# Fix: Run black without --check
black services/ shared/ subagents/ hooks/ plugins/ tests/
```

### "Imports are incorrectly sorted"

```bash
# Error from isort
# Fix: Run isort without --check-only
isort services/ shared/ subagents/ hooks/ plugins/ tests/
```

### "F401 imported but unused"

```bash
# Error from Flake8/Ruff
# Fix: Remove the unused import or use ruff --fix
ruff check --fix services/
```

### "E501 line too long"

```python
# Error: Line exceeds 88 characters
# Fix: Let Black handle it, or split manually

# Before
some_function(arg1, arg2, arg3, arg4, arg5, arg6, arg7, arg8, arg9, arg10)

# After
some_function(
    arg1, arg2, arg3, arg4, arg5,
    arg6, arg7, arg8, arg9, arg10
)
```

### "Missing type annotation"

```python
# Error from mypy
# Fix: Add type hints

# Before
def process(data):
    return data

# After
from typing import Any

def process(data: Any) -> Any:
    return data
```

## Best Practices

1. **Always run auto-fixers first** - Don't manually format code
2. **Use the check-code.ps1 script** - It runs all checks in the right order
3. **Fix issues before committing** - Don't push code that fails checks
4. **Add type hints** - Help mypy catch errors early
5. **Follow async patterns** - Use async/await for I/O operations
6. **Use Pydantic** - For configuration and validation
7. **Write clear commit messages** - Explain what was fixed and why

## Summary

For lint failures in CI:

```bash
# 1. Auto-fix everything possible
black services/ shared/ subagents/ hooks/ plugins/ tests/
isort services/ shared/ subagents/ hooks/ plugins/ tests/
ruff check --fix services/ shared/ subagents/ hooks/ plugins/ tests/

# 2. Verify
./check-code.ps1

# 3. Commit and push
git add .
git commit -m "fix: resolve linting issues"
git push origin HEAD
```

This will fix 95% of lint failures automatically. The remaining 5% require manual fixes (usually type annotations or complex refactoring).
