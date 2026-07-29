"""Confirmation gate for a material FaultSpec change during planning.

``FaultSpec`` is the only durable fault contract.  A plan-change tool call is
an ephemeral proposal: this node validates it against the reviewed revision,
asks the user, and atomically replaces the contract only after approval.
"""

from __future__ import annotations

from copy import deepcopy
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import interrupt

from chaos_agent.agent.nodes.store._store_sync import sync_node_status_to_session, sync_to_store
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.agent.spec.fault_spec import (
    FaultSpec,
    is_full_fault_spec_proposal,
    read_fault_spec,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state

logger = logging.getLogger(__name__)


def _terminal_rejection_failure(state: AgentState, detail: str) -> dict:
    context = state.get("replan_context")
    original_error = (
        str(context.get("error_summary") or "").strip()
        if isinstance(context, dict)
        else ""
    )
    if original_error:
        return fail_state(FailureCategory.EXECUTION_FAILED, f"{original_error} | {detail}")
    return fail_state(FailureCategory.USER_REJECTED, detail)


def _extract_proposal(state: AgentState, current: FaultSpec) -> tuple[str, FaultSpec, int] | None:
    """Read one transient proposal from the most recent Phase 1 tool call."""
    for message in reversed(state.get("messages", [])):
        if not isinstance(message, AIMessage):
            continue
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            if name != "propose_plan_change":
                continue
            args = call.get("args") if isinstance(call, dict) else getattr(call, "args", {})
            if not isinstance(args, dict):
                return None
            raw = args.get("proposed_fault")
            if not is_full_fault_spec_proposal(raw):
                return None
            try:
                revision = int(args.get("fault_revision"))
            except (TypeError, ValueError):
                return None
            candidate = FaultSpec.from_intent_args(raw, existing=current)
            if not candidate.is_complete:
                return None
            reason = str(args.get("reason") or "").strip()
            return reason, candidate, revision
        break
    return None


def _public_fault(spec: FaultSpec) -> dict[str, Any]:
    """Use a stable, renderer-friendly projection without a duplicate model."""
    return {
        "scope": spec.scope,
        "blade_target": spec.blade_target,
        "blade_action": spec.blade_action,
        "fault_type": spec.fault_type,
        "fault_spec": spec.to_intent_dict(),
        "boundaries": list(spec.boundaries),
        "constraints": list(spec.constraints),
        "assumptions": list(spec.assumptions),
        "revision": spec.revision,
    }


def _replace_batch_item(state: AgentState, spec: FaultSpec) -> dict | None:
    """Keep the serial batch list equal to the approved canonical contract."""
    batch = state.get("batch_submit_args")
    if not isinstance(batch, dict) or not isinstance(batch.get("faults"), list):
        return None
    try:
        index = int(state.get("current_fault_index") or 0)
    except (TypeError, ValueError):
        return None
    faults = list(batch["faults"])
    if index < 0 or index >= len(faults):
        return None
    faults[index] = spec.to_dict()
    result = deepcopy(batch)
    result["faults"] = faults
    first = FaultSpec.from_dict(faults[0]) if faults else None
    result["fault_revision"] = first.revision if first is not None else 0
    return result


async def plan_change_confirm(state: AgentState) -> dict:
    """Confirm an explicit plan-time replacement of the reviewed FaultSpec."""
    current = read_fault_spec(state)
    if current is None:
        return {}
    proposal = _extract_proposal(state, current)
    if proposal is None:
        return {}
    reason, candidate, submitted_revision = proposal
    if submitted_revision != current.revision:
        return {
            "messages": [HumanMessage(content=(
                "[PLAN CHANGE RETRY] The proposal referenced a stale FaultSpec revision. "
                "Read the current reviewed contract and propose a complete replacement."
            ))],
        }
    if candidate.contract_dict() == current.contract_dict():
        return {
            "messages": [HumanMessage(content=(
                "[PLAN CHANGE RETRY] The proposal does not change the reviewed FaultSpec. "
                "Continue planning or finish with the current contract."
            ))],
        }

    rejected_count = int(state.get("plan_change_reject_count") or 0)
    if state.get("interaction_mode") == "cli":
        rejected_count += 1
        if rejected_count >= 2:
            result = {
                "plan_change_reject_count": rejected_count,
                **_terminal_rejection_failure(state, "Plan change rejected twice in CLI mode; terminating."),
            }
        else:
            result = {
                "plan_change_reject_count": rejected_count,
                "messages": [HumanMessage(content=(
                    "[PLAN CHANGE REJECTED] CLI mode does not support interactive plan changes. "
                    "Continue with the reviewed FaultSpec or finish_planning(rejected=True)."
                ))],
            }
        await sync_to_store(state, result)
        return result

    decision = interrupt({
        "type": "plan_change",
        "reason": reason or "Planning requires a materially different fault contract.",
        "original": _public_fault(current),
        "proposed": _public_fault(candidate),
    })
    if decision != "approved":
        rejected_count += 1
        if rejected_count >= 2:
            result = {
                "plan_change_reject_count": rejected_count,
                **_terminal_rejection_failure(state, "Plan change rejected twice; terminating planning."),
            }
        else:
            result = {
                "plan_change_reject_count": rejected_count,
                "messages": [HumanMessage(content=(
                    "[PLAN CHANGE REJECTED] The user declined the replacement. Continue with the "
                    "reviewed FaultSpec, try a different proposal, or finish_planning(rejected=True)."
                ))],
            }
        await sync_to_store(state, result)
        return result

    approved = candidate.replace(
        revision=current.revision + 1,
        source=current.source,
        user_description=candidate.user_description or current.user_description,
    )
    batch = _replace_batch_item(state, approved)
    result: dict[str, Any] = {
        "fault_spec": approved.to_dict(),
        "skill_name": None,
        "plan": None,
        "plan_path": None,
        "is_complex": False,
        "skill_case_content": None,
        "plan_change_reject_count": 0,
        "safety_status": "pending",
        "safety_reason": None,
        "safety_checked_detail": None,
        "feasibility_report": None,
        "conflict_uids": None,
        "needs_confirmation": False,
        "approved_target": None,
        "baseline_data": None,
        "inject_layer1_cache": None,
        "verification": None,
        "messages": [HumanMessage(content=(
            f"[PLAN CHANGE APPROVED] FaultSpec revision {approved.revision} is now authoritative: "
            f"{approved.fault_type}. Re-evaluate feasibility, choose a matching skill, and build "
            "a fresh plan. Do not reuse evidence or execution assumptions from the old contract."
        ))],
    }
    if batch is not None:
        result["batch_submit_args"] = batch
    sync_node_status_to_session(
        state,
        "plan_change_confirm",
        f"Plan change approved: {current.fault_type} -> {approved.fault_type}",
        detail={"approved": True, "old_revision": current.revision, "new_revision": approved.revision},
    )
    await sync_to_store(state, result)
    return result
