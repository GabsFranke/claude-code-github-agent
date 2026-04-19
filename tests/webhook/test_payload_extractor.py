"""Tests for the payload extraction registry."""

import pytest

from services.webhook.payload_extractor import (
    EventExtractionConfig,
    ExtractedFields,
    ExtractionRule,
    PayloadExtractor,
)
from shared.utils import resolve_path

# ---------------------------------------------------------------------------
# resolve_path tests
# ---------------------------------------------------------------------------


class TestResolvePath:
    """Tests for the resolve_path utility."""

    def test_simple_key(self):
        """Resolve a top-level key."""
        assert resolve_path({"action": "opened"}, "action") == "opened"

    def test_nested_path(self):
        """Resolve a dot-separated nested path."""
        data = {"pull_request": {"user": {"login": "alice"}}}
        assert resolve_path(data, "pull_request.user.login") == "alice"

    def test_missing_key_returns_none(self):
        """Return None for missing keys."""
        assert resolve_path({"a": 1}, "b") is None

    def test_missing_nested_key_returns_none(self):
        """Return None when an intermediate key is missing."""
        data = {"pull_request": {"number": 42}}
        assert resolve_path(data, "pull_request.user.login") is None

    def test_none_intermediate_returns_none(self):
        """Return None when an intermediate value is None."""
        data = {"pull_request": None}
        assert resolve_path(data, "pull_request.number") is None

    def test_deeply_nested(self):
        """Resolve a deeply nested path."""
        data = {"a": {"b": {"c": {"d": "deep"}}}}
        assert resolve_path(data, "a.b.c.d") == "deep"

    def test_list_intermediate_returns_none(self):
        """Return None when an intermediate value is a list (non-dict).

        This documents the expected behavior for payloads where a path
        segment hits a non-dict intermediate like a list.
        """
        data = {"label": [{"name": "bug"}]}
        assert resolve_path(data, "label.name") is None

    def test_int_intermediate_returns_none(self):
        """Return None when an intermediate value is an int (non-dict)."""
        data = {"count": 5}
        assert resolve_path(data, "count.something") is None

    def test_string_intermediate_returns_none(self):
        """Return None when an intermediate value is a string (non-dict)."""
        data = {"label": "bug"}
        assert resolve_path(data, "label.name") is None


# ---------------------------------------------------------------------------
# ExtractionRule / EventExtractionConfig model tests
# ---------------------------------------------------------------------------


class TestExtractionModels:
    """Tests for Pydantic extraction models."""

    def test_extraction_rule_defaults(self):
        """ExtractionRule has sensible defaults."""
        rule = ExtractionRule(path="sender.login")
        assert rule.path == "sender.login"
        assert rule.required is False
        assert rule.default is None

    def test_extraction_rule_custom(self):
        """ExtractionRule accepts custom values."""
        rule = ExtractionRule(path="issue.number", required=True, default=0)
        assert rule.required is True
        assert rule.default == 0

    def test_event_config_defaults(self):
        """EventExtractionConfig has sensible defaults."""
        config = EventExtractionConfig()
        assert config.issue_number is None
        assert config.ref is None
        assert config.user == ExtractionRule(path="sender.login")
        assert config.extra == {}

    def test_extracted_fields_defaults(self):
        """ExtractedFields has correct defaults."""
        fields = ExtractedFields()
        assert fields.issue_number is None
        assert fields.ref == "main"
        assert fields.user == "unknown"
        assert fields.extra == {}


# ---------------------------------------------------------------------------
# PayloadExtractor core tests
# ---------------------------------------------------------------------------


class TestPayloadExtractorLookup:
    """Tests for config lookup with action-qualified fallback."""

    def test_finds_exact_event_type(self):
        """Find config by event type without action."""
        extractor = PayloadExtractor()
        config = extractor._find_config("issues", None)
        assert config is not None
        assert config.issue_number is not None

    def test_finds_action_qualified(self):
        """Action-qualified key takes priority over bare event type."""
        custom_rules = {
            "workflow_job": EventExtractionConfig(
                user=ExtractionRule(path="sender.login"),
            ),
            "workflow_job.completed": EventExtractionConfig(
                user=ExtractionRule(path="sender.login"),
                extra={
                    "conclusion": ExtractionRule(
                        path="workflow_job.conclusion", required=True
                    ),
                },
            ),
        }
        extractor = PayloadExtractor(rules=custom_rules)

        # Action-qualified match
        config = extractor._find_config("workflow_job", "completed")
        assert config is not None
        assert "conclusion" in config.extra

        # Bare fallback
        config = extractor._find_config("workflow_job", "queued")
        assert config is not None
        assert "conclusion" not in config.extra

    def test_returns_none_for_unknown_event(self):
        """Return None when no config exists."""
        extractor = PayloadExtractor()
        assert extractor._find_config("nonexistent_event", None) is None


class TestPayloadExtractorExtract:
    """Tests for the extract method."""

    def setup_method(self):
        self.extractor = PayloadExtractor()

    # --- Pull request events ---

    def test_pull_request_event(self):
        """Extract fields from a pull_request.opened payload."""
        data = {
            "action": "opened",
            "pull_request": {
                "number": 42,
                "user": {"login": "alice"},
                "head": {"ref": "feature-branch"},
            },
            "sender": {"login": "alice"},
        }
        result = self.extractor.extract("pull_request", "opened", data)
        assert result.issue_number == 42
        assert result.ref == "refs/pull/42/head"
        assert result.user == "alice"

    def test_pull_request_missing_required_number(self):
        """Raise ValueError when required issue_number is missing."""
        data = {"pull_request": {"user": {"login": "alice"}}}
        with pytest.raises(ValueError, match="Required field"):
            self.extractor.extract("pull_request", "opened", data)

    # --- Issue events ---

    def test_issues_event(self):
        """Extract fields from an issues.opened payload."""
        data = {
            "action": "opened",
            "issue": {"number": 99, "user": {"login": "bob"}},
            "sender": {"login": "bob"},
        }
        result = self.extractor.extract("issues", "opened", data)
        assert result.issue_number == 99
        assert result.ref == "main"
        assert result.user == "bob"

    # --- Issue comment events ---

    def test_issue_comment_event(self):
        """Extract fields from an issue_comment.created payload."""
        data = {
            "action": "created",
            "issue": {"number": 55, "user": {"login": "charlie"}},
            "comment": {"user": {"login": "reviewer"}},
            "sender": {"login": "reviewer"},
        }
        result = self.extractor.extract("issue_comment", "created", data)
        assert result.issue_number == 55
        assert result.ref == "main"
        assert result.user == "reviewer"

    # --- Workflow job events ---

    def test_workflow_job_event(self):
        """Extract fields from a workflow_job.completed payload."""
        data = {
            "action": "completed",
            "workflow_job": {
                "run_id": 789,
                "workflow_name": "CI Pipeline",
                "name": "test-job",
                "conclusion": "failure",
                "head_branch": "develop",
            },
            "sender": {"login": "github-actions"},
        }
        result = self.extractor.extract("workflow_job", "completed", data)
        assert result.issue_number == 789
        assert result.ref == "refs/heads/develop"
        assert result.user == "github-actions"
        assert result.extra["run_id"] == 789
        assert result.extra["workflow_name_gh"] == "CI Pipeline"
        assert result.extra["job_name"] == "test-job"
        assert result.extra["conclusion"] == "failure"
        assert result.extra["head_branch"] == "develop"

    def test_workflow_job_missing_head_branch(self):
        """Fall back to 'main' when head_branch is missing."""
        data = {
            "action": "completed",
            "workflow_job": {
                "run_id": 100,
                "workflow_name": "CI",
                "name": "build",
                "conclusion": "failure",
            },
            "sender": {"login": "github-actions"},
        }
        result = self.extractor.extract("workflow_job", "completed", data)
        assert result.ref == "main"

    # --- Discussion events ---

    def test_discussion_event(self):
        """Extract fields from a discussion.created payload."""
        data = {
            "action": "created",
            "discussion": {"number": 10, "user": {"login": "dave"}},
            "sender": {"login": "dave"},
        }
        result = self.extractor.extract("discussion", "created", data)
        assert result.issue_number == 10
        assert result.user == "dave"

    def test_discussion_comment_event(self):
        """Extract fields from a discussion_comment.created payload."""
        data = {
            "action": "created",
            "discussion": {"number": 10},
            "comment": {"user": {"login": "eve"}},
            "sender": {"login": "eve"},
        }
        result = self.extractor.extract("discussion_comment", "created", data)
        assert result.issue_number == 10
        assert result.user == "eve"

    # --- Label events ---

    def test_label_event(self):
        """Extract fields from a label.created payload."""
        data = {
            "action": "created",
            "label": {"name": "bug", "color": "ff0000"},
            "sender": {"login": "maintainer"},
        }
        result = self.extractor.extract("label", "created", data)
        assert result.issue_number is None
        assert result.ref == "main"
        assert result.user == "maintainer"
        assert result.extra["label_name"] == "bug"
        assert result.extra["label_color"] == "ff0000"

    # --- Release events ---

    def test_release_event(self):
        """Extract fields from a release.published payload."""
        data = {
            "action": "published",
            "release": {
                "tag_name": "v1.0.0",
                "name": "Version 1.0",
                "body": "Release notes",
            },
            "sender": {"login": "releaser"},
        }
        result = self.extractor.extract("release", "published", data)
        assert result.issue_number is None
        assert result.ref == "v1.0.0"
        assert result.user == "releaser"
        assert result.extra["tag_name"] == "v1.0.0"

    # --- Star events ---

    def test_star_event(self):
        """Extract fields from a star.created payload."""
        data = {
            "action": "created",
            "sender": {"login": "fan"},
        }
        result = self.extractor.extract("star", "created", data)
        assert result.issue_number is None
        assert result.ref == "main"
        assert result.user == "fan"

    # --- Fork events ---

    def test_fork_event(self):
        """Extract fields from a fork payload."""
        data = {
            "forkee": {
                "full_name": "forker/repo",
                "owner": {"login": "forker"},
            },
            "sender": {"login": "forker"},
        }
        result = self.extractor.extract("fork", None, data)
        assert result.user == "forker"
        assert result.extra["fork_full_name"] == "forker/repo"

    # --- Push events ---

    def test_push_event(self):
        """Extract fields from a push payload."""
        data = {
            "ref": "refs/heads/main",
            "sender": {"login": "pusher"},
        }
        result = self.extractor.extract("push", None, data)
        assert result.ref == "refs/heads/main"
        assert result.user == "pusher"

    # --- Pull request review events ---

    def test_pull_request_review_event(self):
        """Extract fields from a pull_request_review.submitted payload."""
        data = {
            "action": "submitted",
            "pull_request": {
                "number": 30,
                "head": {"ref": "feature"},
            },
            "review": {"user": {"login": "reviewer"}},
            "sender": {"login": "reviewer"},
        }
        result = self.extractor.extract("pull_request_review", "submitted", data)
        assert result.issue_number == 30
        assert result.ref == "refs/pull/30/head"
        assert result.user == "reviewer"

    # --- Pull request review comment events ---

    def test_pull_request_review_comment_event(self):
        """Extract fields from a pull_request_review_comment payload."""
        data = {
            "action": "created",
            "pull_request": {
                "number": 30,
                "head": {"ref": "feature"},
            },
            "comment": {"user": {"login": "commenter"}},
            "sender": {"login": "commenter"},
        }
        result = self.extractor.extract("pull_request_review_comment", "created", data)
        assert result.issue_number == 30
        assert result.ref == "refs/pull/30/head"
        assert result.user == "commenter"

    # --- Unknown events ---

    def test_unknown_event_uses_sender_login(self):
        """Unknown events fall back to sender.login for user."""
        data = {
            "action": "something",
            "sender": {"login": "someone"},
        }
        result = self.extractor.extract("unknown_event", "something", data)
        assert result.issue_number is None
        assert result.ref == "main"
        assert result.user == "someone"

    def test_unknown_event_missing_sender(self):
        """Unknown events without sender use 'unknown' default."""
        result = self.extractor.extract("unknown_event", None, {})
        assert result.user == "unknown"


# ---------------------------------------------------------------------------
# Parity tests: verify identical output to current hardcoded logic
# ---------------------------------------------------------------------------


class TestParityWithCurrentLogic:
    """Verify the extractor produces the same results as the existing code."""

    def setup_method(self):
        self.extractor = PayloadExtractor()

    def test_parity_pr_opened(self):
        """Match current hardcoded logic for pull_request.opened."""
        data = {
            "action": "opened",
            "pull_request": {
                "number": 123,
                "title": "Test PR",
                "user": {"login": "testuser"},
                "head": {"ref": "feature-branch"},
            },
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "testuser"},
        }
        result = self.extractor.extract("pull_request", "opened", data)

        # Current code: issue_number = data["pull_request"]["number"]
        assert result.issue_number == 123
        # Current code: ref = f"refs/pull/{issue_number}/head"
        assert result.ref == "refs/pull/123/head"
        # Current code: user = data["pull_request"]["user"]["login"]
        assert result.user == "testuser"

    def test_parity_issues_opened(self):
        """Match current hardcoded logic for issues.opened."""
        data = {
            "action": "opened",
            "issue": {"number": 456, "user": {"login": "issuemaker"}},
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "issuemaker"},
        }
        result = self.extractor.extract("issues", "opened", data)

        # Current code: issue_number = data["issue"]["number"]
        assert result.issue_number == 456
        # Current code: ref = "main" (default)
        assert result.ref == "main"
        # Current code: user = data["issue"]["user"]["login"]
        assert result.user == "issuemaker"

    def test_parity_issue_comment(self):
        """Match current hardcoded logic for issue_comment field extraction."""
        data = {
            "action": "created",
            "issue": {
                "number": 789,
                "user": {"login": "author"},
                "pull_request": {"url": "http://example.com"},
            },
            "comment": {
                "body": "/review",
                "user": {"login": "commenter"},
            },
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "commenter"},
        }
        result = self.extractor.extract("issue_comment", "created", data)

        # Current code: issue_number = data["issue"]["number"]
        assert result.issue_number == 789
        # Current code: ref = "main" (PR ref computed in overlay, not here)
        assert result.ref == "main"
        # Current code: user = data["comment"]["user"]["login"]
        assert result.user == "commenter"

    def test_parity_workflow_job_completed(self):
        """Match current hardcoded logic for workflow_job.completed."""
        data = {
            "action": "completed",
            "workflow_job": {
                "run_id": 111,
                "workflow_name": "CI",
                "name": "build",
                "conclusion": "failure",
                "head_branch": "develop",
            },
            "repository": {"full_name": "owner/repo"},
            "sender": {"login": "github-actions"},
        }
        result = self.extractor.extract("workflow_job", "completed", data)

        # Current code: issue_number = run_id
        assert result.issue_number == 111
        # Current code: ref = f"refs/heads/{head_branch}"
        assert result.ref == "refs/heads/develop"
        # Current code: user = "unknown" (not extracted for workflow_job)
        # Note: extractor improves this to use sender.login, which is safe
        assert result.user == "github-actions"
        # Current code extra fields
        assert result.extra["run_id"] == 111
        assert result.extra["workflow_name_gh"] == "CI"
        assert result.extra["job_name"] == "build"
        assert result.extra["conclusion"] == "failure"
        assert result.extra["head_branch"] == "develop"


# ---------------------------------------------------------------------------
# Registry completeness tests
# ---------------------------------------------------------------------------


class TestRegistryCompleteness:
    """Verify the extraction rules registry covers important event types."""

    def test_all_existing_events_have_rules(self):
        """All 4 currently handled event types have extraction rules."""
        extractor = PayloadExtractor()
        for event_type in [
            "pull_request",
            "issues",
            "issue_comment",
            "workflow_job",
        ]:
            config = extractor._find_config(event_type, None)
            assert config is not None, f"Missing extraction rules for {event_type}"

    def test_new_event_types_have_rules(self):
        """Extended event types have extraction rules."""
        extractor = PayloadExtractor()
        for event_type in [
            "discussion",
            "discussion_comment",
            "label",
            "release",
            "star",
            "fork",
            "push",
            "deployment",
            "pull_request_review",
            "pull_request_review_comment",
            "check_run",
            "check_suite",
            "pull_request_review_thread",
            "create",
            "delete",
            "commit_comment",
            "repository_dispatch",
            "workflow_dispatch",
            "gollum",
            "merge_group",
            "status",
            "branch_protection_rule",
            "sub_issues",
        ]:
            config = extractor._find_config(event_type, None)
            assert config is not None, f"Missing extraction rules for {event_type}"


# ---------------------------------------------------------------------------
# New event type tests
# ---------------------------------------------------------------------------


class TestNewEventTypes:
    """Tests for newly added event types."""

    def setup_method(self):
        self.extractor = PayloadExtractor()

    def test_pull_request_review_thread(self):
        """Extract fields from pull_request_review_thread event."""
        data = {
            "action": "resolved",
            "pull_request": {"number": 15},
            "thread": {"node_id": "thread_abc"},
            "sender": {"login": "resolver"},
        }
        result = self.extractor.extract("pull_request_review_thread", "resolved", data)
        assert result.issue_number == 15
        assert result.user == "resolver"

    def test_create_event(self):
        """Extract fields from create event (branch/tag creation)."""
        data = {
            "ref": "feature-branch",
            "ref_type": "branch",
            "sender": {"login": "creator"},
        }
        result = self.extractor.extract("create", None, data)
        assert result.ref == "refs/heads/feature-branch"
        assert result.user == "creator"
        assert result.extra["ref_type"] == "branch"

    def test_delete_event(self):
        """Extract fields from delete event (branch/tag deletion)."""
        data = {
            "ref": "old-branch",
            "ref_type": "branch",
            "sender": {"login": "deleter"},
        }
        result = self.extractor.extract("delete", None, data)
        assert result.ref == "refs/heads/old-branch"
        assert result.user == "deleter"
        assert result.extra["ref_type"] == "branch"

    def test_commit_comment_event(self):
        """Extract fields from commit_comment event."""
        data = {
            "action": "created",
            "comment": {
                "user": {"login": "commenter"},
                "commit_id": "abc123",
            },
            "sender": {"login": "commenter"},
        }
        result = self.extractor.extract("commit_comment", "created", data)
        assert result.user == "commenter"
        assert result.extra["commit_id"] == "abc123"

    def test_repository_dispatch_event(self):
        """Extract fields from repository_dispatch event."""
        data = {
            "action": "on-demand-test",
            "branch": "develop",
            "client_payload": {"unit": False, "integration": True},
            "sender": {"login": "dispatcher"},
        }
        result = self.extractor.extract("repository_dispatch", "on-demand-test", data)
        assert result.ref == "refs/heads/develop"
        assert result.user == "dispatcher"
        assert result.extra["client_payload"]["integration"] is True

    def test_workflow_dispatch_event(self):
        """Extract fields from workflow_dispatch event."""
        data = {
            "ref": "refs/heads/main",
            "workflow": ".github/workflows/deploy.yml",
            "inputs": {"environment": "staging"},
            "sender": {"login": "dispatcher"},
        }
        result = self.extractor.extract("workflow_dispatch", None, data)
        assert result.ref == "refs/heads/main"
        assert result.user == "dispatcher"
        assert result.extra["workflow"] == ".github/workflows/deploy.yml"

    def test_gollum_event(self):
        """Extract fields from gollum (wiki) event."""
        data = {
            "pages": [{"page_name": "Home", "action": "edited"}],
            "sender": {"login": "wiki-editor"},
        }
        result = self.extractor.extract("gollum", None, data)
        assert result.user == "wiki-editor"
        assert len(result.extra["pages"]) == 1

    def test_merge_group_event(self):
        """Extract fields from merge_group event."""
        data = {
            "action": "checks_requested",
            "merge_group": {
                "head_sha": "abc123",
                "head_ref": "refs/heads/gh-readonly-queue/main/abc",
                "base_ref": "main",
            },
            "sender": {"login": "github-merge-queue"},
        }
        result = self.extractor.extract("merge_group", "checks_requested", data)
        assert result.ref == "refs/heads/gh-readonly-queue/main/abc"
        assert result.user == "github-merge-queue"
        assert result.extra["base_ref"] == "main"

    def test_status_event(self):
        """Extract fields from status event."""
        data = {
            "state": "failure",
            "sha": "abc123def456",
            "context": "ci/circleci",
            "description": "Tests failed",
            "sender": {"login": "circleci"},
        }
        result = self.extractor.extract("status", None, data)
        assert result.user == "circleci"
        assert result.extra["state"] == "failure"
        assert result.extra["sha"] == "abc123def456"
        assert result.extra["context"] == "ci/circleci"

    def test_branch_protection_rule_event(self):
        """Extract fields from branch_protection_rule event."""
        data = {
            "action": "created",
            "rule": {"name": "release/*"},
            "sender": {"login": "admin"},
        }
        result = self.extractor.extract("branch_protection_rule", "created", data)
        assert result.user == "admin"
        assert result.extra["rule_name"] == "release/*"

    def test_sub_issues_event(self):
        """Extract fields from sub_issues event."""
        data = {
            "action": "sub_issue_added",
            "sub_issue": {"number": 10},
            "parent_issue": {"number": 5},
            "sender": {"login": "organizer"},
        }
        result = self.extractor.extract("sub_issues", "sub_issue_added", data)
        assert result.issue_number == 10
        assert result.user == "organizer"
        assert result.extra["parent_issue_number"] == 5


# ---------------------------------------------------------------------------
# Bug fix verification tests
# ---------------------------------------------------------------------------


class TestBugFixes:
    """Verify corrected dot-paths for security alert events."""

    def setup_method(self):
        self.extractor = PayloadExtractor()

    def test_secret_scanning_alert_uses_alert_number(self):
        """Verify secret_scanning_alert uses alert.number path."""
        data = {
            "action": "created",
            "alert": {"number": 42},
            "sender": {"login": "github"},
        }
        result = self.extractor.extract("secret_scanning_alert", "created", data)
        assert result.extra["alert_number"] == 42

    def test_dependabot_alert_uses_alert_number(self):
        """Verify dependabot_alert uses alert.number path."""
        data = {
            "action": "created",
            "alert": {"number": 99},
            "sender": {"login": "github"},
        }
        result = self.extractor.extract("dependabot_alert", "created", data)
        assert result.extra["alert_number"] == 99

    def test_code_scanning_alert_uses_alert_number(self):
        """Verify code_scanning_alert uses alert.number path."""
        data = {
            "action": "created",
            "alert": {"number": 7},
            "sender": {"login": "github"},
        }
        result = self.extractor.extract("code_scanning_alert", "created", data)
        assert result.extra["alert_number"] == 7
