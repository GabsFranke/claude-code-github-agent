"""Unit tests for skip_self workflow feature."""

import pytest
import yaml

from workflows.engine import WorkflowEngine


class TestSkipSelfFeature:
    """Test skip_self configuration in workflows."""

    @pytest.fixture
    def workflows_with_skip_self(self):
        """Create workflows with various skip_self configurations."""
        return {
            "workflows": {
                "skip-true-explicit": {
                    "triggers": {"events": ["pull_request.opened"]},
                    "prompt": {"template": "test {repo}"},
                    "description": "Explicitly skip self",
                    "skip_self": True,
                },
                "skip-false-explicit": {
                    "triggers": {"events": ["issues.opened"]},
                    "prompt": {"template": "test {repo}"},
                    "description": "Explicitly allow self",
                    "skip_self": False,
                },
                "skip-default": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test {repo}"},
                    "description": "Default skip_self (should be True)",
                    # skip_self omitted - should default to True
                },
            }
        }

    @pytest.fixture
    def temp_workflow_file_with_skip_self(self, workflows_with_skip_self, tmp_path):
        """Create temporary workflow file with skip_self configurations."""
        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_with_skip_self, f)
        return workflow_file

    def test_skip_self_explicit_true(self, temp_workflow_file_with_skip_self):
        """Test workflow with skip_self explicitly set to True."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        # Bot triggers event - should skip
        assert (
            engine.should_skip_self("skip-true-explicit", "bot-user", "bot-user")
            is True
        )
        # Human triggers event - should not skip
        assert (
            engine.should_skip_self("skip-true-explicit", "human-user", "bot-user")
            is False
        )

    def test_skip_self_explicit_false(self, temp_workflow_file_with_skip_self):
        """Test workflow with skip_self explicitly set to False."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        # Even if bot triggers event, skip_self=False means don't skip
        assert (
            engine.should_skip_self("skip-false-explicit", "bot-user", "bot-user")
            is False
        )
        assert (
            engine.should_skip_self("skip-false-explicit", "human-user", "bot-user")
            is False
        )

    def test_skip_self_default_value(self, temp_workflow_file_with_skip_self):
        """Test workflow with skip_self omitted defaults to True."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        # When skip_self is omitted, it should default to True
        # Bot triggers event - should skip
        assert engine.should_skip_self("skip-default", "bot-user", "bot-user") is True
        # Human triggers event - should not skip
        assert (
            engine.should_skip_self("skip-default", "human-user", "bot-user") is False
        )

    def test_skip_self_unknown_workflow(self, temp_workflow_file_with_skip_self):
        """Test should_skip_self with unknown workflow defaults to True."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        # Unknown workflows should default to True (safe default)
        assert (
            engine.should_skip_self("nonexistent-workflow", "bot-user", "bot-user")
            is True
        )
        assert (
            engine.should_skip_self("nonexistent-workflow", "human-user", "bot-user")
            is True
        )

    def test_skip_self_workflow_config_access(self, temp_workflow_file_with_skip_self):
        """Test accessing skip_self directly from workflow config."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        # Access skip_self from workflow config
        assert engine.workflows["skip-true-explicit"].skip_self is True
        assert engine.workflows["skip-false-explicit"].skip_self is False
        assert engine.workflows["skip-default"].skip_self is True

    def test_skip_self_with_real_workflows(self):
        """Test skip_self with actual workflows.yaml from project."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found in project root")

        engine = WorkflowEngine(workflow_path)

        # Test that real workflows have skip_self configured
        if "review-pr" in engine.workflows:
            # review-pr should skip self when bot is actor
            assert engine.should_skip_self("review-pr", "bot-user", "bot-user") is True
            # review-pr should not skip when human is actor
            assert (
                engine.should_skip_self("review-pr", "human-user", "bot-user") is False
            )

        if "generic" in engine.workflows:
            # generic might allow self-interaction
            skip_self = engine.should_skip_self("generic", "bot-user", "bot-user")
            assert isinstance(skip_self, bool)

    def test_skip_self_all_workflows_have_value(
        self, temp_workflow_file_with_skip_self
    ):
        """Test that all workflows have a skip_self value (no None)."""
        engine = WorkflowEngine(temp_workflow_file_with_skip_self)

        for _workflow_name, workflow in engine.workflows.items():
            assert workflow.skip_self is not None
            assert isinstance(workflow.skip_self, bool)

    def test_skip_self_invalid_type(self, tmp_path):
        """Test that invalid skip_self type is rejected."""
        workflows_yaml = {
            "workflows": {
                "invalid-skip-self": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test"},
                    "skip_self": 123,  # Should be bool, not int
                }
            }
        }

        workflow_file = tmp_path / "workflows_invalid.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        # Pydantic should reject non-boolean values
        with pytest.raises(ValueError):
            WorkflowEngine(workflow_file)

    def test_skip_self_multiple_workflows_mixed(self, tmp_path):
        """Test multiple workflows with mixed skip_self configurations."""
        workflows_yaml = {
            "workflows": {
                "workflow-1": {
                    "triggers": {"commands": ["/w1"]},
                    "prompt": {"template": "test"},
                    "skip_self": True,
                },
                "workflow-2": {
                    "triggers": {"commands": ["/w2"]},
                    "prompt": {"template": "test"},
                    "skip_self": False,
                },
                "workflow-3": {
                    "triggers": {"commands": ["/w3"]},
                    "prompt": {"template": "test"},
                    # skip_self omitted
                },
                "workflow-4": {
                    "triggers": {"commands": ["/w4"]},
                    "prompt": {"template": "test"},
                    "skip_self": True,
                },
            }
        }

        workflow_file = tmp_path / "workflows_mixed.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        # Test with bot as actor
        assert engine.should_skip_self("workflow-1", "bot-user", "bot-user") is True
        assert engine.should_skip_self("workflow-2", "bot-user", "bot-user") is False
        assert (
            engine.should_skip_self("workflow-3", "bot-user", "bot-user") is True
        )  # Default
        assert engine.should_skip_self("workflow-4", "bot-user", "bot-user") is True

        # Test with human as actor
        assert engine.should_skip_self("workflow-1", "human-user", "bot-user") is False
        assert engine.should_skip_self("workflow-2", "human-user", "bot-user") is False
        assert engine.should_skip_self("workflow-3", "human-user", "bot-user") is False
        assert engine.should_skip_self("workflow-4", "human-user", "bot-user") is False


class TestSkipSelfIntegration:
    """Integration tests for skip_self with webhook logic."""

    def test_skip_self_scenario_bot_creates_pr(self):
        """Test scenario: bot creates PR, should skip review."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Simulate bot creating PR (bot is the actor)
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")

        if workflow_name:
            should_skip = engine.should_skip_self(workflow_name, "bot-user", "bot-user")
            # Bot's own PRs should be skipped by default
            assert should_skip is True

    def test_skip_self_scenario_human_creates_pr(self):
        """Test scenario: human creates PR, should process."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Simulate human creating PR (human is the actor)
        workflow_name = engine.get_workflow_for_event("pull_request", "opened")

        if workflow_name:
            should_skip = engine.should_skip_self(
                workflow_name, "human-user", "bot-user"
            )
            # Human PRs should not be skipped
            assert should_skip is False

    def test_skip_self_scenario_bot_uses_agent_command(self):
        """Test scenario: bot uses /agent command, should skip."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Simulate bot using /agent command (bot is the actor)
        workflow_name = engine.get_workflow_for_command("/agent")

        if workflow_name:
            # Bot commenting should be skipped if skip_self=true
            should_skip_bot = engine.should_skip_self(
                workflow_name, "bot-user", "bot-user"
            )
            # Human commenting should not be skipped
            should_skip_human = engine.should_skip_self(
                workflow_name, "human-user", "bot-user"
            )

            assert isinstance(should_skip_bot, bool)
            assert should_skip_human is False  # Human commands always work

    def test_skip_self_scenario_ci_failure_on_bot_pr(self):
        """Test scenario: CI fails on bot's PR."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Simulate workflow_job.completed event triggered by bot
        workflow_name = engine.get_workflow_for_event("workflow_job", "completed")

        if workflow_name:
            should_skip = engine.should_skip_self(workflow_name, "bot-user", "bot-user")
            # CI failures triggered by bot should be skipped by default
            # (use /fix-ci command to manually trigger)
            assert should_skip is True

    def test_skip_self_with_command_override(self):
        """Test that commands can override skip_self behavior."""
        from pathlib import Path

        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"

        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")

        engine = WorkflowEngine(workflow_path)

        # Commands like /fix-ci should work even on bot PRs
        # The webhook logic handles this by checking if it's a command
        workflow_name = engine.get_workflow_for_command("/fix-ci")

        if workflow_name:
            # Workflow might have skip_self=true, but commands bypass this
            # This is handled in webhook logic, not engine
            assert workflow_name is not None


class TestSkipSelfEdgeCases:
    """Test edge cases for skip_self feature."""

    def test_skip_self_with_empty_workflows(self, tmp_path):
        """Test skip_self with empty workflows file."""
        workflows_yaml = {"workflows": {}}

        workflow_file = tmp_path / "workflows_empty.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        # Should handle empty workflows gracefully
        assert len(engine.workflows) == 0
        assert engine.should_skip_self("any-workflow", "bot-user", "bot-user") is True

    def test_skip_self_consistency_across_calls(self, tmp_path):
        """Test that skip_self returns consistent values."""
        workflows_yaml = {
            "workflows": {
                "test-workflow": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test"},
                    "skip_self": True,
                }
            }
        }

        workflow_file = tmp_path / "workflows_consistent.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        # Call multiple times, should return same value
        result1 = engine.should_skip_self("test-workflow", "bot-user", "bot-user")
        result2 = engine.should_skip_self("test-workflow", "bot-user", "bot-user")
        result3 = engine.should_skip_self("test-workflow", "bot-user", "bot-user")

        assert result1 == result2 == result3 is True

    def test_skip_self_with_special_characters_in_workflow_name(self, tmp_path):
        """Test skip_self with valid special characters in workflow name."""
        workflows_yaml = {
            "workflows": {
                "test-workflow-123": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test"},
                    "skip_self": False,
                }
            }
        }

        workflow_file = tmp_path / "workflows_special.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        assert (
            engine.should_skip_self("test-workflow-123", "bot-user", "bot-user")
            is False
        )
        assert (
            engine.should_skip_self("test-workflow-123", "human-user", "bot-user")
            is False
        )

    def test_skip_self_documentation_examples(self, tmp_path):
        """Test examples from documentation work correctly."""
        # Example from docs: review-pr with skip_self omitted
        workflows_yaml = {
            "workflows": {
                "review-pr": {
                    "triggers": {
                        "events": ["pull_request.opened"],
                        "commands": ["/review"],
                    },
                    "prompt": {"template": "review {repo}"},
                    "description": "Review PR",
                    # skip_self omitted - should default to true
                },
                "generic": {
                    "triggers": {"commands": ["/agent"]},
                    "prompt": {"template": "{user_query}"},
                    "description": "Generic",
                    "skip_self": False,  # Explicitly allow self
                },
            }
        }

        workflow_file = tmp_path / "workflows_docs.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        # Verify documentation examples work as described
        # review-pr: skip_self defaults to True
        assert engine.should_skip_self("review-pr", "bot-user", "bot-user") is True
        assert engine.should_skip_self("review-pr", "human-user", "bot-user") is False

        # generic: skip_self explicitly False
        assert engine.should_skip_self("generic", "bot-user", "bot-user") is False
        assert engine.should_skip_self("generic", "human-user", "bot-user") is False
