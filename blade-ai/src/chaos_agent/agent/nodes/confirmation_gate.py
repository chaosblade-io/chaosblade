"""Confirmation gate node: interrupt() for human-in-the-loop approval."""

import logging

from langchain_core.messages import AIMessage
from langgraph.types import interrupt

from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.errors import FailureReason
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


def _format_dry_run_preview(state: AgentState) -> str:
    """Render a human-readable preview of what would happen if approved."""
    skill_name = state.get("skill_name", "(未识别)")
    target = state.get("target") or {}
    params = state.get("params") or {}
    plan = state.get("plan_summary") or state.get("plan") or ""

    lines = ["📋 Dry-Run 预览 — 仅展示计划，不会真正执行。"]
    lines.append(f"  • 技能: {skill_name}")

    if isinstance(target, dict) and target:
        ns = target.get("namespace", "—")
        names = target.get("names") or []
        names_str = ", ".join(str(n) for n in names) if isinstance(names, list) else str(names)
        lines.append(f"  • 目标: namespace={ns}  names=[{names_str or '—'}]")

    if isinstance(params, dict) and params:
        scope = params.get("scope", "")
        action = params.get("action", "")
        target_act = params.get("target", "")
        if scope or target_act or action:
            lines.append(f"  • 故障类型: {'-'.join(p for p in (scope, target_act, action) if p)}")
        for k, v in params.items():
            if k in ("scope", "action", "target", "namespace"):
                continue
            lines.append(f"  • {k}: {v}")

    safety_status = state.get("safety_status", "")
    if safety_status:
        lines.append(f"  • 安全检查: {safety_status}")
    safety_reason = state.get("safety_reason")
    if safety_reason:
        lines.append(f"  • 安全说明: {safety_reason}")

    if plan:
        lines.append("")
        lines.append("📝 计划摘要:")
        for ln in str(plan).strip().splitlines():
            lines.append(f"  {ln}")

    lines.append("")
    lines.append("继续 /plan <修改建议> 调整计划，或 /run 落地执行。")
    return "\n".join(lines)


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
    target = state.get("target") or {}
    safety_status = state.get("safety_status", "safe")

    # Emit status: waiting for confirmation
    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "confirmation_gate",
        f"Waiting for human confirmation for skill '{skill_name}'",
        {"skill_name": skill_name, "target": target},
    )

    # Dry-Run preview: emit the "what would happen" AIMessage and exit cleanly.
    if state.get("dry_run"):
        preview = _format_dry_run_preview(state)
        logger.info("dry_run preview emitted for task %s", task_id)
        tracker.complete("Dry-Run preview rendered")
        sync_node_status_to_session(
            state,
            "confirmation_gate",
            "Dry-Run preview rendered",
            detail={"dry_run": True},
        )
        result = {
            "messages": [AIMessage(content=preview)],
            "needs_confirmation": False,
            "plan_summary": state.get("plan_summary") or plan or "",
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
        result = {"needs_confirmation": False}
        await sync_to_store(state, result)
        return result

    # Build the confirmation request
    confirmation_info = {
        "skill_name": skill_name,
        "target": target,
        "plan_summary": plan[:500] if plan else "",
        "safety_status": safety_status,
        "safety_reason": state.get("safety_reason"),
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
            "failure_reason": f"{FailureReason.SAFETY_REJECTED.value}: confirm_required without --force-override; {safety_reason}",
        }
        await sync_to_store(state, result)
        return result

    # Interrupt and wait for resume
    decision = interrupt(confirmation_info)

    if decision == "approved":
        tracker.complete("Execution approved by user")
        sync_node_status_to_session(state, "confirmation_gate", "Execution approved",
            detail={"approved": True})
        result = {"needs_confirmation": False}
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
            "failure_reason": f"{FailureReason.USER_REJECTED.value}: User rejected the execution at confirmation gate",
        }
        await sync_to_store(state, result)
        return result
