"""Phase screeners as graph-edge nodes — the unified screening paradigm.

``phase1_screener`` and ``tool_screener`` proved the pattern: an
independent graph node inspects the pending ``tool_calls`` between the
LLM node and its ToolNode, fabricates a ToolMessage for EVERY call in a
refused batch (rejection for offenders, "skipped" notice for legitimate
siblings — keeping the "every tool_call needs a ToolMessage" invariant),
and routes the batch back to the LLM node via ``screener_route``.

The read-only phases were screened differently (a ToolNode wrapper for
the capability dimension, a strip-and-advise pass inside the loop node
for the read-only discipline). This module migrates them to the same
edge-node paradigm:

  verifier_loop            -> verifier_screener          -> verifier_tools
  recover_verifier_loop    -> recover_verifier_screener  -> recover_verifier_tools
  plan_builder             -> plan_builder_screener      -> plan_builder_tools

Each screener runs a two-stage verdict on the same batch:

  1. capability: ``capabilities.screen_tool_calls`` — may this tool run
     in this environment? (shared verdict + fail-closed-on-exception);
  2. read-only discipline (when the phase demands it):
     ``_readonly_screen.find_readonly_violations`` — the target_guard
     classifier verdict, the shared capability-probe exemption
     (``kubectl_read debug``), fail-open on classifier errors.

Any violation refuses the WHOLE batch: offenders get a rejection
ToolMessage, legitimate siblings a skipped notice. This replaces the old
wrapper's partial-dispatch behaviour — the model re-issues a clean
batch next turn, matching phase1/tool_screener semantics.

Route values REUSE the existing ``state.screener_route`` field (shared
by design with phase1_screener/tool_screener — only one screener is
ever on the active path); each router reads the field with a "pass"
default.

Evolution note (not a defect of this design): unification currently
reaches the PROTOCOL layer (fabricated ToolMessage pairing + shared
``screener_route``), not the implementation layer — ``phase1_screener``
and ``tool_screener`` remain hand-written because they carry
phase-specific duties this factory deliberately does not model:
``tool_screener`` runs the target-drift guard (``check_target``,
execute-only) and can route ``replan``; ``phase1_screener`` carries
the capability-probe exemption (``kubectl_read debug``). Folding them
in would require
replan / drift-guard hooks whose complexity would exceed the payoff.
Revisit only if those duties ever crystallise into pluggable hooks.
"""

from __future__ import annotations

import logging
from typing import Callable

from langchain_core.messages import ToolMessage

logger = logging.getLogger(__name__)

# Sentinel route values — same shape as phase1_screener's.
SCREENER_ROUTE_PASS = "pass"
SCREENER_ROUTE_RETRY = "retry"


def make_phase_screener(
    *,
    capability_phase: str,
    readonly: bool | Callable[[dict], bool] = False,
    phase_duty: str = "",
    verdict_guidance: str = "",
    stop_retry_hint: bool = False,
) -> tuple[Callable, Callable]:
    """Build ``(screener_node, route_fn)`` for one phase's ToolNode edge.

    Args:
        capability_phase: capability phase key registered in
            ``capabilities.context._PHASE_TO_PROVIDER_PHASE`` (an
            unregistered phase fails closed and would block every call).
        readonly: ``True`` when the phase may only observe, or a
            ``state -> bool`` predicate (recover gates on
            ``recover_phase`` — Layer 1 repairs, Layer 2 only verifies).
        phase_duty: one-sentence duty statement rendered into rejection
            feedback (should name what the phase judges and what it
            never does).
        verdict_guidance: instructions for the correct verdict path;
            must name the phase's verdict word (``unverified`` /
            ``unrecovered``).
        stop_retry_hint: append "retrying cannot work — reply with your
            conclusion as plain text" when the WHOLE batch is refused.
            Required for loops with no iteration bound
            (``should_continue_plan_builder``) so a stubborn model
            cannot spin to the graph recursion limit.

    Returns:
        ``(node, route_fn)``: the async graph node and its conditional
        edge dispatcher reading ``state.screener_route``.
    """
    from chaos_agent.agent.capabilities import (
        explain_tool_refusal,
        screen_tool_calls,
        tool_call_field,
    )
    from chaos_agent.agent.nodes._guard_rejection import (
        is_malformed_probe,
        read_only_rejection_reason,
        scope_floor_note,
    )
    from chaos_agent.agent.target_guard.classifier import (
        SCOPE_BANNED,
        SCOPE_UNKNOWN,
    )

    async def screener_node(state: dict) -> dict:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        calls = list(getattr(last, "tool_calls", None) or [])
        if not calls:
            return {"screener_route": SCREENER_ROUTE_PASS}

        # Stage 1 — capability verdict (shared, fail-closed-on-exception).
        _, cap_rejected = screen_tool_calls(calls, state, capability_phase)
        rejected_ids = {tool_call_field(c, "id") for c in cap_rejected}

        # Stage 2 — read-only discipline (classifier verdict, probe
        # exemption, fail-open). Skipped entirely when the phase allows
        # mutations right now (recover Layer 1).
        ro_active = readonly(state) if callable(readonly) else bool(readonly)
        ro_violations = []
        if ro_active:
            from chaos_agent.agent.nodes._readonly_screen import (
                find_readonly_violations,
            )

            ro_violations = [
                v for v in find_readonly_violations(calls)
                if v[1] not in rejected_ids
            ]
        ro_ids = {v[1] for v in ro_violations}

        if not rejected_ids and not ro_ids:
            return {"screener_route": SCREENER_ROUTE_PASS}

        # Whole-batch refusal — fabricated ToolMessages keep the
        # conversation well-formed (phase1/tool_screener protocol).
        logger.info(
            "phase screener (%s): refused %d/%d tool_calls "
            "(%d capability, %d read-only; route=retry)",
            capability_phase,
            len(rejected_ids) + len(ro_ids), len(calls),
            len(rejected_ids), len(ro_ids),
        )
        # NOTHING survived the screen: the refusal is systematic (the
        # whole tool surface is wrong for this environment), not a
        # mis-picked tool. Some loops around a screened ToolNode have
        # no iteration bound (should_continue_plan_builder), so say
        # explicitly that retrying cannot work — merged into the LAST
        # rejection so the one-ToolMessage-per-call pairing holds.
        nothing_survived = len(rejected_ids) + len(ro_ids) == len(calls)
        stop_hint = (
            "\n\nNo tool from this domain is available in the current "
            "environment, so retrying or substituting another one will "
            "be refused as well. Stop calling tools and reply with your "
            "conclusion as plain text."
            if stop_retry_hint and nothing_survived
            else ""
        )

        ro_details = {v[1]: v[2] for v in ro_violations}
        ro_effectives = {v[1]: v[4] for v in ro_violations}
        fabricated: list[ToolMessage] = []
        for idx, c in enumerate(calls):
            c_id = tool_call_field(c, "id")
            c_name = tool_call_field(c, "name") or "<unknown tool>"
            if c_id in rejected_ids:
                # Truthful cause from the module holding the resolved profile
                # (names the profile in force and what to use instead) — the
                # old generic sentence was the same for every tool in every
                # profile, which is the exact regression intent_screener fixed.
                cap_reason, cap_suggestion = explain_tool_refusal(
                    c_name, state, capability_phase,
                )
                content = (
                    f"Error: capability_profile_violation\n\n"
                    f"{cap_reason} {cap_suggestion}"
                )
                if idx == len(calls) - 1:
                    content += stop_hint
            elif c_id in ro_ids:
                # Shared truth-first renderer (reject_detail > probe reason
                # > raw command > scope word) — same chain phase1_screener
                # uses. Rendering from the two string fields alone (the old
                # way) silently dropped the recorded reject_detail, e.g. the
                # host-escape primitive the classifier had named.
                eff = ro_effectives[c_id]
                ro_reason, ro_suggestion = read_only_rejection_reason(eff)
                probe_refusal = is_malformed_probe(eff)
                fix_block = (
                    f"How to fix: {ro_suggestion}\n" if ro_suggestion else ""
                )
                shape_note = (
                    "This refusal is about the COMMAND SHAPE, not about the "
                    "tool itself — a correctly shaped read-only probe IS "
                    "allowed here and will pass.\n"
                    if probe_refusal else ""
                )
                # Anti-bypass floor only for BANNED/UNKNOWN: that half of
                # scope_floor_note is phase-neutral ("all mutation paths are
                # blocked here by the same classifier"). Its other half names
                # Phase 2 as the forward path — planning-specific and wrong
                # here, where phase_duty already carries the boundary frame.
                floor_note = (
                    scope_floor_note(eff.scope)
                    if not probe_refusal
                    and eff.scope in (SCOPE_BANNED, SCOPE_UNKNOWN)
                    else ""
                )
                content = (
                    f"Error: readonly_phase_violation\n\n"
                    f"'{ro_details.get(c_id, c_name)}' was REFUSED and "
                    f"nothing was executed. {phase_duty}\n"
                    f"Reason: {ro_reason}\n"
                    f"{fix_block}"
                    f"{shape_note}"
                    f"{verdict_guidance}"
                )
                if floor_note:
                    content += f"\n\n{floor_note}"
                if idx == len(calls) - 1:
                    content += stop_hint
            else:
                content = (
                    "(skipped — a sibling tool_call in this batch "
                    "violated the phase screen; resolve the violation "
                    "and re-issue this call alone in the next turn if "
                    "still needed)"
                )
            fabricated.append(ToolMessage(
                content=content,
                tool_call_id=c_id,
                name=c_name,
                # Routers skip error ToolMessages when deciding whether
                # a control tool ran; an unmarked refusal would be read
                # as a successful result (route_after_verifier_tools).
                status="error",
            ))

        return {
            "messages": fabricated,
            "screener_route": SCREENER_ROUTE_RETRY,
        }

    def route_fn(state: dict) -> str:
        """Conditional edge dispatcher — reads the route the screener set."""
        return state.get("screener_route", SCREENER_ROUTE_PASS)

    return screener_node, route_fn


__all__ = ["SCREENER_ROUTE_PASS", "SCREENER_ROUTE_RETRY", "make_phase_screener"]
