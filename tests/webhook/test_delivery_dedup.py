"""End-to-end tests for delivery deduplication on the /webhook endpoint.

These drive the real FastAPI handler so the wiring is covered, not just
``WebhookDeduplicator`` in isolation: the claim has to happen after
signature verification, before anything is queued, and it has to be given
back when handling fails so GitHub's retry still gets through.
"""

import hashlib
import hmac
import json
import shutil
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRET = "test_webhook_secret"


@pytest.fixture(scope="module")
def workflow_config():
    """Ensure a workflows.yaml exists so the webhook module can import.

    workflows.yaml is gitignored, so it is absent on a clean clone and in
    CI. Seed it from the tracked example and remove it again afterwards,
    leaving a developer's own file alone.
    """
    config = PROJECT_ROOT / "workflows.yaml"
    if config.exists():
        yield config
        return
    shutil.copy(PROJECT_ROOT / "workflows.example.yaml", config)
    try:
        yield config
    finally:
        config.unlink(missing_ok=True)


@pytest.fixture(scope="module")
def webhook_main(workflow_config):
    """Import services/webhook/main.py, which uses flat sibling imports."""
    webhook_dir = str(PROJECT_ROOT / "services" / "webhook")
    added = webhook_dir not in sys.path
    if added:
        sys.path.insert(0, webhook_dir)
    try:
        import main

        yield main
    finally:
        if added:
            sys.path.remove(webhook_dir)


@pytest.fixture
def client(webhook_main, monkeypatch):
    """Test client with queues stubbed out so nothing is really published."""
    for queue_name in ("queue", "sync_queue", "cleanup_queue"):
        monkeypatch.setattr(
            getattr(webhook_main, queue_name), "publish", AsyncMock(), raising=False
        )
    return TestClient(webhook_main.app)


@pytest.fixture
def dedup(webhook_main, monkeypatch):
    """Replace the module-level deduplicator with an in-memory one."""

    class InMemoryDeduplicator:
        def __init__(self):
            self.seen: set[str] = set()

        async def claim(self, delivery_id: str) -> bool:
            if not delivery_id:
                return True
            if delivery_id in self.seen:
                return False
            self.seen.add(delivery_id)
            return True

        async def release(self, delivery_id: str) -> None:
            self.seen.discard(delivery_id)

    stub = InMemoryDeduplicator()
    monkeypatch.setattr(webhook_main, "deduplicator", stub)
    return stub


def post(client, payload: dict, event: str, delivery_id: str | None):
    """POST a correctly signed webhook delivery."""
    body = json.dumps(payload).encode()
    headers = {
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    return client.post("/webhook", content=body, headers=headers)


@pytest.fixture
def push_payload():
    """A push event — the shortest path that reaches a queue publish."""
    return {
        "ref": "refs/heads/main",
        "repository": {"full_name": "owner/repo"},
        "installation": {"id": 12345},
    }


class TestDeliveryDeduplication:
    """Replayed deliveries must not be queued twice."""

    def test_first_delivery_is_accepted(self, client, dedup, push_payload):
        response = post(client, push_payload, "push", "delivery-1")
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def test_replayed_delivery_is_reported_as_duplicate(
        self, client, dedup, push_payload
    ):
        post(client, push_payload, "push", "delivery-1")
        response = post(client, push_payload, "push", "delivery-1")
        assert response.status_code == 200
        assert response.json()["status"] == "duplicate"

    def test_replay_does_not_publish_a_second_job(
        self, client, dedup, webhook_main, push_payload
    ):
        post(client, push_payload, "push", "delivery-1")
        post(client, push_payload, "push", "delivery-1")
        assert webhook_main.sync_queue.publish.await_count == 1

    def test_distinct_deliveries_are_both_processed(
        self, client, dedup, webhook_main, push_payload
    ):
        post(client, push_payload, "push", "delivery-1")
        post(client, push_payload, "push", "delivery-2")
        assert webhook_main.sync_queue.publish.await_count == 2

    def test_delivery_without_id_is_processed(
        self, client, dedup, webhook_main, push_payload
    ):
        """Deliveries lacking the header (manual curl) are never dropped."""
        post(client, push_payload, "push", None)
        post(client, push_payload, "push", None)
        assert webhook_main.sync_queue.publish.await_count == 2

    def test_duplicate_returns_200_so_github_stops_retrying(
        self, client, dedup, push_payload
    ):
        post(client, push_payload, "push", "delivery-1")
        assert post(client, push_payload, "push", "delivery-1").status_code == 200


class TestClaimOrdering:
    """The claim sits between signature verification and any queueing."""

    def test_bad_signature_does_not_consume_the_delivery_id(
        self, client, dedup, push_payload
    ):
        """Otherwise a forged request could suppress the genuine delivery."""
        body = json.dumps(push_payload).encode()
        forged = client.post(
            "/webhook",
            content=body,
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": "delivery-1",
                "X-Hub-Signature-256": "sha256=wrong",
                "Content-Type": "application/json",
            },
        )
        assert forged.status_code == 401
        assert "delivery-1" not in dedup.seen

        genuine = post(client, push_payload, "push", "delivery-1")
        assert genuine.json()["status"] == "accepted"


class TestRetryAfterFailure:
    """A failed delivery must not poison its own retry."""

    def test_claim_is_released_when_handling_raises(
        self, client, dedup, webhook_main, push_payload, monkeypatch
    ):
        monkeypatch.setattr(
            webhook_main.sync_queue,
            "publish",
            AsyncMock(side_effect=RuntimeError("redis exploded")),
        )
        assert post(client, push_payload, "push", "delivery-1").status_code == 500
        assert "delivery-1" not in dedup.seen

    def test_retry_after_failure_is_processed(
        self, client, dedup, webhook_main, push_payload, monkeypatch
    ):
        failing = AsyncMock(side_effect=RuntimeError("redis exploded"))
        monkeypatch.setattr(webhook_main.sync_queue, "publish", failing)
        assert post(client, push_payload, "push", "delivery-1").status_code == 500

        monkeypatch.setattr(webhook_main.sync_queue, "publish", AsyncMock())
        retry = post(client, push_payload, "push", "delivery-1")
        assert retry.json()["status"] == "accepted"


class TestSignatureIsMandatory:
    """An unset webhook secret must never mean "trust everyone"."""

    def test_unsigned_request_is_rejected_when_secret_is_configured(
        self, client, dedup, push_payload
    ):
        response = client.post(
            "/webhook",
            content=json.dumps(push_payload).encode(),
            headers={"X-GitHub-Event": "push", "Content-Type": "application/json"},
        )
        assert response.status_code == 401

    def test_missing_secret_rejects_instead_of_accepting(
        self, client, dedup, webhook_main, push_payload, monkeypatch
    ):
        """Fail closed: no secret and no explicit opt-in => refuse the request."""
        monkeypatch.setattr(
            webhook_main.config.github, "github_webhook_secret", "", raising=False
        )
        monkeypatch.setattr(
            webhook_main.config, "allow_unsigned_webhooks", False, raising=False
        )
        response = client.post(
            "/webhook",
            content=json.dumps(push_payload).encode(),
            headers={"X-GitHub-Event": "push", "Content-Type": "application/json"},
        )
        assert response.status_code == 500
        assert webhook_main.sync_queue.publish.await_count == 0

    def test_unsigned_allowed_only_with_explicit_opt_in(
        self, client, dedup, webhook_main, push_payload, monkeypatch
    ):
        monkeypatch.setattr(
            webhook_main.config.github, "github_webhook_secret", "", raising=False
        )
        monkeypatch.setattr(
            webhook_main.config, "allow_unsigned_webhooks", True, raising=False
        )
        response = client.post(
            "/webhook",
            content=json.dumps(push_payload).encode(),
            headers={"X-GitHub-Event": "push", "Content-Type": "application/json"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "accepted"
