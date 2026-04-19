"""Tests for fix-review workflow integration."""

from pathlib import Path

import pytest
import yaml

from workflows.engine import WorkflowEngine


class TestFixReviewWorkflow:
    """Test fix-review workflow configuration and routing."""

    @pytest.fixture
    def real_workflows_yaml(self):
        """Load the actual workflows.yaml file."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found in project root")
        return workflow_path

    @pytest.fixture
    def engine(self, real_workflows_yaml):
        """Create WorkflowEngine with real workflows.yaml."""
        return WorkflowEngine(real_workflows_yaml)

    def test_fix_review_workflow_exists(self, engine):
        """Test that fix-review workflow is defined."""
        assert "fix-review" in engine.workflows
        workflow = engine.workflows["fix-review"]
        assert workflow is not None
        assert workflow.triggers is not None
        assert workflow.prompt is not None

    def test_fix_review_event_trigger(self, engine):
        """Test that pull_request.labeled event triggers fix-review workflow."""
        workflow = engine.get_workflow_for_event("pull_request", "labeled")

        assert workflow == "fix-review", (
            "pull_request.labeled event should trigger fix-review workflow. "
            "Check workflows.yaml triggers configuration."
        )

    def test_fix_review_label_filter_matches(self, engine):
        """Test that fix-review label passes the filter."""
        payload = {"label": {"name": "fix-review"}}
        assert (
            engine.check_filters("fix-review", payload, "pull_request.labeled") is True
        )

    def test_fix_review_label_filter_alternative_fix_it(self, engine):
        """Test that fix-it alternative label passes the filter."""
        payload = {"label": {"name": "fix-it"}}
        assert (
            engine.check_filters("fix-review", payload, "pull_request.labeled") is True
        )

    def test_fix_review_label_filter_alternative_pr_fix(self, engine):
        """Test that pr-fix alternative label passes the filter."""
        payload = {"label": {"name": "pr-fix"}}
        assert (
            engine.check_filters("fix-review", payload, "pull_request.labeled") is True
        )

    def test_fix_review_label_filter_non_matching(self, engine):
        """Test that a non-matching label is rejected by the filter."""
        payload = {"label": {"name": "random-label"}}
        assert (
            engine.check_filters("fix-review", payload, "pull_request.labeled") is False
        )

    def test_fix_review_command_trigger(self, engine):
        """Test that /fix-it command triggers fix-review workflow."""
        assert engine.get_workflow_for_command("/fix-it") == "fix-review"

    def test_fix_review_build_prompt(self, engine):
        """Test building prompt for fix-review workflow."""
        prompt, system_context = engine.build_prompt(
            workflow_name="fix-review",
            repo="owner/test-repo",
            issue_number=42,
        )

        assert "/pr-fix:fix-review" in prompt
        assert "owner/test-repo" in prompt
        assert "42" in prompt
        assert system_context is None

    def test_fix_review_workflow_description(self, engine):
        """Test that fix-review has a proper description."""
        workflows = engine.list_workflows()

        assert "fix-review" in workflows
        description = workflows["fix-review"]
        assert description is not None
        assert len(description) > 0
        assert "fix" in description.lower() or "review" in description.lower()


class TestFixReviewWorkflowValidation:
    """Test fix-review workflow configuration validation in workflows.yaml."""

    def test_fix_review_triggers_configuration(self):
        """Test that fix-review has proper triggers configured."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        with open(workflow_path, encoding="utf-8") as f:
            workflows_data = yaml.safe_load(f)

        fix_review = workflows_data["workflows"]["fix-review"]
        triggers = fix_review["triggers"]

        # Should have both events and commands
        assert "events" in triggers
        assert "commands" in triggers

        # Should include pull_request.labeled event with filters
        event_entries = []
        for e in triggers["events"]:
            if isinstance(e, dict):
                event_entries.append(e)
            else:
                event_entries.append({"event": e})

        labeled_event = next(
            (e for e in event_entries if e["event"] == "pull_request.labeled"),
            None,
        )
        assert (
            labeled_event is not None
        ), "fix-review must include 'pull_request.labeled' in events triggers"
        assert "filters" in labeled_event
        assert "label.name" in labeled_event["filters"]

        # Should include /fix-it command
        commands = triggers["commands"]
        assert "/fix-it" in commands

    def test_fix_review_prompt_configuration(self):
        """Test that fix-review prompt is properly configured."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        with open(workflow_path, encoding="utf-8") as f:
            workflows_data = yaml.safe_load(f)

        fix_review = workflows_data["workflows"]["fix-review"]
        prompt = fix_review["prompt"]

        # Should have template
        assert "template" in prompt
        assert "/pr-fix:fix-review" in prompt["template"]
