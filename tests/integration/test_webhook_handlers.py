"""Deployment tests for the webhook service.

Unlike the rest of the suite these talk to a *running container*, so they
prove something no in-process test can: that the image builds, boots, and
serves. Everything about request handling itself is covered in-process by
``tests/webhook/test_delivery_dedup.py``.

They are gated on ``RUN_DEPLOYMENT_TESTS=1`` rather than skipping on
``ConnectionError``. That distinction matters: catching the connection error
made "the service failed to start" look identical to "no service configured",
so a broken image was indistinguishable from a green run. With the gate, CI
sets the flag and a service that will not come up is a *failure*.
"""

import hashlib
import hmac
import os
import uuid

import pytest
import requests

RUN_DEPLOYMENT_TESTS = os.getenv("RUN_DEPLOYMENT_TESTS") == "1"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "http://localhost:10000")

# Read from WEBHOOK_SECRET, not GITHUB_WEBHOOK_SECRET: conftest's autouse
# clean_env fixture resets GITHUB_WEBHOOK_SECRET to a test default for every
# test, which would sign these requests with the wrong key and get a 401.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

requires_running_service = pytest.mark.skipif(
    not RUN_DEPLOYMENT_TESTS,
    reason=(
        "Deployment tests need a running webhook service. "
        "Start the stack and set RUN_DEPLOYMENT_TESTS=1 to run them."
    ),
)


@pytest.mark.integration
@requires_running_service
class TestWebhookDeployment:
    """The built image boots and serves over HTTP."""

    def test_health_endpoint_reports_healthy(self):
        """A booted service answers /health with its status and queue type."""
        response = requests.get(f"{WEBHOOK_URL}/health", timeout=10)

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "healthy"
        assert body["service"] == "webhook"
        assert "queue_type" in body

    def test_root_endpoint_serves(self):
        response = requests.get(f"{WEBHOOK_URL}/", timeout=10)

        assert response.status_code == 200
        assert "status" in response.json()

    def test_unsigned_request_is_rejected_by_the_running_service(
        self, sample_github_webhook_payload
    ):
        """Signature verification is live in the deployed image, not just in tests."""
        response = requests.post(
            f"{WEBHOOK_URL}/webhook",
            json=sample_github_webhook_payload,
            headers={"X-GitHub-Event": "pull_request"},
            timeout=10,
        )

        assert response.status_code == 401

    def _signed_push(self, delivery_id: str):
        """POST a correctly signed push delivery and return the response."""
        body = (
            b'{"ref": "refs/heads/main", '
            b'"repository": {"full_name": "owner/repo"}, '
            b'"installation": {"id": 1}}'
        )
        signature = (
            "sha256="
            + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        )
        return requests.post(
            f"{WEBHOOK_URL}/webhook",
            data=body,
            headers={
                "X-GitHub-Event": "push",
                "X-GitHub-Delivery": delivery_id,
                "X-Hub-Signature-256": signature,
                "Content-Type": "application/json",
            },
            timeout=10,
        )

    @pytest.fixture
    def delivery_id(self) -> str:
        """A delivery id unique to this run.

        The service remembers delivery ids for 24 h, so a fixed id would be
        accepted on the first run and reported as a duplicate on every run
        after that against the same Redis. Fresh ids keep these tests
        re-runnable and order-agnostic.
        """
        return f"deployment-test-{uuid.uuid4()}"

    def test_signed_request_is_accepted_by_the_running_service(self, delivery_id):
        """End-to-end: a correctly signed delivery reaches the workflow engine.

        Uses a push event, the shortest path that exercises signature
        verification, dedup and a queue publish inside the container.
        """
        response = self._signed_push(delivery_id)

        assert response.status_code == 200
        assert response.json()["status"] == "accepted"

    def test_replayed_delivery_is_deduplicated_by_the_running_service(
        self, delivery_id
    ):
        """Dedup works against the real Redis the container is wired to."""
        first = self._signed_push(delivery_id)
        replay = self._signed_push(delivery_id)

        assert first.json()["status"] == "accepted"
        assert replay.json()["status"] == "duplicate"


@pytest.mark.integration
class TestWebhookValidation:
    """Signature helper behaviour. Pure logic — no service required."""

    def test_signature_validation_logic(self):
        from services.webhook.validators.signature_validator import verify_signature

        secret = "test_secret"
        payload = b'{"test": "data"}'

        signature = (
            "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        )

        assert verify_signature(payload, signature, secret) is True
        assert verify_signature(payload, "sha256=invalid", secret) is False
        assert verify_signature(payload, None, secret) is False


SESSION_SERVICE_URL = os.getenv("SESSION_SERVICE_URL", "http://localhost:10001")


@pytest.mark.integration
@requires_running_service
class TestSessionServiceDeployment:
    """The session service boots as a package inside its container.

    ``services/session_service/main.py`` imports its siblings relatively, so
    it only works when loaded as ``services.session_service.main``. Loading it
    as a flat ``main`` module raises "attempted relative import with no known
    parent package" at startup. Unit tests import it as a package and so never
    see that; only booting the image does.
    """

    def test_health_endpoint_reports_redis_connected(self):
        response = requests.get(f"{SESSION_SERVICE_URL}/health", timeout=10)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_session_listing_route_is_served(self):
        """A real route, not just /health — proves the app object loaded."""
        response = requests.get(
            f"{SESSION_SERVICE_URL}/api/sessions",
            params={"repo": "owner/repo"},
            timeout=10,
        )

        assert response.status_code == 200

    def test_react_client_is_served_from_the_image(self):
        """The image must build the client; a clean clone has no dist/.

        Before the multi-stage Dockerfile, / answered with JSON saying the
        front end was not built, and only a developer machine with a stale
        local dist/ appeared to work.
        """
        response = requests.get(f"{SESSION_SERVICE_URL}/", timeout=10)

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="root"' in response.text
        assert (
            "/assets/" in response.text
        ), "index.html should reference the built bundle"

    def test_spa_deep_link_falls_through_to_index(self):
        response = requests.get(
            f"{SESSION_SERVICE_URL}/session/owner/repo/issues/1/generic", timeout=10
        )

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
