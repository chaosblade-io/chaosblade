"""intent_confirm node — intent confirmation gate before agent_loop.

Two-layer confirmation defense:
  Layer 1 (this node): Confirms the LLM's understanding of the user's fault
  injection intent before proceeding to planning/execution.
  Layer 2 (confirmation_gate): Confirms the generated plan before actual execution.

Uses LangGraph interrupt() to pause the graph. The TUI renders a summary panel
and collects Y/N from the user. Resume with Command(resume="approved"|"rejected").

If rejected, the graph ends (returns to TUI REPL). The user can continue
the conversation in the next invocation to refine their intent.
"""

from __future__ import annotations

import logging

from langgraph.types import interrupt

from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory

logger = logging.getLogger(__name__)


def _format_intent_summary(fault_intent: dict) -> str:
    """Format fault_intent dict into a human-readable summary."""
    parts = []
    parts.append(f"故障类型: {fault_intent.get('fault_type', '未知')}")
    parts.append(f"范围: {fault_intent.get('scope', '未知')}")
    parts.append(f"目标: {fault_intent.get('target', '未知')}")
    parts.append(f"动作: {fault_intent.get('action', '未知')}")
    parts.append(f"命名空间: {fault_intent.get('namespace', '未知')}")
    if fault_intent.get("labels"):
        parts.append(f"标签选择器: {fault_intent['labels']}")
    if fault_intent.get("names"):
        parts.append(f"目标资源: {', '.join(fault_intent['names'])}")
    if fault_intent.get("params"):
        params_str = ", ".join(f"{k}={v}" for k, v in fault_intent["params"].items())
        parts.append(f"参数: {params_str}")
    if fault_intent.get("user_description"):
        parts.append(f"用户描述: {fault_intent['user_description']}")
    return "\n".join(parts)


async def intent_confirm(state: AgentState) -> dict:
    """Pause and ask user to confirm their fault injection intent.

    Presents a structured summary of the parsed fault intent and waits
    for user approval before routing to agent_loop.

    Resume with Command(resume="approved") to proceed, or
    Command(resume="rejected") to abort (graph ends, back to TUI REPL).
    """
    task_id = state.get("task_id", "")
    fault_intent = state.get("fault_intent") or {}
    intent_confidence = float(state.get("intent_confidence") or 0.0)

    tracker = get_tracker(task_id) if task_id else None
    # Phase 3c.2 — Dry-Run short-circuit. ``/plan <NL>`` runs the
    # whole planning pipeline (agent_loop → safety_check →
    # confirmation_gate) so the user sees a real "what would happen"
    # summary, but the user-facing intent gate is the wrong place to
    # prompt for approval — the user already opted into "preview only"
    # by typing /plan. Without this skip the user would have to click
    # Y on a Layer-1 confirm card before the plan even materialises.
    # ``confirmation_gate`` already understands dry_run and emits the
    # final preview AIMessage, so falling straight through to
    # agent_loop here is what the rest of the graph expects.
    if state.get("dry_run"):
        if tracker:
            tracker.start(
                StatusCategory.NODE,
                "intent_confirm",
                "Dry-Run: 跳过意图确认，进入计划生成",
                {"dry_run": True, "fault_intent": fault_intent},
            )
            tracker.complete("Dry-Run: bypassed Layer-1 confirm")
        logger.info("intent_confirm bypassed for dry_run task %s", task_id)
        return {}

    if tracker:
        tracker.start(
            StatusCategory.NODE,
            "intent_confirm",
            "等待用户确认故障注入意图",
            {"fault_intent": fault_intent, "intent_confidence": intent_confidence},
        )

    # Build confirmation payload for TUI rendering
    summary = _format_intent_summary(fault_intent)
    confirmation_info = {
        "type": "intent_confirm",
        "fault_intent": fault_intent,
        "summary": summary,
        "intent_confidence": intent_confidence,
    }

    # Interrupt: TUI renders the summary and collects Y/N
    decision = interrupt(confirmation_info)

    if decision == "approved":
        if tracker:
            tracker.complete("用户确认意图，进入执行阶段")
        logger.info("Intent confirmed by user: %s", fault_intent.get("fault_type"))
        return {}
    else:
        # User rejected — clear confirmed_intent so router routes to END
        if tracker:
            tracker.complete("用户拒绝意图，返回对话")
        logger.info("Intent rejected by user, returning to conversation")
        return {
            "confirmed_intent": None,
            "fault_intent": None,
        }
