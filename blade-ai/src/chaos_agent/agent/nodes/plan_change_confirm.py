"""Plan change confirmation: replan-only fault type switch with user approval.

Slotted after ``phase1_tools`` when the LLM calls ``propose_plan_change``
during a replan re-entry. Presents a comparison card to the user (TUI) or
auto-rejects (CLI) and tracks rejection count for hard termination.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from langgraph.types import interrupt

from chaos_agent.agent.fault_spec import read_fault_spec
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory

logger = logging.getLogger(__name__)


async def plan_change_confirm(state: AgentState) -> dict:
    """Confirm or reject an LLM-proposed fault type change during replan."""
    proposed = _extract_proposal(state)
    if not proposed:
        return {}

    spec = read_fault_spec(state)
    if not spec:
        return {}

    plan_change_reject_count = int(state.get("plan_change_reject_count") or 0)

    if state.get("interaction_mode") == "cli":
        plan_change_reject_count += 1
        if plan_change_reject_count >= 2:
            logger.info("plan_change_confirm: CLI auto-reject #%d → terminating", plan_change_reject_count)
            sync_node_status_to_session(state, "plan_change_confirm",
                "CLI auto-reject terminated (2nd rejection)",
                detail={"approved": False, "cli": True, "reject_count": plan_change_reject_count})
            result = {
                "plan_change_reject_count": plan_change_reject_count,
                **fail_state(
                    FailureCategory.USER_REJECTED,
                    "Plan change rejected twice in CLI mode; terminating.",
                ),
            }
            await sync_to_store(state, result)
            return result
        logger.info("plan_change_confirm: CLI auto-reject #%d", plan_change_reject_count)
        sync_node_status_to_session(state, "plan_change_confirm",
            "CLI auto-reject (plan change not supported)",
            detail={"approved": False, "cli": True, "reject_count": plan_change_reject_count})
        result = {
            "plan_change_reject_count": plan_change_reject_count,
            "messages": [HumanMessage(content=(
                "[PLAN CHANGE REJECTED] CLI mode does not support interactive "
                "plan changes. Use finish_planning(rejected=True) or continue "
                "with the original fault type."
            ))],
        }
        await sync_to_store(state, result)
        return result

    plan_change_info = {
        "type": "plan_change",
        "reason": proposed["reason"],
        "original": {
            "scope": spec.scope,
            "blade_target": spec.blade_target,
            "blade_action": spec.blade_action,
            "fault_type": spec.fault_type,
        },
        "proposed": {
            "scope": proposed["scope"],
            "blade_target": proposed["target"],
            "blade_action": proposed["action"],
            "fault_type": f"{proposed['scope']}-{proposed['target']}-{proposed['action']}",
        },
    }
    decision = interrupt(plan_change_info)

    if decision == "approved":
        new_spec = spec.replace(
            scope=proposed["scope"],
            blade_target=proposed["target"],
            blade_action=proposed["action"],
        )
        logger.info("plan_change_confirm: approved → %s", new_spec.fault_type)
        sync_node_status_to_session(state, "plan_change_confirm",
            f"Plan change approved: {spec.fault_type} → {new_spec.fault_type}",
            detail={"approved": True, "old_fault_type": spec.fault_type,
                    "new_fault_type": new_spec.fault_type})
        result = {
            "fault_spec": new_spec.to_dict(),
            "skill_name": None,
            "plan": None,
            "plan_path": None,
            "is_complex": False,
            "matched_use_case_path": None,
            "plan_change_reject_count": 0,
            "messages": [HumanMessage(content=(
                f"[PLAN CHANGE APPROVED] Fault type changed to "
                f"{new_spec.fault_type}. Re-activate the correct skill "
                f"and continue planning with the new approach."
            ))],
        }
        await sync_to_store(state, result)
        return result

    plan_change_reject_count += 1
    if plan_change_reject_count >= 2:
        logger.info("plan_change_confirm: rejected #%d → terminating", plan_change_reject_count)
        sync_node_status_to_session(state, "plan_change_confirm",
            "Plan change rejected twice — terminating",
            detail={"approved": False, "reject_count": plan_change_reject_count})
        result = {
            "plan_change_reject_count": plan_change_reject_count,
            **fail_state(
                FailureCategory.USER_REJECTED,
                "Plan change rejected twice; terminating planning.",
            ),
        }
        await sync_to_store(state, result)
        return result
    logger.info("plan_change_confirm: rejected #%d", plan_change_reject_count)
    sync_node_status_to_session(state, "plan_change_confirm",
        "Plan change rejected by user",
        detail={"approved": False, "reject_count": plan_change_reject_count})
    result = {
        "plan_change_reject_count": plan_change_reject_count,
        "messages": [HumanMessage(content=(
            "[PLAN CHANGE REJECTED] The user declined the proposed change. "
            "Continue with the original fault type, try a different "
            "alternative, or use finish_planning(rejected=True) to abort."
        ))],
    }
    await sync_to_store(state, result)
    return result


def _extract_proposal(state: AgentState) -> dict[str, Any] | None:
    """Extract propose_plan_change args from the most recent AIMessage."""
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
            if name == "propose_plan_change":
                args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", {})
                return {
                    "reason": args.get("reason", ""),
                    "scope": args.get("scope", ""),
                    "target": args.get("target", ""),
                    "action": args.get("action", ""),
                }
        break
    return None
