#!/usr/bin/env python3
"""
Claude Code -> Langfuse hook

"""

import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# --- Langfuse import (fail-open) ---
try:
    from langfuse import Langfuse, propagate_attributes
except Exception:
    sys.exit(0)

# --- Paths ---
STATE_DIR = Path.home() / ".claude" / "state"
LOG_FILE = STATE_DIR / "langfuse_hook.log"
STATE_FILE = STATE_DIR / "langfuse_state.json"
LOCK_FILE = STATE_DIR / "langfuse_state.lock"

DEBUG = os.environ.get("CC_LANGFUSE_DEBUG", "").lower() == "true"
MAX_CHARS = int(os.environ.get("CC_LANGFUSE_MAX_CHARS", "20000"))


# ----------------- Logging -----------------
def _log(level: str, message: str) -> None:
    """Log to file only (hook runs after agent stops, so logs appear post-execution)"""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_line = f"{ts} [{level}] {message}\n"
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_line)
    except Exception:
        # Never block
        pass


def debug(msg: str) -> None:
    if DEBUG:
        _log("DEBUG", msg)


def info(msg: str) -> None:
    _log("INFO", msg)


def warn(msg: str) -> None:
    _log("WARN", msg)


def error(msg: str) -> None:
    _log("ERROR", msg)


# ----------------- State locking (best-effort) -----------------
class FileLock:
    def __init__(self, path: Path, timeout_s: float | None = None):
        self.path = path
        # Allow configurable timeout, default to 10s for parallel workers
        self.timeout_s = timeout_s or float(
            os.getenv("LANGFUSE_LOCK_TIMEOUT_S", "10.0")
        )
        self._fh: Any = None

    def __enter__(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl  # Unix only

            deadline = time.time() + self.timeout_s
            while True:
                try:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
                    break
                except BlockingIOError:
                    if time.time() > deadline:
                        # Log warning when lock timeout is hit
                        print(
                            f"Warning: Failed to acquire lock on {self.path} after {self.timeout_s}s",
                            file=sys.stderr,
                        )
                        break
                    time.sleep(0.05)
        except Exception:
            # If locking isn't available, proceed without it.
            pass
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            import fcntl

            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass


def load_state() -> Dict[str, Any]:
    try:
        if not STATE_FILE.exists():
            return {}
        data: Dict[str, Any] = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data
    except Exception:
        return {}


def save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        debug(f"save_state failed: {e}")


def state_key(session_id: str, transcript_path: str) -> str:
    # stable key even if session_id collides
    raw = f"{session_id}::{transcript_path}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ----------------- Hook payload -----------------
def read_hook_payload() -> Dict[str, Any]:
    """
    Claude Code hooks pass a JSON payload on stdin.
    This script tolerates missing/empty stdin by returning {}.
    """
    try:
        data = sys.stdin.read()
        if not data.strip():
            return {}
        result: Dict[str, Any] = json.loads(data)
        return result
    except Exception:
        return {}


def extract_session_and_transcript(
    payload: Dict[str, Any],
) -> Tuple[Optional[str], Optional[Path], Optional[str], Optional[str]]:
    """
    Tries a few plausible field names; exact keys can vary across hook types/versions.
    Prefer structured values from stdin over heuristics.

    For SubagentStop events, use agent_transcript_path and agent_id.
    Returns: (session_id, transcript_path, agent_type, parent_session_id)
    """
    session_id: Optional[str] = (
        payload.get("sessionId")
        or payload.get("session_id")
        or payload.get("session", {}).get("id")
    )

    # For SubagentStop, use agent_transcript_path and create unique session_id
    hook_event = payload.get("hook_event_name")
    agent_type: Optional[str] = None
    parent_session_id: Optional[str] = None

    if hook_event == "SubagentStop":
        transcript = payload.get("agent_transcript_path")
        agent_id = payload.get("agent_id")
        agent_type = payload.get("agent_type")

        # Keep parent session ID for linking
        parent_session_id = session_id

        # Create unique session ID for subagent
        if session_id and agent_id:
            session_id = f"{session_id}::{agent_id}"
    else:
        transcript = (
            payload.get("transcriptPath")
            or payload.get("transcript_path")
            or payload.get("transcript", {}).get("path")
        )

    if transcript:
        try:
            transcript_path: Optional[Path] = Path(transcript).expanduser().resolve()
        except Exception:
            transcript_path = None
    else:
        transcript_path = None

    return session_id, transcript_path, agent_type, parent_session_id


# ----------------- Transcript parsing helpers -----------------
def get_content(msg: Dict[str, Any]) -> Any:
    if "message" in msg and isinstance(msg.get("message"), dict):
        return msg["message"].get("content")
    return msg.get("content")


def get_role(msg: Dict[str, Any]) -> Optional[str]:
    # Claude Code transcript lines commonly have type=user/assistant OR message.role
    t = msg.get("type")
    if t in ("user", "assistant"):
        return str(t)
    m = msg.get("message")
    if isinstance(m, dict):
        r = m.get("role")
        if r in ("user", "assistant"):
            return str(r)
    return None


def is_tool_result(msg: Dict[str, Any]) -> bool:
    role = get_role(msg)
    if role != "user":
        return False
    content = get_content(msg)
    if isinstance(content, list):
        return any(
            isinstance(x, dict) and x.get("type") == "tool_result" for x in content
        )
    return False


def iter_tool_results(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_result":
                out.append(x)
    return out


def iter_tool_uses(content: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for x in content:
            if isinstance(x, dict) and x.get("type") == "tool_use":
                out.append(x)
    return out


def extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for x in content:
            if isinstance(x, dict) and x.get("type") == "text":
                parts.append(x.get("text", ""))
            elif isinstance(x, str):
                parts.append(x)
        return "\n".join([p for p in parts if p])
    return ""


def truncate_text(
    s: str | None, max_chars: int = MAX_CHARS
) -> Tuple[str, Dict[str, Any]]:
    if s is None:
        return "", {"truncated": False, "orig_len": 0}
    orig_len = len(s)
    if orig_len <= max_chars:
        return s, {"truncated": False, "orig_len": orig_len}
    head = s[:max_chars]
    return head, {
        "truncated": True,
        "orig_len": orig_len,
        "kept_len": len(head),
        "sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(),
    }


def get_model(msg: Dict[str, Any]) -> str:
    m = msg.get("message")
    if isinstance(m, dict):
        return m.get("model") or "claude"
    return "claude"


def get_message_id(msg: Dict[str, Any]) -> Optional[str]:
    m = msg.get("message")
    if isinstance(m, dict):
        mid = m.get("id")
        if isinstance(mid, str) and mid:
            return str(mid)
    return None


# ----------------- Incremental reader -----------------
@dataclass
class SessionState:
    offset: int = 0
    buffer: str = ""
    turn_count: int = 0


def load_session_state(global_state: Dict[str, Any], key: str) -> SessionState:
    s = global_state.get(key, {})
    return SessionState(
        offset=int(s.get("offset", 0)),
        buffer=str(s.get("buffer", "")),
        turn_count=int(s.get("turn_count", 0)),
    )


def write_session_state(
    global_state: Dict[str, Any], key: str, ss: SessionState
) -> None:
    global_state[key] = {
        "offset": ss.offset,
        "buffer": ss.buffer,
        "turn_count": ss.turn_count,
        "updated": datetime.now(timezone.utc).isoformat(),
    }


def read_new_jsonl(
    transcript_path: Path, ss: SessionState
) -> Tuple[List[Dict[str, Any]], SessionState]:
    """
    Reads only new bytes since ss.offset. Keeps ss.buffer for partial last line.
    Returns parsed JSON lines (best-effort) and updated state.
    """
    if not transcript_path.exists():
        return [], ss

    try:
        with open(transcript_path, "rb") as f:
            f.seek(ss.offset)
            chunk = f.read()
            new_offset = f.tell()
    except Exception as e:
        debug(f"read_new_jsonl failed: {e}")
        return [], ss

    if not chunk:
        return [], ss

    try:
        text = chunk.decode("utf-8", errors="replace")
    except Exception:
        text = chunk.decode(errors="replace")

    combined = ss.buffer + text
    lines = combined.split("\n")
    # last element may be incomplete
    ss.buffer = lines[-1]
    ss.offset = new_offset

    msgs: List[Dict[str, Any]] = []
    for line in lines[:-1]:
        line = line.strip()
        if not line:
            continue
        try:
            msgs.append(json.loads(line))
        except Exception:
            continue

    return msgs, ss


# ----------------- Turn assembly -----------------
@dataclass
class Turn:
    user_msg: Dict[str, Any]
    assistant_msgs: List[Dict[str, Any]]
    tool_results_by_id: Dict[str, Any]


def build_turns(messages: List[Dict[str, Any]]) -> List[Turn]:
    """
    Groups incremental transcript rows into turns:
    user (non-tool-result) -> assistant messages -> (tool_result rows, possibly interleaved)
    Uses:
    - assistant message dedupe by message.id (latest row wins)
    - tool results dedupe by tool_use_id (latest wins)
    """
    turns: List[Turn] = []
    current_user: Optional[Dict[str, Any]] = None

    # assistant messages for current turn:
    assistant_order: List[
        str
    ] = []  # message ids in order of first appearance (or synthetic)
    assistant_latest: Dict[str, Dict[str, Any]] = {}  # id -> latest msg

    tool_results_by_id: Dict[str, Any] = {}  # tool_use_id -> content

    def flush_turn():
        nonlocal current_user, assistant_order, assistant_latest, tool_results_by_id, turns
        if current_user is None:
            return
        if not assistant_latest:
            return
        assistants = [
            assistant_latest[mid] for mid in assistant_order if mid in assistant_latest
        ]
        turns.append(
            Turn(
                user_msg=current_user,
                assistant_msgs=assistants,
                tool_results_by_id=dict(tool_results_by_id),
            )
        )

    for msg in messages:
        role = get_role(msg)

        # tool_result rows show up as role=user with content blocks of type tool_result
        if is_tool_result(msg):
            for tr in iter_tool_results(get_content(msg)):
                tid = tr.get("tool_use_id")
                if tid:
                    tool_results_by_id[str(tid)] = tr.get("content")
            continue

        if role == "user":
            # new user message -> finalize previous turn
            flush_turn()

            # start a new turn
            current_user = msg
            assistant_order = []
            assistant_latest = {}
            tool_results_by_id = {}
            continue

        if role == "assistant":
            if current_user is None:
                # ignore assistant rows until we see a user message
                continue

            mid = get_message_id(msg) or f"noid:{len(assistant_order)}"
            if mid not in assistant_latest:
                assistant_order.append(mid)
            assistant_latest[mid] = msg
            continue

        # ignore unknown rows

    # flush last
    flush_turn()
    return turns


# ----------------- Langfuse emit -----------------
def _tool_calls_from_assistants(
    assistant_msgs: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    for am in assistant_msgs:
        for tu in iter_tool_uses(get_content(am)):
            tid = tu.get("id") or ""
            calls.append(
                {
                    "id": str(tid),
                    "name": tu.get("name") or "unknown",
                    "input": (
                        tu.get("input")
                        if isinstance(
                            tu.get("input"), (dict, list, str, int, float, bool)
                        )
                        else {}
                    ),
                }
            )
    return calls


def emit_turn(
    langfuse: Langfuse,
    session_id: str,
    turn_num: int,
    turn: Turn,
    transcript_path: Path,
    trace_prefix: str = "Claude Code",
    agent_type: Optional[str] = None,
    parent_session_id: Optional[str] = None,
) -> None:
    user_text_raw = extract_text(get_content(turn.user_msg))
    user_text, user_text_meta = truncate_text(user_text_raw)

    last_assistant = turn.assistant_msgs[-1]
    assistant_text_raw = extract_text(get_content(last_assistant))
    assistant_text, assistant_text_meta = truncate_text(assistant_text_raw)

    model = get_model(turn.assistant_msgs[0])

    tool_calls = _tool_calls_from_assistants(turn.assistant_msgs)

    # attach tool outputs
    for c in tool_calls:
        if c["id"] and c["id"] in turn.tool_results_by_id:
            out_raw = turn.tool_results_by_id[c["id"]]
            out_str = (
                out_raw
                if isinstance(out_raw, str)
                else json.dumps(out_raw, ensure_ascii=False)
            )
            out_trunc, out_meta = truncate_text(out_str)
            c["output"] = out_trunc
            c["output_meta"] = out_meta
        else:
            c["output"] = None

    trace_name = f"{trace_prefix} - Turn {turn_num}"

    # Build tags - add agent type if this is a subagent
    tags = ["claude-code"]
    if agent_type:
        tags.extend(["subagent", f"agent:{agent_type}"])
    else:
        tags.append("main-agent")

    # Build metadata
    trace_metadata = {
        "source": "claude-code",
        "session_id": session_id,
        "turn_number": turn_num,
        "transcript_path": str(transcript_path),
        "user_text": user_text_meta,
        "agent_type": agent_type or "main",
        "is_subagent": agent_type is not None,
    }

    # Add parent session ID for subagents
    if parent_session_id:
        trace_metadata["parent_session_id"] = parent_session_id

    with propagate_attributes(
        session_id=session_id,
        trace_name=trace_name,
        tags=tags,
    ):
        with langfuse.start_as_current_span(
            name=trace_name,
            input={"role": "user", "content": user_text},
            metadata=trace_metadata,
        ) as trace_span:
            # LLM generation
            with langfuse.start_as_current_observation(
                name=f"{agent_type or 'Claude'} Response",
                as_type="generation",
                model=model,
                input={"role": "user", "content": user_text},
                output={"role": "assistant", "content": assistant_text},
                metadata={
                    "assistant_text": assistant_text_meta,
                    "tool_count": len(tool_calls),
                    "agent_type": agent_type or "main",
                },
            ):
                pass

            # Tool observations
            for tc in tool_calls:
                in_obj = tc["input"]
                # truncate tool input if it's a large string payload
                if isinstance(in_obj, str):
                    in_obj, in_meta = truncate_text(in_obj)
                else:
                    in_meta = None

                with langfuse.start_as_current_observation(
                    name=f"Tool: {tc['name']}",
                    as_type="tool",
                    input=in_obj,
                    metadata={
                        "tool_name": tc["name"],
                        "tool_id": tc["id"],
                        "input_meta": in_meta,
                        "output_meta": tc.get("output_meta"),
                        "agent_type": agent_type or "main",
                    },
                ) as tool_obs:
                    tool_obs.update(output=tc.get("output"))

            trace_span.update(output={"role": "assistant", "content": assistant_text})


# ----------------- Main -----------------
def main() -> int:
    start = time.time()
    debug("Hook started")

    if os.environ.get("TRACE_TO_LANGFUSE", "").lower() != "true":
        debug("TRACE_TO_LANGFUSE not enabled, exiting")
        return 0

    public_key = os.environ.get("CC_LANGFUSE_PUBLIC_KEY") or os.environ.get(
        "LANGFUSE_PUBLIC_KEY"
    )
    secret_key = os.environ.get("CC_LANGFUSE_SECRET_KEY") or os.environ.get(
        "LANGFUSE_SECRET_KEY"
    )
    host = (
        os.environ.get("CC_LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_BASE_URL")
        or "https://cloud.langfuse.com"
    )

    if not public_key or not secret_key:
        debug("Langfuse credentials not found, exiting")
        return 0

    debug(f"Langfuse configured: host={host}")

    payload = read_hook_payload()
    (
        session_id,
        transcript_path,
        agent_type,
        parent_session_id,
    ) = extract_session_and_transcript(payload)

    # Enhanced debug logging
    hook_event = payload.get("hook_event_name", "unknown")
    if agent_type:
        debug(
            f"Hook event: {hook_event}, Agent: {agent_type}, Session: {session_id}, Parent: {parent_session_id}"
        )
    else:
        debug(f"Hook event: {hook_event}, Session: {session_id}")

    if not session_id or not transcript_path:
        # No structured payload; fail open (do not guess)
        debug("Missing session_id or transcript_path from hook payload; exiting.")
        return 0

    if not transcript_path.exists():
        debug(f"Transcript path does not exist: {transcript_path}")
        return 0

    # Add agent type to trace name if this is a subagent
    trace_prefix = f"Agent: {agent_type}" if agent_type else "Claude Code"
    debug(f"Processing transcript: {transcript_path} (prefix={trace_prefix})")

    try:
        langfuse = Langfuse(public_key=public_key, secret_key=secret_key, host=host)
    except Exception:
        return 0

    try:
        with FileLock(LOCK_FILE):
            state = load_state()
            key = state_key(session_id, str(transcript_path))
            ss = load_session_state(state, key)

            msgs, ss = read_new_jsonl(transcript_path, ss)
            if not msgs:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            turns = build_turns(msgs)
            if not turns:
                write_session_state(state, key, ss)
                save_state(state)
                return 0

            # emit turns
            emitted = 0
            for t in turns:
                emitted += 1
                turn_num = ss.turn_count + emitted
                try:
                    # Use trace_prefix, agent_type, and parent_session_id for subagents
                    emit_turn(
                        langfuse,
                        session_id,
                        turn_num,
                        t,
                        transcript_path,
                        trace_prefix,
                        agent_type,
                        parent_session_id,
                    )
                except Exception as e:
                    debug(f"emit_turn failed: {e}")
                    # continue emitting other turns

            ss.turn_count += emitted
            write_session_state(state, key, ss)
            save_state(state)

        try:
            langfuse.flush()
        except Exception:
            pass

        dur = time.time() - start
        agent_info = f" (agent={agent_type})" if agent_type else ""
        info(
            f"Processed {emitted} turns in {dur:.2f}s (session={session_id}{agent_info})"
        )

    except Exception as e:
        debug(f"Unexpected failure: {e}")

    finally:
        # SECURITY FIX: Only flush, don't shutdown
        # This script may be called multiple times, shutdown destroys the client
        try:
            langfuse.flush()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
