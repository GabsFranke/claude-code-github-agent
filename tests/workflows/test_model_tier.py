"""Tests for the per-workflow ``model`` tier key in workflows.yaml.

The key carries a CLI alias (opus/sonnet/haiku), never a dated model id. The
alias is resolved by the Claude Code CLI through ``ANTHROPIC_DEFAULT_*_MODEL``,
so this layer only validates and forwards it.
"""

import pytest
import yaml
from pydantic import ValidationError

from workflows.engine import WorkflowConfig, WorkflowEngine

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

    def test_repo_workflows_only_use_tier_aliases(self, repo_workflow_config):
        """Whatever the repo pins must be a tier, not a dated id.

        Which workflow gets which tier is an operator decision that changes
        with cost and context-window tradeoffs, so this asserts the vocabulary
        rather than any particular assignment.
        """
        engine = WorkflowEngine(str(repo_workflow_config))
        pinned = {name: wf.model for name, wf in engine.workflows.items() if wf.model}
        assert all(tier in ("opus", "sonnet", "haiku") for tier in pinned.values())
