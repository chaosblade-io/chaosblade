"""B76 ③ — guard rejections travel into replan context as HARD constraints.

B76's cost amplifier: a replan round re-wrote a plan whose addressing form the
target_guard had already rejected ("case exists" beat "this form is dead" in
the planner's reasoning), burning a full planning cycle before the terminal
rejection. The fix funnels guard rejection receipts into
``replan_context.guard_rejections`` and renders them as constraints —
categorically distinct from the "evidence, not verdict" failure chain.
"""

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import pytest

from chaos_agent.agent.nodes.execute.execute_loop import (
    _build_replan_context,
    _collect_guard_rejections,
)
from chaos_agent.agent.nodes.planning.tool_screener import (
    _CLEARED_VERDICTS,
    _format_deferred_for_llm,
    _format_rejection_for_llm,
)
from chaos_agent.agent.prompts.sections.workflow import get_replan_section
from chaos_agent.agent.replan import ReplanRequest
from chaos_agent.agent.target_guard.types import GuardVerdict


def _guard_tool_msg(content: str, tool_call_id: str = "tc_g") -> ToolMessage:
    return ToolMessage(content=content, name="kubectl", tool_call_id=tool_call_id)


_REJECT_DRIFT_MSG = (
    "[target_guard] REJECT_DRIFT — resource selection drift: "
    "approved.labels={'app': 'drill-nodedown-target'} vs "
    "effective.names=['cn-shanghai-cloudspe.25.209.71.189']"
)


def test_collect_parses_verdict_newest_first_with_limit():
    messages = [
        _guard_tool_msg("[target_guard] REJECT_BANNED — old rejection", "tc_1"),
        ToolMessage(content="kubectl get nodes …", name="kubectl", tool_call_id="tc_2"),
        _guard_tool_msg(_REJECT_DRIFT_MSG, "tc_3"),
    ]
    rejections = _collect_guard_rejections(messages)
    assert [r["verdict"] for r in rejections] == ["REJECT_DRIFT", "REJECT_BANNED"]
    assert rejections[0]["tool"] == "kubectl"
    assert "resource selection drift" in rejections[0]["message"]

    many = [
        _guard_tool_msg(f"[target_guard] REJECT_DRIFT — rejection {i}", f"tc_{i}")
        for i in range(7)
    ]
    assert len(_collect_guard_rejections(many)) == 5


def test_collect_excludes_stagnant_and_unknown_verdicts():
    """P1-1: REJECT_STAGNANT alternates by design (its receipt says "adjust
    and retry") and an unknown verdict string may be pre-upgrade text —
    neither is a never-relaxing boundary, so both stay in the failure
    chain's evidence semantics instead. GuardVerdict.is_form_level_rejection
    is the single source of truth; this pins the collector to it."""
    messages = [
        _guard_tool_msg("[target_guard] REJECT_STAGNANT — repeated 6 times", "tc_s"),
        _guard_tool_msg("[target_guard] SOME_FUTURE_VERDICT — not in the enum", "tc_u"),
        _guard_tool_msg("[target_guard] REJECT_UNKNOWN — unknown tool", "tc_k"),
    ]
    rejections = _collect_guard_rejections(messages)
    assert [r["verdict"] for r in rejections] == ["REJECT_UNKNOWN"]


def test_collect_ignores_non_guard_and_mid_sentence_mentions():
    messages = [
        ToolMessage(content="Error: kubectl get (exit 1): not found", name="kubectl", tool_call_id="tc_e"),
        # Guard prefix NOT at position 0 — prose quoting the guard, not a receipt.
        ToolMessage(content="note: saw [target_guard] REJECT_DRIFT earlier", name="kubectl", tool_call_id="tc_p"),
        ToolMessage(content="[some-other-guard] REJECT — different emitter", name="kubectl", tool_call_id="tc_o"),
    ]
    assert _collect_guard_rejections(messages) == []


def test_build_replan_context_carries_guard_rejections():
    state = {
        "messages": [
            HumanMessage(content="plan"),
            _guard_tool_msg(_REJECT_DRIFT_MSG),
        ],
        "execute_loop_count": 3,
    }
    request = ReplanRequest(
        kind="feasibility",
        decision="plan_invalid",
        invalidated_assumption="approved selector resolves to zero nodes",
        affected_step="inject fault",
    )
    context = _build_replan_context(state, request)
    rejections = context.get("guard_rejections")
    assert rejections and rejections[0]["verdict"] == "REJECT_DRIFT"
    assert "resource selection drift" in rejections[0]["message"]


def test_replan_section_renders_guard_rejections_as_hard_constraints():
    section = get_replan_section({
        "error_summary": "selector resolves to zero nodes",
        "iteration_at_failure": 3,
        "failed_tool_calls": [
            {"name": "kubectl", "args": {}, "error": "Error: exit 1"},
        ],
        "guard_rejections": [
            {"verdict": "REJECT_DRIFT", "tool": "kubectl", "message": _REJECT_DRIFT_MSG},
        ],
    })
    assert "GUARD REJECTIONS — HARD CONSTRAINTS" in section
    assert "NOT" in section and "evidence to re-weigh" in section
    assert "REJECT_DRIFT" in section
    # The escape hatches must be named: fix the contract or reject.
    assert "propose_plan_change" in section
    assert "finish_planning(rejected=True)" in section
    # The ordinary failure chain keeps its own, evidence-flavoured framing.
    assert "Failure Chain" in section


def test_replan_section_without_guard_rejections_has_no_constraint_block():
    section = get_replan_section({
        "error_summary": "tool error",
        "iteration_at_failure": 1,
        "failed_tool_calls": [],
    })
    assert "GUARD REJECTIONS" not in section


# ── P2-2 golden contract: producer → collector → renderer on REAL receipts ──
#
# The unit tests above pin the collector/renderer on representative fixtures.
# This block anchors the actual chain: every GuardVerdict member is rendered
# by the REAL _format_rejection_for_llm / _format_deferred_for_llm (the same
# branch the screener takes at tool_screener.py's rejection_msgs site), then
# fed to the REAL collector. Any drift in the receipt format (separator,
# casing, verdict set) or in the form-level classification turns this red —
# which the mutation probe proved the fixture-only tests cannot catch.


def _render_real_receipt(verdict: GuardVerdict) -> str:
    """Render exactly as the screener does for this verdict's decisions."""
    decision = {
        "verdict": verdict.value,
        "tool_name": "kubectl",
        "reason": f"golden receipt for {verdict.value}",
        "suggestion": "reshape the call",
        "is_hard_floor": False,
        "effective": None,
    }
    if verdict.value in _CLEARED_VERDICTS:
        return _format_deferred_for_llm(decision)
    return _format_rejection_for_llm(decision, approved_missing=False, approved=None)


def test_golden_receipt_shape_for_every_verdict():
    """Every REJECT_* receipt opens with ``[target_guard] VERDICT — ``
    (the exact prefix the collector regex anchors on); cleared verdicts
    render as DEFERRED and never carry the [target_guard] prefix."""
    for verdict in GuardVerdict:
        receipt = _render_real_receipt(verdict)
        if verdict.value in _CLEARED_VERDICTS:
            assert receipt.startswith("[screener] DEFERRED"), receipt
        else:
            assert receipt.startswith(f"[target_guard] {verdict.name} — "), receipt


def test_golden_chain_collects_form_level_verdicts_only():
    """Real receipts through the real collector: DRIFT/BANNED/UNKNOWN enter
    the hard-constraint set; STAGNANT (alternates by design) and the cleared
    DEFERRED renderings do not. The membership is not restated here — it is
    read off GuardVerdict.is_form_level_rejection, the single source."""
    receipts = [
        ToolMessage(content=_render_real_receipt(v), name="kubectl", tool_call_id=f"tc_{v.name}")
        for v in GuardVerdict
    ]
    collected = _collect_guard_rejections(receipts)
    expected = sorted(
        v.name for v in GuardVerdict if v.is_form_level_rejection
    )
    assert sorted(r["verdict"] for r in collected) == expected
    # And what entered renders as hard constraints in the replan section.
    section = get_replan_section({
        "error_summary": "selector resolves to zero nodes",
        "iteration_at_failure": 1,
        "failed_tool_calls": [],
        "guard_rejections": collected,
    })
    assert "GUARD REJECTIONS — HARD CONSTRAINTS" in section
    for r in collected:
        assert f"[{r['verdict']}]" in section


def test_enum_form_level_classification_is_pinned():
    """Pin the classification itself: the three form-level verdicts, and
    everything else False — a NEW verdict must consciously opt in (default
    fail-open to evidence semantics), not silently freeze plans."""
    form_level = {v.name for v in GuardVerdict if v.is_form_level_rejection}
    assert form_level == {"REJECT_DRIFT", "REJECT_BANNED", "REJECT_UNKNOWN"}
    for cleared in ("allow", "readonly"):
        assert GuardVerdict(cleared).is_form_level_rejection is False
    assert GuardVerdict.REJECT_STAGNANT.is_form_level_rejection is False


# ── Contract-replacement boundary (B76 review P1-2) ──
#
# Guard receipts are CONTRACT-RELATIVE: each was judged against the approved
# target frozen at the time. A plan-change approval replaces that contract —
# and the B76 canonical continuation re-approves the very form an old receipt
# rejected (corrupted node+label contract → CLI auto-approve re-targets the
# user's named node). Collected blindly, the old receipt would render the NEW
# contract's only legal target as a never-relaxing constraint — measured on
# the production chain (guard → _format_rejection_for_llm →
# plan_change_confirm → _build_replan_context → get_replan_section) before
# this fix.


def _approval_notice(cli: bool = True) -> HumanMessage:
    """The exact message shape plan_change_confirm emits, reminder-wrapped."""
    from chaos_agent.agent.prompts.reminder import wrap_system_reminder

    head = (
        "[PLAN CHANGE APPROVED — CLI non-interactive] The proposal re-targets "
        "the node the user explicitly named in the request. "
        if cli
        else ""
    )
    return HumanMessage(content=wrap_system_reminder(
        head + "[PLAN CHANGE APPROVED] FaultSpec revision 1 is now authoritative."
    ))


def test_collect_stops_at_plan_change_approval():
    """Receipts older than the newest approval belong to a replaced
    contract — the scan stops at the approval notice. Both emission forms
    (CLI auto-approve prefix and the plain TUI approval) are boundaries."""
    for notice in (_approval_notice(cli=True), _approval_notice(cli=False)):
        messages = [
            _guard_tool_msg(_REJECT_DRIFT_MSG, "tc_old"),
            notice,
            _guard_tool_msg(
                "[target_guard] REJECT_BANNED — manifest file apply", "tc_new",
            ),
        ]
        rejections = _collect_guard_rejections(messages)
        assert [r["verdict"] for r in rejections] == ["REJECT_BANNED"]


def test_collect_continues_past_rejected_and_retry_notices():
    """REJECTED / RETRY notices replace nothing — the contract stands and
    the receipts around them stay valid constraints."""
    from chaos_agent.agent.prompts.reminder import wrap_system_reminder

    messages = [
        _guard_tool_msg("[target_guard] REJECT_DRIFT — older receipt", "tc_1"),
        HumanMessage(content=wrap_system_reminder(
            "[PLAN CHANGE REJECTED] The user declined the replacement."
        )),
        HumanMessage(content=wrap_system_reminder(
            "[PLAN CHANGE RETRY] The proposal referenced a stale revision."
        )),
        _guard_tool_msg("[target_guard] REJECT_BANNED — newer receipt", "tc_2"),
    ]
    assert [r["verdict"] for r in _collect_guard_rejections(messages)] == [
        "REJECT_BANNED", "REJECT_DRIFT",
    ]


def test_replan_context_after_reapproval_drops_old_contract_receipts():
    """End-to-end on the B76 canonical continuation: the old contract's
    REJECT_DRIFT rejected the user's named node, the approval re-targeted
    that SAME node, the new contract then fails for an unrelated reason —
    the replan must not outlaw the new contract's only legal target."""
    state = {
        "messages": [
            _guard_tool_msg(_REJECT_DRIFT_MSG, "tc_old"),
            _approval_notice(cli=True),
            HumanMessage(content="replan context"),
            ToolMessage(
                content="Error: blade create exit 1 (transport timeout)",
                name="blade_create", tool_call_id="tc_new",
            ),
        ],
        "execute_loop_count": 1,
    }
    request = ReplanRequest(
        kind="feasibility",
        decision="plan_invalid",
        invalidated_assumption="blade create transport timeout",
        affected_step="inject fault",
    )
    context = _build_replan_context(state, request)
    assert context["guard_rejections"] == []
    section = get_replan_section(context)
    # The receipt may still appear in the failure chain (it IS history, and
    # that block's semantics are "evidence to re-weigh") — but never as a
    # never-relaxing constraint on the new contract.
    assert "GUARD REJECTIONS — HARD CONSTRAINTS" not in section
    assert "stays rejected no matter how the new plan words it" not in section


# ── C1: verify-replan branch must see the SAME guard-rejection constraints ──
#
# The verifier's own probes can be guard-rejected (r4 task inject-5552c6e4
# msg[231]), and verify-replan fires right after. Probe-measured before the
# fix: the verify branch's context carried NO guard_rejections and its
# section rendered NO hard-constraint block — the optimistic re-planning
# pathway stayed open exactly where the original deadlock happened.


def test_verify_replan_context_collects_guard_rejections():
    from chaos_agent.agent.nodes.verify._verifier_finalize import (
        _build_verify_replan_context,
    )

    verification = {
        "level": "unverified",
        "layer1": {"status": "passed", "details": "injection executed"},
        "layer2": {"status": "failed", "details": "guard rejected verifier probe"},
        "checklist": {"items": [
            {"step": 3, "status": "failed", "evidence": "probe rejected"},
        ]},
    }
    ctx = _build_verify_replan_context(
        verification, residuals_cleaned=[], verify_replan_count=0,
        skill_name="x",
        messages=[
            _approval_notice(cli=True),  # boundary respected here too
            _guard_tool_msg(_REJECT_DRIFT_MSG, "tc_v"),
        ],
    )
    assert [r["verdict"] for r in ctx["guard_rejections"]] == ["REJECT_DRIFT"]
    section = get_replan_section(ctx)
    assert "GUARD REJECTIONS — HARD CONSTRAINTS" in section
    assert "REJECT_DRIFT" in section


def test_verify_replan_section_without_rejections_has_no_constraint_block():
    from chaos_agent.agent.nodes.verify._verifier_finalize import (
        _build_verify_replan_context,
    )

    verification = {
        "level": "unverified",
        "layer1": {"status": "passed", "details": ""},
        "layer2": {"status": "failed", "details": ""},
        "checklist": {"items": []},
    }
    ctx = _build_verify_replan_context(
        verification, residuals_cleaned=[], verify_replan_count=0,
        skill_name="x", messages=[],
    )
    section = get_replan_section(ctx)
    assert "GUARD REJECTIONS" not in section


# ── C2: the boundary must anchor on the REAL approval message ──
#
# Mutation-proved gap: rewording plan_change_confirm's approval prefix left
# every boundary test green (hand-written fixtures cannot notice) while
# production went blind — old-contract receipts flooded the new contract's
# constraint set. This golden test renders the approval message by calling
# the REAL node, so a wording change turns it red.


@pytest.mark.asyncio
async def test_golden_boundary_uses_real_approval_message():
    from chaos_agent.agent.nodes.planning.plan_change_confirm import (
        plan_change_confirm,
    )
    from chaos_agent.agent.spec.fault_spec import FaultSpec

    intent = (
        "模拟节点宕机：在节点 cn-shanghai-cloudspe.25.209.71.189 上切断该节点与 "
        "API Server 的网络通信"
    )
    current = FaultSpec(
        scope="node", namespace="default",
        labels={"app": "drill-nodedown-target"},
        fault_target="network", fault_action="drop", duration_seconds=240,
        source="cli_nl", user_description=intent,
        objective="simulate node down", boundaries=("test only",),
        constraints=("one logical experiment",),
    )
    proposed = current.to_intent_dict() | {
        "names": ["cn-shanghai-cloudspe.25.209.71.189"], "labels": {},
    }
    call = {
        "name": "propose_plan_change", "id": "golden-1",
        "args": {
            "reason": "selector resolves to zero nodes",
            "fault_revision": current.revision,
            "proposed_fault": proposed,
        },
    }
    state = {
        "messages": [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content="ok", name=call["name"], tool_call_id=call["id"]),
        ],
        "interaction_mode": "cli",
        "replan_context": {"error_summary": "selector resolves to zero nodes"},
        "plan_change_reject_count": 0,
        "fault_spec": current.to_dict(),
        "batch_submit_args": None,
    }
    result = await plan_change_confirm(state)  # aligned → CLI auto-approve
    real_notice = result["messages"][0]
    assert "[PLAN CHANGE APPROVED" in real_notice.content
    # The explicit contract seam (C3): the old context must not survive.
    assert result["replan_context"] is None

    messages = [
        _guard_tool_msg(_REJECT_DRIFT_MSG, "tc_old"),
        real_notice,
        _guard_tool_msg(
            "[target_guard] REJECT_BANNED — new contract receipt", "tc_new",
        ),
    ]
    assert [r["verdict"] for r in _collect_guard_rejections(messages)] == [
        "REJECT_BANNED",
    ]
