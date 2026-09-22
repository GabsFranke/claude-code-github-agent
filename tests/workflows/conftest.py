"""Shared fixtures for the workflow-config tests."""

from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def repo_workflow_config() -> Path:
    """Path to the workflow config these tests may assert against.

    Always the tracked ``workflows.example.yaml``, never ``workflows.yaml``.
    The latter is gitignored operator config that changes whenever cost,
    routing or trigger-chaining tradeoffs change, so an assertion on a value
    read from it is not a test of anything: it breaks on an edit the operator
    is entitled to make, and it is absent altogether on a fresh checkout.
    The example is reviewed like source, so assertions against it stay
    meaningful and give the same answer on every machine.
    """
    return PROJECT_ROOT / "workflows.example.yaml"
