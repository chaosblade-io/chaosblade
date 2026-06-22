"""Reject node: produce a rejection result."""

import logging

from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.operation_outcome import read_operation_outcome
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


def _infer_failure_detail(state: AgentState) -> dict:
    """Infer the appropriate FailureCategory for the reject node.

    Reject is reached from:
    - should_continue_agent_loop: max iterations (planning_timeout)
    - route_after_safety: safety_status=rejected (safety_rejected)
    - route_after_confirmation: safety_status=rejected (user_rejected)
    - extract_planning_metadata: planning_rejected with error (planning_rejected)
    """
    # If planning was explicitly rejected by the LLM, use the stored reason
    # directly as llm_analysis instead of scanning messages.
    rejection_reason = state.get("_planning_rejection_reason", "")
    if rejection_reason:
        alternatives = state.get("_planning_alternatives", "")
        return fail_state(
            FailureCategory.PLANNING_REJECTED,
            rejection_reason,
            alternatives=alternatives,
            llm_analysis=rejection_reason,
        )

    safety_status = state.get("safety_status", "")
    safety_reason = state.get("safety_reason") or ""

    if safety_status == "rejected":
        if "user" in safety_reason.lower() or "reject" in safety_reason.lower():
            return fail_state(FailureCategory.USER_REJECTED, safety_reason)
        return fail_state(FailureCategory.SAFETY_REJECTED, safety_reason)

    agent_loop_count = state.get("agent_loop_count", 0)
    if agent_loop_count > 0:
        return fail_state(FailureCategory.PLANNING_TIMEOUT, "max_iterations exceeded")

    return fail_state(FailureCategory.SAFETY_REJECTED, safety_reason or "Unknown rejection reason")


async def reject(state: AgentState) -> dict:
    """Produce a rejection result.

    This node is reached when the agent loop exceeds max iterations,
    safety checks fail, or the user rejects the confirmation.
    """
    task_id = state.get("task_id", "unknown")
    reason = state.get("safety_reason", "Unknown reason")
    outcome = read_operation_outcome(state)
    error_val = outcome.error or reason

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "reject",
        f"Task rejected: {reason}",
        {"reason": reason},
    )
    tracker.fail(f"Rejected: {error_val}")

    # Trust upstream-set failure_detail if present; otherwise infer as fallback.
    if outcome.failure_detail:
        fs = {"failure_detail": outcome.failure_detail}
    else:
        fs = _infer_failure_detail(state)

    result = {
        "result": {
            "status": "rejected",
            "reason": error_val,
        },
        "error": error_val,
        "failure_detail": fs.get("failure_detail"),
        "finished_at": now_iso(),
    }
    await sync_to_store(state, result)
    return result
