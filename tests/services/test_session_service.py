"""Tests for session_service REST endpoints - Task 8: Session CRUD REST endpoints TDD.

RED phase: Tests are written BEFORE endpoint implementation.
All tests should FAIL until endpoints are implemented in services/session_service/main.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from shared.constants import PENDING_JOB_QUEUE  # noqa: E402

# ---------------------------------------------------------------------------
# Token helpers (mirrors implementation)
# ---------------------------------------------------------------------------


def _encode_token(repo: str, thread_type: str, thread_id: str, workflow: str) -> str:
    """Encode composite session key into a URL-safe token."""
    payload = json.dumps([repo, thread_type, str(thread_id), workflow])
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def client(mock_session_store_v2: MagicMock, mock_redis: MagicMock) -> TestClient:
    """FastAPI TestClient with mocked get_store() and Redis."""
    mock_wrapper = MagicMock()
    mock_wrapper.store = mock_session_store_v2

    with (
        patch("services.session_service.main.get_store", return_value=mock_wrapper),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        from services.session_service.main import app  # noqa: E402

        with TestClient(app) as tc:
            yield tc


@pytest.fixture
def valid_token() -> str:
    """Return a valid session token for GET/DELETE/PUT tests."""
    return _encode_token("owner/test-repo", "issue", "42", "test-workflow")


# ---------------------------------------------------------------------------
# TestSessionCRUD - RED phase
# ---------------------------------------------------------------------------


class TestSessionCRUD:
    """Session CRUD REST endpoint tests (TDD - RED phase)."""

    def test_create_session_returns_201(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        sample_unified_session_data: dict,
    ):
        """POST /api/sessions with valid body -> 201."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        saved_session = UnifiedSessionInfo.model_validate(sample_unified_session_data)
        mock_session_store_v2.get_session.return_value = saved_session

        request_body = {
            "repo": "owner/test-repo",
            "thread_type": "issue",
            "thread_id": "42",
            "workflow": "test-workflow",
            "session_id": "ses_abc123def456",
            "worktree_path": "/tmp/worktrees/test-repo",
            "ref": "main",
        }

        response = client.post("/api/sessions", json=request_body)
        assert (
            response.status_code == 201
        ), f"Expected 201, got {response.status_code}: {response.text}"
        data = response.json()
        assert "token" in data, f"Response missing 'token': {data}"
        assert isinstance(data["token"], str) and len(data["token"]) > 0
        for field in (
            "repo",
            "thread_type",
            "thread_id",
            "workflow_name",
            "session_id",
            "status",
        ):
            assert field in data, f"Field '{field}' missing from response: {data}"
        assert data["repo"] == sample_unified_session_data["repo"]
        assert data["status"] == "active"
        mock_session_store_v2.save_session.assert_called_once()

    def test_create_session_invalid_body_returns_422(
        self, client: TestClient, mock_session_store_v2: MagicMock
    ):
        """POST /api/sessions with missing required fields -> 422."""
        response = client.post("/api/sessions", json={"repo": "o/r"})
        assert response.status_code == 422

    def test_get_session_by_token_returns_200(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        valid_token: str,
        sample_unified_session_data: dict,
    ):
        """GET /api/sessions/{valid_token} -> 200 with full session data."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        mock_session_store_v2.get_session.return_value = (
            UnifiedSessionInfo.model_validate(sample_unified_session_data)
        )
        response = client.get(f"/api/sessions/{valid_token}")
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        expected_fields = [
            "session_id",
            "repo",
            "thread_type",
            "thread_id",
            "workflow_name",
            "ref",
            "worktree_path",
            "created_at",
            "last_run",
            "turn_count",
            "status",
            "summary",
            "streaming_token",
            "installation_id",
            "initial_query",
            "conversation_config",
            "transcript_path",
            "run_count",
            "session_proxy_url",
            "issue_number",
            "user",
        ]
        for field in expected_fields:
            assert field in data, f"Field '{field}' missing from response: {data}"
        mock_session_store_v2.get_session.assert_called_once()

    def test_get_session_not_found_returns_404(
        self, client: TestClient, mock_session_store_v2: MagicMock
    ):
        """GET /api/sessions/{nonexistent} -> 404 with error JSON."""
        mock_session_store_v2.get_session.return_value = None
        token = _encode_token("nonexistent/repo", "pr", "99", "no-wf")
        response = client.get(f"/api/sessions/{token}")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "detail" in data

    def test_delete_session_returns_200(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        valid_token: str,
    ):
        """DELETE /api/sessions/{token} -> 200."""
        response = client.delete(f"/api/sessions/{valid_token}")
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get("status") == "deleted"
        mock_session_store_v2.close_session.assert_called_once()

    def test_list_sessions_for_repo_returns_200(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        sample_unified_session_data: dict,
    ):
        """GET /api/sessions?repo=owner/test-repo -> 200 with array."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        mock_session_store_v2.list_sessions.return_value = [
            UnifiedSessionInfo.model_validate(sample_unified_session_data),
        ]
        response = client.get("/api/sessions", params={"repo": "owner/test-repo"})
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["repo"] == "owner/test-repo"

    def test_list_sessions_empty_repo_returns_200(
        self, client: TestClient, mock_session_store_v2: MagicMock
    ):
        """GET /api/sessions?repo=empty -> 200 with empty array."""
        mock_session_store_v2.list_sessions.return_value = []
        response = client.get("/api/sessions", params={"repo": "empty/repo"})
        assert response.status_code == 200
        data = response.json()
        assert data == []

    def test_expire_session_returns_200(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        valid_token: str,
    ):
        """PUT /api/sessions/{token}/expire -> 200."""
        response = client.put(
            f"/api/sessions/{valid_token}/expire", json={"ttl_hours": 24}
        )
        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get("status") == "expired"
        assert data.get("ttl_hours") == 24
        mock_session_store_v2.expire_session.assert_called_once()

    def test_expire_session_default_ttl(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
        valid_token: str,
    ):
        """PUT /api/sessions/{token}/expire without ttl_hours -> 200."""
        response = client.put(f"/api/sessions/{valid_token}/expire", json={})
        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "expired"
        assert "ttl_hours" in data

    def test_error_format_is_consistent(
        self, client: TestClient, mock_session_store_v2: MagicMock
    ):
        """All error responses use format: {"error": "message", "detail": "..."}"""
        mock_session_store_v2.get_session.return_value = None
        token = _encode_token("bad/repo", "issue", "1", "wf")
        response = client.get(f"/api/sessions/{token}")
        assert response.status_code == 404
        data = response.json()
        assert "error" in data, f"Missing 'error' in {data}"
        assert "detail" in data, f"Missing 'detail' in {data}"
        assert isinstance(data["error"], str)
        assert isinstance(data["detail"], str)


# ============================================================================
# TestSessionLifecycle — Task 10: Session lifecycle state machine
# ============================================================================

from services.session_service.store import (  # noqa: E402
    InvalidSessionTransition,
    SessionNotFoundError,
    SessionStoreWrapper,
)
from shared.session_store import (  # noqa: E402
    SessionStore,
    UnifiedSessionInfo,
)


def _session_with_status(status: str) -> UnifiedSessionInfo:
    """Build a minimal UnifiedSessionInfo with the given status."""
    return UnifiedSessionInfo(
        session_id="ses_abc123",
        repo="owner/repo",
        thread_type="issue",
        thread_id="42",
        workflow_name="test-workflow",
        ref="main",
        worktree_path="/tmp/wt",
        created_at="2026-01-01T00:00:00Z",
        last_run="2026-01-01T00:00:00Z",
        status=status,
        installation_id="inst-1",
        initial_query="hello",
        conversation_config="{}",
        session_proxy_url="http://x",
        issue_number="1",
        user="test",
    )


def _make_mock_store():
    """Create a mock SessionStore with all async methods the wrapper needs."""
    store = MagicMock(spec=SessionStore)
    store.create_session = AsyncMock()
    store.get_streaming_session = AsyncMock()
    store.set_completed = AsyncMock()
    store.set_running = AsyncMock()
    store.set_ttl = AsyncMock()
    store.increment_run_count = AsyncMock(return_value=2)
    store.increment_subscribers = AsyncMock(return_value=1)
    store.decrement_subscribers = AsyncMock(return_value=0)
    store.has_subscribers = AsyncMock(return_value=False)
    store.delete_session = AsyncMock()
    return store


def _make_wrapper(store=None):
    """Build a SessionStoreWrapper backed by a mock SessionStore."""
    if store is None:
        store = _make_mock_store()
    wrapper = SessionStoreWrapper(redis_client=MagicMock())
    wrapper._store = store
    return wrapper


class TestSessionLifecycle:
    """State machine: running -> completed|error -> (resume) -> running -> expire."""

    @pytest.mark.asyncio
    async def test_create_session_delegates_to_store(self):
        store = _make_mock_store()
        wrapper = _make_wrapper(store)

        await wrapper.create_session(
            token="tok-001",
            repo="owner/repo",
            issue_number=42,
            workflow="test-wf",
        )

        store.create_session.assert_awaited_once()
        call_args = store.create_session.call_args
        assert call_args.kwargs["token"] == "tok-001"
        assert call_args.kwargs["repo"] == "owner/repo"
        assert call_args.kwargs["issue_number"] == 42
        assert call_args.kwargs["workflow"] == "test-wf"

    @pytest.mark.asyncio
    async def test_create_session_passes_optional_fields(self):
        store = _make_mock_store()
        wrapper = _make_wrapper(store)

        await wrapper.create_session(
            token="tok-002",
            repo="owner/repo",
            issue_number=1,
            workflow="wf",
            installation_id="inst-99",
            initial_query="fix bug",
            thread_type="pr",
            ref="feature/x",
            user="dev1",
            conversation_config='{"persist":true}',
            session_id="sdk-001",
        )

        call_kwargs = store.create_session.call_args.kwargs
        assert call_kwargs["installation_id"] == "inst-99"
        assert call_kwargs["initial_query"] == "fix bug"
        assert call_kwargs["thread_type"] == "pr"
        assert call_kwargs["ref"] == "feature/x"
        assert call_kwargs["user"] == "dev1"
        assert call_kwargs["conversation_config"] == '{"persist":true}'
        assert call_kwargs["session_id"] == "sdk-001"

    @pytest.mark.asyncio
    async def test_transition_to_running_from_completed(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("completed")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_running("tok-001")

        store.get_streaming_session.assert_awaited_once_with("tok-001")
        store.set_running.assert_awaited_once_with("tok-001")
        store.increment_run_count.assert_awaited_once_with("tok-001")

    @pytest.mark.asyncio
    async def test_transition_to_running_from_error(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("error")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_running("tok-001")

        store.set_running.assert_awaited_once_with("tok-001")
        store.increment_run_count.assert_awaited_once_with("tok-001")

    @pytest.mark.asyncio
    async def test_transition_to_running_from_running_is_idempotent(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("running")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_running("tok-001")

        store.set_running.assert_awaited_once_with("tok-001")
        store.increment_run_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transition_to_running_session_not_found(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(return_value=None)
        wrapper = _make_wrapper(store)

        with pytest.raises(SessionNotFoundError, match="not found"):
            await wrapper.transition_to_running("nonexistent")

    @pytest.mark.asyncio
    async def test_transition_to_running_from_active(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("active")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_running("tok-001")

        store.set_running.assert_awaited_once_with("tok-001")
        store.increment_run_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transition_to_completed_from_running(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("running")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_completed("tok-001")

        store.get_streaming_session.assert_awaited_once_with("tok-001")
        store.set_completed.assert_awaited_once()
        _, kwargs = store.set_completed.call_args
        assert kwargs.get("token") == "tok-001"
        assert kwargs.get("is_error") is False

    @pytest.mark.asyncio
    async def test_transition_to_error_from_running(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("running")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_completed("tok-001", is_error=True)

        store.set_completed.assert_awaited_once()
        _, kwargs = store.set_completed.call_args
        assert kwargs.get("is_error") is True

    @pytest.mark.asyncio
    async def test_transition_to_completed_with_session_id(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("running")
        )
        wrapper = _make_wrapper(store)

        await wrapper.transition_to_completed("tok-001", session_id="sdk-sess-999")

        store.set_completed.assert_awaited_once()
        _, kwargs = store.set_completed.call_args
        assert kwargs.get("session_id") == "sdk-sess-999"

    @pytest.mark.asyncio
    async def test_transition_to_completed_session_not_found(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(return_value=None)
        wrapper = _make_wrapper(store)

        with pytest.raises(SessionNotFoundError, match="not found"):
            await wrapper.transition_to_completed("nonexistent")

    @pytest.mark.asyncio
    async def test_invalid_completed_to_completed(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("completed")
        )
        wrapper = _make_wrapper(store)

        with pytest.raises(InvalidSessionTransition, match="completed.*completed"):
            await wrapper.transition_to_completed("tok-001")

    @pytest.mark.asyncio
    async def test_invalid_error_to_completed(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("error")
        )
        wrapper = _make_wrapper(store)

        with pytest.raises(InvalidSessionTransition, match="error.*completed"):
            await wrapper.transition_to_completed("tok-001")

    @pytest.mark.asyncio
    async def test_invalid_from_expired(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("expired")
        )
        wrapper = _make_wrapper(store)

        with pytest.raises(InvalidSessionTransition, match="expired.*running"):
            await wrapper.transition_to_running("tok-001")

    @pytest.mark.asyncio
    async def test_expire_session_shortens_ttl(self):
        store = _make_mock_store()
        wrapper = _make_wrapper(store)

        await wrapper.expire_session_by_token("tok-001", ttl_hours=1)

        store.set_ttl.assert_awaited_once_with("tok-001", 3600)

    @pytest.mark.asyncio
    async def test_expire_session_default_ttl(self):
        store = _make_mock_store()
        wrapper = _make_wrapper(store)

        await wrapper.expire_session_by_token("tok-001")

        store.set_ttl.assert_awaited_once_with("tok-001", 72 * 3600)

    @pytest.mark.asyncio
    async def test_connect_subscriber(self):
        store = _make_mock_store()
        store.increment_subscribers = AsyncMock(return_value=3)
        wrapper = _make_wrapper(store)

        count = await wrapper.connect_subscriber("tok-001")

        store.increment_subscribers.assert_awaited_once_with("tok-001")
        assert count == 3

    @pytest.mark.asyncio
    async def test_disconnect_subscriber(self):
        store = _make_mock_store()
        store.decrement_subscribers = AsyncMock(return_value=0)
        wrapper = _make_wrapper(store)

        count = await wrapper.disconnect_subscriber("tok-001")

        store.decrement_subscribers.assert_awaited_once_with("tok-001")
        assert count == 0

    @pytest.mark.asyncio
    async def test_has_subscribers(self):
        store = _make_mock_store()
        store.has_subscribers = AsyncMock(return_value=True)
        wrapper = _make_wrapper(store)

        result = await wrapper.has_subscribers("tok-001")

        store.has_subscribers.assert_awaited_once_with("tok-001")
        assert result is True

    @pytest.mark.asyncio
    async def test_subscriber_connect_disconnect_cycle(self):
        store = _make_mock_store()
        store.increment_subscribers = AsyncMock(side_effect=[1, 2])
        store.decrement_subscribers = AsyncMock(side_effect=[1, 0])
        wrapper = _make_wrapper(store)

        c1 = await wrapper.connect_subscriber("tok-001")
        c2 = await wrapper.connect_subscriber("tok-001")
        assert c1 == 1
        assert c2 == 2

        d1 = await wrapper.disconnect_subscriber("tok-001")
        d2 = await wrapper.disconnect_subscriber("tok-001")
        assert d1 == 1
        assert d2 == 0

        assert store.increment_subscribers.call_count == 2
        assert store.decrement_subscribers.call_count == 2

    @pytest.mark.asyncio
    async def test_full_lifecycle_create_run_complete_resume(self):
        store = _make_mock_store()
        session_sequence = [
            _session_with_status("running"),
            _session_with_status("completed"),
            _session_with_status("running"),
        ]
        store.get_streaming_session = AsyncMock(side_effect=session_sequence)
        wrapper = _make_wrapper(store)

        await wrapper.create_session(
            token="full-lifecycle",
            repo="owner/repo",
            issue_number=99,
            workflow="lifecycle-wf",
        )
        store.create_session.assert_awaited_once()

        await wrapper.transition_to_completed("full-lifecycle")
        store.set_completed.assert_awaited_once()
        _, kw1 = store.set_completed.call_args
        assert kw1.get("is_error") is False

        await wrapper.transition_to_running("full-lifecycle")
        store.set_running.assert_awaited_once_with("full-lifecycle")
        store.increment_run_count.assert_awaited_once_with("full-lifecycle")

        store.set_completed.reset_mock()
        await wrapper.transition_to_completed("full-lifecycle")
        store.set_completed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_invalid_transition_message_is_descriptive(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(
            return_value=_session_with_status("completed")
        )
        wrapper = _make_wrapper(store)

        with pytest.raises(InvalidSessionTransition) as exc_info:
            await wrapper.transition_to_completed("tok-001")

        assert "completed" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_session_not_found_message_includes_token(self):
        store = _make_mock_store()
        store.get_streaming_session = AsyncMock(return_value=None)
        wrapper = _make_wrapper(store)

        with pytest.raises(SessionNotFoundError) as exc_info:
            await wrapper.transition_to_running("my-token-abc")

        assert "my-token-abc" in str(exc_info.value)


class TestTokenResolution:
    """Token resolution endpoint with transcript fallback — Task 9 TDD.

    Endpoint:  GET /api/resolve/{owner}/{repo}/{type}/{number}/{workflow}

    Resolution chain:
        1. find_active_session -> running/active token
        2. find_session -> any session token (completed/error)
        3. transcript scan -> transcript:{stem} pseudo-token
        4. 404 — nothing found
    """

    def test_active_session_returns_token_and_running_status(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """Active (running) session -> 200 with token and status=running."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        token = "tok_active_001"
        session = UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_test",
                "repo": "test/repo",
                "thread_type": "issue",
                "thread_id": "42",
                "workflow_name": "test-workflow",
                "ref": "main",
                "worktree_path": "/tmp/test",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_run": "2026-01-01T00:00:00+00:00",
                "turn_count": 0,
                "status": "running",
                "streaming_token": token,
            }
        )
        mock_session_store_v2.find_active_session.return_value = token
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=session)

        response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        body = response.json()
        assert body["token"] == token
        assert body["status"] == "found"
        assert body["session"]["status"] == "running"

    def test_completed_session_returns_token_and_completed_status(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """Completed session -> 200 with token and status=completed."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        token = "tok_completed_001"
        session = UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_test",
                "repo": "test/repo",
                "thread_type": "issue",
                "thread_id": "42",
                "workflow_name": "test-workflow",
                "ref": "main",
                "worktree_path": "/tmp/test",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_run": "2026-01-01T00:00:00+00:00",
                "turn_count": 0,
                "status": "completed",
                "streaming_token": token,
            }
        )
        mock_session_store_v2.find_active_session.return_value = None
        mock_session_store_v2.find_session.return_value = token
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=session)

        response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        body = response.json()
        assert body["token"] == token
        assert body["status"] == "found"
        assert body["session"]["status"] == "completed"

    def test_error_session_returns_token_and_error_status(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """Session that errored out -> 200 with status=error."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        token = "tok_error_001"
        session = UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_test",
                "repo": "test/repo",
                "thread_type": "issue",
                "thread_id": "42",
                "workflow_name": "test-workflow",
                "ref": "main",
                "worktree_path": "/tmp/test",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_run": "2026-01-01T00:00:00+00:00",
                "turn_count": 0,
                "status": "error",
                "streaming_token": token,
            }
        )
        mock_session_store_v2.find_active_session.return_value = None
        mock_session_store_v2.find_session.return_value = token
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=session)

        response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert response.status_code == 200
        body = response.json()
        assert body["token"] == token
        assert body["status"] == "found"
        assert body["session"]["status"] == "error"

    def test_transcript_fallback_when_no_redis_session(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """No Redis session but transcript exists -> 200 with transcript: pseudo-token."""
        mock_session_store_v2.find_active_session.return_value = None
        mock_session_store_v2.find_session.return_value = None

        with patch(
            "services.session_service.main._find_transcript_token",
            return_value="transcript:sid_abc123def",
        ):
            response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        body = response.json()
        assert body["token"] == "transcript:sid_abc123def"
        assert body["status"] == "found"
        assert body["session"]["status"] == "completed"

    def test_not_found_returns_200_pending(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """Nothing in Redis, no transcript -> 200 with status=pending."""
        mock_session_store_v2.find_active_session.return_value = None
        mock_session_store_v2.find_session.return_value = None

        with patch(
            "services.session_service.main._find_transcript_token",
            return_value=None,
        ):
            response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert (
            response.status_code == 200
        ), f"Expected 200, got {response.status_code}: {response.text}"
        body = response.json()
        assert body["status"] == "pending"

    def test_invalid_number_returns_400(
        self, client: TestClient, mock_session_store_v2: MagicMock
    ):
        """Non-integer issue number -> 400."""
        response = client.get(
            "/api/resolve/test/repo/issues/not-a-number/test-workflow"
        )
        assert response.status_code == 400
        body = response.json()
        assert "detail" in body

    def test_pull_segment_maps_to_pr_thread_type(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """URL segment 'pull' -> thread_type='pr' passed to find_active_session."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        token = "tok_pr_001"
        session = UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_test",
                "repo": "test/repo",
                "thread_type": "pr",
                "thread_id": "42",
                "workflow_name": "test-workflow",
                "ref": "main",
                "worktree_path": "/tmp/test",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_run": "2026-01-01T00:00:00+00:00",
                "turn_count": 0,
                "status": "running",
                "streaming_token": token,
            }
        )
        mock_session_store_v2.find_active_session.return_value = token
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=session)

        response = client.get("/api/resolve/test/repo/pull/42/test-workflow")

        assert response.status_code == 200
        mock_session_store_v2.find_active_session.assert_called_once_with(
            "test/repo", 42, "test-workflow", thread_type="pr"
        )

    def test_pull_url_finds_session_stored_under_issue_thread_type(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """A PR session can be stored as "issue" — GitHub delivers PR comments
        as ``issue_comment`` events — so a /pull/ URL must still resolve it."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        token = "tok_pr_as_issue"
        session = UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_test",
                "repo": "test/repo",
                "thread_type": "issue",
                "thread_id": "42",
                "workflow_name": "test-workflow",
                "ref": "refs/pull/42/head",
                "worktree_path": "/tmp/test",
                "created_at": "2026-01-01T00:00:00+00:00",
                "last_run": "2026-01-01T00:00:00+00:00",
                "turn_count": 0,
                "status": "completed",
                "streaming_token": token,
            }
        )

        async def find_session(repo, number, workflow, thread_type=""):
            return token if thread_type == "issue" else None

        mock_session_store_v2.find_active_session = AsyncMock(return_value=None)
        mock_session_store_v2.find_session = AsyncMock(side_effect=find_session)
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=session)

        response = client.get("/api/resolve/test/repo/pull/42/test-workflow")

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "found"
        assert body["token"] == token

    def test_requested_thread_type_wins_over_the_others(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """When both a PR and an issue session exist, /pull/ gets the PR one."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        def _session(thread_type: str, token: str) -> UnifiedSessionInfo:
            return UnifiedSessionInfo.model_validate(
                {
                    "session_id": "ses_test",
                    "repo": "test/repo",
                    "thread_type": thread_type,
                    "thread_id": "42",
                    "workflow_name": "test-workflow",
                    "ref": "main",
                    "worktree_path": "/tmp/test",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "last_run": "2026-01-01T00:00:00+00:00",
                    "turn_count": 0,
                    "status": "completed",
                    "streaming_token": token,
                }
            )

        tokens = {"pr": "tok_pr", "issue": "tok_issue"}
        sessions = {v: _session(k, v) for k, v in tokens.items()}

        async def find_session(repo, number, workflow, thread_type=""):
            return tokens.get(thread_type)

        mock_session_store_v2.find_active_session = AsyncMock(return_value=None)
        mock_session_store_v2.find_session = AsyncMock(side_effect=find_session)
        mock_session_store_v2.get_streaming_session = AsyncMock(
            side_effect=lambda token: sessions[token]
        )

        response = client.get("/api/resolve/test/repo/pull/42/test-workflow")

        assert response.json()["token"] == "tok_pr"

    def test_active_session_wins_over_a_completed_one_of_another_type(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """Every thread type is checked for a running session before any
        completed session is considered."""
        from shared.session_store import UnifiedSessionInfo  # noqa: E402

        def _session(thread_type: str, token: str, status: str) -> UnifiedSessionInfo:
            return UnifiedSessionInfo.model_validate(
                {
                    "session_id": "ses_test",
                    "repo": "test/repo",
                    "thread_type": thread_type,
                    "thread_id": "42",
                    "workflow_name": "test-workflow",
                    "ref": "main",
                    "worktree_path": "/tmp/test",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "last_run": "2026-01-01T00:00:00+00:00",
                    "turn_count": 0,
                    "status": status,
                    "streaming_token": token,
                }
            )

        sessions = {
            "tok_running_issue": _session("issue", "tok_running_issue", "running"),
            "tok_done_pr": _session("pr", "tok_done_pr", "completed"),
        }

        async def find_active(repo, number, workflow, thread_type=""):
            return "tok_running_issue" if thread_type == "issue" else None

        async def find_session(repo, number, workflow, thread_type=""):
            return "tok_done_pr" if thread_type == "pr" else "tok_running_issue"

        mock_session_store_v2.find_active_session = AsyncMock(side_effect=find_active)
        mock_session_store_v2.find_session = AsyncMock(side_effect=find_session)
        mock_session_store_v2.get_streaming_session = AsyncMock(
            side_effect=lambda token: sessions[token]
        )

        response = client.get("/api/resolve/test/repo/pull/42/test-workflow")

        body = response.json()
        assert body["token"] == "tok_running_issue"
        assert body["session"]["status"] == "running"

    def test_stale_token_falls_through_to_transcript(
        self,
        client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """find_session returns token but streaming hash missing -> transcript fallback."""
        mock_session_store_v2.find_active_session.return_value = None
        mock_session_store_v2.find_session.return_value = "stale_token"
        mock_session_store_v2.get_streaming_session = AsyncMock(return_value=None)

        with patch(
            "services.session_service.main._find_transcript_token",
            return_value="transcript:sid_stale",
        ):
            response = client.get("/api/resolve/test/repo/issues/42/test-workflow")

        assert response.status_code == 200
        body = response.json()
        assert body["token"] == "transcript:sid_stale"
        assert body["status"] == "found"
        assert body["session"]["status"] == "completed"


# ============================================================================
# TestTranscriptFallback - Task 11: Transcript pseudo-token fallback TDD
# ============================================================================


@pytest.fixture
def transcript_project_dir(tmp_path):
    """Create a fake ~/.claude/projects/ directory with a JSONL transcript."""
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    project_dir = projects / "owner--repo-issue-42-my-workflow-abc123"
    project_dir.mkdir()
    return project_dir


@pytest.fixture
def transcript_file(transcript_project_dir):
    """Create a valid 4-line JSONL transcript file."""
    path = transcript_project_dir / "ses_test_123.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello"},
                "session_id": "ses_test_123",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Hi there!"},
                "session_id": "ses_test_123",
            }
        ),
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "Do something"},
                "session_id": "ses_test_123",
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Sure!"},
                "session_id": "ses_test_123",
            }
        ),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def empty_transcript_file(transcript_project_dir):
    """Create an empty JSONL transcript file."""
    path = transcript_project_dir / "ses_empty.jsonl"
    path.write_text("", encoding="utf-8")
    return path


@pytest.fixture
def malformed_transcript_file(transcript_project_dir):
    """Create a JSONL transcript with malformed lines."""
    path = transcript_project_dir / "ses_malformed.jsonl"
    lines = [
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": "Valid line"}}
        ),
        "this is not JSON at all",
        "{ also not valid json",
        "",
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "Another valid"},
            }
        ),
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


@pytest.fixture
def transcript_dir_with_workflow(tmp_path):
    """Create a project dir with workflow in name for repo-based lookup."""
    projects = tmp_path / "projects"
    projects.mkdir(parents=True)
    project_dir = projects / "test-owner--test-repo-issue-5-my-wf-xyz789"
    project_dir.mkdir()
    path = project_dir / "ses_abc456.jsonl"
    path.write_text(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": "#5 fix this"}}
        )
        + "\n",
        encoding="utf-8",
    )
    return project_dir


class TestTranscriptFallback:
    """Task 11: Transcript pseudo-token fallback tests."""

    def test_find_transcript_exists(self, transcript_file, monkeypatch):
        from services.session_service.transcript import find_transcript

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        result = find_transcript(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert result is not None
        assert result.name == "ses_test_123.jsonl"
        assert result.exists()

    def test_find_transcript_nonexistent(self, tmp_path, monkeypatch):
        from services.session_service.transcript import find_transcript

        empty = tmp_path / "empty-projects"
        empty.mkdir()
        monkeypatch.setattr("services.session_service.transcript.PROJECTS_DIR", empty)
        result = find_transcript(
            repo="nonexistent/repo",
            thread_type="issue",
            thread_id="999",
            workflow="no-workflow",
        )
        assert result is None

    def test_find_transcript_no_projects_dir(self, tmp_path, monkeypatch):
        from services.session_service.transcript import find_transcript

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            tmp_path / "does-not-exist",
        )
        result = find_transcript(
            repo="any/repo", thread_type="issue", thread_id="1", workflow="wf"
        )
        assert result is None

    def test_find_transcript_by_repo_exists(
        self, transcript_dir_with_workflow, monkeypatch
    ):
        from services.session_service.transcript import (
            find_transcript_by_repo,
        )

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_dir_with_workflow.parent,
        )
        result = find_transcript_by_repo(
            repo="test-owner/test-repo",
            thread_type="issue",
            thread_id="5",
            workflow="my-wf",
        )
        assert result is not None
        assert result.suffix == ".jsonl"

    def test_find_transcript_by_repo_wrong_repo(self, transcript_file, monkeypatch):
        from services.session_service.transcript import (
            find_transcript_by_repo,
        )

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        result = find_transcript_by_repo(
            repo="wrong-owner/wrong-repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert result is None

    def test_find_transcript_by_repo_wrong_workflow(self, transcript_file, monkeypatch):
        from services.session_service.transcript import (
            find_transcript_by_repo,
        )

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        result = find_transcript_by_repo(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="nonexistent-workflow",
        )
        assert result is None

    def test_find_transcript_by_repo_no_projects_dir(self, tmp_path, monkeypatch):
        from services.session_service.transcript import (
            find_transcript_by_repo,
        )

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR", tmp_path / "no-such-dir"
        )
        result = find_transcript_by_repo(
            repo="any/repo", thread_type="issue", thread_id="1", workflow="wf"
        )
        assert result is None

    def test_load_transcript_history_valid(self, transcript_file):
        from services.session_service.transcript import load_transcript_history

        messages = load_transcript_history(transcript_file)
        assert len(messages) == 4
        assert messages[0]["type"] == "user"
        assert messages[0]["message"]["content"] == "Hello"
        assert messages[1]["type"] == "assistant"
        assert messages[3]["message"]["content"] == "Sure!"

    def test_load_transcript_history_empty(self, empty_transcript_file):
        from services.session_service.transcript import load_transcript_history

        messages = load_transcript_history(empty_transcript_file)
        assert messages == []
        assert isinstance(messages, list)

    def test_load_transcript_history_malformed(self, malformed_transcript_file, caplog):
        from services.session_service.transcript import load_transcript_history

        with caplog.at_level(
            logging.WARNING, logger="services.session_service.transcript"
        ):
            messages = load_transcript_history(malformed_transcript_file)
        assert len(messages) == 2
        assert messages[0]["type"] == "user"
        assert messages[1]["type"] == "assistant"
        warnings_list = [
            r.message for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any(
            "malformed" in w.lower() or "skipping" in w.lower() for w in warnings_list
        )

    def test_load_transcript_history_missing(self, tmp_path):
        from services.session_service.transcript import load_transcript_history

        messages = load_transcript_history(tmp_path / "nonexistent.jsonl")
        assert messages == []

    def test_load_transcript_history_blank_lines(self, transcript_project_dir):
        from services.session_service.transcript import load_transcript_history

        path = transcript_project_dir / "ses_blanks.jsonl"
        content = (
            json.dumps(
                {"type": "user", "message": {"role": "user", "content": "first"}}
            )
            + "\n\n\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "second"},
                }
            )
            + "\n\n"
        )
        path.write_text(content, encoding="utf-8")
        messages = load_transcript_history(path)
        assert len(messages) == 2
        assert messages[0]["type"] == "user"
        assert messages[1]["type"] == "assistant"

    def test_build_pseudo_token_valid(self):
        from services.session_service.transcript import build_pseudo_token

        token = build_pseudo_token("ses_abc123")
        assert token == "transcript:ses_abc123"

    def test_build_pseudo_token_invalid_chars(self):
        from services.session_service.transcript import build_pseudo_token

        with pytest.raises(ValueError, match="Invalid session_id"):
            build_pseudo_token("bad/session/id")

    def test_build_pseudo_token_path_traversal(self):
        from services.session_service.transcript import build_pseudo_token

        with pytest.raises(ValueError, match="Invalid session_id"):
            build_pseudo_token("../../../etc/passwd")

    def test_cache_transcript_lookup(self, transcript_file, monkeypatch):
        from services.session_service.transcript import find_transcript

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        r1 = find_transcript(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert r1 is not None
        r2 = find_transcript(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert r2 == r1

    def test_cache_miss_different_params(self, transcript_file, monkeypatch):
        from services.session_service.transcript import find_transcript

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        r1 = find_transcript(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert r1 is not None
        r2 = find_transcript(
            repo="different/repo",
            thread_type="issue",
            thread_id="999",
            workflow="unknown-wf",
        )
        assert r2 is None or isinstance(r2, Path)

    def test_pseudo_token_strip_prefix(self):
        from services.session_service.transcript import build_pseudo_token

        token = build_pseudo_token("my_session_1")
        assert token.startswith("transcript:")
        session_id = token[len("transcript:") :]
        assert session_id == "my_session_1"

    def test_full_pseudo_token_flow(self, transcript_file, monkeypatch):
        from services.session_service.transcript import (
            build_pseudo_token,
            find_transcript,
            load_transcript_history,
        )

        monkeypatch.setattr(
            "services.session_service.transcript.PROJECTS_DIR",
            transcript_file.parent.parent,
        )
        path = find_transcript(
            repo="owner/repo",
            thread_type="issue",
            thread_id="42",
            workflow="my-workflow",
        )
        assert path is not None
        session_id = path.stem
        token = build_pseudo_token(session_id)
        assert token == f"transcript:{session_id}"
        messages = load_transcript_history(path)
        assert len(messages) == 4
        assert all(isinstance(m, dict) for m in messages)


# ============================================================================
# TestWebSocket — Task 13: WebSocket handler TDD (port from session_proxy)
# ============================================================================

from collections.abc import AsyncGenerator  # noqa: E402

# ── Helper: build an async generator that simulates Redis pubsub.listen() ──


async def _async_listen(
    messages: list[dict],
    *,
    disconnect_after: bool = False,
) -> AsyncGenerator[dict, None]:
    yield {"type": "subscribe", "channel": b"session:msg:test", "data": 1}
    for msg in messages:
        yield {
            "type": "message",
            "channel": b"session:msg:test",
            "data": json.dumps(msg),
        }
    if disconnect_after:
        return
    # Keep yielding periodic messages so _redis_to_ws calls send_text()
    # regularly, which lets it detect WebSocketDisconnect when the test
    # client closes. Without this, _redis_to_ws blocks on pubsub.listen()
    # and never discovers the client disconnected.
    heartbeat = json.dumps({"type": "noop", "ts": "2026-06-09T00:00:00Z"})
    while True:
        await asyncio.sleep(0.05)
        yield {
            "type": "message",
            "channel": b"session:msg:test",
            "data": heartbeat,
        }


# ── Clients ──────────────────────────────────────────────────────────────────


@pytest.fixture
def ws_client(mock_session_store_v2: MagicMock, mock_redis: MagicMock) -> TestClient:
    from shared.session_store import UnifiedSessionInfo  # noqa: E402

    store = mock_session_store_v2
    # Lookups return a token; the conftest default returns session data.
    store.find_active_session = AsyncMock(return_value=None)
    store.find_session = AsyncMock(return_value="tok_ws_test")
    store.get_streaming_session = AsyncMock(
        return_value=UnifiedSessionInfo.model_validate(
            {
                "session_id": "ses_ws_test",
                "repo": "owner/repo",
                "thread_type": "issue",
                "thread_id": "42",
                "workflow_name": "my-workflow",
                "ref": "main",
                "worktree_path": "/tmp/ws_test",
                "created_at": "2026-06-09T10:00:00+00:00",
                "last_run": "2026-06-09T12:00:00+00:00",
                "turn_count": 1,
                "status": "running",
                "streaming_token": "tok_ws_test",
                "installation_id": "inst_123",
                "initial_query": "test query",
                "conversation_config": "{}",
                "transcript_path": "",
                "run_count": 1,
                "session_proxy_url": "",
                "issue_number": "42",
                "user": "testuser",
            }
        )
    )
    store.increment_subscribers = AsyncMock(return_value=1)
    store.decrement_subscribers = AsyncMock(return_value=0)
    store.get_history = AsyncMock(return_value=[])

    # Default pubsub mock — tests override listen() for specific scenarios
    pubsub_default = MagicMock()
    pubsub_default.subscribe = AsyncMock(return_value=None)
    pubsub_default.unsubscribe = AsyncMock(return_value=None)
    pubsub_default.close = AsyncMock(return_value=None)
    pubsub_default.listen = MagicMock(side_effect=lambda: _async_listen([]))
    mock_redis.pubsub = MagicMock(return_value=pubsub_default)
    mock_redis.rpush = AsyncMock(return_value=1)
    mock_redis.expire = AsyncMock(return_value=True)

    from services.session_service.store import SessionStoreWrapper  # noqa: E402

    mock_wrapper = MagicMock(spec=SessionStoreWrapper)
    mock_wrapper.store = store

    with (
        patch("services.session_service.main.get_store", return_value=mock_wrapper),
        patch("redis.asyncio.from_url", return_value=mock_redis),
    ):
        from services.session_service.main import app  # noqa: E402

        with TestClient(app) as tc:
            yield tc


# ── Tests ────────────────────────────────────────────────────────────────────


class TestWebSocket:
    """WebSocket endpoint tests — TDD RED phase (Task 13).

    Endpoint:  ws://session-service/ws/{owner}/{repo}/{type}/{number}/{wf}

    Tests:
        - connect → receive history
        - connect → receive live messages via pub/sub
        - connect → send inject_message → message in inbox
        - connect → disconnect → subscriber count decremented
        - reconnect → history still available
    """

    # ── CSWSH validation ─────────────────────────────────────────────────

    def test_cswsh_rejects_disallowed_origin(self, ws_client: TestClient, monkeypatch):
        """WebSocket from an untrusted origin → 403 close."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://trusted.example.com")

        with pytest.raises(Exception):  # noqa: B017
            with ws_client.websocket_connect(
                "/ws/owner/repo/issues/42/my-workflow",
                headers={"origin": "https://evil.example.com"},
            ) as ws:
                ws.receive_text()

    def test_cswsh_allows_configured_origin(self, ws_client: TestClient, monkeypatch):
        """WebSocket from a configured origin should connect."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "https://app.example.com")

        # Should connect (accept the WebSocket)
        with ws_client.websocket_connect(
            "/ws/owner/repo/issues/42/my-workflow",
            headers={"origin": "https://app.example.com"},
        ) as _ws:  # noqa: F841
            pass

    def test_cswsh_wildcard_allows_any_origin(self, ws_client: TestClient, monkeypatch):
        """ALLOWED_ORIGINS='*' allows any origin."""
        monkeypatch.setenv("ALLOWED_ORIGINS", "*")

        with ws_client.websocket_connect(
            "/ws/owner/repo/issues/42/my-workflow",
            headers={"origin": "https://anything.example.com"},
        ) as _ws:  # noqa: F841
            pass

    # ── History replay ───────────────────────────────────────────────────

    def test_connect_receives_history(
        self, ws_client: TestClient, mock_session_store_v2: MagicMock
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(
            return_value=[
                {"type": "user", "message": {"role": "user", "content": "Hello"}},
                {
                    "type": "assistant",
                    "message": {"role": "assistant", "content": "Hi!"},
                },
            ]
        )

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws:
            received: list[dict] = []
            for _ in range(10):
                msg = ws.receive_json()
                received.append(msg)

        history_msgs = [m for m in received if m.get("type") in ("user", "assistant")]
        assert len(history_msgs) == 2, f"Expected 2 history messages, got {received}"
        assert history_msgs[0]["type"] == "user"
        assert history_msgs[0]["message"]["content"] == "Hello"

    def test_connect_no_history_receives_meta_only(
        self, ws_client: TestClient, mock_session_store_v2: MagicMock
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=[])

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws:
            received: list[dict] = []
            for _ in range(10):
                msg = ws.receive_json()
                received.append(msg)

        meta_types = {m.get("type") for m in received}
        assert "session_meta" in meta_types, f"Expected session_meta in {received}"

    # ── Live message forwarding ──────────────────────────────────────────

    def test_receives_live_messages_via_pubsub(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=[])

        live_msg = {
            "type": "stream_event",
            "data": {"event": "thinking", "session_id": "ses_abc"},
            "ts": "2026-06-09T10:00:00Z",
        }

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([live_msg]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws:
            received: list[dict] = []
            for _ in range(20):
                msg = ws.receive_json()
                received.append(msg)

        stream_msgs = [m for m in received if m.get("type") == "stream_event"]
        assert len(stream_msgs) >= 1, f"Expected stream_event, got {received}"
        assert stream_msgs[0]["data"]["event"] == "thinking"

    # ── Inject message ───────────────────────────────────────────────────

    def test_send_inject_message_to_redis(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=[])

        mock_redis.publish = AsyncMock(return_value=1)

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws:
            ws.send_json({"type": "inject_message", "content": "Please fix the bug"})
            import time

            time.sleep(0.3)

        publish_calls = [
            c
            for c in mock_redis.publish.call_args_list
            if c.args and "session:ctl:" in str(c.args[0])
        ]
        assert (
            len(publish_calls) >= 1
        ), f"Expected publish to ctl, got {mock_redis.publish.call_args_list}"

    def test_send_inject_message_stores_in_inbox(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=[])

        mock_redis.publish = AsyncMock(return_value=1)
        mock_redis.rpush = AsyncMock(return_value=1)
        mock_redis.expire = AsyncMock(return_value=True)

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws:
            ws.send_json({"type": "inject_message", "content": "Inbox test"})
            import time

            time.sleep(0.3)

        inbox_calls = [
            c
            for c in mock_redis.rpush.call_args_list
            if c.args and "session:history:" in str(c.args[0])
        ]
        assert (
            len(inbox_calls) >= 1
        ), f"Expected rpush to history, got {mock_redis.rpush.call_args_list}"

    # ── Subscriber tracking ──────────────────────────────────────────────

    def test_connect_increments_subscribers(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=[])

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect(
            "/ws/owner/repo/issues/42/my-workflow"
        ) as ws:  # noqa: F841
            import time

            time.sleep(0.2)

        store.increment_subscribers.assert_called()
        assert store.increment_subscribers.call_count >= 1

    def test_disconnect_decrements_subscribers(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        store = mock_session_store_v2
        store.decrement_subscribers = AsyncMock(return_value=0)
        store.get_history = AsyncMock(return_value=[])

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect(
            "/ws/owner/repo/issues/42/my-workflow"
        ) as ws:  # noqa: F841
            import time

            time.sleep(0.5)

        store.decrement_subscribers.assert_called()
        assert store.decrement_subscribers.call_count >= 1

    # ── Reconnection ──────────────────────────────────────────────────────

    def test_reconnect_still_gets_history(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
        mock_redis: MagicMock,
    ):
        history_data = [
            {"type": "user", "message": {"role": "user", "content": "First msg"}},
        ]
        store = mock_session_store_v2
        store.get_history = AsyncMock(return_value=history_data)

        pubsub_mock = MagicMock()
        pubsub_mock.subscribe = AsyncMock(return_value=None)
        pubsub_mock.unsubscribe = AsyncMock(return_value=None)
        pubsub_mock.close = AsyncMock(return_value=None)
        pubsub_mock.listen = MagicMock(side_effect=lambda: _async_listen([]))
        mock_redis.pubsub = MagicMock(return_value=pubsub_mock)

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws1:
            received1 = [ws1.receive_json() for _ in range(5)]

            import time

            time.sleep(0.5)

        with ws_client.websocket_connect("/ws/owner/repo/issues/42/my-workflow") as ws2:
            received2 = [ws2.receive_json() for _ in range(5)]

        assert len(received1) >= 1
        assert len(received2) >= 1

    # ── Error cases ──────────────────────────────────────────────────────

    def test_invalid_issue_number_closes_with_4400(self, ws_client: TestClient):
        """Non-integer issue number → close code 4400."""
        with pytest.raises(Exception):  # noqa: B017
            with ws_client.websocket_connect(
                "/ws/owner/repo/issues/not-a-number/my-workflow"
            ) as ws:
                ws.receive_text()

    def test_session_not_found_closes_with_4404(
        self,
        ws_client: TestClient,
        mock_session_store_v2: MagicMock,
    ):
        """No matching session → close code 4404."""
        store = mock_session_store_v2
        store.find_session = AsyncMock(return_value=None)

        with pytest.raises(Exception):  # noqa: B017
            with ws_client.websocket_connect("/ws/owner/repo/issues/99999/no-wf") as ws:
                ws.receive_text()


# ---------------------------------------------------------------------------
# Pub/sub bridge tests — TDD RED phase (Task 14)
# ---------------------------------------------------------------------------


def _make_listen_gen(
    relay: asyncio.Queue | None = None,
    *,
    channels: list[str] | None = None,
    preload: list[dict] | None = None,
    error_after_subscribe: BaseException | None = None,
) -> MagicMock:
    """Build a mock pubsub.listen() side-effect via an asyncio relay queue.

    The returned ``listen()`` mock yields:

    1. ``subscribe`` confirmations for each channel.
    2. Preloaded messages (if any).
    3. Then pulls from *relay* (blocking) — publish calls push here.
    4. Periodic heartbeat entries so the ``async for`` loop can check
       cancellation / shutdown between publishes.
    """
    import asyncio

    _relay = relay or asyncio.Queue()

    async def _gen():
        for ch_name in channels or []:
            ch_bytes = ch_name.encode() if isinstance(ch_name, str) else ch_name
            yield {"type": "subscribe", "channel": ch_bytes, "data": 1}
        if error_after_subscribe is not None:
            raise error_after_subscribe
        for msg in preload or []:
            yield msg
        while True:
            try:
                msg = await asyncio.wait_for(_relay.get(), timeout=0.05)
                yield msg
            except TimeoutError:
                yield {
                    "type": "message",
                    "channel": b"__hb__",
                    "data": '{"type":"noop"}',
                }

    return MagicMock(side_effect=lambda: _gen())


class TestPubSubBridge:
    """Pub/sub bridge tests — TDD RED phase (Task 14).

    Verifies:
      - subscribe → handler receives published message
      - unsubscribe → handler stops receiving
      - Redis disconnect → auto-reconnect with exponential backoff
      - Multiple subscribers → all receive same message
    """

    @pytest.mark.asyncio
    async def test_subscribe_receives_published_message(self):
        """``subscribe(channel, handler)`` → handler fires on published message."""
        import asyncio

        from services.session_service.bridge import SessionBridge  # noqa: E402

        redis = MagicMock()
        relay: asyncio.Queue = asyncio.Queue()

        redis.publish = AsyncMock(
            side_effect=lambda ch, msg: relay.put_nowait(
                {
                    "type": "message",
                    "channel": ch.encode() if isinstance(ch, str) else ch,
                    "data": msg,
                }
            )
        )

        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        pubsub.listen = _make_listen_gen(relay, channels=["test-ch"])
        redis.pubsub = MagicMock(return_value=pubsub)

        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        bridge = SessionBridge(redis)
        await bridge.subscribe("test-ch", handler)
        await asyncio.sleep(0.05)

        assert len(received) == 0, "no messages before publish"

        await bridge.publish("test-ch", "hello-world")
        await asyncio.sleep(0.15)

        assert len(received) >= 1, f"expected ≥1 received, got {len(received)}"
        assert received[0]["data"] == "hello-world"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_unsubscribe_stops_receiving(self):
        """``unsubscribe(channel)`` → handler no longer fires on publish."""
        import asyncio

        from services.session_service.bridge import SessionBridge  # noqa: E402

        redis = MagicMock()
        relay: asyncio.Queue = asyncio.Queue()

        redis.publish = AsyncMock(
            side_effect=lambda ch, msg: relay.put_nowait(
                {
                    "type": "message",
                    "channel": ch.encode() if isinstance(ch, str) else ch,
                    "data": msg,
                }
            )
        )

        pubsub_1st = MagicMock()
        pubsub_1st.subscribe = AsyncMock()
        pubsub_1st.unsubscribe = AsyncMock()
        pubsub_1st.aclose = AsyncMock()
        pubsub_1st.listen = _make_listen_gen(relay, channels=["ch-a"])

        pubsub_after = MagicMock()
        pubsub_after.subscribe = AsyncMock()
        pubsub_after.unsubscribe = AsyncMock()
        pubsub_after.aclose = AsyncMock()
        pubsub_after.listen = _make_listen_gen(relay, channels=[])

        redis.pubsub = MagicMock(side_effect=[pubsub_1st, pubsub_after])

        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        bridge = SessionBridge(redis)
        await bridge.subscribe("ch-a", handler)
        await asyncio.sleep(0.05)

        await bridge.publish("ch-a", "before-unsub")
        await asyncio.sleep(0.1)
        assert len(received) == 1

        await bridge.unsubscribe("ch-a")
        await asyncio.sleep(0.1)

        count_before = len(received)
        await bridge.publish("ch-a", "after-unsub")
        await asyncio.sleep(0.15)

        assert len(received) == count_before, (
            f"Received {len(received) - count_before} messages after unsubscribe: "
            f"{received[count_before:]}"
        )

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_redis_disconnect_auto_reconnect(self):
        """Redis pub/sub disconnect → auto-reconnect within ~5 s, messages flow again."""
        import asyncio

        from services.session_service.bridge import SessionBridge  # noqa: E402

        redis = MagicMock()
        relay: asyncio.Queue = asyncio.Queue()

        redis.publish = AsyncMock(
            side_effect=lambda ch, msg: relay.put_nowait(
                {
                    "type": "message",
                    "channel": ch.encode() if isinstance(ch, str) else ch,
                    "data": msg,
                }
            )
        )

        # First pubsub fails after subscribe (simulates disconnect)
        first_pubsub = MagicMock()
        first_pubsub.subscribe = AsyncMock()
        first_pubsub.unsubscribe = AsyncMock()
        first_pubsub.aclose = AsyncMock()
        first_pubsub.listen = _make_listen_gen(
            relay,
            channels=["ch1"],
            error_after_subscribe=ConnectionError("pubsub lost"),
        )

        # Second pubsub is healthy, delivers messages
        second_pubsub = MagicMock()
        second_pubsub.subscribe = AsyncMock()
        second_pubsub.unsubscribe = AsyncMock()
        second_pubsub.aclose = AsyncMock()
        second_pubsub.listen = _make_listen_gen(relay, channels=["ch1"])

        redis.pubsub = MagicMock(side_effect=[first_pubsub, second_pubsub])

        received: list[dict] = []

        async def handler(msg: dict) -> None:
            received.append(msg)

        bridge = SessionBridge(redis)
        await bridge.subscribe("ch1", handler)

        # Wait for reconnect (first backoff = 1 s, plus spin-up)
        await asyncio.sleep(1.5)

        await bridge.publish("ch1", "after-reconnect")
        await asyncio.sleep(0.2)

        assert (
            len(received) >= 1
        ), f"Expected >=1 message after reconnect, got {len(received)}"
        assert received[0]["data"] == "after-reconnect"

        await bridge.stop()

    @pytest.mark.asyncio
    async def test_multiple_subscribers_all_receive(self):
        """2+ subscribers on same channel → both receive identical message."""
        import asyncio

        from services.session_service.bridge import SessionBridge  # noqa: E402

        redis = MagicMock()
        relay: asyncio.Queue = asyncio.Queue()

        redis.publish = AsyncMock(
            side_effect=lambda ch, msg: relay.put_nowait(
                {
                    "type": "message",
                    "channel": ch.encode() if isinstance(ch, str) else ch,
                    "data": msg,
                }
            )
        )

        pubsub = MagicMock()
        pubsub.subscribe = AsyncMock()
        pubsub.unsubscribe = AsyncMock()
        pubsub.aclose = AsyncMock()
        pubsub.listen = _make_listen_gen(relay, channels=["broadcast"])
        redis.pubsub = MagicMock(return_value=pubsub)

        received_a: list[dict] = []
        received_b: list[dict] = []

        async def handler_a(msg: dict) -> None:
            received_a.append(msg)

        async def handler_b(msg: dict) -> None:
            received_b.append(msg)

        bridge = SessionBridge(redis)
        await bridge.subscribe("broadcast", handler_a)
        await bridge.subscribe("broadcast", handler_b)
        await asyncio.sleep(0.05)

        await bridge.publish("broadcast", "fan-out")
        await asyncio.sleep(0.15)

        assert len(received_a) >= 1, f"handler-a got {len(received_a)} messages"
        assert len(received_b) >= 1, f"handler-b got {len(received_b)} messages"
        assert received_a[0]["data"] == "fan-out"
        assert received_b[0]["data"] == "fan-out"

        await bridge.stop()


# ============================================================================
# TestResumeJobs — Task 15: Resume job + inbox management TDD
# ============================================================================


# ── Helpers ───────────────────────────────────────────────────────────


def _make_resume_mock_store():
    """Create a mock SessionStore for resume job testing."""
    store = MagicMock(spec=SessionStore)
    store.get_streaming_session = AsyncMock()
    store.set_running = AsyncMock()
    store.increment_run_count = AsyncMock(return_value=2)
    store.pop_inbox_messages = AsyncMock(return_value=[])
    store.push_inbox_message = AsyncMock()
    return store


def _make_resume_redis():
    """Create a mock Redis client for resume job testing."""
    r = MagicMock()
    r.setex = AsyncMock(return_value=True)
    r.setnx = AsyncMock(return_value=1)
    r.rpush = AsyncMock(return_value=1)
    r.publish = AsyncMock(return_value=1)
    r.expire = AsyncMock(return_value=True)
    r.hset = AsyncMock(return_value=1)
    r.delete = AsyncMock(return_value=1)
    return r


def _mock_session_hash(status="completed", **overrides):
    """Build a UnifiedSessionInfo with the given status and optional overrides."""
    base = {
        "session_id": "ses_abc123",
        "repo": "owner/repo",
        "thread_type": "issue",
        "thread_id": "42",
        "workflow_name": "test-wf",
        "ref": "main",
        "worktree_path": "/tmp/wt",
        "created_at": "2026-01-01T00:00:00Z",
        "last_run": "2026-01-01T00:00:00Z",
        "status": status,
        "installation_id": "inst-1",
        "initial_query": "hello",
        "conversation_config": '{"persist":true,"ttl_hours":72}',
        "session_proxy_url": "http://x",
        "issue_number": "42",
        "user": "testuser",
    }
    base.update(overrides)
    return UnifiedSessionInfo.model_validate(base)


class TestResumeJobs:
    """Resume job creation when a user messages a completed session — Task 15 TDD.

    Implements the resume flow ported from session_proxy/_handle_resume_message():
      - completed/error session + user message → resume job
      - session status transitions to running
      - run_count incremented
      - inbox drained atomically
      - duplicate resume prevention via lock
    """

    # ── Core resume flow ───────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_completed_session_creates_resume_job(self):
        """Session status=completed, user message → resume job enqueued."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        store.pop_inbox_messages.return_value = ["fix this bug please"]

        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-01",
            message="fix this bug please",
            store=store,
            redis=r,
        )

        assert result is not None, "Expected resume job to be created"
        assert result.get("session_mode") == "resume"
        assert result.get("session_token") == "tok-resume-01"

        r.setex.assert_called()
        assert any(
            PENDING_JOB_QUEUE in str(c.args[0]) for c in r.rpush.call_args_list
        ), f"Expected rpush to pending queue, got {r.rpush.call_args_list}"

    @pytest.mark.asyncio
    async def test_resume_transitions_session_to_running(self):
        """Session completed → set_running called on resume."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        await handle_resume(
            token="tok-resume-02",
            message="continue please",
            store=store,
            redis=r,
        )

        store.set_running.assert_awaited_once_with("tok-resume-02", ttl_seconds=2592000)

    @pytest.mark.asyncio
    async def test_resume_increments_run_count(self):
        """Session completed → run_count incremented on resume."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        store.increment_run_count.return_value = 3
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-03",
            message="run again",
            store=store,
            redis=r,
        )

        store.increment_run_count.assert_awaited_once_with("tok-resume-03")
        assert result is not None

    @pytest.mark.asyncio
    async def test_resume_drains_inbox_atomically(self):
        """Inbox messages drained atomically during resume."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        store.pop_inbox_messages.return_value = [
            "msg1: hello",
            "msg2: please fix",
            "msg3: also this",
        ]
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        await handle_resume(
            token="tok-resume-04",
            message="resume me",
            store=store,
            redis=r,
        )

        store.pop_inbox_messages.assert_awaited_once_with("tok-resume-04")

    # ── Duplicate prevention ───────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_duplicate_resume_no_duplicate_job(self):
        """Already-pending resume → no duplicate job created."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        r = _make_resume_redis()
        r.setnx.return_value = 0

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-05",
            message="resume again",
            store=store,
            redis=r,
        )

        assert result is None, "Expected None on duplicate resume attempt"
        r.rpush.assert_not_called()
        r.setex.assert_not_called()
        store.set_running.assert_not_awaited()
        store.increment_run_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_running_session_no_resume(self):
        """Session already running → no resume (idempotent)."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("running")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-06",
            message="hello",
            store=store,
            redis=r,
        )

        assert result is None
        store.set_running.assert_not_awaited()
        store.increment_run_count.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_session_creates_resume_job(self):
        """Session status=error → resume job created (same as completed)."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("error")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-07",
            message="retry after error",
            store=store,
            redis=r,
        )

        assert result is not None
        assert result.get("session_mode") == "resume"
        store.set_running.assert_awaited_once_with("tok-resume-07", ttl_seconds=2592000)
        store.increment_run_count.assert_awaited_once_with("tok-resume-07")

    # ── Job data integrity ─────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_resume_job_data_includes_session_id(self):
        """Resume job includes original session_id from the completed session."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _mock_session_hash(
            "completed", session_id="ses_original_run_42"
        )
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-08",
            message="continue",
            store=store,
            redis=r,
        )

        assert result is not None
        assert result.get("session_id") == "ses_original_run_42"

    @pytest.mark.asyncio
    async def test_resume_job_has_correct_mode_and_token(self):
        """Resume job has session_mode='resume' and includes session_token."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-09",
            message="mode check",
            store=store,
            redis=r,
        )

        assert result is not None
        assert result.get("session_mode") == "resume"
        assert result.get("session_token") == "tok-resume-09"
        assert result.get("prompt") == "mode check"

    @pytest.mark.asyncio
    async def test_resume_job_data_includes_all_required_fields(self):
        """Resume job contains all fields needed by sandbox_worker."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _mock_session_hash(
            "completed",
            session_id="ses_full_001",
            repo="my-org/my-repo",
            thread_type="pr",
            thread_id="99",
            issue_number="99",
            workflow_name="review-pr",
            ref="feature/x",
            user="dev42",
            installation_id="inst-999",
            conversation_config='{"persist":true,"ttl_hours":48}',
        )
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-resume-10",
            message="full suite test",
            store=store,
            redis=r,
        )

        assert result is not None
        assert result.get("repo") == "my-org/my-repo"
        assert result.get("issue_number") == 99
        assert result.get("prompt") == "full suite test"
        assert result.get("user") == "dev42"
        assert result.get("workflow_name") == "review-pr"
        assert result.get("ref") == "feature/x"
        assert result.get("session_mode") == "resume"
        assert result.get("session_id") == "ses_full_001"
        assert result.get("session_token") == "tok-resume-10"
        assert result.get("streaming_enabled") is True
        assert result.get("installation_id") == "inst-999"
        assert result.get("thread_type") == "pr"
        assert result.get("thread_id") == "99"
        assert result.get("conversation_config") == {"persist": True, "ttl_hours": 48}
        assert result.get("user_query") == "full suite test"
        assert "event_data" in result
        assert result["event_data"]["event_type"] == "remote_control"

    # ── Edge cases ────────────────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_session_not_found_returns_none(self):
        """Non-existent session → None (no crash, no job)."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = None
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-nonexistent",
            message="hello",
            store=store,
            redis=r,
        )

        assert result is None
        store.set_running.assert_not_awaited()
        r.rpush.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_session_returns_none(self):
        """Session expired → no resume."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("expired")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-expired",
            message="try resume",
            store=store,
            redis=r,
        )

        assert result is None
        store.set_running.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_session_returns_none(self):
        """Session status=active → no resume (needs transition_to_running first)."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("active")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        result = await handle_resume(
            token="tok-active",
            message="go",
            store=store,
            redis=r,
        )

        assert result is None
        store.set_running.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_publishes_message_to_session_channel(self):
        """User message published to MSG_CHANNEL during resume."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        await handle_resume(
            token="tok-publish-01",
            message="broadcast this",
            store=store,
            redis=r,
        )

        publish_calls = [
            c
            for c in r.publish.call_args_list
            if c.args and "session:msg:" in str(c.args[0])
        ]
        assert len(publish_calls) >= 1

    @pytest.mark.asyncio
    async def test_resume_lock_released_on_success(self):
        """Resume lock released (deleted) after successful job creation."""
        store = _make_resume_mock_store()
        store.get_streaming_session.return_value = _session_with_status("completed")
        r = _make_resume_redis()

        from services.session_service.resume import handle_resume

        await handle_resume(
            token="tok-lock-rel",
            message="release me",
            store=store,
            redis=r,
        )

        r.setnx.assert_called()
        r.delete.assert_called()
