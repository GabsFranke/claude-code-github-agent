"""Tests for the per-workflow ``model`` tier key in workflows.yaml.

The key carries a CLI alias (opus/sonnet/haiku), never a dated model id. The
alias is resolved by the Claude Code CLI through ``ANTHROPIC_DEFAULT_*_MODEL``,
so this layer only validates and forwards it.
"""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from workflows.engine import WorkflowConfig, WorkflowEngine

REPO_ROOT = Path(__file__).resolve().parents[2]

BASE = {"triggers": {"commands": ["/x"]}, "prompt": {"template": "x"}}


@pytest.mark.unit
class TestWorkflowModelTier:
    def test_defaults_to_unset(self):
        assert WorkflowConfig(**BASE).model is None

    @pytest.mark.parametrize("tier", ["opus", "sonnet", "haiku"])
    def test_accepts_tier_aliases(self, tier):
        assert WorkflowConfig(**BASE, model=tier).model == tier

    def test_rejects_full_model_id(self):
        """Dated ids belong in ANTHROPIC_DEFAULT_*_MODEL, not workflows.yaml."""
        with pytest.raises(ValidationError):
            WorkflowConfig(**BASE, model="claude-opus-5")

    def test_loaded_from_yaml(self, tmp_path):
        config_file = tmp_path / "workflows.yaml"
        config_file.write_text(
            yaml.dump({"workflows": {"wf": {**BASE, "model": "opus"}}})
        )
        engine = WorkflowEngine(str(config_file))
        assert engine.workflows["wf"].model == "opus"

    def test_repo_review_pr_uses_opus(self):
        engine = WorkflowEngine(str(REPO_ROOT / "workflows.yaml"))
        assert engine.workflows["review-pr"].model == "opus"
