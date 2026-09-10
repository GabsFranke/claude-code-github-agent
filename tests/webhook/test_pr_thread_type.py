"""The webhook must classify PR comments as PR threads, not issue threads.

GitHub delivers a comment on a pull request as an ``issue_comment`` event;
the only marker is ``issue.pull_request``. The handler has that payload, the
downstream worker does not — it sees only ``event_data``. If the flag is not
carried across, ``resolve_thread_type`` falls through to ``"issue"`` and the
session is stored under a key no ``/pull/...`` session URL can find.
"""

import pytest

from shared.session_store import resolve_thread_type

from .conftest import post


@pytest.fixture
def pr_comment_payload():
    """A ``/review`` comment on pull request #39."""
    return {
        "action": "created",
        "issue": {
            "number": 39,
            "state": "open",
            "pull_request": {"url": "https://api.github.com/repos/owner/repo/pulls/39"},
        },
        "comment": {"body": "/review please", "user": {"login": "octocat"}},
        "repository": {"full_name": "owner/repo"},
        "installation": {"id": 12345},
        "sender": {"login": "octocat"},
    }


@pytest.fixture
def issue_comment_payload(pr_comment_payload):
    """The same comment on a plain issue — no ``pull_request`` marker."""
    payload = dict(pr_comment_payload)
    payload["issue"] = {"number": 39, "state": "open"}
    return payload


def _queued_job(webhook_main):
    """The first job handed to the agent queue.

    A comment can fan out to several workflows depending on the local
    workflows.yaml; every job carries the same event_data, so the first
    one is enough.
    """
    assert webhook_main.queue.publish.await_count >= 1
    return webhook_main.queue.publish.await_args_list[0].args[0]


class TestPullRequestCommentClassification:
    def test_pr_comment_marks_event_data_as_pr(
        self, client, dedup, webhook_main, pr_comment_payload
    ):
        response = post(client, pr_comment_payload, "issue_comment", "delivery-pr")

        assert response.json()["status"] == "accepted"
        assert _queued_job(webhook_main)["event_data"]["is_pr"] is True

    def test_worker_resolves_pr_comment_to_pr_thread_type(
        self, client, dedup, webhook_main, pr_comment_payload
    ):
        """The flag is only useful if resolve_thread_type acts on it."""
        post(client, pr_comment_payload, "issue_comment", "delivery-pr")

        event_data = _queued_job(webhook_main)["event_data"]
        assert resolve_thread_type(event_data) == "pr"

    def test_pr_comment_still_targets_the_pr_head_ref(
        self, client, dedup, webhook_main, pr_comment_payload
    ):
        post(client, pr_comment_payload, "issue_comment", "delivery-pr")

        assert _queued_job(webhook_main)["ref"] == "refs/pull/39/head"

    def test_plain_issue_comment_is_not_marked_as_pr(
        self, client, dedup, webhook_main, issue_comment_payload
    ):
        post(client, issue_comment_payload, "issue_comment", "delivery-issue")

        event_data = _queued_job(webhook_main)["event_data"]
        assert event_data["is_pr"] is False
        assert resolve_thread_type(event_data) == "issue"
