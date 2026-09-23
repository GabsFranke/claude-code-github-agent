"""Centralized SDK execution for all workers.

All SDK invocations go through this module for consistency,
instrumentation, and observability.
"""

import asyncio
import logging
import os
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from shared import SDKError, SDKTimeoutError
from shared.dlq import is_transient_error
from shared.session_stream import SessionStreamBridge

logger = logging.getLogger(__name__)

# Background agent tasks whose completion wakes the parent for another turn.
# Mirrors the SDK's own bookkeeping (DEFERRING_TASK_TYPES / TERMINAL_TASK_STATUSES
# in claude_agent_sdk._internal.query): a ResultMessage ends a *turn*, not the
# run, while any of these are still running. Background shells are excluded on
# purpose; they may never report a terminal status.
_DEFERRING_TASK_TYPES = frozenset({"local_agent", "local_workflow"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "stopped", "killed"})


def _track_task_lifecycle(
    message: SystemMessage, inflight: set[str], settled: set[str] | None = None
) -> None:
    """Update ``inflight`` from a task lifecycle system message.

    ``task_started`` adds a delegated agent task; ``task_notification`` or a
    ``task_updated`` patch with a terminal status removes it and, when given,
    records it in ``settled`` (see the continuation grace logic in the loop).
    """
    data = message.data if isinstance(message.data, dict) else {}
    task_id = data.get("task_id")
    if not task_id:
        return
    if message.subtype == "task_started":
        if data.get("task_type") in _DEFERRING_TASK_TYPES:
            inflight.add(task_id)
            logger.info(
                f"Background task started: {task_id} "
                f"({data.get('description', '')[:80]})"
            )
        return
    status: str | None
    if message.subtype == "task_notification":
        status = data.get("status") or "completed"
    elif message.subtype == "task_updated":
        patch = data.get("patch")
        status = patch.get("status") if isinstance(patch, dict) else None
        if status not in _TERMINAL_TASK_STATUSES:
            return
    else:
        return
    inflight.discard(task_id)
    if settled is not None:
        settled.add(task_id)
    logger.info(f"Background task {status}: {task_id}")


def _continuation_grace_seconds() -> float:
    """How long to hold the client open after a result that may not end the run.

    A background task that settles after the model's last message but before
    the turn's result frame (the Stop hook window) wakes the parent for a
    continuation turn *after* that result. Nothing in the frame says so, hence
    a bounded wait for the continuation to show up.
    """
    return float(os.getenv("SDK_CONTINUATION_GRACE_SECONDS", "30"))


# Only enable SDK debug logging if SDK_DEBUG is set
sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
if sdk_debug:
    logging.getLogger("claude_agent_sdk").setLevel(logging.DEBUG)
else:
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


async def execute_sdk(
    prompt: str,
    options: ClaudeAgentOptions,
    collect_text: bool = True,
    max_retries: int = 1,
    retry_base_delay: float = 5.0,
    streaming_bridge: SessionStreamBridge | None = None,
) -> dict:
    """Execute Claude Agent SDK with given options.

    This is the single point of SDK execution for all workers (sandbox,
    retrospector, memory). Provides consistent error handling and
    observability.

    A run is never given a deadline of our own. Agent work legitimately takes
    hours, there is no runtime we could call "too long" without guessing, and
    killing a session mid-flight loses everything it had done. A timeout
    raised by the SDK or the transport beneath it is a genuine failure and is
    surfaced as one; we simply do not manufacture one.

    Args:
        prompt: User prompt to send to the agent
        options: Pre-built ClaudeAgentOptions instance
        collect_text: Whether to collect text blocks into response (default: True)
        max_retries: Maximum number of retry attempts (default: 1 = no retry)
        retry_base_delay: Base delay in seconds for exponential backoff (default: 5.0)
                         Delays: 5s, 15s, 45s for attempts 1, 2, 3
        streaming_bridge: Optional SessionStreamBridge. When provided, every SDK
                          message is published to Redis as it arrives, enabling
                          real-time browser observation via session_proxy.

    Returns:
        dict with:
            - response: str (if collect_text=True, None otherwise)
            - num_turns: int
            - duration_ms: int
            - is_error: bool
            - messages: list (all messages received)

    Raises:
        SDKTimeoutError: If the SDK or its transport times out on its own
        SDKError: If SDK execution fails or returns empty response
    """
    last_error: Exception | None = None

    for attempt in range(max_retries):
        try:
            return await _execute_sdk_once(
                prompt=prompt,
                options=options,
                collect_text=collect_text,
                streaming_bridge=streaming_bridge,
            )
        except SDKTimeoutError:
            # The SDK gave up on its own. Retrying would replay the whole
            # session from the start, so surface it instead.
            logger.error(
                f"SDK reported a timeout (attempt {attempt + 1}/{max_retries}). "
                "Not retrying — replaying the session would repeat all work."
            )
            raise
        except Exception as e:
            last_error = e
            if not is_transient_error(e):
                # Permanent errors (config issues, validation, etc.) are not
                # worth retrying — they will fail the same way every time.
                logger.error(
                    f"SDK execution failed with permanent error "
                    f"(attempt {attempt + 1}/{max_retries}): "
                    f"{type(e).__name__}: {e}. Not retrying."
                )
                raise
            if attempt < max_retries - 1:
                # Transient error — retry with exponential backoff
                # Delays: 5s, 15s, 45s (with base_delay=5.0)
                delay = retry_base_delay * (3**attempt)
                logger.warning(
                    f"SDK execution attempt {attempt + 1}/{max_retries} failed "
                    f"(transient): {type(e).__name__}: {e}. "
                    f"Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)

    # All retries exhausted - log as error
    logger.error(
        f"SDK execution failed after {max_retries} attempt(s): {type(last_error).__name__}: {last_error}",
        exc_info=True,
    )
    if last_error is not None:
        raise last_error
    raise RuntimeError("SDK execution failed without capturing an error")


def _collected_text(
    message: AssistantMessage, collect_text: bool, sdk_debug: bool
) -> list[str]:
    """Text blocks worth keeping from an assistant message.

    Empty when the caller is not collecting text, so the message loop needs
    no branch of its own for it.
    """
    if not collect_text:
        return []
    texts = []
    for block in message.content:
        if isinstance(block, TextBlock):
            texts.append(block.text)
            if sdk_debug:
                logger.debug(f"Text block content: {block.text[:200]}...")
    return texts


async def _stream_session(
    *,
    prompt: str,
    options,
    streaming_bridge: SessionStreamBridge | None,
    collect_text: bool,
    sdk_debug: bool,
    response_parts: list[str],
    all_messages: list[Any],
) -> dict[str, Any]:
    """Run one client session and return the result frame that ended it.

    Kept out of _execute_sdk_once so the message loop does not start four
    blocks deep. ``response_parts`` and ``all_messages`` are filled in place;
    everything else the loop tracks is local to a session.
    """
    inflight_tasks: set[str] = set()
    settled_since_output: set[str] = set()
    assistant_messages_seen = 0
    pending_result: dict[str, Any] | None = None
    result_info: dict[str, Any] = {
        "num_turns": 0,
        "duration_ms": 0,
        "is_error": False,
    }

    async with ClaudeSDKClient(options=options) as client:
        logger.info("SDK client created, sending query...")

        await client.query(prompt)

        logger.info("Waiting for SDK response...")

        # Pump messages through a queue so the consumer can wait with a
        # timeout (grace window) without cancelling the SDK generator.
        queue: asyncio.Queue[Any] = asyncio.Queue()
        end_of_stream = object()

        async def _pump() -> None:
            try:
                async for m in client.receive_messages():
                    await queue.put(m)
            except Exception as e:  # re-raised by the consumer
                await queue.put(e)
            finally:
                await queue.put(end_of_stream)

        pump = asyncio.create_task(_pump())
        try:
            while True:
                if pending_result is None:
                    message = await queue.get()
                else:
                    try:
                        message = await asyncio.wait_for(
                            queue.get(), _continuation_grace_seconds()
                        )
                    except TimeoutError:
                        logger.info(
                            "No continuation turn within the grace "
                            "window; treating the last result as final"
                        )
                        result_info = pending_result
                        break
                if message is end_of_stream:
                    if pending_result is not None:
                        result_info = pending_result
                    break
                if isinstance(message, Exception):
                    raise message

                all_messages.append(message)

                # Publish to streaming bridge (if session is being observed)
                if streaming_bridge is not None:
                    await streaming_bridge.publish_message(message)

                if sdk_debug:
                    logger.debug(f"Received message type: {type(message).__name__}")

                if pending_result is not None and not isinstance(
                    message, (SystemMessage, ResultMessage)
                ):
                    # The CLI woke the parent after all: the run goes on.
                    logger.info("Continuation turn started; run continues")
                    pending_result = None

                if isinstance(message, AssistantMessage):
                    assistant_messages_seen += 1
                    # Output after a settle means the CLI delivered the
                    # notification inside this turn; no wake-up pending.
                    settled_since_output.clear()
                    logger.info(f"Received response with {len(message.content)} blocks")
                    response_parts.extend(
                        _collected_text(message, collect_text, sdk_debug)
                    )

                elif isinstance(message, ResultMessage):
                    settled = settled_since_output
                    settled_since_output = set()
                    if inflight_tasks:
                        # One turn ended but delegated agents are still
                        # running; each completion wakes the parent for
                        # a follow-up turn that ends in another result.
                        # Closing the client here would kill them.
                        logger.info(
                            f"Turn ended with {len(inflight_tasks)} "
                            f"background task(s) still running; waiting "
                            f"for follow-up turn"
                        )
                        continue
                    if (
                        message.num_turns == 0
                        and not message.is_error
                        and assistant_messages_seen == 0
                    ):
                        # A turn that never called the model. Seen on
                        # resume when orphaned tasks settle as "stopped"
                        # and wake the parent for an empty continuation
                        # turn before our query is processed.
                        logger.info(
                            "Ignoring 0-turn result frame before any "
                            "assistant output; waiting for the response "
                            "to our query"
                        )
                        continue
                    info = {
                        "num_turns": message.num_turns,
                        "duration_ms": message.duration_ms,
                        "is_error": message.is_error,
                        "session_id": getattr(message, "session_id", None),
                    }
                    logger.info(
                        f"SDK completed - {message.num_turns} turns, "
                        f"{message.duration_ms}ms, error={message.is_error}"
                    )
                    if sdk_debug:
                        logger.debug(
                            f"ResultMessage details: "
                            f"is_error={message.is_error}, "
                            f"subtype={message.subtype}"
                        )
                    if settled and not message.is_error:
                        # A task settled after the model's last message
                        # (the Stop hook window). The CLI will wake the
                        # parent for a continuation turn after this
                        # result; hold the client open for it.
                        logger.info(
                            f"{len(settled)} background task(s) settled "
                            f"after the last output; holding up to "
                            f"{_continuation_grace_seconds():.0f}s for a "
                            f"continuation turn"
                        )
                        pending_result = info
                        continue
                    result_info = info
                    break

                elif isinstance(message, SystemMessage):
                    if message.subtype == "init":
                        # The CLI reports the resolved model id here;
                        # the alias we passed (if any) is logged as
                        # "Model:".
                        logger.info(
                            f"SDK session init - "
                            f"model={message.data.get('model')}, "
                            f"session_id={message.data.get('session_id')}"
                        )
                    else:
                        _track_task_lifecycle(
                            message, inflight_tasks, settled_since_output
                        )

                elif sdk_debug:
                    # Log any other message types only in debug mode
                    logger.debug(
                        f"Received other message type: " f"{type(message).__name__}"
                    )
                    if hasattr(message, "__dict__"):
                        logger.debug(f"Message content: {message.__dict__}")
        finally:
            pump.cancel()
            try:
                await pump
            except (asyncio.CancelledError, Exception):
                pass

    return result_info


async def _execute_sdk_once(
    prompt: str,
    options,
    collect_text: bool = True,
    streaming_bridge: SessionStreamBridge | None = None,
) -> dict:
    """Execute Claude Agent SDK once (internal implementation).

    Args:
        prompt: User prompt to send to the agent
        options: Pre-built ClaudeAgentOptions instance
        collect_text: Whether to collect text blocks into response (default: True)
        streaming_bridge: Optional bridge to publish messages to Redis in real-time.

    Returns:
        dict with response, num_turns, duration_ms, is_error, messages

    Raises:
        SDKTimeoutError: If the SDK or its transport times out on its own
        SDKError: If SDK execution fails or returns empty response
    """
    response_parts: list[str] = []
    all_messages: list[Any] = []
    result_info: dict[str, Any] = {
        "num_turns": 0,
        "duration_ms": 0,
        "is_error": False,
    }

    logger.info(f"Starting SDK execution (prompt: {len(prompt)} chars)...")
    logger.info(f"Model: {options.model or '(CLI default)'}")

    # Only show detailed info if SDK_DEBUG is enabled
    if sdk_debug:
        logger.debug(f"Prompt preview: {prompt[:200]}...")
        logger.debug(f"Working directory: {options.cwd}")
        logger.debug(f"Setting sources: {options.setting_sources}")
        logger.debug(f"Allowed tools: {options.allowed_tools}")

        # Verify we can access the working directory
        try:
            files = os.listdir(options.cwd)
            logger.debug(f"Working directory contains {len(files)} items")
            logger.debug(f"First 10 items: {files[:10]}")
        except Exception as e:
            logger.error(f"Cannot access working directory: {e}")

    try:
        # No asyncio.timeout wrapper: the run is allowed to take as long as it
        # takes. See the note in execute_sdk's docstring.
        result_info = await _stream_session(
            prompt=prompt,
            options=options,
            streaming_bridge=streaming_bridge,
            collect_text=collect_text,
            sdk_debug=sdk_debug,
            response_parts=response_parts,
            all_messages=all_messages,
        )

    except TimeoutError as e:
        # Not ours. Raised by the SDK or the transport under it, so it is a
        # real failure rather than a deadline we chose to enforce.
        raise SDKTimeoutError(f"SDK reported a timeout: {e}") from e
    except Exception as e:
        # MessageParseError from the SDK indicates incomplete streaming data
        # from the API — the connection was interrupted before the full
        # assistant message (including its 'signature' field) arrived.
        # This is a transient connection-level issue; a retry with a fresh
        # connection will produce a complete response.
        if type(e).__qualname__ == "MessageParseError":
            raise SDKError(f"SDK streaming connection interrupted: {e}") from e
        raise SDKError(f"SDK execution failed: {e}") from e

    response = "\n".join(response_parts) if collect_text else None

    if collect_text and (not response or not response.strip()):
        raise SDKError("SDK returned empty response")

    logger.info(
        f"SDK execution complete - collected {len(response_parts)} response parts"
    )

    return {
        "response": response,
        "messages": all_messages,
        "session_id": result_info.get("session_id"),
        **{k: v for k, v in result_info.items() if k != "session_id"},
    }
