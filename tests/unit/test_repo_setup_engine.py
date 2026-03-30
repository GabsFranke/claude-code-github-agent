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


@pytest.mark.asyncio
async def test_run_setup_success(temp_config_file, tmp_path):
    """Test running setup commands successfully."""
    engine = RepoSetupEngine(config_path=temp_config_file)
    config = engine.get_setup_config("owner/repo1")

    result = await engine.run_setup(str(tmp_path), "owner/repo1", config)

    assert result["completed"] is True
    assert result["all_successful"] is True
    assert len(result["results"]) == 2
    assert result["results"][0]["success"] is True
    assert result["results"][1]["success"] is True


@pytest.mark.asyncio
async def test_run_setup_failure(tmp_path):
    """Test handling setup command failure."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/fail": {
                    "setup_commands": ["exit 1", "echo 'should not run'"],
                    "timeout": 10,
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
        assert len(result["results"]) == 1  # Second command should not run
        assert result["results"][0]["success"] is False

    finally:
        config_path.unlink()


@pytest.mark.asyncio
async def test_run_setup_timeout(tmp_path):
    """Test handling setup command timeout."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        config = {
            "repositories": {
                "owner/timeout": {
                    "setup_commands": ["sleep 10"],
                    "timeout": 1,  # 1 second timeout
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
