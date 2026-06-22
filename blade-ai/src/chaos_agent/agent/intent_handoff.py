"""Intent Graph → Pipeline Graph handoff helpers.

The Intent Graph may keep rich dialogue history, but once an inject or batch
operation is dispatched, executable one-shot payload fields must be cleared so
the next conversational turn does not re-launch a stale intent.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any


DISPATCHED_OPERATION_CLEAR_UPDATE: dict[str, Any] = {
    "confirmed_intent": None,
    "batch_submit_args": None,
    "fault_spec": None,
    "handoff_summary": None,
    "intent_reasoning": None,
    "intent_confidence": 0.0,
    "clarification_round": 0,
}


@dataclass(frozen=True)
class PipelineHandoff:
    """Resolved data needed to start a Pipeline Graph from IntentState."""

    operation: str
    task_id: str
    tui_session_id: str
    handoff_summary: str
    fault_spec: dict | None = None
    batch_submit_args: dict | None = None


def clear_dispatched_operation_payload_update() -> dict[str, Any]:
    """Return the IntentState update that clears dispatched one-shot payload."""

    return deepcopy(DISPATCHED_OPERATION_CLEAR_UPDATE)


def detect_dispatchable_operation(
    intent_state: dict,
    *,
    has_pending_interrupt: bool = False,
) -> str | None:
    """Return the operation ready for Pipeline dispatch, if any."""

    if has_pending_interrupt:
        return None

    confirmed = intent_state.get("confirmed_intent")
    if confirmed == "batch_inject" and intent_state.get("batch_submit_args"):
        return "batch_inject"
    if confirmed == "inject" and intent_state.get("fault_spec"):
        return "inject"
    return None


def build_pipeline_handoff_from_intent_state(
    intent_state: dict,
    *,
    operation: str,
    task_id: str,
    default_tui_session_id: str = "",
) -> PipelineHandoff:
    """Extract immutable handoff data from IntentState for Pipeline startup."""

    if operation not in ("inject", "batch_inject"):
        raise ValueError(f"Unsupported pipeline handoff operation: {operation}")

    batch_submit_args = (
        deepcopy(intent_state.get("batch_submit_args"))
        if operation == "batch_inject"
        else None
    )
    return PipelineHandoff(
        operation=operation,
        task_id=task_id,
        tui_session_id=(intent_state.get("tui_session_id") or default_tui_session_id or ""),
        handoff_summary=str(intent_state.get("handoff_summary") or ""),
        fault_spec=deepcopy(intent_state.get("fault_spec")),
        batch_submit_args=batch_submit_args,
    )


__all__ = [
    "DISPATCHED_OPERATION_CLEAR_UPDATE",
    "PipelineHandoff",
    "build_pipeline_handoff_from_intent_state",
    "clear_dispatched_operation_payload_update",
    "detect_dispatchable_operation",
]
