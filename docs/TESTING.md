# Testing Guide

## Quick Start

```bash
# Run all tests
pytest

# Run with coverage report
pytest --cov=shared --cov=services --cov-report=html

# Run specific test file
pytest tests/shared/test_config.py

# Run tests matching pattern
pytest -k "rate_limiter"
```

## Test Structure

```
tests/
├── conftest.py              # Shared fixtures and test config
├── shared/                  # Tests for shared/ modules
├── agent_worker/            # Tests for services/agent_worker/
├── webhook/                 # Tests for services/webhook/
├── integration/             # Tests requiring real services (Redis)
└── fixtures/                # Test data and payloads
```

## Running Tests

```bash
# All tests
pytest

# Verbose output
pytest -v

# Stop on first failure
pytest -x

# Show local variables on failure
pytest -l

# Run with coverage
pytest --cov=shared --cov=services --cov-report=html
```

### Windows

```powershell
# Run tests with code quality checks
.\check-code.ps1
```

## Integration Tests

Integration tests require Docker services running:

```bash
# Start services
docker-compose -f docker-compose.minimal.yml up -d

# Run all tests (includes integration)
pytest

# Run only integration tests
pytest tests/integration -v
```

If services aren't running, integration tests are automatically skipped

## Writing Tests

### Async Test

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_queue_publish(mock_redis):
    """Test publishing message to queue."""
    queue = MessageQueue(redis_client=mock_redis)
    await queue.publish({"event": "test"})
    mock_redis.publish.assert_called_once()
```

### Using Fixtures

```python
def test_with_github_payload(sample_github_webhook_payload):
    """Use shared fixture from conftest.py"""
    assert sample_github_webhook_payload["action"] == "opened"
```

Available fixtures in `tests/conftest.py`:

- `mock_redis` - Mock Redis client
- `mock_httpx_client` - Mock HTTP client
- `redis_client` - Real Redis client (integration tests)
- `sample_github_webhook_payload` - GitHub PR payload
- `sample_issue_comment_payload` - GitHub comment payload

## GitHub Actions

Tests run automatically on every PR:

- Unit tests (Python 3.11 & 3.12)
- Integration tests (with Redis)
- Linting (black, isort, ruff, mypy)
- Coverage reports (uploaded as artifacts)

See `.github/workflows/test.yml`

## Mocking

```python
from unittest.mock import AsyncMock, patch

# Mock external service
@patch("services.webhook.handlers.github_client")
async def test_handler(mock_github):
    mock_github.get_pr = AsyncMock(return_value={"number": 123})
    # Test code

# Use fixture
def test_with_mock_redis(mock_redis):
    mock_redis.ping = AsyncMock(return_value=True)
```

## Debugging

```bash
# Drop into debugger on failure
pytest --pdb

# Show print statements
pytest -s

# Show local variables
pytest -l

# Verbose output
pytest -vv
```

## Common Issues

**Redis connection failed**: Start Docker services with `docker-compose up -d`

**Import errors**: Install package with `pip install -e ".[dev]"`

**Async warnings**: Use `@pytest.mark.asyncio` decorator for async tests
