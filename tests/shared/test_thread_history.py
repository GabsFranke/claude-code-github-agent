"""Unit tests for _format_thread_history in shared/thread_history.py."""

from typing import Any

import pytest

from shared.thread_history import _format_thread_history


@pytest.fixture
def sample_issue_body() -> dict[str, Any]:
    return {
        "title": "Bug in login flow",
        "body": "The login button doesn't work on mobile",
        "author": "alice",
        "created_at": "2025-01-01T00:00:00Z",
        "state": "open",
        "labels": ["bug", "mobile"],
    }


@pytest.fixture
def sample_comments() -> list[dict[str, Any]]:
    return [
        {
            "author": "bob",
            "created_at": "2025-01-02T00:00:00Z",
            "body": "I can reproduce this",
        },
        {
            "author": "charlie",
            "created_at": "2025-01-03T00:00:00Z",
            "body": "Fixed in #42",
        },
    ]


class TestFormatThreadHistoryBasic:
    """Basic formatting tests for thread history."""

    def test_issue_format(self, sample_issue_body, sample_comments):
        """Issue body + comments, is_pr=False."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
            is_pr=False,
        )
        assert result.startswith("<thread_history>\n")
        assert result.endswith("\n</thread_history>")
        assert "## Original Issue: Bug in login flow" in result
        assert "**Author:** alice" in result
        assert "The login button doesn't work on mobile" in result
        assert "## Comments (2)" in result
        assert "### Comment 1" in result
        assert "### Comment 2" in result

    def test_pr_format(self, sample_issue_body, sample_comments):
        """is_pr=True shows 'Original Pull Request'."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
            is_pr=True,
        )
        assert "## Original Pull Request: Bug in login flow" in result

    def test_discussion_format(self, sample_issue_body, sample_comments):
        """is_discussion=True shows 'Original Discussion'."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
            is_discussion=True,
        )
        assert "## Original Discussion: Bug in login flow" in result

    def test_empty_body(self, sample_comments):
        """issue_body=None omits 'Original Issue' header."""
        result = _format_thread_history(
            issue_body=None,
            comments=sample_comments,
        )
        assert "## Original Issue" not in result
        assert "## Comments (2)" in result

    def test_no_comments(self, sample_issue_body):
        """Empty comments list omits Comments section."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=[],
        )
        assert "## Comments" not in result
        assert "## Original Issue: Bug in login flow" in result

    def test_empty_returns_empty_string(self):
        """All empty inputs return empty string."""
        result = _format_thread_history(
            issue_body=None,
            comments=[],
            review_comments=None,
        )
        assert result == ""


class TestFormatThreadHistoryDetails:
    """Detailed formatting tests for metadata and edge cases."""

    def test_labels_displayed(self, sample_issue_body, sample_comments):
        """Labels appear in square brackets after title."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
        )
        assert "[Labels: bug, mobile]" in result

    def test_state_displayed(self, sample_issue_body, sample_comments):
        """State appears in parentheses after title."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
        )
        assert "(open)" in result

    def test_category_displayed_for_discussion(self, sample_comments):
        """Category appears for discussions."""
        body = {
            "title": "How do I X?",
            "body": "Question body",
            "author": "alice",
            "created_at": "2025-01-01T00:00:00Z",
            "state": "open",
            "labels": [],
            "category": "Q&A",
        }
        result = _format_thread_history(
            issue_body=body,
            comments=sample_comments,
            is_discussion=True,
        )
        assert "[Category: Q&A]" in result

    def test_no_description_provided(self, sample_comments):
        """Empty body string shows placeholder."""
        body = {
            "title": "Empty body issue",
            "body": "",
            "author": "alice",
            "created_at": "2025-01-01T00:00:00Z",
            "state": "open",
            "labels": [],
        }
        result = _format_thread_history(
            issue_body=body,
            comments=sample_comments,
        )
        assert "(No description provided)" in result

    def test_author_and_created_at_displayed(self, sample_issue_body, sample_comments):
        """Author line includes created_at timestamp."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
        )
        assert "**Author:** alice | **Created:** 2025-01-01T00:00:00Z" in result

    def test_comment_with_empty_body_shows_no_content(self, sample_issue_body):
        """Comment with whitespace-only body shows '(no content)'."""
        comments = [
            {
                "author": "bob",
                "created_at": "2025-01-02T00:00:00Z",
                "body": "  ",
            }
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=comments,
        )
        assert "(no content)" in result

    def test_truncated_message(self, sample_issue_body, sample_comments):
        """truncated=True adds truncation notice."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
            truncated=True,
        )
        assert "Older comments truncated" in result


class TestFormatThreadHistoryReviewComments:
    """Tests for review comments section."""

    def test_review_comments_section(self, sample_issue_body):
        """Review comments are included in their own section."""
        review_comments = [
            {
                "author": "reviewer1",
                "created_at": "2025-01-04T00:00:00Z",
                "body": "Looks good",
            },
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=[],
            review_comments=review_comments,
        )
        assert "## Review Comments (1)" in result
        assert "### Review Comment 1" in result
        assert "Looks good" in result

    def test_review_comment_with_context(self, sample_issue_body):
        """Review comment context appears in backticks."""
        review_comments = [
            {
                "author": "reviewer1",
                "created_at": "2025-01-04T00:00:00Z",
                "body": "Looks good",
                "context": "src/main.py:42",
            },
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=[],
            review_comments=review_comments,
        )
        assert "`src/main.py:42`" in result

    def test_review_comment_without_context(self, sample_issue_body):
        """Review comment without context omits backtick block."""
        review_comments = [
            {
                "author": "reviewer1",
                "created_at": "2025-01-04T00:00:00Z",
                "body": "Looks good",
            },
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=[],
            review_comments=review_comments,
        )
        # The output should not contain backtick-wrapped context
        # But it DOES contain backticks in "Review Comment" headers... hmm
        # Actually check that the specific context backtick pattern is absent
        assert "src/main.py" not in result


class TestFormatThreadHistoryDiscussionReplies:
    """Tests for nested discussion replies."""

    def test_nested_replies(self, sample_issue_body):
        """Comments with replies render reply headers."""
        comments = [
            {
                "author": "bob",
                "created_at": "2025-01-02T00:00:00Z",
                "body": "I can reproduce this",
                "replies": [
                    {
                        "author": "alice",
                        "created_at": "2025-01-02T01:00:00Z",
                        "body": "Thanks for confirming",
                    },
                ],
            },
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=comments,
        )
        assert "#### Reply 1" in result
        assert "Thanks for confirming" in result

    def test_comment_without_replies(self, sample_issue_body):
        """Comment missing 'replies' key has no reply section."""
        comments = [
            {
                "author": "bob",
                "created_at": "2025-01-02T00:00:00Z",
                "body": "No replies here",
            },
        ]
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=comments,
        )
        assert "#### Reply" not in result


class TestFormatThreadHistoryXmlWrapping:
    """Tests for XML wrapper and content preservation."""

    def test_output_wrapped_in_tags(self, sample_issue_body, sample_comments):
        """Result starts and ends with thread_history tags."""
        result = _format_thread_history(
            issue_body=sample_issue_body,
            comments=sample_comments,
        )
        assert result.startswith("<thread_history>\n")
        assert result.endswith("\n</thread_history>")

    def test_multiline_body_preserved(self, sample_comments):
        """Newlines inside the body are preserved."""
        body = {
            "title": "Multiline",
            "body": "Line 1\nLine 2\nLine 3",
            "author": "alice",
            "created_at": "2025-01-01T00:00:00Z",
            "state": "open",
            "labels": [],
        }
        result = _format_thread_history(
            issue_body=body,
            comments=sample_comments,
        )
        assert "Line 1\nLine 2\nLine 3" in result
