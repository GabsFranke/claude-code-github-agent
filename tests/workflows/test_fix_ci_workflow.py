"""Tests for fix-ci workflow integration."""

from pathlib import Path

import pytest
import yaml

from workflows.engine import WorkflowEngine


class TestFixCIWorkflow:
    """Test fix-ci workflow configuration and routing."""

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

    def test_fix_ci_workflow_exists(self, engine):
        """Test that fix-ci workflow is defined."""
        assert "fix-ci" in engine.workflows
        workflow = engine.workflows["fix-ci"]
        assert workflow is not None
        assert workflow.triggers is not None
        assert workflow.prompt is not None

    def test_fix_ci_event_trigger(self, engine):
        """Test that workflow_job.completed event triggers fix-ci workflow."""
        # This test will FAIL until the workflow engine properly handles this event
        workflow = engine.get_workflow_for_event("workflow_job", "completed")

        assert workflow == "fix-ci", (
            "workflow_job.completed event should trigger fix-ci workflow. "
            "Check workflows.yaml triggers configuration."
        )

    def test_fix_ci_command_triggers(self, engine):
        """Test that fix-ci commands are properly mapped."""
        # Test all command aliases
        assert engine.get_workflow_for_command("/fix-ci") == "fix-ci"
        assert engine.get_workflow_for_command("/fix-build") == "fix-ci"
        assert engine.get_workflow_for_command("/fix-tests") == "fix-ci"

    def test_fix_ci_prompt_template(self, engine):
        """Test that fix-ci prompt template is correctly configured."""
        workflow = engine.workflows["fix-ci"]
        prompt_config = workflow.prompt

        assert prompt_config.template is not None
        template = prompt_config.template

        # Should use the ci-failure-toolkit command
        assert "/ci-failure-toolkit:fix-ci" in template
        assert "{repo}" in template
        assert "{issue_number}" in template

    def test_fix_ci_system_context(self, engine):
        """Test that fix-ci does not have system context configured."""
        workflow = engine.workflows["fix-ci"]
        prompt_config = workflow.prompt

        # Should NOT have system_context - the plugin provides its own context
        assert prompt_config.system_context is None

    def test_fix_ci_build_prompt(self, engine):
        """Test building prompt for fix-ci workflow."""
        prompt, system_context = engine.build_prompt(
            workflow_name="fix-ci",
            repo="owner/test-repo",
            issue_number=12345,
        )

        # Should contain the command with repo and run ID
        assert "/ci-failure-toolkit:fix-ci" in prompt
        assert "owner/test-repo" in prompt
        assert "12345" in prompt
        # Should NOT have system context - plugin provides its own
        assert system_context is None

    def test_fix_ci_workflow_description(self, engine):
        """Test that fix-ci has a proper description."""
        workflows = engine.list_workflows()

        assert "fix-ci" in workflows
        description = workflows["fix-ci"]
        assert description is not None
        assert len(description) > 0
        assert "ci" in description.lower() or "failure" in description.lower()


class TestFixCIWorkflowJobEvent:
    """Test workflow_job event handling specifically."""

    @pytest.fixture
    def sample_workflow_job_payload(self):
        """Sample workflow_job.completed webhook payload."""
        return {
            "action": "completed",
            "workflow_job": {
                "id": 12345,
                "run_id": 67890,
                "workflow_name": "CI",
                "head_branch": "feature-branch",
                "run_url": "https://github.com/owner/repo/actions/runs/67890",
                "status": "completed",
                "conclusion": "failure",
                "started_at": "2024-01-01T10:00:00Z",
                "completed_at": "2024-01-01T10:05:00Z",
                "name": "test",
                "steps": [
                    {
                        "name": "Run tests",
                        "status": "completed",
                        "conclusion": "failure",
                        "number": 1,
                    }
                ],
            },
            "repository": {
                "full_name": "owner/repo",
                "name": "repo",
                "owner": {"login": "owner"},
            },
            "installation": {"id": 12345},
        }

    def test_workflow_job_completed_event_format(self, sample_workflow_job_payload):
        """Test that workflow_job.completed event has expected structure."""
        assert sample_workflow_job_payload["action"] == "completed"
        assert "workflow_job" in sample_workflow_job_payload
        assert sample_workflow_job_payload["workflow_job"]["conclusion"] == "failure"

    def test_workflow_job_event_routing(self):
        """Test that workflow_job.completed routes to fix-ci."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # This is the critical test that will FAIL
        workflow = engine.get_workflow_for_event("workflow_job", "completed")

        assert workflow == "fix-ci", (
            "Expected workflow_job.completed to route to fix-ci workflow. "
            "The workflow engine should map this GitHub event to the fix-ci workflow."
        )

    def test_workflow_job_without_action(self):
        """Test that workflow_job without action doesn't trigger."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Generic workflow_job event without action should not trigger
        workflow = engine.get_workflow_for_event("workflow_job")

        # Should be None or not fix-ci (depends on implementation)
        # We only want workflow_job.completed to trigger
        assert workflow is None or workflow != "fix-ci"


class TestFixCIPromptGeneration:
    """Test prompt generation for fix-ci workflow."""

    @pytest.fixture
    def engine(self):
        """Create WorkflowEngine with real workflows.yaml."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")
        return WorkflowEngine(workflow_path)

    def test_fix_ci_prompt_with_run_id(self, engine):
        """Test prompt generation with workflow run ID."""
        prompt, system_context = engine.build_prompt(
            workflow_name="fix-ci",
            repo="owner/repo",
            issue_number=67890,  # This would be the run_id
        )

        assert "/ci-failure-toolkit:fix-ci" in prompt
        assert "owner/repo" in prompt
        assert "67890" in prompt
        # Plugin provides its own context
        assert system_context is None

    def test_fix_ci_prompt_with_pr_number(self, engine):
        """Test prompt generation with PR number."""
        prompt, system_context = engine.build_prompt(
            workflow_name="fix-ci",
            repo="owner/repo",
            issue_number=123,  # This would be the PR number
        )

        assert "/ci-failure-toolkit:fix-ci" in prompt
        assert "owner/repo" in prompt
        # Plugin provides its own context
        assert system_context is None
        assert "123" in prompt

    def test_fix_ci_prompt_includes_system_context(self, engine):
        """Test that fix-ci prompt does not include system context from file."""
        prompt, system_context = engine.build_prompt(
            workflow_name="fix-ci",
            repo="test/repo",
            issue_number=999,
        )

        # The prompt should NOT include content from prompts/fix-ci.md
        # The plugin provides its own context
        assert len(prompt) > 0
        assert "/ci-failure-toolkit:fix-ci" in prompt
        assert system_context is None


class TestFixCIWorkflowValidation:
    """Test fix-ci workflow configuration validation."""

    def test_fix_ci_triggers_configuration(self):
        """Test that fix-ci has proper triggers configured."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        with open(workflow_path, encoding="utf-8") as f:
            workflows_data = yaml.safe_load(f)

        fix_ci = workflows_data["workflows"]["fix-ci"]
        triggers = fix_ci["triggers"]

        # Should have both events and commands
        assert "events" in triggers
        assert "commands" in triggers

        # Should include workflow_job.completed event
        assert (
            "workflow_job.completed" in triggers["events"]
        ), "fix-ci workflow must include 'workflow_job.completed' in events triggers"

        # Should include command aliases
        commands = triggers["commands"]
        assert "/fix-ci" in commands
        assert "/fix-build" in commands
        assert "/fix-tests" in commands

    def test_fix_ci_prompt_configuration(self):
        """Test that fix-ci prompt is properly configured."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        with open(workflow_path, encoding="utf-8") as f:
            workflows_data = yaml.safe_load(f)

        fix_ci = workflows_data["workflows"]["fix-ci"]
        prompt = fix_ci["prompt"]

        # Should have template
        assert "template" in prompt
        assert "/ci-failure-toolkit:fix-ci" in prompt["template"]

        # Should NOT have system_context - plugin provides its own
        assert "system_context" not in prompt or prompt.get("system_context") is None
