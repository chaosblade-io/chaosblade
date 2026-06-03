"""Last-resort guard: reject mutation tool_calls in Phase 1.

Sits between ``agent_loop`` (planner LLM) and ``phase1_tools``
(LangGraph ``ToolNode``). For every ``tool_call`` in the most recent
AIMessage:

  1. Classify via ``target_guard.classifier.infer_effective_target``
     (the same classifier the phase 2 ``tool_screener`` uses).
  2. If the verdict is anything other than READONLY, fabricate a
     ``ToolMessage`` rejection so the LLM sees the failure and can
     adjust on the next turn.
  3. Aggregate into one of two routes:
     - ``pass``  — every call is read-only; ``ToolNode`` runs them.
     - ``retry`` — at least one call mutates; rejections appended,
                   loop back to ``agent_loop`` so the LLM can adjust.

Why this exists despite the earlier layers (A-E):

  - Layer A removes the full ``kubectl`` from Phase 1 tool surface
    (replaced by ``kubectl_ro``). This kills the documented bypass
    (``kubectl exec ... blade create``) at the schema level.
  - Layer D rewrites the ToolNode error so the LLM stops getting hints
    to retry via alternative tools.
  - Layers B / C / E reinforce the same intent in docstrings, system
    prompt, and skill-resource wrappers.

  All of those are PROMPT- or SCHEMA-level signals. They make the LLM
  reliably do the right thing in steady state, but they cannot enforce
  an invariant — a docstring tweak, a tool-table refactor, or a future
  LLM that hallucinates a tool name could re-open a bypass. This
  screener is the **runtime enforcement**: it runs the same recursive
  classifier the phase 2 screener uses, so any call equivalent to
  ``blade_create`` is recognised whether it's invoked directly, via
  ``kubectl exec ... blade create``, via ``kubectl create -f
  some-chaosblade.yaml``, etc.

  In normal traffic this node is a near-zero-cost no-op (one fast-path
  set lookup per tool_call, then a classifier call for the rare misses).
  It only contributes wall-clock time when the LLM tries something it
  shouldn't, and even then it short-circuits the LLM in one turn rather
  than letting the tool execute.

Operating mode:

  - **Default: ENFORCING.** Unlike the phase 2 screener (which has a
    log-only mode for grey rollout — it must not break existing
    execution traffic), Phase 1 enforcement should be on from day one
    because:
      1. It's a NEW guardrail, not a behavior change to existing
         traffic — there are no false-positives to discover.
      2. Layer A already shrinks the tool surface, so the only way to
         reach this screener with a non-READONLY verdict is for the LLM
         to explicitly try a mutation tool name we didn't anticipate.
         Logging that without blocking would defeat the point.
  - The ``settings.phase1_screener_enforcing`` flag (added later) can
    flip to log-only for postmortem analysis if needed; default is
    enforcing.

Routing contract:

  - Sets ``state.screener_route`` to ``"pass"`` or ``"retry"`` (the
    existing field, shared with the phase 2 screener — the wizard
    overwrites it on every pass so no cross-turn leakage).
  - When rejecting, appends ToolMessage(s) for EVERY tool_call in the
    batch (one rejection per bad call, one "skipped" note per
    legitimate sibling). LangChain's ``ToolNode`` would normally
    enforce the "every tool_call needs a corresponding ToolMessage"
    invariant; bypassing ToolNode means we have to satisfy that
    ourselves, otherwise the next LLM iteration sees a malformed
    conversation.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.state import AgentState
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


# Sentinel route values written to ``state.screener_route``. Mirror of
# the phase 2 screener's constants so route_after_phase1_screener and
# route_after_screener can share field semantics. We deliberately
# REUSE the existing ``screener_route`` field (rather than introducing
# a new ``phase1_screener_route``) because the two screeners run in
# disjoint graph regions — phase 1 screener never observes a state
# touched by phase 2 screener and vice versa.
PHASE1_SCREENER_ROUTE_PASS = "pass"
PHASE1_SCREENER_ROUTE_RETRY = "retry"


def _make_violation_message(
    tool_name: str, reason: str, tc_id: str,
) -> ToolMessage:
    """Build the rejection ToolMessage.

    Wording mirrors Layer D's tool-not-found handler so the LLM gets a
    consistent signal across all Phase 1 enforcement paths. Critical
    properties:
      - Does NOT list alternative tools (that's what Layer D's
        original LangChain default did, and it actively trained the
        LLM to bypass via kubectl exec — see task-ce9647931ce1).
      - Explains the restriction is intentional + machine-enforced.
      - Points to the ONLY legitimate forward path: emit final summary
        text without tool_calls.
    """
    return ToolMessage(
        content=(
            f"Error: phase1_readonly_violation\n"
            f"\n"
            f"Tool '{tool_name}' would mutate cluster state in this call.\n"
            f"Reason: {reason}\n"
            f"\n"
            f"Phase 1 (planning) is read-only by runtime enforcement. "
            f"Mutation tools (blade_create, blade_destroy, full kubectl "
            f"with exec/delete/patch/...) are bound automatically in "
            f"Phase 2 after your plan is approved by the user.\n"
            f"\n"
            f"DO NOT retry with `kubectl exec ... blade create` or any "
            f"other equivalent path — all mutation paths are blocked "
            f"here by the same classifier.\n"
            f"\n"
            f"To advance to Phase 2: finish your planning observations, "
            f"then emit a final summary text WITHOUT any tool_calls. The "
            f"system runs safety_check → confirmation_gate → "
            f"execute_loop automatically once you stop calling tools."
        ),
        tool_call_id=tc_id,
        name=tool_name,
        status="error",
    )


def _make_skipped_message(tool_name: str, tc_id: str) -> ToolMessage:
    """Companion ToolMessage for legitimate calls in a batch where a
    sibling tool_call was rejected.

    Required by LangChain's contract: every tool_call in an AIMessage
    must have a corresponding ToolMessage in the next turn, otherwise
    the message history is malformed and the LLM provider raises a
    validation error. The "skipped" note tells the LLM that this call
    would have been legitimate but was held back so the whole batch
    can be re-issued cleanly next turn.
    """
    return ToolMessage(
        content=(
            "(skipped — a sibling tool_call in this batch violated "
            "Phase 1 read-only enforcement; resolve the violation and "
            "re-issue this call alone in the next turn if still needed)"
        ),
        tool_call_id=tc_id,
        name=tool_name,
    )


async def phase1_screener(state: AgentState) -> dict[str, Any]:
    """Inspect pending tool_calls; reject mutations.

    Fail-open policy on classifier errors: if ``infer_effective_target``
    itself raises (malformed args, unexpected tool_call shape) we let
    the call through with a WARNING log. The downstream ``ToolNode`` +
    Layer D handler will catch genuinely-unknown tools anyway, and
    fail-closing on a classifier bug would create unrecoverable loops
    (LLM has no way to satisfy a phantom rejection).
    """
    # Enforcing switch — default True. Operators can flip to False via
    # ``BLADE_AI_PHASE1_SCREENER_ENFORCING=false`` for postmortem
    # log-only mode after an incident.
    enforcing = getattr(settings, "phase1_screener_enforcing", True)

    messages = state.get("messages", [])
    if not messages:
        return {"screener_route": PHASE1_SCREENER_ROUTE_PASS}

    last = messages[-1]
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"screener_route": PHASE1_SCREENER_ROUTE_PASS}

    rejections: list[ToolMessage] = []
    legitimate_ids: list[tuple[str, str]] = []  # (tool_name, tc_id)

    for tc in last.tool_calls:
        # tool_call may be a dict (LangChain >=0.3 TypedDict form) or
        # an object (older releases / custom wrappers). Mirror the
        # shape-handling that the phase 2 ``tool_screener`` uses so
        # both screeners behave identically across SDK upgrades.
        if isinstance(tc, dict):
            tool_name = tc.get("name") or ""
            tc_args = tc.get("args") if tc.get("args") is not None else {}
            tc_id = tc.get("id") or ""
        else:
            tool_name = getattr(tc, "name", "") or ""
            tc_args_raw = getattr(tc, "args", None)
            tc_args = tc_args_raw if tc_args_raw is not None else {}
            tc_id = getattr(tc, "id", "") or ""

        # Classify via the shared classifier. ``skill_script_allowed``
        # comes from settings — same flag the phase 2 screener honours.
        try:
            effective = infer_effective_target(
                tool_name,
                tc_args,
                skill_script_allowed=getattr(
                    settings, "skill_script_default_allow", False,
                ),
            )
        except Exception as e:
            logger.warning(
                "phase1_screener: classifier raised for %s(%s): %s — "
                "failing open (tool_call passes through)",
                tool_name, tc_args, e,
            )
            legitimate_ids.append((tool_name, tc_id))
            continue

        scope = effective.scope
        if scope == SCOPE_READONLY:
            legitimate_ids.append((tool_name, tc_id))
            continue

        # Non-readonly verdict — build a rejection reason
        if scope == SCOPE_BANNED:
            reason_str = (
                effective.raw_command
                or "operation classified as banned (e.g. exec ... blade "
                   "create, apply -f chaosblade.yaml, ...)"
            )
            reason = f"banned operation — {reason_str[:200]}"
        elif scope == SCOPE_UNKNOWN:
            reason = (
                "unclassifiable call (defensive reject — Phase 1 cannot "
                "verify the call is read-only without classifier signal)"
            )
        else:
            # destructive_known or actual targeted scope — call would
            # mutate cluster state on a real resource
            reason = (
                f"call would mutate {scope} resources "
                f"(classifier verdict: destructive)"
            )

        rejections.append(_make_violation_message(tool_name, reason, tc_id))

    if not rejections:
        return {"screener_route": PHASE1_SCREENER_ROUTE_PASS}

    # Log-only mode: surface the verdict but let the calls through.
    # The Phase 1 ToolNode + Layer D handler still applies as the
    # ultimate backstop.
    if not enforcing:
        logger.warning(
            "phase1_screener (log-only): would reject %d/%d tool_calls "
            "(set BLADE_AI_PHASE1_SCREENER_ENFORCING=true to block)",
            len(rejections), len(last.tool_calls),
        )
        return {"screener_route": PHASE1_SCREENER_ROUTE_PASS}

    # Enforcing — fabricate ToolMessages for every tool_call in the
    # batch (legitimate ones get a "skipped" notice) so the next turn's
    # conversation is well-formed.
    fabricated: list[ToolMessage] = list(rejections)
    rejected_ids = {r.tool_call_id for r in rejections}
    for tool_name, tc_id in legitimate_ids:
        if tc_id not in rejected_ids:
            fabricated.append(_make_skipped_message(tool_name, tc_id))

    logger.info(
        "phase1_screener: rejected %d/%d tool_calls (route=retry)",
        len(rejections), len(last.tool_calls),
    )

    return {
        "messages": fabricated,
        "screener_route": PHASE1_SCREENER_ROUTE_RETRY,
    }


def route_after_phase1_screener(state: AgentState) -> str:
    """Conditional edge dispatcher — reads the route the screener set."""
    return state.get("screener_route", PHASE1_SCREENER_ROUTE_PASS)


__all__ = [
    "phase1_screener",
    "route_after_phase1_screener",
    "PHASE1_SCREENER_ROUTE_PASS",
    "PHASE1_SCREENER_ROUTE_RETRY",
]
