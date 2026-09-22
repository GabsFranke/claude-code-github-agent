"""Shared harness for tests that drive the real /webhook FastAPI handler.

Signing, workflow config, module import and queue stubbing are identical for
every end-to-end webhook test, so they live here rather than in one test
module that others would have to copy.
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
