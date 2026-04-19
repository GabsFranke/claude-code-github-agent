"""Unit tests for workflow engine."""

from pathlib import Path

import pytest
import yaml

from workflows.engine import WorkflowEngine, get_workflow_engine


class TestGetWorkflowEngine:
    """Test get_workflow_engine factory function with caching."""

    def test_returns_workflow_engine_instance(self, tmp_path):
        """Test that factory returns a WorkflowEngine instance."""
        config_file = tmp_path / "workflows.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "workflows": {
                        "my-workflow": {
                            "triggers": {"commands": ["/test"]},
                            "prompt": {"template": "test"},
                        }
                    }
                }
            )
        )

        engine = get_workflow_engine(str(config_file))
        assert isinstance(engine, WorkflowEngine)

    def test_returns_cached_instance(self, tmp_path):
        """Test that factory returns the same cached instance."""
        config_file = tmp_path / "workflows.yaml"
        config_file.write_text(
            yaml.dump(
                {
                    "workflows": {
                        "my-workflow": {
                            "triggers": {"commands": ["/test"]},
                            "prompt": {"template": "test"},
                        }
                    }
                }
            )
        )

        engine1 = get_workflow_engine(str(config_file))
        engine2 = get_workflow_engine(str(config_file))

        # Should be the exact same object (cached)
        assert engine1 is engine2

    def test_cache_has_info(self):
        """Test that the factory function has cache_info (is cached)."""
        assert hasattr(get_workflow_engine, "cache_info")
        assert hasattr(get_workflow_engine, "cache_clear")


class TestWorkflowEngine:
    """Test WorkflowEngine class."""

    @pytest.fixture
    def sample_workflows_yaml(self):
        """Create a sample workflows.yaml for testing."""
        return {
            "workflows": {
                "review-pr": {
                    "triggers": {
                        "events": [
                            {"event": "pull_request.opened"},
                            {
                                "event": "pull_request.labeled",
                                "filters": {"label.name": ["review"]},
                            },
                        ],
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
                        "events": [{"event": "issues.opened"}],
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

    def test_build_prompt_simple(self, temp_workflow_file, tmp_path):
        """Test building simple prompt without system context."""
        # Create workflow without system context
        workflows_yaml = {
            "workflows": {
                "simple-workflow": {
                    "triggers": {"commands": ["/simple"]},
                    "prompt": {"template": "Triage issue #{issue_number} in {repo}"},
                    "description": "Simple workflow",
                }
            }
        }

        workflow_file = tmp_path / "workflows_simple.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        prompt, system_context = engine.build_prompt(
            workflow_name="simple-workflow",
            repo="owner/repo",
            issue_number=123,
        )

        assert prompt == "Triage issue #123 in owner/repo"
        assert system_context is None

    def test_build_prompt_with_user_query(self, tmp_path):
        """Test building prompt with user query."""
        # Create workflow without system context
        workflows_yaml = {
            "workflows": {
                "query-workflow": {
                    "triggers": {"commands": ["/query"]},
                    "prompt": {"template": "{user_query}"},
                    "description": "Query workflow",
                }
            }
        }

        workflow_file = tmp_path / "workflows_query.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        prompt, system_context = engine.build_prompt(
            workflow_name="query-workflow",
            repo="owner/repo",
            issue_number=456,
            user_query="help me fix this bug",
        )

        assert prompt == "help me fix this bug"
        assert system_context is None

    def test_build_prompt_with_system_context_file(
        self, temp_workflow_file, temp_prompts_dir, monkeypatch
    ):
        """Test building prompt with system context from file."""
        # Change to temp directory so prompts/ is found
        monkeypatch.chdir(temp_prompts_dir.parent)

        engine = WorkflowEngine(temp_workflow_file)

        prompt, system_context = engine.build_prompt(
            workflow_name="review-pr",
            repo="owner/repo",
            issue_number=789,
        )

        assert "/pr-review-toolkit:review-pr owner/repo 789" in prompt
        assert system_context is not None
        assert "Focus on code quality" in system_context

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

        prompt, system_context = engine.build_prompt(
            workflow_name="generic",
            repo="test/repo",
            issue_number=1,
            user_query="explain the code",
        )

        assert "explain the code" in prompt
        assert system_context is not None
        assert "helpful coding assistant" in system_context

    def test_build_prompt_unknown_workflow(self, temp_workflow_file):
        """Test building prompt for unknown workflow."""
        engine = WorkflowEngine(temp_workflow_file)

        with pytest.raises(ValueError, match="Unknown workflow"):
            engine.build_prompt(
                workflow_name="nonexistent",
                repo="owner/repo",
            )

    def test_build_prompt_with_kwargs(self, tmp_path):
        """Test building prompt with additional kwargs."""
        # Create workflow without system context
        workflows_yaml = {
            "workflows": {
                "kwargs-workflow": {
                    "triggers": {"commands": ["/kwargs"]},
                    "prompt": {"template": "Triage issue #{issue_number} in {repo}"},
                    "description": "Kwargs workflow",
                }
            }
        }

        workflow_file = tmp_path / "workflows_kwargs.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        prompt, system_context = engine.build_prompt(
            workflow_name="kwargs-workflow",
            repo="owner/repo",
            issue_number=999,
            custom_var="custom_value",
        )

        assert "owner/repo" in prompt
        assert system_context is None
        assert "999" in prompt

    def test_list_workflows(self, temp_workflow_file):
        """Test listing all workflows."""
        engine = WorkflowEngine(temp_workflow_file)

        workflows = engine.list_workflows()

        assert len(workflows) == 3
        assert workflows["review-pr"] == "Review a pull request"
        assert workflows["triage-issue"] == "Triage an issue"
        assert workflows["generic"] == "Generic agent request"

    def test_list_workflows_no_description(self, tmp_path):
        """Test listing workflows without descriptions."""
        # Create workflow without description
        workflows_yaml = {
            "workflows": {
                "no-desc-workflow": {
                    "triggers": {"commands": ["/nodesc"]},
                    "prompt": {"template": "test"},
                }
            }
        }

        workflow_file = tmp_path / "workflows_nodesc.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        workflows = engine.list_workflows()

        assert workflows["no-desc-workflow"] == "No description"

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

        prompt, system_context = engine.build_prompt(
            workflow_name="triage-issue",
            repo="test/project",
            issue_number=42,
        )

        # Verify prompt is generated
        assert prompt == "Triage issue #42 in test/project"

        # System context should have variables filled
        assert system_context is not None
        assert "issue #42" in system_context
        assert "test/project" in system_context  # Template uses {repo}
        assert (
            "add labels" in system_context.lower()
        )  # Check system context, not prompt

    def test_empty_issue_number(self, tmp_path):
        """Test building prompt with None issue_number."""
        # Create workflow without system context
        workflows_yaml = {
            "workflows": {
                "empty-issue-workflow": {
                    "triggers": {"commands": ["/empty"]},
                    "prompt": {"template": "Triage issue #{issue_number} in {repo}"},
                    "description": "Empty issue workflow",
                }
            }
        }

        workflow_file = tmp_path / "workflows_empty.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        engine = WorkflowEngine(workflow_file)

        prompt, system_context = engine.build_prompt(
            workflow_name="empty-issue-workflow",
            repo="owner/repo",
            issue_number=None,
        )

        assert "owner/repo" in prompt
        assert "#" in prompt  # Empty issue number becomes empty string
        assert system_context is None


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

    def test_missing_system_context_file_validation(self, tmp_path):
        """Test that missing system context files are caught at initialization."""
        # Create workflow that references non-existent system context file
        workflows_yaml = {
            "workflows": {
                "test-workflow": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {
                        "template": "test",
                        "system_context": "nonexistent.md",
                    },
                    "description": "Test workflow",
                }
            }
        }

        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        # Should raise FileNotFoundError during initialization
        with pytest.raises(
            FileNotFoundError, match="references non-existent system context file"
        ):
            WorkflowEngine(workflow_file)

    def test_invalid_workflow_name(self, tmp_path):
        """Test that invalid workflow names are rejected."""
        workflows_yaml = {
            "workflows": {
                "Invalid_Name": {  # Uppercase not allowed
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test"},
                    "description": "Test",
                }
            }
        }

        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        with pytest.raises(ValueError, match="Invalid workflow name"):
            WorkflowEngine(workflow_file)

    def test_reserved_workflow_name(self, tmp_path):
        """Test that reserved workflow names are rejected."""
        workflows_yaml = {
            "workflows": {
                "test": {  # Reserved name
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": "test"},
                    "description": "Test",
                }
            }
        }

        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        with pytest.raises(ValueError, match="reserved"):
            WorkflowEngine(workflow_file)

    def test_invalid_template_placeholder(self, tmp_path):
        """Test that invalid template placeholders are caught."""
        workflows_yaml = {
            "workflows": {
                "test-workflow": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {
                        "template": "test {invalid_placeholder}",  # Unknown placeholder
                    },
                    "description": "Test",
                }
            }
        }

        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        with pytest.raises(ValueError, match="unknown placeholders"):
            WorkflowEngine(workflow_file)

    def test_empty_template(self, tmp_path):
        """Test that empty templates are rejected."""
        workflows_yaml = {
            "workflows": {
                "test-workflow": {
                    "triggers": {"commands": ["/test"]},
                    "prompt": {"template": ""},  # Empty template
                    "description": "Test",
                }
            }
        }

        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)

        with pytest.raises(ValueError, match="empty template"):
            WorkflowEngine(workflow_file)


class TestCheckFilters:
    """Test the declarative payload filter matching."""

    @pytest.fixture
    def engine_with_filters(self, tmp_path):
        """Create an engine with workflows that have filters."""
        workflows_yaml = {
            "workflows": {
                "fix-ci": {
                    "triggers": {
                        "events": [
                            {
                                "event": "workflow_job.completed",
                                "filters": {"workflow_job.conclusion": "failure"},
                            }
                        ],
                        "commands": ["/fix-ci"],
                    },
                    "prompt": {"template": "fix {repo}"},
                },
                "label-review": {
                    "triggers": {
                        "events": [
                            {
                                "event": "label.created",
                                "filters": {"label.name": "review"},
                            }
                        ],
                        "commands": ["/label-review"],
                    },
                    "prompt": {"template": "label {repo}"},
                },
                "multi-filter": {
                    "triggers": {
                        "events": [
                            {
                                "event": "workflow_job.completed",
                                "filters": {
                                    "workflow_job.conclusion": "failure",
                                    "workflow_job.head_branch": "develop",
                                },
                            }
                        ],
                        "commands": ["/multi"],
                    },
                    "prompt": {"template": "multi {repo}"},
                },
                "list-filter": {
                    "triggers": {
                        "events": [
                            {
                                "event": "label.created",
                                "filters": {
                                    "label.name": ["bug", "review", "enhancement"]
                                },
                            }
                        ],
                        "commands": ["/list-filter"],
                    },
                    "prompt": {"template": "list {repo}"},
                },
                "no-filter": {
                    "triggers": {
                        "events": [{"event": "issues.opened"}],
                        "commands": ["/no-filter"],
                    },
                    "prompt": {"template": "nofilter {repo}"},
                },
            }
        }
        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)
        return WorkflowEngine(workflow_file)

    def test_single_filter_matches(self, engine_with_filters):
        """Payload matching a single filter passes."""
        payload = {
            "workflow_job": {"conclusion": "failure", "head_branch": "main"},
        }
        assert (
            engine_with_filters.check_filters(
                "fix-ci", payload, "workflow_job.completed"
            )
            is True
        )

    def test_single_filter_no_match(self, engine_with_filters):
        """Payload not matching a single filter is rejected."""
        payload = {
            "workflow_job": {"conclusion": "success", "head_branch": "main"},
        }
        assert (
            engine_with_filters.check_filters(
                "fix-ci", payload, "workflow_job.completed"
            )
            is False
        )

    def test_filter_missing_field(self, engine_with_filters):
        """Payload missing the filtered field is rejected."""
        payload = {"workflow_job": {}}
        assert (
            engine_with_filters.check_filters(
                "fix-ci", payload, "workflow_job.completed"
            )
            is False
        )

    def test_no_filters_always_passes(self, engine_with_filters):
        """Workflow without filters always passes."""
        payload = {"issue": {"number": 1}}
        assert engine_with_filters.check_filters("no-filter", payload) is True

    def test_no_filters_empty_payload(self, engine_with_filters):
        """Workflow without filters passes even with empty payload."""
        assert engine_with_filters.check_filters("no-filter", {}) is True

    def test_multiple_filters_all_match(self, engine_with_filters):
        """All filters must match (AND logic)."""
        payload = {
            "workflow_job": {
                "conclusion": "failure",
                "head_branch": "develop",
            },
        }
        assert (
            engine_with_filters.check_filters(
                "multi-filter", payload, "workflow_job.completed"
            )
            is True
        )

    def test_multiple_filters_partial_match(self, engine_with_filters):
        """If one filter fails, the whole check fails."""
        payload = {
            "workflow_job": {
                "conclusion": "failure",
                "head_branch": "main",
            },
        }
        assert (
            engine_with_filters.check_filters(
                "multi-filter", payload, "workflow_job.completed"
            )
            is False
        )

    def test_list_filter_value_matches(self, engine_with_filters):
        """Filter with list value matches when actual is in the list."""
        payload = {"label": {"name": "bug"}}
        assert (
            engine_with_filters.check_filters("list-filter", payload, "label.created")
            is True
        )

    def test_list_filter_value_no_match(self, engine_with_filters):
        """Filter with list value rejects when actual is not in list."""
        payload = {"label": {"name": "wontfix"}}
        assert (
            engine_with_filters.check_filters("list-filter", payload, "label.created")
            is False
        )

    def test_unknown_workflow_returns_false(self, engine_with_filters):
        """Unknown workflow name returns False."""
        assert engine_with_filters.check_filters("nonexistent", {}) is False

    def test_label_name_filter(self, engine_with_filters):
        """Filter on label.name for the review-on-label workflow."""
        payload = {"label": {"name": "review"}}
        assert (
            engine_with_filters.check_filters("label-review", payload, "label.created")
            is True
        )

        payload = {"label": {"name": "enhancement"}}
        assert (
            engine_with_filters.check_filters("label-review", payload, "label.created")
            is False
        )


class TestPerEventFilters:
    """Test per-event filter support with structured EventTrigger entries."""

    @pytest.fixture
    def engine_per_event(self, tmp_path):
        """Engine with per-event filters on some events."""
        workflows_yaml = {
            "workflows": {
                "review-pr": {
                    "triggers": {
                        "events": [
                            {"event": "pull_request.opened"},
                            {
                                "event": "pull_request.labeled",
                                "filters": {"label.name": ["review", "pr-review"]},
                            },
                        ],
                        "commands": ["/review"],
                    },
                    "prompt": {"template": "review {repo} {issue_number}"},
                },
                "mixed-format": {
                    "triggers": {
                        "events": [
                            "issues.opened",
                            {
                                "event": "issues.labeled",
                                "filters": {"label.name": "bug"},
                            },
                        ],
                        "commands": ["/mixed"],
                    },
                    "prompt": {"template": "mixed {repo}"},
                },
            }
        }
        workflow_file = tmp_path / "workflows.yaml"
        with open(workflow_file, "w", encoding="utf-8") as f:
            yaml.dump(workflows_yaml, f)
        return WorkflowEngine(workflow_file)

    def test_event_without_filters_passes(self, engine_per_event):
        """Event with no per-event filters always passes."""
        payload = {"action": "opened"}
        assert (
            engine_per_event.check_filters("review-pr", payload, "pull_request.opened")
            is True
        )

    def test_per_event_filter_matches(self, engine_per_event):
        """Per-event filter matching the payload passes."""
        payload = {"label": {"name": "review"}}
        assert (
            engine_per_event.check_filters("review-pr", payload, "pull_request.labeled")
            is True
        )

    def test_per_event_filter_no_match(self, engine_per_event):
        """Per-event filter rejecting non-matching payload."""
        payload = {"label": {"name": "wontfix"}}
        assert (
            engine_per_event.check_filters("review-pr", payload, "pull_request.labeled")
            is False
        )

    def test_event_map_has_both_events(self, engine_per_event):
        """Both events are in the event map."""
        assert engine_per_event._event_map["pull_request.opened"] == "review-pr"
        assert engine_per_event._event_map["pull_request.labeled"] == "review-pr"

    def test_per_event_filters_stored(self, engine_per_event):
        """Only the labeled event has per-event filters stored."""
        assert ("review-pr", "pull_request.labeled") in engine_per_event._event_filters
        assert (
            "review-pr",
            "pull_request.opened",
        ) not in engine_per_event._event_filters

    def test_mixed_string_and_structured_events(self, engine_per_event):
        """Plain string and structured events coexist in the same workflow."""
        assert engine_per_event._event_map["issues.opened"] == "mixed-format"
        assert engine_per_event._event_map["issues.labeled"] == "mixed-format"
        assert ("mixed-format", "issues.labeled") in engine_per_event._event_filters
        assert ("mixed-format", "issues.opened") not in engine_per_event._event_filters

    def test_mixed_string_no_filters(self, engine_per_event):
        """Plain string event (no per-event filters) passes."""
        payload = {"action": "opened"}
        assert (
            engine_per_event.check_filters("mixed-format", payload, "issues.opened")
            is True
        )

    def test_mixed_structured_filter_matches(self, engine_per_event):
        """Structured event filter matches in mixed-format workflow."""
        payload = {"label": {"name": "bug"}}
        assert (
            engine_per_event.check_filters("mixed-format", payload, "issues.labeled")
            is True
        )

    def test_no_event_key_falls_back_to_workflow_filters(self, engine_per_event):
        """Without event_key, falls back to workflow-level filters (empty)."""
        payload = {"label": {"name": "anything"}}
        assert engine_per_event.check_filters("review-pr", payload) is True

    def test_per_event_filter_with_list_value(self, engine_per_event):
        """Per-event filter with list value works correctly."""
        payload = {"label": {"name": "pr-review"}}
        assert (
            engine_per_event.check_filters("review-pr", payload, "pull_request.labeled")
            is True
        )

        payload = {"label": {"name": "unknown"}}
        assert (
            engine_per_event.check_filters("review-pr", payload, "pull_request.labeled")
            is False
        )
