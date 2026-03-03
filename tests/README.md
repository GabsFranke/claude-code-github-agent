# Testing Strategy

Comprehensive testing for the Simple Claude Code GitHub Agent.

## Test Structure

```
tests/
├── conftest.py              # Shared fixtures and pytest configuration
├── shared/                  # Tests for shared/ modules (61 tests)
│   ├── test_config.py       # Configuration validation (16 tests)
│   ├── test_health.py       # Health check utilities (6 tests)
│   ├── test_http_client.py  # HTTP client wrapper (6 tests)
│   ├── test_models.py       # Pydantic models (6 tests)
│   ├── test_retry.py        # Retry logic (4 tests)
│   ├── test_exceptions.py   # Custom exceptions (5 tests)
│   ├── test_rate_limiter.py # Rate limiting (10 tests)
│   └── test_queue.py        # Message queue abstraction (8 tests)
├── agent_worker/            # Tests for services/agent_worker/ (58 tests)
│   ├── test_github_token_manager.py # GitHub App authentication (18 tests)
│   ├── test_command_base.py         # Command base classes (10 tests)
│   ├── test_command_registry.py     # Command registry (19 tests)
│   └── test_claude_settings.py      # Claude settings configuration (11 tests)
├── webhook/                 # Tests for services/webhook/ (0 tests)
│   └── __init__.py
├── integration/             # Integration tests (6 tests, skip without services)
│   ├── test_webhook_handlers.py    # Webhook handlers (4 skipped)
│   └── test_queue_integration.py   # Redis integration (2 skipped)
└── fixtures/                # Test data and utilities
    └── github_payloads.py   # GitHub webhook payload generators
```

## Test Organization

Tests mirror the project structure for easy navigation:

- `tests/shared/` → tests for `shared/` modules
- `tests/agent_worker/` → tests for `services/agent_worker/` modules
- `tests/webhook/` → tests for `services/webhook/` modules
- `tests/integration/` → integration tests requiring external services
- `tests/fixtures/` → shared test data and utilities

## Test Statistics

- **119 passing**, 6 skipped
- **43% coverage** overall
- **0 errors**, 0 warnings

## Running Tests

```bash
# Run all tests
pytest

# Run tests for specific module
pytest tests/shared/           # All shared module tests
pytest tests/agent_worker/     # All agent_worker tests
pytest tests/webhook/          # All webhook tests

# Run specific test file
pytest tests/shared/test_config.py -v

# Run integration tests
pytest tests/integration/ -v

# Run with coverage
pytest --cov=shared --cov=services --cov-report=html

# Skip slow/integration tests
pytest -m "not slow"
pytest --ignore=tests/integration/
```

## Coverage by Module

High coverage modules:

- `shared/config.py`: 80%
- `shared/health.py`: 87%
- `shared/http_client.py`: 100%
- `shared/models.py`: 100%
- `shared/retry.py`: 100%
- `shared/exceptions.py`: 100%
- `services/agent_worker/auth/github_token_manager.py`: 98%
- `services/agent_worker/commands/base.py`: 100%
- `services/agent_worker/commands/registry.py`: 92%
- `services/agent_worker/config/claude_settings.py`: 100%

## Writing Tests

### Unit Test Example

```python
import pytest
from shared.config import RedisConfig

class TestRedisConfig:
    def test_defaults(self):
        config = RedisConfig()
        assert config.host == "localhost"
        assert config.port == 6379
```

### Async Test Example

```python
@pytest.mark.asyncio
async def test_token_refresh(token_manager, mock_http_client):
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_response.json.return_value = {"token": "ghs_token"}
    mock_http_client.post = AsyncMock(return_value=mock_response)

    token = await token_manager.get_token()
    assert token == "ghs_token"
```

## Test Fixtures

Common fixtures in `conftest.py`:

- `clean_env` - Clears environment variables (auto-used)
- `mock_redis` - Mock Redis client
- `mock_httpx_client` - Mock HTTP client
- `redis_client` - Real Redis client for integration tests (async)
- `sample_github_webhook_payload` - Sample webhook data
- `sample_issue_comment_payload` - Sample issue comment data

## CI/CD

Tests run on every PR via GitHub Actions:

- Unit tests on Python 3.11 & 3.12
- Integration tests with Redis service
- Linting (black, isort, ruff, mypy)
- Coverage reporting

## Important Notes

- Tests use `pytest-asyncio` for async support
- `.env` file is NOT loaded (dotenv mocked in conftest.py)
- Integration tests skip gracefully when services unavailable
- Test structure mirrors project for easy navigation
