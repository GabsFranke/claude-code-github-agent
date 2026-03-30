"""Tests for repository setup engine."""

import tempfile
from pathlib import Path

import pytest
import yaml

from repo_setup.engine import RepoSetupEngine


@pytest.fixture
def temp_config_file():
    """Create a temporary config file."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/repo1": {
                    "setup_commands": ["echo 'test1'", "echo 'test2'"],
                    "timeout": 300,
                },
                "owner/repo2": {
                    "setup_commands": ["pip install -r requirements.txt"],
                    "timeout": 600,
                    "env": {"PYTHONPATH": "/workspace"},
                },
            },
            "default": {"enabled": False, "setup_commands": [], "timeout": 300},
        }
        yaml.dump(config, f)
        path = Path(f.name)

    yield path

    # Cleanup
    path.unlink()


def test_load_config(temp_config_file):
    """Test loading configuration from YAML."""
    engine = RepoSetupEngine(config_path=temp_config_file)

    assert len(engine.config.repositories) == 2
    assert "owner/repo1" in engine.config.repositories
    assert "owner/repo2" in engine.config.repositories


def test_get_setup_config_explicit(temp_config_file):
    """Test getting explicit repository configuration."""
    engine = RepoSetupEngine(config_path=temp_config_file)

    config = engine.get_setup_config("owner/repo1")
    assert config is not None
    assert len(config.setup_commands) == 2
    assert config.timeout == 300


def test_get_setup_config_not_found(temp_config_file):
    """Test getting configuration for unconfigured repo."""
    engine = RepoSetupEngine(config_path=temp_config_file)

    config = engine.get_setup_config("owner/unknown")
    assert config is None


def test_get_setup_config_with_env(temp_config_file):
    """Test getting configuration with custom environment."""
    engine = RepoSetupEngine(config_path=temp_config_file)

    config = engine.get_setup_config("owner/repo2")
    assert config is not None
    assert config.env is not None
    assert config.env["PYTHONPATH"] == "/workspace"


def test_list_configured_repos(temp_config_file):
    """Test listing configured repositories."""
    engine = RepoSetupEngine(config_path=temp_config_file)

    repos = engine.list_configured_repos()
    assert len(repos) == 2
    assert "owner/repo1" in repos
    assert "owner/repo2" in repos


def test_missing_config_file():
    """Test handling missing config file."""
    engine = RepoSetupEngine(config_path="/nonexistent/path.yaml")

    # Should initialize with empty config
    assert len(engine.config.repositories) == 0
    assert engine.get_setup_config("any/repo") is None


def test_stop_on_failure_default():
    """Test that stop_on_failure defaults to True."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/repo": {"setup_commands": ["echo hi"]},
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/repo")
        assert config.stop_on_failure is True
    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_run_setup_success(temp_config_file, tmp_path):
    """Test running setup commands successfully — each returns its own result."""
    engine = RepoSetupEngine(config_path=temp_config_file)
    config = engine.get_setup_config("owner/repo1")

    result = await engine.run_setup(str(tmp_path), "owner/repo1", config)

    assert result["completed"] is True
    assert result["all_successful"] is True
    # One result per command
    assert len(result["results"]) == 2
    assert result["results"][0]["success"] is True
    assert result["results"][1]["success"] is True
    assert "test1" in result["results"][0]["stdout"]
    assert "test2" in result["results"][1]["stdout"]


@pytest.mark.asyncio
async def test_run_setup_failure_stops_on_first(tmp_path):
    """Test that stop_on_failure=True halts execution after first failure."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/fail": {
                    "setup_commands": ["exit 1", "echo 'should not run'"],
                    "timeout": 10,
                    "stop_on_failure": True,
                }
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/fail")

        result = await engine.run_setup(str(tmp_path), "owner/fail", config)

        assert result["completed"] is True
        assert result["all_successful"] is False
        # Only the first command ran
        assert len(result["results"]) == 1
        assert result["results"][0]["success"] is False
        assert result["results"][0]["command"] == "exit 1"

    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_run_setup_continue_on_failure(tmp_path):
    """Test that stop_on_failure=False runs all commands regardless of failures."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/continue": {
                    "setup_commands": ["exit 1", "echo 'still runs'"],
                    "timeout": 10,
                    "stop_on_failure": False,
                }
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/continue")

        result = await engine.run_setup(str(tmp_path), "owner/continue", config)

        assert result["completed"] is True
        assert result["all_successful"] is False
        # Both commands ran
        assert len(result["results"]) == 2
        assert result["results"][0]["success"] is False
        assert result["results"][1]["success"] is True
        assert "still runs" in result["results"][1]["stdout"]

    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_run_setup_timeout(tmp_path):
    """Test that a command exceeding the timeout budget is killed."""
    import sys

    if sys.platform == "win32":
        sleep_cmd = "ping -n 11 127.0.0.1 > nul"
    else:
        sleep_cmd = "sleep 10"

    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/timeout": {
                    "setup_commands": [sleep_cmd],
                    "timeout": 1,
                }
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/timeout")

        result = await engine.run_setup(str(tmp_path), "owner/timeout", config)

        assert result["completed"] is True
        assert result["all_successful"] is False
        assert len(result["results"]) == 1
        assert result["results"][0]["success"] is False
        assert result["results"][0].get("error") == "timeout"

    finally:
        config_path.unlink()


def test_default_enabled():
    """Test default configuration when enabled."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {},
            "default": {
                "enabled": True,
                "setup_commands": ["echo 'default'"],
                "timeout": 300,
            },
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)

        # Should return default config for any repo
        config = engine.get_setup_config("any/repo")
        assert config is not None
        assert len(config.setup_commands) == 1
        assert config.setup_commands[0] == "echo 'default'"

    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_each_command_has_own_result(tmp_path):
    """Test that each command produces its own result entry."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/multi-cmd": {
                    "setup_commands": ["echo first", "echo second"],
                    "timeout": 10,
                }
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/multi-cmd")

        result = await engine.run_setup(str(tmp_path), "owner/multi-cmd", config)

        assert result["completed"] is True
        assert result["all_successful"] is True
        assert len(result["results"]) == 2
        assert result["results"][0]["command"] == "echo first"
        assert result["results"][1]["command"] == "echo second"
        assert "first" in result["results"][0]["stdout"]
        assert "second" in result["results"][1]["stdout"]

    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_custom_env_variables(tmp_path):
    """Test that custom environment variables are applied."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/env-test": {
                    "setup_commands": ["echo test"],
                    "timeout": 10,
                    "env": {"CUSTOM_VAR": "custom_value"},
                }
            }
        }
        yaml.dump(config, f)
        config_path = Path(f.name)

    try:
        engine = RepoSetupEngine(config_path=config_path)
        config = engine.get_setup_config("owner/env-test")

        assert config.env is not None
        assert config.env["CUSTOM_VAR"] == "custom_value"

        result = await engine.run_setup(str(tmp_path), "owner/env-test", config)

        assert result["completed"] is True
        assert result["all_successful"] is True

    finally:
        config_path.unlink()
