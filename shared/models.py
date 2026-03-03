"""Pydantic models for message queue and internal data structures."""

from pydantic import BaseModel, ConfigDict, Field


class AgentRequest(BaseModel):
    """Message format for agent requests in the queue."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "repository": "owner/repo",
                "issue_number": 123,
                "command": "Review this PR",
                "user": "developer",
                "auto_review": True,
                "auto_triage": False,
                "event_id": "webhook-123456",
            }
        }
    )

    repository: str = Field(..., description="Full repository name (owner/repo)")
    issue_number: int = Field(..., description="Issue or PR number")
    command: str = Field(..., description="Command or task description")
    user: str = Field(..., description="GitHub username who triggered the request")
    auto_review: bool = Field(
        default=False, description="Whether this is an automatic PR review"
    )
    auto_triage: bool = Field(
        default=False, description="Whether this is an automatic issue triage"
    )
    event_id: str | None = Field(None, description="Unique event ID for deduplication")


class AgentResponse(BaseModel):
    """Response from agent execution."""

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "response": "Review completed successfully",
                "error": None,
                "duration_ms": 45000,
                "num_turns": 5,
                "cost_usd": 0.15,
            }
        }
    )

    success: bool = Field(..., description="Whether the request succeeded")
    response: str | None = Field(None, description="Agent response text")
    error: str | None = Field(None, description="Error message if failed")
    duration_ms: int | None = Field(None, description="Execution duration in ms")
    num_turns: int | None = Field(None, description="Number of agent turns")
    cost_usd: float | None = Field(None, description="Estimated cost in USD")
