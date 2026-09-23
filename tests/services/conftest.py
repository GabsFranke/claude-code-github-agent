"""Shared fixtures for tests/services.

``services/scheduler/main.py`` builds its ``WorkflowScheduler`` singleton at
*import* time (it calls ``WorkflowEngine(build_routing=False)`` at module
scope), so ``workflows.yaml`` must already exist before
``test_scheduler_api.py`` is even collected -- a pytest fixture runs too
late to help, since collection imports the module before any fixture body
executes.

``workflows.yaml`` is gitignored operator config, absent on a clean
checkout and in CI (see ``tests/webhook/conftest.py`` for the same problem
solved for the webhook service, where the import can be deferred into a
fixture instead). Seed it here from the tracked example, and remove it
again once the session ends -- leaving a developer's own file alone.
"""

import shutil
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOWS_YAML = PROJECT_ROOT / "workflows.yaml"
_SEEDED_WORKFLOWS_YAML = False

if not _WORKFLOWS_YAML.exists():
    shutil.copy(PROJECT_ROOT / "workflows.example.yaml", _WORKFLOWS_YAML)
    _SEEDED_WORKFLOWS_YAML = True


def pytest_unconfigure(config) -> None:  # noqa: ARG001 - pytest hook signature
    """Remove the seeded workflows.yaml, leaving a developer's own file alone."""
    if _SEEDED_WORKFLOWS_YAML:
        _WORKFLOWS_YAML.unlink(missing_ok=True)
