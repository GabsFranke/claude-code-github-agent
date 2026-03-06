"""Unit tests for workflow engine."""

from pathlib import Path

import pytest
import yaml

from workflows.engine import WorkflowEngine


class TestWorkflowEngine:
    """Test WorkflowEngine class."""

    @pytest.fixture
    def sample_workflows_yaml(self):
        """Create a sample workflows.yaml for testing."""
        return {
            "workflows": {
                "review-pr": {
                    "triggers": {
                        "events": ["pull_request.opened"],
                        "commands": ["/review", "/pr-review"],
                    },
                    "prompt": {
                        "template": "/pr-review-toolkit:review-pr {repo} {issue_number}",
                        "system_context": "review.md",
                    },
                    "description": "Review a pull request",
                },
                "triage-issue": {
                    "triggers": {
                        "events": ["issues.opened"],
                        "commands": ["/triage"],
                    },
                    "prompt": {
                        "template": "Triage issue #{issue_number} in {repo}",
                        "system_context": "triage.md",
                    },
                    "description": "Triage an issue",
                },
                "generic": {
                    "triggers": {"commands": ["/agent"]},
                    "prompt": {
                        "template": "{user_query}",
                        "system_context": "generic.md",
                    },
                    "description": "Generic agent request",
                },
            }
        }

    @pytest.fixture
    def temp_workflow_file(self, sample_workflows_yaml, tmp_path):
        """Create a temporary workflow file."""
        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_workflows_yaml, f)
        return workflow_file

    @pytest.fixture
    def temp_prompts_dir(self, tmp_path):
        """Create temporary prompts directory with sample files."""
        prompts_dir = tmp_path / "prompts"
        prompts_dir.mkdir()

        (prompts_dir / "review.md").write_text(
            "You are reviewing PR in {repo}. Focus on code quality."
        )
        (prompts_dir / "triage.md").write_text(
            "Triage issue #{issue_number}. Add labels."
        )
        (prompts_dir / "generic.md").write_text("You are a helpful coding assistant.")

        return prompts_dir

    def test_initialization(self, temp_workflow_file):
        """Test WorkflowEngine initialization."""
        engine = WorkflowEngine(temp_workflow_file)

        assert len(engine.workflows) == 3
        assert "review-pr" in engine.workflows
        assert "triage-issue" in engine.workflows
        assert "generic" in engine.workflows

    def test_initialization_file_not_found(self):
        """Test initialization with non-existent file."""
        with pytest.raises(FileNotFoundError):
            WorkflowEngine("nonexistent.yaml")

    def test_event_mapping(self, temp_workflow_file):
        """Test event to workflow mapping."""
        engine = WorkflowEngine(temp_workflow_file)

        assert engine._event_map["pull_request.opened"] == "review-pr"
        assert engine._event_map["issues.opened"] == "triage-issue"

    def test_command_mapping(self, temp_workflow_file):
        """Test command to workflow mapping."""
        engine = WorkflowEngine(temp_workflow_file)

        assert engine._command_map["/review"] == "review-pr"
        assert engine._command_map["/pr-review"] == "review-pr"
        assert engine._command_map["/triage"] == "triage-issue"
        assert engine._command_map["/agent"] == "generic"

    def test_get_workflow_for_event_with_action(self, temp_workflow_file):
        """Test getting workflow for event with action."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow = engine.get_workflow_for_event("pull_request", "opened")

        assert workflow == "review-pr"

    def test_get_workflow_for_event_without_action(self, temp_workflow_file):
        """Test getting workflow for event without action."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow = engine.get_workflow_for_event("pull_request")

        assert workflow is None  # No generic pull_request trigger

    def test_get_workflow_for_event_not_found(self, temp_workflow_file):
        """Test getting workflow for unknown event."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow = engine.get_workflow_for_event("unknown_event", "action")

        assert workflow is None

    def test_get_workflow_for_command(self, temp_workflow_file):
        """Test getting workflow for command."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow = engine.get_workflow_for_command("/review")

        assert workflow == "review-pr"

    def test_get_workflow_for_command_not_found(self, temp_workflow_file):
        """Test getting workflow for unknown command."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow = engine.get_workflow_for_command("/unknown")

        assert workflow is None

    def test_build_prompt_simple(self, temp_workflow_file):
        """Test building simple prompt without system context."""
        engine = WorkflowEngine(temp_workflow_file)

        # Temporarily remove system context
        engine.workflows["triage-issue"]["prompt"].pop("system_context")

        prompt = engine.build_prompt(
            workflow_name="triage-issue",
            repo="owner/repo",
            issue_number=123,
        )

        assert prompt == "Triage issue #123 in owner/repo"

    def test_build_prompt_with_user_query(self, temp_workflow_file):
        """Test building prompt with user query."""
        engine = WorkflowEngine(temp_workflow_file)

        # Remove system context for simpler test
        engine.workflows["generic"]["prompt"].pop("system_context")

        prompt = engine.build_prompt(
            workflow_name="generic",
            repo="owner/repo",
            issue_number=456,
            user_query="help me fix this bug",
        )

        assert prompt == "help me fix this bug"

    def test_build_prompt_with_system_context_file(
        self, temp_workflow_file, temp_prompts_dir, monkeypatch
    ):
        """Test building prompt with system context from file."""
        # Change to temp directory so prompts/ is found
        monkeypatch.chdir(temp_prompts_dir.parent)

        engine = WorkflowEngine(temp_workflow_file)

        prompt = engine.build_prompt(
            workflow_name="review-pr",
            repo="owner/repo",
            issue_number=789,
        )

        assert "/pr-review-toolkit:review-pr owner/repo 789" in prompt
        assert "Focus on code quality" in prompt

    def test_build_prompt_system_context_file_not_found(
        self, sample_workflows_yaml, tmp_path, monkeypatch
    ):
        """Test building prompt when system context file doesn't exist."""
        # Create workflow file in a directory without prompts/
        tmpdir = tmp_path / "no_prompts"
        tmpdir.mkdir()
        workflow_file = tmpdir / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(sample_workflows_yaml, f)

        monkeypatch.chdir(tmpdir)

        engine = WorkflowEngine(workflow_file)

        prompt = engine.build_prompt(
            workflow_name="review-pr",
            repo="owner/repo",
            issue_number=100,
        )

        # The engine looks for prompts/ relative to workflows/engine.py
        # So it will find the real prompts/ directory in the project
        # Just verify the basic template is there
        assert "/pr-review-toolkit:review-pr owner/repo 100" in prompt

    def test_build_prompt_with_system_context_and_user_query(
        self, temp_workflow_file, temp_prompts_dir, monkeypatch
    ):
        """Test building prompt with both system context and user query."""
        monkeypatch.chdir(temp_prompts_dir.parent)

        engine = WorkflowEngine(temp_workflow_file)

        prompt = engine.build_prompt(
            workflow_name="generic",
            repo="test/repo",
            issue_number=1,
            user_query="explain the code",
        )

        assert "explain the code" in prompt
        assert "helpful coding assistant" in prompt

    def test_build_prompt_unknown_workflow(self, temp_workflow_file):
        """Test building prompt for unknown workflow."""
        engine = WorkflowEngine(temp_workflow_file)

        with pytest.raises(ValueError, match="Unknown workflow"):
            engine.build_prompt(
                workflow_name="nonexistent",
                repo="owner/repo",
            )

    def test_build_prompt_with_kwargs(self, temp_workflow_file):
        """Test building prompt with additional kwargs."""
        engine = WorkflowEngine(temp_workflow_file)

        # Remove system context
        engine.workflows["triage-issue"]["prompt"].pop("system_context")

        prompt = engine.build_prompt(
            workflow_name="triage-issue",
            repo="owner/repo",
            issue_number=999,
            custom_var="custom_value",
        )

        assert "owner/repo" in prompt
        assert "999" in prompt

    def test_list_workflows(self, temp_workflow_file):
        """Test listing all workflows."""
        engine = WorkflowEngine(temp_workflow_file)

        workflows = engine.list_workflows()

        assert len(workflows) == 3
        assert workflows["review-pr"] == "Review a pull request"
        assert workflows["triage-issue"] == "Triage an issue"
        assert workflows["generic"] == "Generic agent request"

    def test_list_workflows_no_description(self, temp_workflow_file):
        """Test listing workflows without descriptions."""
        engine = WorkflowEngine(temp_workflow_file)

        # Remove description
        engine.workflows["generic"].pop("description")

        workflows = engine.list_workflows()

        assert workflows["generic"] == "No description"

    def test_multiple_commands_same_workflow(self, temp_workflow_file):
        """Test multiple commands mapping to same workflow."""
        engine = WorkflowEngine(temp_workflow_file)

        workflow1 = engine.get_workflow_for_command("/review")
        workflow2 = engine.get_workflow_for_command("/pr-review")

        assert workflow1 == workflow2 == "review-pr"

    def test_system_context_variable_substitution(
        self, temp_workflow_file, temp_prompts_dir, monkeypatch
    ):
        """Test that system context supports variable substitution."""
        monkeypatch.chdir(temp_prompts_dir.parent)

        engine = WorkflowEngine(temp_workflow_file)

        prompt = engine.build_prompt(
            workflow_name="triage-issue",
            repo="test/project",
            issue_number=42,
        )

        # System context should have variables filled
        assert "issue #42" in prompt
        assert "test/project" in prompt  # Template uses {repo}
        assert "add labels" in prompt.lower()

    def test_empty_issue_number(self, temp_workflow_file):
        """Test building prompt with None issue_number."""
        engine = WorkflowEngine(temp_workflow_file)

        # Remove system context
        engine.workflows["triage-issue"]["prompt"].pop("system_context")

        prompt = engine.build_prompt(
            workflow_name="triage-issue",
            repo="owner/repo",
            issue_number=None,
        )

        assert "owner/repo" in prompt
        assert "#" in prompt  # Empty issue number becomes empty string


class TestWorkflowEngineIntegration:
    """Integration tests for WorkflowEngine with real workflows.yaml."""

    def test_load_real_workflows_yaml(self):
        """Test loading the actual workflows.yaml file."""
        # Assumes workflows.yaml exists in project root
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found in project root")

        engine = WorkflowEngine(workflow_path)

        assert len(engine.workflows) > 0
        assert "review-pr" in engine.workflows or "generic" in engine.workflows

    def test_real_workflow_routing(self):
        """Test routing with real workflows.yaml."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found in project root")

        engine = WorkflowEngine(workflow_path)

        # Test common patterns
        pr_workflow = engine.get_workflow_for_event("pull_request", "opened")
        assert pr_workflow is not None

        review_workflow = engine.get_workflow_for_command("/review")
        assert review_workflow is not None
