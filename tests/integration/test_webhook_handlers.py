"""Integration tests for webhook handlers."""

import hashlib
import hmac

import pytest
import requests


@pytest.mark.integration
class TestWebhookEndpoint:
    """Test webhook endpoint integration with running service."""

    @pytest.fixture
    def webhook_url(self):
        """Get webhook service URL."""
        # Docker Compose maps webhook to port 10000
        return "http://localhost:10000"

    def test_webhook_service_running(self, webhook_url):
        """Test that webhook service is accessible."""
        try:
            response = requests.get(f"{webhook_url}/health", timeout=5)
            assert response.status_code == 200
        except requests.exceptions.ConnectionError:
            pytest.skip("Webhook service not running at http://localhost:10000")

    def test_health_endpoint(self, webhook_url):
        """Test health check endpoint."""
        try:
            response = requests.get(f"{webhook_url}/health", timeout=5)
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
        except requests.exceptions.ConnectionError:
            pytest.skip("Webhook service not running")

    def test_webhook_endpoint_requires_signature(
        self, webhook_url, sample_github_webhook_payload
    ):
        """Test webhook endpoint requires valid signature."""
        try:
            response = requests.post(
                f"{webhook_url}/webhook",
                json=sample_github_webhook_payload,
                headers={"X-GitHub-Event": "pull_request"},
                timeout=5,
            )
            # Should reject without signature or return 401/403
            assert response.status_code in [400, 401, 403]
        except requests.exceptions.ConnectionError:
            pytest.skip("Webhook service not running")


@pytest.mark.integration
class TestWebhookValidation:
    """Test webhook signature validation."""

    def test_signature_validation_logic(self):
        """Test signature validation function."""
        from services.webhook.validators.signature_validator import verify_signature

        secret = "test_secret"
        payload = b'{"test": "data"}'

        # Generate valid signature
        signature = (
            "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        )

        # Test valid signature
        assert verify_signature(payload, signature, secret) is True

        # Test invalid signature
        assert verify_signature(payload, "sha256=invalid", secret) is False

        # Test missing signature
        assert verify_signature(payload, None, secret) is False
