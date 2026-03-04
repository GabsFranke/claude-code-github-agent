# Development Guide

Complete guide for developers working on the Simple Claude Code GitHub Agent.

## Setup

### Install Dependencies

```bash
# Clone repository
git clone https://github.com/yourusername/simple-claude-code-github-agent.git
cd simple-claude-code-github-agent

# Create and activate virtual environment
python -m venv venv

# Activate venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install all dependencies (services + dev tools)
pip install -r requirements-dev.txt
```

### Configure Environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

See [CONFIGURATION.md](CONFIGURATION.md) for all options.

### Start Services

```bash
# Docker (recommended)
docker-compose up --build -d

# Manual (not recommended - complex setup)
# You'll need to run 4 services in separate terminals:
# Terminal 1: redis-server --requirepass myredissecret
# Terminal 2: cd services/webhook && python main.py
# Terminal 3: cd services/agent_worker && python worker.py
# Terminal 4: python -m services.sandbox_executor.sandbox_worker
# Note: Manual setup requires proper environment variables for each service
```

## Project Structure

```
simple-claude-code-github-agent/
├── services/
│   ├── agent_worker/         # Job coordinator
│   ├── sandbox_executor/     # Claude SDK execution
│   ├── result_poster/        # GitHub response posting
│   └── webhook/              # Webhook receiver
├── shared/                   # Shared utilities
│   ├── config.py            # Pydantic configuration
│   ├── queue.py             # Message queue abstraction
│   ├── job_queue.py         # Job queue implementation
│   ├── rate_limiter.py      # Rate limiting
│   └── health.py            # Health monitoring
├── plugins/                  # Claude SDK plugins
│   └── pr-review-toolkit/
├── subagents/               # Subagent definitions
├── tests/                   # Test suite
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── docs/                    # Documentation
└── .kiro/steering/          # Kiro steering rules
```

## Testing

### Run Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=shared --cov=services --cov-report=html

# Specific test file
pytest tests/shared/test_config.py

# Pattern matching
pytest -k "rate_limiter"

# Verbose
pytest -v

# Stop on first failure
pytest -x
```

### Test Structure

```
tests/
├── conftest.py              # Shared fixtures
├── shared/                  # Tests for shared/ modules
├── agent_worker/            # Tests for services/agent_worker/
├── webhook/                 # Tests for services/webhook/
├── integration/             # Integration tests (require Redis)
└── fixtures/                # Test data
```

### Integration Tests

Require Docker services:

```bash
# Start services
docker-compose -f docker-compose.minimal.yml up -d

# Run integration tests
pytest tests/integration -v

# Run only integration tests
pytest -m integration
```

If services aren't running, integration tests are automatically skipped.

### Writing Tests

**Async test**:

```python
import pytest
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_queue_publish(mock_redis):
    queue = MessageQueue(redis_client=mock_redis)
    await queue.publish({"event": "test"})
    mock_redis.publish.assert_called_once()
```

**Using fixtures**:

```python
def test_with_payload(sample_github_webhook_payload):
    assert sample_github_webhook_payload["action"] == "opened"
```

Available fixtures in `tests/conftest.py`:

- `mock_redis` - Mock Redis client
- `mock_httpx_client` - Mock HTTP client
- `redis_client` - Real Redis (integration tests)
- `sample_github_webhook_payload` - GitHub PR payload
- `sample_issue_comment_payload` - GitHub comment payload

## Code Quality

### Run All Checks

```bash
# Windows
.\check-code.ps1

# Auto-fix formatting
.\check-code.ps1 -Fix

# Fast mode (skip mypy)
.\check-code.ps1 -Fast

# Verbose output
.\check-code.ps1 -Verbose
```

### Individual Tools

```bash
# Format code
black .
isort .

# Lint
ruff check .
flake8 .
pylint shared/ services/

# Type check
mypy .
```

### Configuration

- `pyproject.toml` - black, isort, mypy, pytest, ruff
- `.flake8` - Flake8 configuration
- `.pylintrc` - Pylint configuration

## Debugging

### View Logs

```bash
# All services
docker-compose logs -f

# Specific service
docker-compose logs -f sandbox_worker
docker-compose logs -f worker
docker-compose logs -f webhook

# Langfuse hook logs (inside container only)
docker-compose exec sandbox_worker cat /root/.claude/state/langfuse_hook.log
docker-compose exec sandbox_worker tail -n 50 /root/.claude/state/langfuse_hook.log
```

### Debug with pdb

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

### Langfuse Traces

If using full Docker Compose setup:

1. Open http://localhost:7500
2. Login: `admin@example.com` / `admin123`
3. View traces for detailed execution flow

See [LANGFUSE_SETUP.md](LANGFUSE_SETUP.md) for details.

## Common Tasks

### Add New Subagent

1. Create file in `subagents/`:

```python
# subagents/my_specialist.py
from claude_agent_sdk import AgentDefinition

MY_SPECIALIST = AgentDefinition(
    description="Brief description and when to use proactively",
    prompt="""System prompt with instructions...""",
    model="inherit"
)
```

2. Export in `subagents/__init__.py`:

```python
from .my_specialist import MY_SPECIALIST

AGENTS = {
    "my-specialist": MY_SPECIALIST,
    # ... other agents
}
```

3. Rebuild and restart:

```bash
docker-compose build worker
docker-compose up -d worker
```

See [SUBAGENTS.md](SUBAGENTS.md) for details.

### Add New Plugin

1. Copy plugin to `plugins/`:

```bash
cp -r /path/to/plugin ./plugins/
```

2. Update `services/agent_worker/worker.py`:

```python
options = ClaudeAgentOptions(
    plugins=[
        {"type": "local", "path": "/app/plugins/pr-review-toolkit"},
        {"type": "local", "path": "/app/plugins/new-plugin"},  # Add here
    ],
    # ... other options
)
```

3. Rebuild and restart:

```bash
docker-compose build worker
docker-compose up -d worker
```

See [PLUGINS.md](PLUGINS.md) for details.

### Modify Configuration

1. Edit `shared/config.py` to add new settings
2. Add validation if needed
3. Update `.env.example` with new variables
4. Update [CONFIGURATION.md](CONFIGURATION.md)
5. Add tests in `tests/shared/test_config.py`

### Add New Command

1. Edit `services/agent_worker/processors/command_registry.py`
2. Add command to registry
3. Add tests
4. Update documentation

## Troubleshooting

### Configuration Errors

```bash
# Check required fields
grep -E "GITHUB_|ANTHROPIC_" .env

# Test configuration
python -c "from shared.config import get_worker_config; print(get_worker_config())"
```

### Import Errors

```bash
# Install package
pip install -e .

# Verify installation
python -c "import shared; print(shared.__file__)"
```

### Health Check Failures

```bash
# Check health file
docker-compose exec worker cat /tmp/worker_health

# Check logs
docker-compose logs worker | tail -50

# Restart worker
docker-compose restart worker
```

### Rate Limiting Issues

```bash
# Check rate limit logs
docker-compose logs worker | grep "Rate limit"

# Adjust limits in .env
GITHUB_RATE_LIMIT=4000
ANTHROPIC_RATE_LIMIT=80

# Restart
docker-compose restart worker
```

### Redis Connection

```bash
# Check Redis is running
redis-cli ping  # Should return PONG

# Check connection
docker-compose logs redis

# Test connection
redis-cli -h localhost -p 6379 -a "$REDIS_PASSWORD" ping
```

### Subagents Not Working

```bash
# Check files exist
docker-compose exec worker ls -la /root/.claude/agents/

# Check file content
docker-compose exec worker cat /root/.claude/agents/bug-hunter.md

# List agents
docker-compose exec worker claude agents

# Rebuild container
docker-compose build worker && docker-compose up -d
```

See [DEBUGGING_SUBAGENTS.md](DEBUGGING_SUBAGENTS.md) for details.

## GitHub Actions

Tests run automatically on every PR:

- Unit tests (Python 3.11 & 3.12)
- Integration tests (with Redis)
- Linting (black, isort, ruff, mypy)
- Coverage reports (uploaded as artifacts)

See `.github/workflows/test.yml`

## Contributing

### Workflow

1. Fork repository
2. Create feature branch: `git checkout -b feature/my-feature`
3. Make changes
4. Run tests: `pytest`
5. Run linters: `.\check-code.ps1`
6. Commit: `git commit -m "Add my feature"`
7. Push: `git push origin feature/my-feature`
8. Open Pull Request

### Code Style

- Follow PEP 8
- Use type hints
- Write docstrings for public functions
- Keep functions small and focused
- Add tests for new features

### Commit Messages

- Use present tense: "Add feature" not "Added feature"
- Be descriptive but concise
- Reference issues: "Fix #123: Add rate limiting"

## Performance

### Profiling

```bash
# Profile specific function
python -m cProfile -o output.prof services/agent_worker/worker.py

# View results
python -m pstats output.prof
```

### Benchmarking

```bash
# Time specific operation
time docker-compose exec worker python -c "from shared.config import get_worker_config; get_worker_config()"
```

## Security

### Secrets Management

- Never commit secrets to git
- Use `.env` file (gitignored)
- Use environment variables in production
- Rotate secrets regularly

### Dependency Updates

```bash
# Check for updates
pip list --outdated

# Update dependencies
pip install --upgrade -r requirements-dev.txt

# Run tests after updates
pytest
```

## Release Process

1. Update version in `setup.py`
2. Update CHANGELOG.md
3. Run full test suite: `pytest`
4. Run linters: `.\check-code.ps1`
5. Tag release: `git tag v1.0.0`
6. Push tag: `git push origin v1.0.0`
7. Create GitHub release

## See Also

- [Getting Started](GETTING_STARTED.md) - Installation
- [Architecture](ARCHITECTURE.md) - System design
- [Configuration](CONFIGURATION.md) - Environment variables
- [Testing](TESTING.md) - Detailed testing guide
- [Subagents](SUBAGENTS.md) - Subagent system
- [Plugins](PLUGINS.md) - Plugin system
