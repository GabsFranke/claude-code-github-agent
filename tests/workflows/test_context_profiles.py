"""Tests for workflow context profiles in workflows.yaml."""

import pytest

from shared.thread_history import ThreadHistoryConfig
from workflows.engine import ContextProfile, WorkflowEngine


class TestContextProfile:
    def test_default_values(self):
        profile = ContextProfile()
        assert isinstance(profile.thread_history, ThreadHistoryConfig)
        assert profile.thread_history.enabled is True
        assert profile.thread_history.max_comments == 100


class TestWorkflowContextProfiles:
    @pytest.fixture
    def engine(self, repo_workflow_config) -> WorkflowEngine:
        return WorkflowEngine(repo_workflow_config)

    def test_triage_issue_has_custom_thread_history(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("triage-issue")
        assert profile["thread_history"]["max_comments"] == 50

    def test_unknown_workflow_returns_empty(self, engine: WorkflowEngine):
        profile = engine.get_context_profile("nonexistent")
        assert profile == {}

    def test_profiles_accessible_from_workflow_config(self, engine: WorkflowEngine):
        """Context profiles should be accessible from workflow configs."""
        for _name, config in engine.workflows.items():
            assert config.context is not None
            assert isinstance(config.context.thread_history, ThreadHistoryConfig)
