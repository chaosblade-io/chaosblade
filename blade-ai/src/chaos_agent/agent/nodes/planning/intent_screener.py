"""Runtime guard for the intent phase's tool surface — two gates per batch.

Stage 1 (capability, fail-CLOSED): the DISCOVERY rule — transport only,
provisional ``fault_spec.scope`` deliberately ignored — so intent can
recognise every registered fault family while its read-only probe still
matches the environment it inspects.

Stage 2 (read-only discipline, fail-OPEN): the shared target_guard
classifier verdict via ``_readonly_screen.find_readonly_violations`` —
the same verdict source phase1_screener and the verification screens use.

Why stage 2 exists (spec: universal-cognitive-architecture /
domain-command-guard, 「预授权相位只读强制」): intent runs BEFORE the
confirmation gate freezes an ``approved_target``, so its read-only-ness
cannot ride the target-drift guard the way phase 2 does — and it must not
ride Layer A binding either (``kubectl_read``'s Literal constraint),
because binding is exactly what the perception-surface collapse (D8)
removes. The classifier gate is the program-side carrier: a crafted or
stale batch carrying ``kubectl delete pod x`` passes the transport rule
(full ``kubectl`` is a k8s-native tool) and previously reached the
ToolNode unscreened; here it is refused with the verdict the classifier
actually reached.

Whole-batch refusal with fabricated ToolMessage pairing (the
phase1/tool_screener protocol): offenders get a rejection, legitimate
siblings a skipped notice, so the conversation routes back to
intent_clarification well-formed.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage, ToolMessage

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

logger = logging.getLogger(__name__)

INTENT_SCREENER_PASS = "pass"
INTENT_SCREENER_RETRY = "retry"

# Duty statement rendered into every read-only rejection: names the phase
# boundary (pre-confirmation-gate, no approved_target) and the only
# legitimate forward path, so the model reads a PHASE rule it can act on,
# not a capability rule (the scope_floor_note lesson: a phase rule misread
# as a capability rule closes paths that are still open).
INTENT_PHASE_DUTY = (
    "Intent clarification runs before any plan exists and before the "
    "confirmation gate: no approved target is frozen yet, so this phase is "
    "read-only by runtime enforcement. Fault execution is bound only after "
    "the intent is submitted, a plan is approved, and the user confirms it."
)

# NOTE: a ``plan_builder_screener`` used to live here, sharing a parameterised
# ``_screen_provider_tool_calls`` helper with this one. It was never wired into
# ``build_pipeline_graph``, so ``plan_builder_tools`` ran unscreened while a
# passing unit test suggested otherwise. That gap is now closed by the
# ``plan_builder_screener`` graph-edge node built with
# ``nodes._phase_screener.make_phase_screener(capability_phase="plan",
# stop_retry_hint=True)`` in ``graph.py`` — the unified phase1/tool_screener
# paradigm (whole-batch refusal with fabricated ToolMessage pairing). With the
# second caller gone the helper was inlined here: its ``phase`` / ``discovery``
# parameters had a combination (neither set) that silently refused every call,
# and speculative generality is what produced the unwired duplicate in the
# first place.


def intent_screener(state: dict) -> dict:
    """Two-gate screen on the pending batch: transport match, then
    read-only classification.

    Uses the DISCOVERY rule (transport only, provisional ``fault_spec.scope``
    deliberately ignored) for stage 1: intent must be able to recognise every
    registered fault family regardless of the connected environment, while
    its read-only probe still has to match the environment it inspects.

    Whole-batch: one offending call refuses the whole turn back to
    intent_clarification. Kept as-is deliberately — the per-call form
    requires screening INSIDE the ToolNode (which reads the latest
    AIMessage, so appending rejections upstream cannot hide a call from
    it). No cross-profile mixed batch was ever observed in this phase
    before the classifier gate; that gate is the first realistic source
    of mixed batches (a read probe beside a smuggled mutation), and a
    batch like that must not dispatch either.
    """
    messages = state.get("messages", [])
    last = messages[-1] if messages else None
    if not isinstance(last, AIMessage) or not last.tool_calls:
        return {"intent_screener_route": INTENT_SCREENER_PASS}

    calls = list(last.tool_calls)

    # Stage 1 — capability verdict + fail-closed-on-exception come from
    # ``capabilities`` (one implementation shared with every other
    # screener). The allowed half is discarded on purpose: rejecting is
    # whole-batch here (see above).
    _, cap_rejected = screen_tool_calls(calls, state, discovery=True)
    cap_ids = {tool_call_field(c, "id") for c in cap_rejected}

    # Stage 2 — read-only discipline (classifier verdict, shared
    # capability-probe exemption, fail-OPEN per call) on the calls stage 1
    # allowed through. Lazy import mirrors ``_phase_screener``:
    # ``_readonly_screen`` pulls ``phase1_screener`` for the probe
    # exception, so a module-level import would be circular.
    from chaos_agent.agent.nodes._readonly_screen import find_readonly_violations

    ro_violations = [
        v for v in find_readonly_violations(calls)
        if v[1] not in cap_ids
    ]
    ro_by_id = {v[1]: v for v in ro_violations}

    if not cap_ids and not ro_by_id:
        return {"intent_screener_route": INTENT_SCREENER_PASS}

    logger.info(
        "intent_screener: refused %d/%d tool_calls "
        "(%d transport, %d read-only; route=retry)",
        len(cap_ids) + len(ro_by_id), len(calls), len(cap_ids), len(ro_by_id),
    )

    # Whole-batch refusal — fabricate a ToolMessage for EVERY call in the
    # batch (offenders get rejections, legitimate siblings a skipped
    # notice) so the conversation the LLM sees next turn is well-formed.
    fabricated: list[ToolMessage] = []
    for call in calls:
        tc_id = tool_call_field(call, "id")
        if tc_id in cap_ids:
            fabricated.append(_refusal_message(call, state))
        elif tc_id in ro_by_id:
            fabricated.append(_readonly_refusal_message(ro_by_id[tc_id]))
        else:
            fabricated.append(_skipped_message(call))

    return {
        "messages": fabricated,
        "intent_screener_route": INTENT_SCREENER_RETRY,
    }


def _refusal_message(call: object, state: dict) -> ToolMessage:
    """Name the transport in force, then say what to do about it.

    The previous wording ("unavailable for the current environment") was the
    same sentence for every tool in every profile: it never said WHICH
    environment was connected, so the model could only retry variations of the
    same call. ``explain_tool_refusal`` is the one place that holds the resolved
    profile, so the cause comes from there. Its ``suggestion`` half is NOT used —
    that half enumerates the reachable tool names, which the model already sees
    in its own bound tool list; the standing instruction below is enough.
    """
    tool_name = tool_call_field(call, "name")
    # ``discovery=True`` mirrors the flag the verdict was made with above, so the
    # explanation resolves the profile the same way (transport only). Without it
    # a host-scoped intent on a k8s transport — normal in this phase — would be
    # explained as an unregistered environment.
    reason, _suggestion = explain_tool_refusal(tool_name, state, discovery=True)
    return ToolMessage(
        content=f"Error: {reason}. Select a tool bound to the active transport.",
        name=tool_name,
        tool_call_id=tool_call_field(call, "id"),
        status="error",
    )


def _readonly_refusal_message(violation: tuple) -> ToolMessage:
    """Render the classifier verdict the shared, truth-first way.

    Same chain every other read-only screener uses
    (``_guard_rejection.read_only_rejection_reason``: reject_detail >
    probe reason > raw command > scope word) — never a template re-invented
    from the scope word. The phase frame comes from ``INTENT_PHASE_DUTY``,
    so the refusal names the boundary the model actually hit instead of
    implying the tool is unavailable — a binding story the perception-
    surface collapse removes, and which the spec explicitly forbids the
    rejection from depending on.
    """
    tool_name, tc_id, detail, _probe_reason, effective = violation
    reason, suggestion = read_only_rejection_reason(effective)
    probe_refusal = is_malformed_probe(effective)
    fix_block = f"How to fix: {suggestion}\n" if suggestion else ""
    shape_note = (
        "This refusal is about the COMMAND SHAPE, not about the tool "
        "itself — a correctly shaped read-only probe IS allowed here and "
        "will pass.\n"
        if probe_refusal else ""
    )
    # Anti-bypass floor only for BANNED/UNKNOWN: that half of
    # scope_floor_note is phase-neutral ("all mutation paths are blocked
    # here by the same classifier"). Its other half names Phase 2 as the
    # forward path — planning-specific; INTENT_PHASE_DUTY already carries
    # this phase's boundary and forward path.
    floor_note = (
        scope_floor_note(effective.scope)
        if not probe_refusal and effective.scope in (SCOPE_BANNED, SCOPE_UNKNOWN)
        else ""
    )
    content = (
        f"Error: intent_readonly_violation\n"
        f"\n"
        f"'{detail}' was REFUSED — nothing was executed.\n"
        f"{INTENT_PHASE_DUTY}\n"
        f"\n"
        f"Reason: {reason}\n"
        f"{fix_block}"
        f"{shape_note}"
    )
    if floor_note:
        content += f"\n\n{floor_note}"
    return ToolMessage(
        content=content,
        name=tool_name,
        tool_call_id=tc_id,
        status="error",
    )


def _skipped_message(call: object) -> ToolMessage:
    """Companion ToolMessage for legitimate calls in a refused batch.

    LangChain requires every tool_call in an AIMessage to be answered by a
    ToolMessage; the skipped notice tells the model this call would have
    run but was held back so the whole batch can be re-issued cleanly next
    turn. Marked ``error`` so routers that skip error ToolMessages when
    deciding whether a control tool ran keep reading it as "did not run".
    """
    return ToolMessage(
        content=(
            "(skipped — a sibling tool_call in this batch violated the "
            "intent-phase screen; resolve the violation and re-issue this "
            "call alone in the next turn if still needed)"
        ),
        name=tool_call_field(call, "name") or "<unknown tool>",
        tool_call_id=tool_call_field(call, "id"),
        status="error",
    )
