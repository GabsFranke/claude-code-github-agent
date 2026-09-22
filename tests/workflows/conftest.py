"""Shared fixtures for the workflow-config tests."""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_workflow_config() -> Path:
    """Path to the repository's workflow config.

    ``workflows.yaml`` is gitignored operator config, so a fresh checkout
    (CI included) only has the tracked ``workflows.example.yaml``. The
    Dockerfiles fall back the same way. Tests that assert on the repo's own
    config therefore read whichever of the two is present, so they exercise
    the real file locally and the example in CI instead of erroring out.
    """
    configured = PROJECT_ROOT / "workflows.yaml"
    if configured.exists():
        return configured
    return PROJECT_ROOT / "workflows.example.yaml"
