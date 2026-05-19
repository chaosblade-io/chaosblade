"""Reject node: produce a rejection result."""

import logging

from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.state import AgentState
from chaos_agent.errors import FailureReason
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


def _infer_failure_reason(state: AgentState) -> str:
    """Infer the appropriate FailureReason for the reject node.

    Reject is reached from:
    - should_continue_agent_loop: max iterations (planning_timeout)
    - route_after_safety: safety_status=rejected (safety_rejected)
    - route_after_confirmation: safety_status=rejected (user_rejected)
    """
    safety_status = state.get("safety_status", "")
    safety_reason = state.get("safety_reason") or ""

    if safety_status == "rejected":
        if "blacklist" in safety_reason.lower():
            return f"{FailureReason.SAFETY_REJECTED.value}: {safety_reason}"
        if "user" in safety_reason.lower() or "reject" in safety_reason.lower():
            return f"{FailureReason.USER_REJECTED.value}: {safety_reason}"
        return f"{FailureReason.SAFETY_REJECTED.value}: {safety_reason}"

    # Fallback: agent_loop max iterations (no skill after max loops)
    agent_loop_count = state.get("agent_loop_count", 0)
    if agent_loop_count > 0:
        return f"{FailureReason.PLANNING_TIMEOUT.value}: Agent loop exceeded max iterations without completing planning"

    return f"{FailureReason.SAFETY_REJECTED.value}: {safety_reason or 'Unknown rejection reason'}"


async def reject(state: AgentState) -> dict:
    """Produce a rejection result.

    This node is reached when the agent loop exceeds max iterations,
    safety checks fail, or the user rejects the confirmation.

    Returns updated error, result, and failure_reason fields.
    """
    task_id = state.get("task_id", "unknown")
    reason = state.get("safety_reason", "Unknown reason")
    error_val = state.get("error", reason)

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "reject",
        f"Task rejected: {reason}",
        {"reason": reason},
    )
    tracker.fail(f"Rejected: {error_val}")

    # Trust upstream-set failure_reason if present; otherwise infer as fallback.
    failure_reason = state.get("failure_reason") or _infer_failure_reason(state)

    result = {
        "result": {
            "status": "rejected",
            "reason": error_val,
        },
        "error": error_val,
        "failure_reason": failure_reason,
        "finished_at": now_iso(),
    }
    await sync_to_store(state, result)
    return result
