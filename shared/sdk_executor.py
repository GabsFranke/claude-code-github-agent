"""Centralized SDK execution for all workers.

All SDK invocations go through this module for consistency,
instrumentation, and observability.
"""

import asyncio
import logging
import os
from typing import TYPE_CHECKING

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)

from shared import SDKError, SDKTimeoutError
from shared.dlq import is_transient_error

if TYPE_CHECKING:
    from shared.session_stream import SessionStreamBridge

logger = logging.getLogger(__name__)

# Only enable SDK debug logging if SDK_DEBUG is set
sdk_debug = os.getenv("SDK_DEBUG", "false").lower() == "true"
if sdk_debug:
    logging.getLogger("claude_agent_sdk").setLevel(logging.DEBUG)
else:
    logging.getLogger("claude_agent_sdk").setLevel(logging.WARNING)


async def execute_sdk(
    prompt: str,
    options: ClaudeAgentOptions,
    timeout: int | None = None,
    collect_text: bool = True,
    max_retries: int = 1,
    retry_base_delay: float = 5.0,
    streaming_bridge: "SessionStreamBridge | None" = None,
) -> dict:
    """Execute Claude Agent SDK with given options.

    This is the single point of SDK execution for all workers (sandbox,
    retrospector, memory). Provides consistent error handling, timeout
    management, and observability.

    Args:
        prompt: User prompt to send to the agent
        options: Pre-built ClaudeAgentOptions instance
        timeout: Optional timeout in seconds (default: from env or 1800)
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
        SDKTimeoutError: If execution exceeds timeout
        SDKError: If SDK execution fails or returns empty response
    """
    last_error: Exception | None = None

    for attempt in range(max_retries):
        try:
            return await _execute_sdk_once(
                prompt=prompt,
                options=options,
                timeout=timeout,
                collect_text=collect_text,
                streaming_bridge=streaming_bridge,
            )
        except SDKTimeoutError:
            # Timeouts are not transient — retrying would just run the full
            # session again and hit the same wall. Raise immediately.
            logger.error(
                f"SDK execution timed out (attempt {attempt + 1}/{max_retries}). "
                "Not retrying — timeout is not a transient error."
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


async def _execute_sdk_once(
    prompt: str,
    options,
    timeout: int | None = None,
    collect_text: bool = True,
    streaming_bridge: "SessionStreamBridge | None" = None,
) -> dict:
    """Execute Claude Agent SDK once (internal implementation).

    Args:
        prompt: User prompt to send to the agent
        options: Pre-built ClaudeAgentOptions instance
        timeout: Optional timeout in seconds (default: from env or 1800)
        collect_text: Whether to collect text blocks into response (default: True)
        streaming_bridge: Optional bridge to publish messages to Redis in real-time.

    Returns:
        dict with response, num_turns, duration_ms, is_error, messages

    Raises:
        SDKTimeoutError: If execution exceeds timeout
        SDKError: If SDK execution fails or returns empty response
    """
    sdk_timeout = timeout or int(os.getenv("SDK_EXECUTION_TIMEOUT", "1800"))
    response_parts = []
    all_messages = []
    from typing import Any

    result_info: dict[str, Any] = {
        "num_turns": 0,
        "duration_ms": 0,
        "is_error": False,
    }

    logger.info(f"Starting SDK execution (prompt: {len(prompt)} chars)...")
    logger.info(f"Model: {options.model}")

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
        async with asyncio.timeout(sdk_timeout):
            async with ClaudeSDKClient(options=options) as client:
                logger.info("SDK client created, sending query...")

                await client.query(prompt)

                logger.info("Waiting for SDK response...")

                async for message in client.receive_messages():
                    all_messages.append(message)

                    # Publish to streaming bridge (if session is being observed)
                    if streaming_bridge is not None:
                        await streaming_bridge.publish_message(message)

                    if sdk_debug:
                        logger.debug(f"Received message type: {type(message).__name__}")

                    if isinstance(message, AssistantMessage):
                        logger.info(
                            f"Received response with {len(message.content)} blocks"
                        )
                        if collect_text:
                            for block in message.content:
                                if isinstance(block, TextBlock):
                                    response_parts.append(block.text)
                                    if sdk_debug:
                                        logger.debug(
                                            f"Text block content: {block.text[:200]}..."
                                        )

                    elif isinstance(message, ResultMessage):
                        result_info = {
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
                                f"ResultMessage details: is_error={message.is_error}, "
                                f"subtype={message.subtype}"
                            )
                        break

                    elif sdk_debug:
                        # Log any other message types only in debug mode
                        logger.debug(
                            f"Received other message type: {type(message).__name__}"
                        )
                        if hasattr(message, "__dict__"):
                            logger.debug(f"Message content: {message.__dict__}")

    except TimeoutError as e:
        raise SDKTimeoutError(f"SDK execution timed out after {sdk_timeout}s") from e
    except Exception as e:
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
