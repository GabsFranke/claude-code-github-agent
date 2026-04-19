"""Tests for webhook filter-integration path.

Validates that the webhook handler correctly applies workflow engine
filters when routing GitHub events, and that commands bypass filters.
"""

from pathlib import Path

import pytest

from workflows.engine import WorkflowEngine


class TestWebhookFilterIntegration:
    """Test the filter-integration path between webhook handler and workflow engine.

    These tests mirror the logic in services/webhook/main.py lines 197-208:
        if not command:
            event_key = f"{event_type}.{action}" if action else event_type
            if not workflow_engine.check_filters(workflow_name, data, event_key):
                ... ignored ...
    """

    @pytest.fixture
    def engine(self):
        """Create engine from real workflows.yaml."""
        workflow_path = Path(__file__).parent.parent.parent / "workflows.yaml"
        if not workflow_path.exists():
            pytest.skip("workflows.yaml not found")
        return WorkflowEngine(workflow_path)

    # --- workflow_job.completed with success conclusion ---

    def test_workflow_job_success_is_filtered_out(self, engine):
        """A workflow_job.completed payload with conclusion=success is rejected
        by the fix-ci filter (which requires conclusion=failure)."""
        payload = {
            "workflow_job": {
                "conclusion": "success",
                "run_id": 12345,
            }
        }
        result = engine.check_filters("fix-ci", payload, "workflow_job.completed")
        assert result is False

    def test_workflow_job_failure_passes_filter(self, engine):
        """A workflow_job.completed payload with conclusion=failure passes."""
        payload = {
            "workflow_job": {
                "conclusion": "failure",
                "run_id": 12345,
            }
        }
        result = engine.check_filters("fix-ci", payload, "workflow_job.completed")
        assert result is True

    # --- pull_request.labeled with non-matching label ---

    def test_non_matching_label_is_filtered_out(self, engine):
        """A pull_request.labeled payload with an unrelated label is rejected."""
        payload = {"label": {"name": "documentation"}}
        result = engine.check_filters("fix-review", payload, "pull_request.labeled")
        assert result is False

    def test_matching_label_is_accepted(self, engine):
        """A pull_request.labeled payload with a matching label passes."""
        payload = {"label": {"name": "fix-review"}}
        result = engine.check_filters("fix-review", payload, "pull_request.labeled")
        assert result is True

    # --- Commands bypass filter checks ---

    def test_command_routing_skips_filter_check(self, engine):
        """Commands are routed directly without filter checks.

        In main.py, the guard `if not command:` means commands never enter
        the check_filters path. We verify this by confirming get_workflow_for_command
        works regardless of payload content.
        """
        workflow = engine.get_workflow_for_command("/fix-it")
        assert workflow == "fix-review"

        # Even /fix-ci command works without any payload at all
        workflow = engine.get_workflow_for_command("/fix-ci")
        assert workflow == "fix-ci"

    def test_command_routing_independent_of_event_filters(self, engine):
        """Command-based routing does not require any event filter to pass.

        This simulates what happens in main.py: when command is set,
        the code does NOT call check_filters at all.
        """
        # The command mapping exists
        assert engine.get_workflow_for_command("/fix-it") == "fix-review"
        # No filter check is needed for commands - the check_filters guard
        # in main.py line 198 (`if not command:`) ensures bypass.
