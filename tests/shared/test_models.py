"""Unit tests for shared data models."""

import pytest
from pydantic import ValidationError

from shared.models import AgentRequest, AgentResponse


class TestAgentRequest:
    """Test AgentRequest model."""

    def test_agent_request_valid(self):
        """Test valid agent request."""
        request = AgentRequest(
            repository="owner/repo",
            issue_number=123,
            command="Review this PR",
            user="developer",
            auto_review=True,
        )

        assert request.repository == "owner/repo"
        assert request.issue_number == 123
        assert request.command == "Review this PR"
        assert request.user == "developer"
        assert request.auto_review is True

    def test_agent_request_missing_required_fields(self):
        """Test agent request with missing required fields."""
        with pytest.raises(ValidationError):
            AgentRequest(repository="owner/repo")

    def test_agent_request_defaults(self):
        """Test agent request with default values."""
        request = AgentRequest(
            repository="owner/repo",
            issue_number=456,
            command="Test command",
            user="testuser",
        )

        assert request.auto_review is False
        assert request.auto_triage is False
        assert request.event_id is None


class TestAgentResponse:
    """Test AgentResponse model."""

    def test_agent_response_success(self):
        """Test successful agent response."""
        response = AgentResponse(
            success=True,
            response="PR reviewed successfully",
            duration_ms=45000,
            num_turns=5,
            cost_usd=0.15,
        )

        assert response.success is True
        assert "successfully" in response.response
        assert response.duration_ms == 45000
        assert response.num_turns == 5
        assert response.cost_usd == 0.15

    def test_agent_response_failure(self):
        """Test failed agent response."""
        response = AgentResponse(
            success=False, response=None, error="API timeout", duration_ms=30000
        )

        assert response.success is False
        assert response.error == "API timeout"
        assert response.response is None

    def test_agent_response_minimal(self):
        """Test agent response with only required fields."""
        response = AgentResponse(success=True)

        assert response.success is True
        assert response.response is None
        assert response.error is None
