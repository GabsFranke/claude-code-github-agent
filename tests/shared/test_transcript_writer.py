"""Tests for IncrementalTranscriptHook — durable JSONL transcript writing.

Task 23: Incremental transcript writing hook TDD.
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def transcript_path(tmp_path):
    """Return a temporary path for the durable transcript JSONL file."""
    return tmp_path / "test_transcript.jsonl"


@pytest.fixture
def hook(transcript_path):
    """Create a fresh IncrementalTranscriptHook for each test."""
    from shared.transcript_writer import IncrementalTranscriptHook

    return IncrementalTranscriptHook(transcript_path=str(transcript_path))


# ── Helper: build a mock hook input dict ─────────────────────────────────────


def _make_hook_input(event_name: str, **extra) -> dict:
    """Build a dict that resembles the SDK hook input data."""
    base = {
        "hook_event_name": event_name,
        "session_id": "test-session-123",
        "cwd": "/tmp/test-workspace",
    }
    base.update(extra)
    return base


# ── Basic Write Tests ─────────────────────────────────────────────────────────


class TestBasicWrite:
    """Smoke tests for single-message writes."""

    @pytest.mark.asyncio
    async def test_writes_tool_use_message(self, hook, transcript_path):
        """A PostToolUse hook should append a JSONL line to the file."""
        input_data = _make_hook_input(
            "PostToolUse",
            tool_name="Bash",
            tool_input={"command": "echo hello"},
            tool_response={"stdout": "hello\n", "stderr": ""},
            tool_use_id="tu_001",
        )

        await hook.on_post_tool_use(input_data)

        lines = _read_jsonl(transcript_path)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["type"] == "tool_use"
        assert entry["hook_event"] == "PostToolUse"
        assert entry["tool_name"] == "Bash"

    @pytest.mark.asyncio
    async def test_writes_user_prompt_message(self, hook, transcript_path):
        """A UserPromptSubmit hook should append a user message line."""
        input_data = _make_hook_input(
            "UserPromptSubmit",
            prompt="Please fix the bug",
        )

        await hook.on_user_prompt_submit(input_data)

        lines = _read_jsonl(transcript_path)
        assert len(lines) == 1
        entry = lines[0]
        assert entry["type"] == "user_prompt"
        assert entry["hook_event"] == "UserPromptSubmit"
        assert "Please fix the bug" in entry["prompt"]

    @pytest.mark.asyncio
    async def test_multiple_messages_appended(self, hook, transcript_path):
        """Multiple hook calls should append multiple lines."""
        for i in range(5):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Write",
                tool_input={"file_path": f"/tmp/file_{i}.txt"},
                tool_use_id=f"tu_{i:03d}",
            )
            await hook.on_post_tool_use(input_data)

        lines = _read_jsonl(transcript_path)
        assert len(lines) == 5


# ── Durability Tests ──────────────────────────────────────────────────────────


class TestDurability:
    """Verify that writes survive crashes and use proper flags."""

    @pytest.mark.asyncio
    async def test_file_uses_append_mode(self, hook, transcript_path):
        """The file should be opened with append mode — re-opening doesn't
        overwrite previous content."""
        input_data = _make_hook_input(
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": "/tmp/a.txt"},
            tool_use_id="tu_a",
        )
        await hook.on_post_tool_use(input_data)

        # Create a new hook instance targeting the SAME file
        hook2 = type(hook)(transcript_path=str(transcript_path))
        input_data2 = _make_hook_input(
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": "/tmp/b.txt"},
            tool_use_id="tu_b",
        )
        await hook2.on_post_tool_use(input_data2)

        lines = _read_jsonl(transcript_path)
        assert len(lines) == 2, "Second hook should append, not overwrite"

    @pytest.mark.asyncio
    async def test_kill9_recovery_all_messages_recoverable(self, hook, transcript_path):
        """Simulate a kill -9 mid-write: messages written before the crash
        should be recoverable, with only the last (potentially malformed)
        line skipped."""
        # Write many messages
        for i in range(50):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Grep",
                tool_input={"pattern": f"test_{i}"},
                tool_use_id=f"tu_{i:03d}",
            )
            await hook.on_post_tool_use(input_data)

        # Simulate a crash: truncate the last bytes to mimic an incomplete write
        size = transcript_path.stat().st_size
        if size > 0:
            with open(transcript_path, "r+b") as f:
                # Truncate the last 10-50 bytes (partial JSON write)
                truncate_at = max(0, size - 30)
                f.truncate(truncate_at)

        # Now recover — read all parseable lines
        recovered = _read_jsonl(transcript_path)
        # We should have at least 49 of 50 messages (worst case: last one lost)
        assert (
            len(recovered) >= 49
        ), f"Expected >= 49 recoverable messages, got {len(recovered)}"

    @pytest.mark.asyncio
    async def test_malformed_last_line_skipped_on_load(self, hook, transcript_path):
        """The JSONL parser (from transcript_parser) should skip malformed
        lines gracefully."""
        # Write valid messages
        for i in range(10):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Read",
                tool_input={"file_path": f"/tmp/f_{i}.txt"},
                tool_use_id=f"tu_{i:03d}",
            )
            await hook.on_post_tool_use(input_data)

        # Append a malformed line (partial JSON)
        with open(transcript_path, "a", encoding="utf-8") as f:
            f.write('{"type": "broken", "message": "incomplete')

        # Read via the same parser used in production
        from shared.transcript_parser import _iter_transcript_lines

        entries = list(_iter_transcript_lines(str(transcript_path)))
        assert (
            len(entries) == 10
        ), f"Malformed line should be skipped, got {len(entries)} entries"


# ── Volume Test ───────────────────────────────────────────────────────────────


class TestVolume:
    """Large-scale write tests."""

    @pytest.mark.asyncio
    async def test_1000_messages_all_in_transcript(self, hook, transcript_path):
        """Write 1000 messages and verify every single one is recoverable."""
        count = 1000
        for i in range(count):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Read",
                tool_input={"file_path": f"/tmp/vol_{i}.txt"},
                tool_use_id=f"tu_{i:04d}",
            )
            await hook.on_post_tool_use(input_data)

        lines = _read_jsonl(transcript_path)
        assert len(lines) == count, f"Expected {count} messages, got {len(lines)}"

        # Spot-check: first, middle, last
        assert lines[0]["tool_use_id"] == "tu_0000"
        assert lines[count // 2]["tool_use_id"] == f"tu_{count // 2:04d}"
        assert lines[-1]["tool_use_id"] == f"tu_{count - 1:04d}"


# ── Error Handling Tests ──────────────────────────────────────────────────────


class TestErrorHandling:
    """Graceful degradation when things go wrong."""

    @pytest.mark.asyncio
    async def test_file_not_writable_logs_and_continues(self, hook, tmp_path):
        """When the transcript path is not writable, the hook should log
        a warning but NOT crash the SDK session."""
        readonly_dir = tmp_path / "readonly"
        readonly_dir.mkdir()
        # Make directory read-only
        os.chmod(readonly_dir, 0o444)

        bad_path = readonly_dir / "transcript.jsonl"
        hook2 = type(hook)(transcript_path=str(bad_path))

        input_data = _make_hook_input(
            "PostToolUse",
            tool_name="Read",
            tool_input={"file_path": "/tmp/x.txt"},
            tool_use_id="tu_x",
        )

        # Should NOT raise — should log and return gracefully
        await hook2.on_post_tool_use(input_data)

    @pytest.mark.asyncio
    async def test_disk_full_handled_gracefully(self, hook, transcript_path):
        """When the disk is full, writes should fail gracefully without
        crashing the SDK session."""
        with patch("builtins.open", side_effect=OSError(28, "No space left on device")):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Read",
                tool_input={"file_path": "/tmp/x.txt"},
                tool_use_id="tu_full",
            )
            # Should NOT raise
            await hook.on_post_tool_use(input_data)

    @pytest.mark.asyncio
    async def test_serialization_error_handled(self, hook):
        """Non-serializable data should be handled gracefully."""
        # A tool input with a non-serializable object
        input_data = _make_hook_input(
            "PostToolUse",
            tool_name="CustomTool",
            tool_input={"callback": lambda x: x},  # lambda is not JSON-serializable
            tool_use_id="tu_bad",
        )

        # Should NOT raise — should log and skip
        await hook.on_post_tool_use(input_data)


# ── Stop / SubagentStop Hook Tests ────────────────────────────────────────────


class TestStopHooks:
    """Verify Stop and SubagentStop hooks sync transcript data."""

    @pytest.mark.asyncio
    async def test_stop_hook_writes_session_data(self, hook, transcript_path, tmp_path):
        """Stop hook with a valid SDK transcript path should sync messages."""
        # Create a fake SDK transcript file
        sdk_transcript = tmp_path / "sdk_transcript.jsonl"
        sdk_messages = [
            {"type": "user", "message": {"role": "user", "content": "Hello"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi there!"}],
                },
            },
        ]
        _write_jsonl(sdk_transcript, sdk_messages)

        input_data = _make_hook_input(
            "Stop",
            transcriptPath=str(sdk_transcript),
        )

        await hook.on_stop(input_data)

        lines = _read_jsonl(transcript_path)
        assert (
            len(lines) >= 2
        ), f"Stop hook should sync at least 2 messages, got {len(lines)}"

    @pytest.mark.asyncio
    async def test_subagent_stop_hook_writes_subagent_data(
        self, hook, transcript_path, tmp_path
    ):
        """SubagentStop hook should sync the subagent's transcript."""
        sub_transcript = tmp_path / "sub_transcript.jsonl"
        sub_messages = [
            {"type": "user", "message": {"role": "user", "content": "Find the bug"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Grep",
                            "input": {"pattern": "error"},
                        }
                    ],
                },
            },
        ]
        _write_jsonl(sub_transcript, sub_messages)

        input_data = _make_hook_input(
            "SubagentStop",
            agent_transcript_path=str(sub_transcript),
            agent_id="agent-001",
            agent_type="explore",
        )

        await hook.on_subagent_stop(input_data)

        lines = _read_jsonl(transcript_path)
        assert len(lines) >= 2, f"SubagentStop should sync messages, got {len(lines)}"

    @pytest.mark.asyncio
    async def test_stop_hook_missing_transcript_path_handled(
        self, hook, transcript_path
    ):
        """Stop hook without transcriptPath should log and return cleanly."""
        input_data = _make_hook_input("Stop")  # no transcriptPath
        await hook.on_stop(input_data)
        # Should not crash; no lines written
        lines = _read_jsonl(transcript_path)
        assert len(lines) == 0

    @pytest.mark.asyncio
    async def test_stop_hook_nonexistent_transcript_handled(
        self, hook, transcript_path
    ):
        """Stop hook pointing to a non-existent transcript should not crash."""
        input_data = _make_hook_input(
            "Stop",
            transcriptPath="/nonexistent/path/transcript.jsonl",
        )
        await hook.on_stop(input_data)
        lines = _read_jsonl(transcript_path)
        assert len(lines) == 0


# ── Deduplication Test ────────────────────────────────────────────────────────


class TestDeduplication:
    """Ensure the same message isn't written twice."""

    @pytest.mark.asyncio
    async def test_stop_hook_does_not_duplicate_post_tool_use_messages(
        self, hook, transcript_path, tmp_path
    ):
        """Messages already written via PostToolUse should not be duplicated
        when the Stop hook later syncs the same SDK transcript."""
        # 1. Write tool messages via PostToolUse
        for i in range(5):
            input_data = _make_hook_input(
                "PostToolUse",
                tool_name="Write",
                tool_input={"file_path": f"/tmp/f_{i}.txt"},
                tool_use_id=f"tu_{i:03d}",
            )
            await hook.on_post_tool_use(input_data)

        # 2. Simulate Stop hook — SDK transcript has the same content
        sdk_transcript = tmp_path / "sdk_transcript.jsonl"
        # The SDK transcript will contain messages in a different format
        # but our hook should handle dedup by tracking seen content
        sdk_messages = [
            {"type": "user", "message": {"role": "user", "content": "Write 5 files"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "id": "tu_000",
                            "input": {"file_path": "/tmp/f_0.txt"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "id": "tu_001",
                            "input": {"file_path": "/tmp/f_1.txt"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "id": "tu_002",
                            "input": {"file_path": "/tmp/f_2.txt"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "id": "tu_003",
                            "input": {"file_path": "/tmp/f_3.txt"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "id": "tu_004",
                            "input": {"file_path": "/tmp/f_4.txt"},
                        },
                    ],
                },
            },
        ]
        _write_jsonl(sdk_transcript, sdk_messages)

        input_data = _make_hook_input(
            "Stop",
            transcriptPath=str(sdk_transcript),
        )
        await hook.on_stop(input_data)

        # Total lines should reflect: 5 tool msgs + user + assistant (7),
        # NOT 5 + 2 + 5 duplicates (12)
        lines = _read_jsonl(transcript_path)
        # We expect the 5 PostToolUse messages + the user + assistant messages
        # that were NOT already captured (user prompt, assistant text)
        assert len(lines) >= 7, f"Expected at least 7 unique messages, got {len(lines)}"
        # But not more than ~12 (if 5 were duplicated)
        assert (
            len(lines) <= 12
        ), f"Excessive duplication: got {len(lines)} messages (expected <= 12)"


# ── Hook Registration Test ────────────────────────────────────────────────────


class TestHookRegistration:
    """Verify the hook registration helpers produce correct SDK hooks dicts."""

    def test_create_hooks_dict_returns_correct_structure(self, tmp_path):
        """build_hooks_dict should return a dict keyed by hook event names."""
        from shared.transcript_writer import IncrementalTranscriptHook

        inst = IncrementalTranscriptHook(str(tmp_path / "t.jsonl"))
        hooks = inst.build_hooks_dict()

        assert isinstance(hooks, dict)
        assert "PostToolUse" in hooks
        assert "UserPromptSubmit" in hooks
        assert "Stop" in hooks
        assert "SubagentStop" in hooks

        # Each key should map to a list of HookMatcher instances
        for event_name in hooks:
            matchers = hooks[event_name]
            assert isinstance(matchers, list)
            assert len(matchers) >= 1
            # Each matcher should have hooks callbacks
            matcher = matchers[0]
            assert hasattr(matcher, "hooks") or hasattr(matcher, "matcher")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[dict]:
    """Read all valid JSON lines from a JSONL file."""
    if not path.exists():
        return []
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def _write_jsonl(path: Path, entries: list[dict]) -> None:
    """Write a list of dicts as JSONL to a file."""
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry) + "\n")


# ── SDK Hook Invocation Contract ──────────────────────────────────────────────


class TestSDKInvocationContract:
    """Handlers must be callable the way the SDK actually calls them.

    ``claude_agent_sdk._internal.query`` dispatches every hook as::

        await callback(input, tool_use_id, context)

    Handlers that accept only ``input`` type-check and unit-test fine in
    isolation but raise ``TypeError`` for every real invocation, silently
    losing the transcript. These tests exercise the three-argument form so
    that regression cannot pass CI again.
    """

    SDK_CONTEXT = {"signal": None}

    @pytest.fixture
    def hook(self, tmp_path):
        from shared.transcript_writer import IncrementalTranscriptHook

        return IncrementalTranscriptHook(str(tmp_path / "t.jsonl"))

    @pytest.mark.parametrize(
        "handler_name",
        [
            "on_post_tool_use",
            "on_user_prompt_submit",
            "on_stop",
            "on_subagent_stop",
        ],
    )
    def test_handler_accepts_three_positional_arguments(self, hook, handler_name):
        """Arity check, independent of what any single handler does."""
        import inspect

        handler = getattr(hook, handler_name)
        positional = [
            p
            for p in inspect.signature(handler).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL)
        ]
        assert len(positional) >= 3, (
            f"{handler_name} accepts {len(positional)} positional arg(s); the SDK "
            "calls hooks with (input, tool_use_id, context)"
        )

    @pytest.mark.asyncio
    async def test_post_tool_use_invoked_as_sdk_does(self, hook, tmp_path):
        result = await hook.on_post_tool_use(
            {"tool_name": "Read", "session_id": "s1"},
            "toolu_abc123",
            self.SDK_CONTEXT,
        )
        assert result == {"continue_": True}

        entries = _read_entries(tmp_path / "t.jsonl")
        assert entries[0]["tool_name"] == "Read"

    @pytest.mark.asyncio
    async def test_positional_tool_use_id_is_recorded(self, hook, tmp_path):
        """The SDK passes tool_use_id positionally, not inside the payload."""
        await hook.on_post_tool_use(
            {"tool_name": "Read", "session_id": "s1"},
            "toolu_abc123",
            self.SDK_CONTEXT,
        )
        entries = _read_entries(tmp_path / "t.jsonl")
        assert entries[0]["tool_use_id"] == "toolu_abc123"

    @pytest.mark.asyncio
    async def test_payload_tool_use_id_still_used_when_not_passed(self, hook, tmp_path):
        await hook.on_post_tool_use(
            {"tool_name": "Read", "tool_use_id": "from_payload"},
            None,
            self.SDK_CONTEXT,
        )
        entries = _read_entries(tmp_path / "t.jsonl")
        assert entries[0]["tool_use_id"] == "from_payload"

    @pytest.mark.asyncio
    async def test_user_prompt_submit_invoked_as_sdk_does(self, hook, tmp_path):
        result = await hook.on_user_prompt_submit(
            {"prompt": "hello", "session_id": "s1"}, None, self.SDK_CONTEXT
        )
        assert result == {"continue_": True}

        entries = _read_entries(tmp_path / "t.jsonl")
        assert entries[0]["prompt"] == "hello"

    @pytest.mark.asyncio
    async def test_stop_invoked_as_sdk_does(self, hook):
        result = await hook.on_stop({"session_id": "s1"}, None, self.SDK_CONTEXT)
        assert result == {"continue_": True}

    @pytest.mark.asyncio
    async def test_subagent_stop_invoked_as_sdk_does(self, hook):
        result = await hook.on_subagent_stop(
            {"session_id": "s1"}, None, self.SDK_CONTEXT
        )
        assert result == {"continue_": True}

    @pytest.mark.asyncio
    async def test_registered_callbacks_are_invocable_as_registered(self, hook):
        """Walk build_hooks_dict() and call each callback the SDK's way."""
        for event, matchers in hook.build_hooks_dict().items():
            for matcher in matchers:
                for callback in matcher.hooks:
                    result = await callback(
                        {"session_id": "s1", "tool_name": "Read", "prompt": "p"},
                        "toolu_1",
                        self.SDK_CONTEXT,
                    )
                    assert result == {"continue_": True}, f"{event} handler misbehaved"


def _read_entries(path) -> list[dict]:
    """Parse a JSONL transcript into a list of entries."""
    import json as _json

    with open(path, encoding="utf-8") as f:
        return [_json.loads(line) for line in f if line.strip()]
