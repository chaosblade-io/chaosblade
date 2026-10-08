"""Time wait tool — allows the LLM to pause between operations.

STRICT CONSTRAINT: Cannot be called consecutively. The LLM MUST call
at least one other tool (any observation/status check) between two
time_wait calls. This prevents idle spinning.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt import InjectedState

from chaos_agent.tools.progress import (
    REDLINE_REMINDER_MARK,
    behavioral_reminder_due,
)

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 60
MAX_CALLS_PER_TASK = 50

# Track state to enforce no-consecutive rule and max call limit.
# Module-level state, reset per task via reset_wait_state().
_last_tool_was_wait: bool = False
_call_count: int = 0


def reset_wait_state():
    """Reset wait state for a new task."""
    global _last_tool_was_wait, _call_count
    _last_tool_was_wait = False
    _call_count = 0


def mark_other_tool_called():
    """Call this when any non-wait tool is executed, to re-enable time_wait."""
    global _last_tool_was_wait
    _last_tool_was_wait = False


def check_and_reset_wait_guard(messages: list) -> None:
    """Scan the most recent batch of ToolMessages and reset the wait guard.

    A "batch" is the contiguous block of ToolMessages at the tail of
    *messages* (all produced by the same AIMessage's tool_calls).  If
    **any** ToolMessage in that batch is not ``time_wait``, the
    consecutive-call flag is cleared so the LLM may call ``time_wait``
    again.

    This replaces the earlier logic that only inspected the single most
    recent ToolMessage.  When the LLM emits parallel tool calls (e.g.
    ``kubectl`` + ``time_wait``) the old check would see ``time_wait``
    as the last message and fail to reset, causing a false-positive
    "consecutive call" rejection on the next iteration.
    """
    found_non_wait = False
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break  # exited the contiguous ToolMessage batch
        name = getattr(msg, "name", "") or ""
        if name != "time_wait":
            found_non_wait = True
    if found_non_wait:
        mark_other_tool_called()


@tool
async def time_wait(
    seconds: int = 10,
    *,
    state: Annotated[dict | None, InjectedState] = None,
) -> str:
    """Pause execution for the given seconds.

    When to use:
      - After an action whose effect propagates asynchronously, wait before
        re-checking state.
      - Between polling checks while observing a slow change.

    STRICT RULES:
      - Never twice in a row: run at least one observation or status-check
        tool between waits, or the call is rejected.
      - For longer pauses, request the TOTAL in one call (max 60s) instead
        of chaining waits.

    Inputs:
      - seconds: Wait length (1-60, default 10). Clamped to 60.

    Output: Confirmation of how long was waited. Side effects: None.
    A missing behavioral probe appends one RED-LINE REMINDER per injection window.
    """
    global _last_tool_was_wait, _call_count

    if _call_count >= MAX_CALLS_PER_TASK:
        return (
            f"Error: time_wait REJECTED — maximum {MAX_CALLS_PER_TASK} calls "
            f"per task reached. Proceed without waiting."
        )

    if _last_tool_was_wait:
        return (
            "Error: time_wait REJECTED — cannot call time_wait consecutively; "
            "NO waiting happened. Run ONE observation or status check that "
            "advances your current goal first — after any non-wait tool "
            "completes, time_wait is accepted again. If you simply need a "
            "longer pause, ask for the TOTAL seconds in a single call "
            "(max 60) instead of chaining waits."
        )

    clamped = max(1, min(seconds, MAX_WAIT_SECONDS))
    logger.info(f"time_wait: sleeping {clamped}s (call %d/%d)", _call_count + 1, MAX_CALLS_PER_TASK)
    await asyncio.sleep(clamped)
    _last_tool_was_wait = True
    _call_count += 1
    receipt = f"Waited {clamped} seconds. Proceed with your next action."
    # o10 (r68 review): the red line bans WAITING on missing behavioral
    # evidence too — finish_execution already guards the third banned
    # move; this guards the first. Soft like the finish gate: the wait
    # itself proceeds, the receipt carries the reminder (one-shot per
    # injection window — behavioral_reminder_due's latch).
    try:
        if behavioral_reminder_due(state):
            receipt += (
                f" {REDLINE_REMINDER_MARK} (soft gate): no behavioral evidence of "
                "the fault's user-visible effect yet — only mechanism "
                "readbacks. Waiting consumes the fault window in which "
                "effect evidence can still be collected: probe once (logs "
                "/ events / top / an exec probe) before waiting further."
            )
    except Exception:  # noqa: BLE001 — a reminder must never break the wait
        logger.debug("behavioral reminder check failed", exc_info=True)
    return receipt
