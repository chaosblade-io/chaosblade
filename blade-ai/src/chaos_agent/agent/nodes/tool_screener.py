"""Tool screener: gate ``execute_loop`` tool_calls against the approved target.

Slotted between ``execute_loop`` (the LLM node) and ``phase2_tools``
(the LangGraph ``ToolNode``). For every tool_call in the most recent
AIMessage:

  1. Classify the call into an ``EffectiveTarget`` via
     ``chaos_agent.agent.target_guard.infer_effective_target``.
  2. Compare against the snapshot in ``state.approved_target`` via
     ``target_drift_guard``.
  3. Aggregate verdicts and choose one of three routes:

     - ``pass``  — all calls allowed; ToolNode executes normally.
     - ``replan`` — at least one call drifted; clear the approval,
                    set replan_requested + replan_context, and route
                    back to ``agent_loop`` so a fresh plan can be
                    user-approved.
     - ``retry`` — at least one call was BANNED/UNKNOWN; fabricate
                   ToolMessage rejections so the LLM sees the failure
                   and tries again next iteration. Route back to
                   ``execute_loop``.

Two operating modes governed by ``settings.target_guard_enforcing``:

  - **Enforcing** (default in production after grey rollout): the
    above logic runs as described. Rejections actually block tools.
  - **Log-only** (default before grey rollout finishes): the verdict
    is computed and logged at WARNING level for any non-ALLOW result,
    but the call is allowed to proceed to phase2_tools. Used to
    surface false-positives in production traffic before flipping
    enforcement on.

The screener emits a fabricated ToolMessage for EVERY tool_call in the
AIMessage when any one is rejected. LangChain's ToolNode would normally
do this matching; bypassing ToolNode means we have to satisfy the
"every tool_call needs a corresponding ToolMessage" invariant ourselves,
otherwise the next LLM iteration sees a malformed conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.attempt_tracker import (
    REASON_LLM_TARGET_SWITCH,
    begin_attempt,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.target_guard import (
    GuardVerdict,
    approved_from_dict,
    infer_effective_target,
    target_drift_guard,
)
from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


# Sentinel used by ``route_after_screener`` to dispatch to the right
# successor node. Cleared each time the screener runs so a stale
# value can't leak into a later iteration.
SCREENER_ROUTE_PASS = "pass"
SCREENER_ROUTE_REPLAN = "replan"
SCREENER_ROUTE_RETRY = "retry"


async def tool_screener(state: AgentState) -> dict:
    """Inspect pending tool_calls and decide whether to forward them.

    Returns a state delta. The delta always sets ``screener_route`` so
    the conditional edge can dispatch deterministically; it may also
    append synthetic ``ToolMessage`` responses (for REJECT cases) and
    set replan fields (for DRIFT cases).

    Fail-open policy: if the screener itself throws (classifier crash
    on malformed args, unexpected tool_call shape, etc.) the whole
    in-flight turn would die. We catch at the per-tool_call boundary,
    log the exception, and treat the offending call as ALLOW. The
    alternative — fail-closed — would let a classifier bug take
    production down. Operator sees ERROR-level logs and can intervene.
    """
    messages = state.get("messages", [])
    last_msg = messages[-1] if messages else None

    # Defensive: no tool_calls to screen → pass through. This shouldn't
    # happen in practice because ``should_continue_execute_loop`` only
    # routes to "continue" when the last AIMessage has tool_calls, but
    # belt-and-braces.
    if not isinstance(last_msg, AIMessage) or not getattr(last_msg, "tool_calls", None):
        return {"screener_route": SCREENER_ROUTE_PASS}

    approved = approved_from_dict(state.get("approved_target"))
    enforcing = bool(settings.target_guard_enforcing)
    skill_script_allowed = bool(settings.skill_script_default_allow)

    decisions: list[dict[str, Any]] = []
    has_drift = False
    has_other_reject = False
    for tc in last_msg.tool_calls:
        tool_name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")
        tool_args = tc.get("args") if isinstance(tc, dict) else getattr(tc, "args", None)
        tool_call_id = (
            tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
        ) or ""

        try:
            effective = infer_effective_target(
                tool_name, tool_args,
                skill_script_allowed=skill_script_allowed,
            )
            decision = target_drift_guard(effective, approved)
        except Exception as exc:
            # Fail-open: classifier or guard crashed. Log loudly so
            # the bug surfaces, but don't kill the turn — produce an
            # ALLOW decision for this tool_call. The pre-existing
            # safety layers (safety_check, confirmation_gate) still
            # gate the broader plan.
            logger.exception(
                "target_guard: screener crashed on tool=%s args=%r; "
                "failing open (allowing the call)",
                tool_name, tool_args,
            )
            decisions.append({
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "verdict": "allow",  # treated as ALLOW for routing
                "reason": f"screener exception: {exc.__class__.__name__}: {exc}",
                "suggestion": "",
                "effective": None,
            })
            continue

        decisions.append({
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "verdict": decision.verdict.value,
            "reason": decision.reason,
            "suggestion": decision.suggestion,
            "effective": effective,
        })

        if decision.verdict == GuardVerdict.REJECT_DRIFT:
            has_drift = True
        elif decision.verdict in (
            GuardVerdict.REJECT_BANNED, GuardVerdict.REJECT_UNKNOWN,
        ):
            has_other_reject = True

    any_reject = has_drift or has_other_reject

    # Log every non-ALLOW outcome so operators can audit false-positives
    # before flipping enforcement on. Logging happens regardless of mode.
    for d in decisions:
        if d["verdict"] in ("allow", "readonly"):
            continue
        logger.warning(
            "target_guard: %s tool=%s reason=%s%s",
            d["verdict"], d["tool_name"], d["reason"],
            "" if enforcing else " (log-only, enforcement disabled)",
        )

    # Log-only mode: pass through regardless of verdicts.
    if not enforcing or not any_reject:
        return {"screener_route": SCREENER_ROUTE_PASS}

    # Enforcing mode + at least one reject — fabricate ToolMessages so
    # the LangChain conversation stays well-formed (every tool_call
    # needs a matching response) and the LLM sees the rejection text.
    rejection_msgs = [
        ToolMessage(
            content=_format_rejection_for_llm(d, approved is None),
            name=d["tool_name"],
            tool_call_id=d["tool_call_id"],
            status="error",
        )
        for d in decisions
    ]

    delta: dict[str, Any] = {
        "messages": rejection_msgs,
        "screener_route": (
            SCREENER_ROUTE_REPLAN if has_drift else SCREENER_ROUTE_RETRY
        ),
    }

    if has_drift:
        # Build a replan context summarising the drift so agent_loop
        # can re-plan with awareness of what the LLM tried (and was
        # blocked from).
        drifted = [d for d in decisions if d["verdict"] == GuardVerdict.REJECT_DRIFT.value]
        summary_lines = [
            f"target_guard rejected tool_call due to drift from approved target:",
        ]
        for d in drifted:
            summary_lines.append(
                f"  - {d['tool_name']}: {d['reason']}. {d['suggestion']}"
            )
        replan_context = {
            "error_summary": "\n".join(summary_lines)[:1000],
            "failed_tool_calls": [
                {"name": d["tool_name"], "error": d["reason"]}
                for d in drifted
            ],
            "existing_blade_uids": [],
            "iteration_at_failure": state.get("execute_loop_count", 0),
            "trigger": "target_drift_guard",
        }
        current_replan_count = int(state.get("replan_count", 0) or 0)
        try:
            max_replan = int(settings.max_replan_count)
        except (TypeError, ValueError):
            max_replan = 2
        if current_replan_count < max_replan:
            delta.update({
                "replan_requested": True,
                "replan_context": replan_context,
                "replan_count": current_replan_count + 1,
                # Approval is invalidated — the next confirmation_gate
                # will refreeze after agent_loop produces a fresh plan.
                "approved_target": None,
                "error": None,
            })
            if settings.replan_reset_execute_count:
                delta["execute_loop_count"] = 0

            # Patch E — screener-triggered replans are a distinct
            # attempt category from graph_replan: they signal "agent
            # behaved unsafely" rather than "tool execution failed".
            # Recording them under REASON_LLM_TARGET_SWITCH lets the
            # TUI / TaskStore surface "attempt #N · target switch
            # blocked by guard" so operators can spot patterns of LLM
            # misbehaviour. The pre-merged state is passed via the
            # spread so begin_attempt sees the about-to-increment
            # replan_count + replan_requested values.
            attempt_delta = begin_attempt(
                {**state, **delta},
                target=state.get("fault_spec"),
                reason=REASON_LLM_TARGET_SWITCH,
                notes=replan_context["error_summary"][:200],
            )
            delta.update(attempt_delta)
        else:
            # Out of replan budget — route to retry path instead so the
            # LLM at least gets a chance to abort gracefully. The
            # ToolMessages already carry the rejection reasons.
            delta["screener_route"] = SCREENER_ROUTE_RETRY
            logger.warning(
                "target_guard: drift detected but replan_count=%d already at max=%d; "
                "falling back to retry-in-place",
                current_replan_count, max_replan,
            )

    return delta


def route_after_screener(state: AgentState) -> str:
    """Map the screener's ``screener_route`` field to a graph edge.

    Mirrors the SCREENER_ROUTE_* sentinels. Defaults to "pass" so a
    missing/unknown value never strands the graph.
    """
    route = state.get("screener_route") or SCREENER_ROUTE_PASS
    if route == SCREENER_ROUTE_REPLAN:
        return "replan"
    if route == SCREENER_ROUTE_RETRY:
        return "retry"
    return "pass"


def _format_rejection_for_llm(decision: dict[str, Any], approved_missing: bool) -> str:
    """Render a ToolMessage body explaining why the call was blocked.

    Three goals:
      - Tell the LLM WHAT went wrong (reason) so it can rethink.
      - Tell the LLM what WOULD have been allowed (suggestion).
      - Be short — long rejections waste context tokens.
    """
    verdict = decision["verdict"]
    reason = decision["reason"]
    suggestion = decision["suggestion"]
    parts = [
        f"[target_guard] {verdict.upper()} — {reason}",
    ]
    if suggestion:
        parts.append(suggestion)
    if approved_missing and verdict == GuardVerdict.REJECT_UNKNOWN.value:
        parts.append(
            "no approved target on record; the screener default-denies "
            "destructive calls until confirmation_gate has been passed."
        )
    parts.append(
        "If your intended target is genuinely different, emit a [REPLAN] "
        "to revise the plan; otherwise correct the tool_call to match "
        "the approved target."
    )
    return " ".join(parts)


__all__ = [
    "SCREENER_ROUTE_PASS",
    "SCREENER_ROUTE_REPLAN",
    "SCREENER_ROUTE_RETRY",
    "route_after_screener",
    "tool_screener",
]
