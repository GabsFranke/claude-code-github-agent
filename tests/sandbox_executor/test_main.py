"""Unit tests for sandbox executor FastAPI service."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


class TestHealthEndpoint:
    """Test health check endpoint."""

    def test_health_returns_healthy(self):
        """Test health endpoint returns healthy status."""
        from services.sandbox_executor.main import app

        client = TestClient(app)
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}


class TestExecuteEndpoint:
    """Test execute endpoint."""

    @pytest.mark.asyncio
    async def test_successful_execution(self):
        """Test successful execution request."""
        from services.sandbox_executor.main import app

        mock_response = "Test agent response"

        with patch(
            "services.sandbox_executor.main.execute_sandbox_request",
            new_callable=AsyncMock,
            return_value=mock_response,
        ) as mock_execute:
            client = TestClient(app)
            response = client.post(
                "/execute",
                json={
                    "prompt": "Test prompt",
                    "github_token": "test_token",
                    "repo": "owner/repo",
                    "issue_number": 123,
                    "user": "testuser",
                    "auto_review": False,
                    "auto_triage": False,
                    "workspace": "/tmp/test",
                },
            )

            assert response.status_code == 200
            assert response.json() == {
                "response": mock_response,
                "status": "success",
            }

            mock_execute.assert_called_once()
            call_kwargs = mock_execute.call_args.kwargs
            assert call_kwargs["prompt"] == "Test prompt"
            assert call_kwargs["github_token"] == "test_token"
            assert call_kwargs["repo"] == "owner/repo"
            assert call_kwargs["issue_number"] == 123
            assert call_kwargs["user"] == "testuser"
            assert call_kwargs["auto_review"] is False
            assert call_kwargs["auto_triage"] is False
            # workspace is set to current directory by main.py, not the input value
            assert "workspace" in call_kwargs

    @pytest.mark.asyncio
    async def test_execution_with_auto_review(self):
        """Test execution with auto_review flag."""
        from services.sandbox_executor.main import app

        with patch(
            "services.sandbox_executor.main.execute_sandbox_request",
            new_callable=AsyncMock,
            return_value="Response",
        ) as mock_execute:
            client = TestClient(app)
            response = client.post(
                "/execute",
                json={
                    "prompt": "Review this PR",
                    "github_token": "test_token",
                    "repo": "owner/repo",
                    "issue_number": 456,
                    "user": "bot",
                    "auto_review": True,
                    "auto_triage": False,
                },
            )

            assert response.status_code == 200
            mock_execute.assert_called_once()
            call_kwargs = mock_execute.call_args.kwargs
            assert call_kwargs["auto_review"] is True
            assert call_kwargs["auto_triage"] is False

    @pytest.mark.asyncio
    async def test_execution_with_auto_triage(self):
        """Test execution with auto_triage flag."""
        from services.sandbox_executor.main import app

        with patch(
            "services.sandbox_executor.main.execute_sandbox_request",
            new_callable=AsyncMock,
            return_value="Response",
        ) as mock_execute:
            client = TestClient(app)
            response = client.post(
                "/execute",
                json={
                    "prompt": "Triage this issue",
                    "github_token": "test_token",
                    "repo": "owner/repo",
                    "issue_number": 789,
                    "user": "bot",
                    "auto_review": False,
                    "auto_triage": True,
                },
            )

            assert response.status_code == 200
            mock_execute.assert_called_once()
            call_kwargs = mock_execute.call_args.kwargs
            assert call_kwargs["auto_review"] is False
            assert call_kwargs["auto_triage"] is True

    @pytest.mark.asyncio
    async def test_execution_failure_returns_500(self):
        """Test execution failure returns 500 error."""
        from services.sandbox_executor.main import app

        with patch(
            "services.sandbox_executor.main.execute_sandbox_request",
            new_callable=AsyncMock,
            side_effect=Exception("Execution failed"),
        ):
            client = TestClient(app)
            response = client.post(
                "/execute",
                json={
                    "prompt": "Test prompt",
                    "github_token": "test_token",
                    "repo": "owner/repo",
                    "issue_number": 123,
                    "user": "testuser",
                },
            )

            assert response.status_code == 500
            assert "Execution failed" in response.json()["detail"]

    def test_missing_required_fields(self):
        """Test request with missing required fields."""
        from services.sandbox_executor.main import app

        client = TestClient(app)
        response = client.post(
            "/execute",
            json={
                "prompt": "Test prompt",
                # Missing github_token, repo, issue_number, user
            },
        )

        assert response.status_code == 422  # Validation error

    def test_invalid_field_types(self):
        """Test request with invalid field types."""
        from services.sandbox_executor.main import app

        client = TestClient(app)
        response = client.post(
            "/execute",
            json={
                "prompt": "Test prompt",
                "github_token": "test_token",
                "repo": "owner/repo",
                "issue_number": "not_a_number",  # Should be int
                "user": "testuser",
            },
        )

        assert response.status_code == 422  # Validation error

    @pytest.mark.asyncio
    async def test_default_values_for_optional_fields(self):
        """Test default values are applied for optional fields."""
        from services.sandbox_executor.main import app

        with patch(
            "services.sandbox_executor.main.execute_sandbox_request",
            new_callable=AsyncMock,
            return_value="Response",
        ) as mock_execute:
            client = TestClient(app)
            response = client.post(
                "/execute",
                json={
                    "prompt": "Test prompt",
                    "github_token": "test_token",
                    "repo": "owner/repo",
                    "issue_number": 123,
                    "user": "testuser",
                    # auto_review and auto_triage not provided
                },
            )

            assert response.status_code == 200
            call_kwargs = mock_execute.call_args.kwargs
            assert call_kwargs["auto_review"] is False
            assert call_kwargs["auto_triage"] is False
