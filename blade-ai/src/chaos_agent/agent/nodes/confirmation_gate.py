"""Confirmation gate node: interrupt() for human-in-the-loop approval."""

import logging

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.target_guard import freeze_approved_target
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


def _generate_dry_run_plan(state: AgentState) -> str:
    """Generate a complete injection plan for dry_run (/plan) output."""
    from chaos_agent.agent.plan_generator import generate_injection_plan
    return generate_injection_plan(state)


async def confirmation_gate(state: AgentState) -> dict:
    """Pause execution and wait for human confirmation.

    Uses LangGraph's interrupt() mechanism to pause the graph.
    The caller (Server route) will resume with Command(resume="approved"|"rejected").

    For confirm_required status (P1: same-target same-action overlay),
    CLI mode checks --force-override flag to skip interrupt().

    Dry-Run mode (TUI `/plan`): when ``state.dry_run`` is True, the gate emits
    a preview AIMessage describing what would happen and returns immediately
    (no interrupt). The post-gate router will then send the graph to END.
    """
    task_id = state.get("task_id", "unknown")
    plan = state.get("plan", "")
    skill_name = state.get("skill_name", "")
    # Read FaultSpec once and project to legacy target dict for the
    # confirm_info payload (TUI confirm card still consumes the
    # 4-key target dict shape — TUI rendering layer change is out
    # of scope for the state refactor).
    spec = read_fault_spec(state) or FaultSpec()
    target = {
        "namespace": spec.namespace,
        "names": list(spec.names),
        "labels": dict(spec.labels),
        "resource_type": spec.scope,
    }
    safety_status = state.get("safety_status", "safe")

    # Emit status: waiting for confirmation
    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "confirmation_gate",
        f"Waiting for human confirmation for skill '{skill_name}'",
        {"skill_name": skill_name, "target": target},
    )

    # Dry-Run: generate a complete injection plan and emit as AIMessage.
    if state.get("dry_run"):
        plan_text = _generate_dry_run_plan(state)
        logger.info("dry_run plan generated for task %s", task_id)
        tracker.complete("Dry-Run plan generated")
        sync_node_status_to_session(
            state,
            "confirmation_gate",
            "Dry-Run plan generated",
            detail={"dry_run": True},
        )
        result = {
            "messages": [AIMessage(content=plan_text)],
            "needs_confirmation": False,
            "plan_summary": plan_text,
        }
        await sync_to_store(state, result)
        return result

    # P1: confirm_required with --force-override → skip interrupt
    if safety_status == "confirm_required" and state.get("force_override"):
        logger.info("confirm_required bypassed via --force-override")
        tracker.complete("Execution auto-approved via --force-override")
        sync_node_status_to_session(state, "confirmation_gate",
            "Auto-approved via --force-override",
            detail={"approved": True, "bypass": "force_override"})
        result = {
            "needs_confirmation": False,
            "approved_target": _freeze_from_state(state),
        }
        await sync_to_store(state, result)
        return result

    # Build the confirmation request.
    #
    # Field rationale (added beyond the original 5-key payload so the
    # TUI confirm card can surface what's already in state instead of
    # collapsing everything into safety_reason prose):
    #   · ``params``               — structured fault params (cpu %,
    #                                timeout, …); the plan_summary
    #                                markdown otherwise hides them.
    #   · ``target_health_report`` — DiskPressure / Evicted / Pending
    #                                pre-check; ``state.py`` comment
    #                                explicitly named confirm card as
    #                                the consumer but the surface was
    #                                missing.
    #   · ``conflict_uids``        — structured list (was already
    #                                embedded in safety_reason as
    #                                free text; structured form lets
    #                                the UI render a list + offer
    #                                /show experiments).
    #   · ``pipeline_attempt``     — N>1 means this is a re-attempt
    #                                after a previous failure; the UI
    #                                can surface "attempt N" so the
    #                                user knows.
    #   · ``is_complex``           — formal plan track flag.
    #   · ``plan_path``            — saved plan file path; UI can
    #                                show "Plan saved to xxx.md".
    #   · ``fault_intent``         — semantic classification from L1
    #                                (fault_type / scope / target /
    #                                action). The L2 confirm card
    #                                previously only had ``target``
    #                                (namespace + names) — operators
    #                                had to reverse-engineer "is this
    #                                a mem-load?" from ``params`` keys.
    #                                Surfacing the L1 classification
    #                                makes the fault category visible
    #                                at a glance without changing
    #                                anything else.
    fault_intent_brief = {
        "fault_type": spec.fault_type,    # derived: "{scope}-{target}-{action}"
        "scope":      spec.scope,
        "target":     spec.blade_target,  # blade "target" axis: cpu / mem / network / ...
        "action":     spec.blade_action,  # blade "action" axis: fullload / load / loss / ...
    } if spec and spec.fault_type else None

    confirmation_info = {
        "skill_name": skill_name,
        "fault_intent": fault_intent_brief,
        "target": target,
        "plan_summary": plan[:500] if plan else "",
        "safety_status": safety_status,
        "safety_reason": state.get("safety_reason"),
        "params": dict(spec.params),
        "target_health_report": state.get("target_health_report"),
        "conflict_uids": list(state.get("conflict_uids") or []),
        "pipeline_attempt": int(state.get("pipeline_attempt") or 0),
        "is_complex": bool(state.get("is_complex")),
        "plan_path": state.get("plan_path") or "",
        # E10 — multi-dimensional numeric safety score for confirm card
        # display. None when safety_check hasn't run (e.g. dry_run path).
        "safety_score": state.get("safety_score"),
        # E18 — injection feasibility report (headroom assessment).
        "feasibility_report": state.get("feasibility_report"),
    }

    # P1: confirm_required without --force-override in CLI mode → reject with guidance
    if safety_status == "confirm_required" and state.get("interaction_mode") == "cli":
        safety_reason = state.get("safety_reason", "")
        logger.info("confirm_required rejected: no --force-override in CLI mode")
        tracker.fail("Execution rejected: --force-override required")
        sync_node_status_to_session(state, "confirmation_gate",
            "Rejected: --force-override required for same-action overlay",
            detail={"approved": False, "reason": "force_override_required"})
        result = {
            "safety_status": "rejected",
            "safety_reason": f"{safety_reason} Add --force-override to proceed.",
            "needs_confirmation": False,
            **fail_state(FailureCategory.SAFETY_REJECTED, f"confirm_required without --force-override; {safety_reason}"),
        }
        await sync_to_store(state, result)
        return result

    # Interrupt and wait for resume
    decision = interrupt(confirmation_info)

    if decision == "approved":
        tracker.complete("Execution approved by user")
        sync_node_status_to_session(state, "confirmation_gate", "Execution approved",
            detail={"approved": True})
        # Freeze the approved target so execute_loop's screener can
        # compare every subsequent tool_call against this snapshot.
        # See chaos_agent.agent.target_guard for the policy.
        result = {
            "needs_confirmation": False,
            "approved_target": _freeze_from_state(state),
        }
        await sync_to_store(state, result)
        return result
    else:
        tracker.fail("Execution rejected by user")
        sync_node_status_to_session(state, "confirmation_gate", "Execution rejected",
            detail={"approved": False})
        result = {
            "safety_status": "rejected",
            "safety_reason": "User rejected the execution",
            "needs_confirmation": False,
            **fail_state(FailureCategory.USER_REJECTED, "User rejected the execution at confirmation gate"),
            # Clear any stale approval — the next attempt will refreeze.
            "approved_target": None,
        }
        await sync_to_store(state, result)
        return result


def _freeze_from_state(state: AgentState) -> dict | None:
    """Convenience wrapper around ``freeze_approved_target`` that
    reads from the FaultSpec — the single source of truth. Returns
    None when no spec is on state (the caller should not be reaching
    this function in that case, but we default-deny to make the bug
    visible in the screener's WARNING log rather than silently
    constructing an empty approval)."""
    spec = read_fault_spec(state)
    if spec is None:
        return None
    return freeze_approved_target(
        target={
            "namespace": spec.namespace, "names": list(spec.names),
            "labels": dict(spec.labels), "resource_type": spec.scope,
        },
        params=dict(spec.params),
        blade_scope=spec.scope,
        blade_target=spec.blade_target,
        blade_action=spec.blade_action,
    )
