"""Time wait tool — allows the LLM to pause between operations.

STRICT CONSTRAINT: Cannot be called consecutively. The LLM MUST call
at least one other tool (e.g., kubectl) between two time_wait calls.
This prevents idle spinning.
"""

import asyncio
import logging

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS = 30
MAX_CALLS_PER_TASK = 3

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


@tool
async def time_wait(seconds: int = 10) -> str:
    """Pause execution for a specified number of seconds.

    When to use:
      - After blade_create reports an error with a UID, wait for the
        ChaosBlade operator to retry before checking cluster state.
      - Between polling checks when observing fault propagation.

    STRICT RULES:
      - Cannot be called twice in a row. You MUST call at least one
        other tool (e.g., kubectl get) between two time_wait calls.
      - If you call time_wait consecutively, it will be rejected.

    Inputs:
      - seconds: How long to wait (1-30, default 10). Clamped to max 30s.

    Output: Confirmation of how long was waited.

    Side effects: None (only delays execution).
    """
    global _last_tool_was_wait, _call_count

    if _call_count >= MAX_CALLS_PER_TASK:
        return (
            f"Error: time_wait REJECTED — maximum {MAX_CALLS_PER_TASK} calls "
            f"per task reached. Proceed without waiting."
        )

    if _last_tool_was_wait:
        return (
            "Error: time_wait REJECTED — cannot call time_wait consecutively. "
            "You MUST call another tool between waits."
        )

    clamped = max(1, min(seconds, MAX_WAIT_SECONDS))
    logger.info(f"time_wait: sleeping {clamped}s (call %d/%d)", _call_count + 1, MAX_CALLS_PER_TASK)
    await asyncio.sleep(clamped)
    _last_tool_was_wait = True
    _call_count += 1
    return f"Waited {clamped} seconds. Proceed with your next action."
