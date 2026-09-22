"""A ResultMessage ends a turn, not the run, while background agents run.

Team/autopilot workflows spawn delegated agents that outlive the turn which
started them. The CLI emits a result frame at the turn boundary and later
wakes the parent with a task_notification. Closing the client at the first
result kills those agents mid-flight (observed on review-pr: three lanes
running, nothing posted).

Three orderings are covered:

1. Lanes still running at the result frame -> in-flight set holds the run.
2. Orphaned lanes settle as "stopped" on resume -> a 0-turn result precedes
   the answer to our query.
3. A lane settles between the model's last message and the result frame
   (the Stop hook window) -> bounded grace wait for the continuation turn.
"""

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from shared import sdk_executor
from shared.sdk_executor import _track_task_lifecycle, execute_sdk


@dataclass
class _System:
    subtype: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Result:
    num_turns: int
    duration_ms: int = 10
    is_error: bool = False
    subtype: str = "success"
    session_id: str = "s"


@dataclass
class _Assistant:
    content: list = field(default_factory=list)


@dataclass
class _User:
    """Stands in for the CLI's wake-up user message / stream event."""

    content: str = ""


def _started(task_id, task_type="local_agent"):
    return _System("task_started", {"task_id": task_id, "task_type": task_type})


def _done(task_id, status="completed"):
    return _System("task_notification", {"task_id": task_id, "status": status})


NEVER = _System("never_reached")


@pytest.mark.unit
class TestTrackTaskLifecycle:
    def test_agent_task_is_tracked_until_notification(self):
        inflight: set[str] = set()
        settled: set[str] = set()
        _track_task_lifecycle(_started("t1"), inflight, settled)
        assert inflight == {"t1"} and settled == set()
        _track_task_lifecycle(_done("t1"), inflight, settled)
        assert inflight == set() and settled == {"t1"}

    def test_background_shell_is_not_tracked(self):
        """Shells may never report a terminal status; mirror the SDK."""
        inflight: set[str] = set()
        _track_task_lifecycle(_started("sh", task_type="local_bash"), inflight)
        assert inflight == set()

    @pytest.mark.parametrize("status", ["completed", "failed", "stopped", "killed"])
    def test_task_updated_terminal_patch_clears(self, status):
        inflight = {"t1"}
        msg = _System("task_updated", {"task_id": "t1", "patch": {"status": status}})
        _track_task_lifecycle(msg, inflight)
        assert inflight == set()

    def test_task_updated_non_terminal_patch_keeps(self):
        inflight = {"t1"}
        msg = _System("task_updated", {"task_id": "t1", "patch": {"status": "running"}})
        _track_task_lifecycle(msg, inflight)
        assert inflight == {"t1"}


async def _run(messages, *, hang_after=False, **env):
    """Drive execute_sdk over a scripted stream with real message classes.

    ``hang_after`` keeps the stream open after the last message, the way the
    CLI does while stdin is open, so grace-window timeouts can be exercised.
    Returns the executor result; ``result["messages"]`` is what it processed.
    """
    import asyncio

    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.query = AsyncMock()

    async def stream():
        for m in messages:
            yield m
        if hang_after:
            await asyncio.Event().wait()

    client.receive_messages.return_value = stream()
    options = MagicMock()
    options.model = None
    options.cwd = "/tmp"

    with (
        patch.object(sdk_executor, "ClaudeSDKClient", return_value=client),
        patch.object(sdk_executor, "SystemMessage", _System),
        patch.object(sdk_executor, "ResultMessage", _Result),
        patch.object(sdk_executor, "AssistantMessage", _Assistant),
        patch.dict("os.environ", env),
    ):
        return await execute_sdk(
            prompt="x", options=options, collect_text=False, max_retries=1
        )


@pytest.mark.unit
@pytest.mark.asyncio
class TestResultWithTasksInFlight:
    async def test_waits_for_follow_up_turn(self):
        messages = [
            _started("lane-1"),
            _started("lane-2"),
            _Result(num_turns=5),  # turn boundary, lanes still running
            _done("lane-1"),
            _done("lane-2"),
            _Assistant(),  # continuation turn
            _Result(num_turns=7),  # run-ending result
            NEVER,
        ]
        result = await _run(messages)

        assert result["num_turns"] == 7
        assert NEVER not in result["messages"]

    async def test_no_tasks_breaks_on_first_result(self):
        result = await _run([_Result(num_turns=1), NEVER])

        assert result["num_turns"] == 1
        assert NEVER not in result["messages"]

    async def test_shell_only_does_not_hold_the_run(self):
        messages = [
            _started("sh", task_type="local_bash"),
            _Result(num_turns=1),
            NEVER,
        ]
        result = await _run(messages)

        assert NEVER not in result["messages"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestZeroTurnResult:
    async def test_zero_turn_result_before_output_is_ignored(self):
        """Resume: orphaned tasks settle as stopped, CLI closes an empty turn."""
        messages = [
            _done("old-1", status="stopped"),
            _done("old-2", status="stopped"),
            _System("init", {"model": "m"}),
            _Result(num_turns=0),  # empty continuation turn, not our answer
            _Assistant(),
            _Result(num_turns=1),
            NEVER,
        ]
        result = await _run(messages)

        assert result["num_turns"] == 1
        assert NEVER not in result["messages"]

    async def test_zero_turn_error_result_is_honored(self):
        result = await _run([_Result(num_turns=0, is_error=True), NEVER])

        assert result["is_error"] is True
        assert NEVER not in result["messages"]


@pytest.mark.unit
@pytest.mark.asyncio
class TestContinuationGrace:
    """A task settled during the turn may wake the parent after the result."""

    async def test_holds_and_continues_when_wake_up_arrives(self):
        messages = [
            _started("lane"),
            _Assistant(),  # "waiting on the lane"
            _done("lane"),  # settles inside the Stop hook window
            _Result(num_turns=3),  # result for the turn above
            _User("task notification"),  # CLI wakes the parent
            _Assistant(),  # posts the review
            _Result(num_turns=4),
            NEVER,
        ]
        result = await _run(messages, SDK_CONTINUATION_GRACE_SECONDS="5")

        assert result["num_turns"] == 4
        assert NEVER not in result["messages"]

    async def test_releases_after_grace_when_nothing_follows(self):
        messages = [
            _started("lane"),
            _done("lane"),
            _Result(num_turns=3),
        ]
        result = await _run(
            messages, hang_after=True, SDK_CONTINUATION_GRACE_SECONDS="0.2"
        )

        assert result["num_turns"] == 3

    async def test_post_turn_system_noise_does_not_end_the_hold(self):
        """session_state_changed follows every result; it is not a continuation."""
        messages = [
            _started("lane"),
            _done("lane"),
            _Result(num_turns=3),
            _System("session_state_changed"),
            _User("wake-up"),
            _Assistant(),
            _Result(num_turns=4),
        ]
        result = await _run(messages, SDK_CONTINUATION_GRACE_SECONDS="5")

        assert result["num_turns"] == 4

    async def test_no_hold_without_settled_tasks(self):
        """Plain runs must not pay the grace window."""
        messages = [_Assistant(), _Result(num_turns=1)]
        result = await _run(
            messages, hang_after=True, SDK_CONTINUATION_GRACE_SECONDS="60"
        )

        assert result["num_turns"] == 1
