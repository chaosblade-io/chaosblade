"""Tests for explicit FaultSpec plan-change confirmation."""

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.config.settings import settings
from chaos_agent.agent.nodes.planning.plan_change_confirm import (
    _alignment_violation,
    _extract_proposal,
    plan_change_confirm,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec, strip_timeout_alias


def _current() -> FaultSpec:
    return FaultSpec.from_intent_args({
        "objective": "validate packet loss", "scope": "pod", "target": "network",
        "action": "drop", "namespace": "default", "names": ["nginx"],
        "params": {}, "duration_seconds": 60, "boundaries": ["staging only"],
        "constraints": ["one logical experiment"],
    }).replace(revision=3)


def _proposal_call(*, revision: int = 3, action: str = "delay") -> dict:
    proposed = _current().to_intent_dict() | {
        "action": action,
        "labels": {"app": "web"},
        "params": {"time": "3000"},
    }
    return {
        "name": "propose_plan_change", "id": "change-1",
        "args": {
            "reason": "The original method is infeasible; delay is viable.",
            "fault_revision": revision,
            "proposed_fault": proposed,
        },
    }


def _state(*, call=None, mode="tui", rejects=0, batch=None) -> dict:
    call = call or _proposal_call()
    return {
        "messages": [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content="ok", name=call["name"], tool_call_id=call["id"]),
        ],
        "interaction_mode": mode,
        "replan_context": {"error_summary": "test failure"},
        "plan_change_reject_count": rejects,
        "fault_spec": _current().to_dict(),
        "batch_submit_args": batch,
    }


def test_extracts_complete_transient_proposal_against_current_spec():
    state = _state()
    proposal = _extract_proposal(state, _current())
    assert proposal is not None
    reason, candidate, revision = proposal
    assert reason.startswith("The original")
    assert candidate.fault_action == "delay"
    assert revision == 3


def test_extract_rejects_missing_or_malformed_contract():
    no_call = {"messages": []}
    assert _extract_proposal(no_call, _current()) is None
    bad = _proposal_call()
    bad["args"].pop("fault_revision")
    assert _extract_proposal(_state(call=bad), _current()) is None
    partial = _proposal_call()
    partial["args"]["proposed_fault"].pop("constraints")
    assert _extract_proposal(_state(call=partial), _current()) is None


@pytest.mark.asyncio
async def test_approval_replaces_spec_increments_revision_and_resets_runtime_state():
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(_state())

    spec = FaultSpec.from_dict(result["fault_spec"])
    assert spec is not None
    assert spec.fault_action == "delay"
    assert spec.revision == 4
    assert result["plan"] is None
    assert result["safety_status"] == "pending"
    assert result["approved_target"] is None
    assert result["baseline_data"] is None
    assert result["verification"] is None


@pytest.mark.asyncio
async def test_approval_updates_current_batch_item_as_fault_spec():
    batch = {"faults": [_current().to_dict()], "execution_order": "serial"}
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(_state(batch=batch))

    changed = FaultSpec.from_dict(result["batch_submit_args"]["faults"][0])
    assert changed is not None
    assert changed.fault_action == "delay"
    assert changed.revision == 4


@pytest.mark.asyncio
async def test_approval_resets_loop_budgets_and_attribution_for_new_contract():
    """New contract == new budget (task-71fa78b6: rev3 inherited 2/3 replan
    debt from the old contract and died on its first replan)."""
    state = _state()
    state["replan_count"] = 2
    state["verify_replan_count"] = 1
    state["execute_loop_count"] = 17
    state["injection_method"] = "host_blade"
    state["kubectl_exec_pod_name"] = "old-carrier"
    state["injection_start_time"] = "2026-01-01T00:00:00"
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    assert result["replan_count"] == 0
    assert result["verify_replan_count"] == 0
    assert result["execute_loop_count"] == 0
    assert result["injection_method"] is None
    assert result["kubectl_exec_pod_name"] is None
    assert result["injection_start_time"] is None
    # Epoch boundary lands at the seam so RESUME scans only new-contract messages.
    expected_epoch = len(state["messages"]) + len(result["messages"])
    assert result["attribution_epoch_index"] == expected_epoch
    # No live experiment in the old contract -> no UID handle carried over.
    assert result["experiment_uid"] is None


@pytest.mark.asyncio
async def test_approval_resets_cli_drift_tally_for_new_contract():
    """drift_reject_count is measured against the CONTRACT's approved
    identity: the first silent drift is counted (self-correct allowance),
    a second terminates. The old contract's tally must not pre-spend the
    new contract's only allowance (code review 2026-09-11,
    cascade-verified: one drift under the old contract + one under the new
    one terminated a task whose current contract had drifted once — the
    same fresh-budget seam as replan_count above)."""
    state = _state()
    state["drift_reject_count"] = 1
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    assert result["drift_reject_count"] == 0


@pytest.mark.asyncio
async def test_approval_refreezes_anchor_spec_to_approved_contract():
    """Cascade review O1: the anchor's fault_spec snapshot froze the
    APPROVED-at-the-time contract and never refreshes (_ledger_seeded
    re-freezes only when the anchor is absent). A user-approved plan
    change must re-freeze it — otherwise the retired spec keeps rendering
    as "Goal (ANCHOR, immutable)" into execute/verify/recover prompts
    while state.fault_spec carries the approved one: two competing truth
    sources. Field-split semantics: goal stays verbatim (user intent
    never changes here), fault_spec follows the approved contract."""
    from chaos_agent.agent.progress_ledger import (
        freeze_anchor,
        merge_progress_ledger,
    )

    state = _state()
    # Anchor frozen on the OLD contract (action=drop, revision=3) —
    # execute_loop's seeding shape, plus live plan-scoped fields.
    state["progress_ledger"] = merge_progress_ledger(
        freeze_anchor(_current().to_dict(), goal="原始演练意图"),
        state_update={"phase": "executing", "current_step": "s1"},
    )
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    led = result["progress_ledger"]
    anchor_spec = led["anchor"]["fault_spec"]
    assert anchor_spec["fault_action"] == "delay", (
        "anchor carries the APPROVED contract, not the retired one"
    )
    assert anchor_spec["revision"] == 4
    # Field-split: the goal (user intent) is immutable across the seam.
    assert led["anchor"]["goal"] == "原始演练意图"
    # The plan-scoped fields retired by the seam reset ride through
    # the composition (this ledger is both reset and re-frozen).
    assert led["state"]["current_step"] is None


@pytest.mark.asyncio
async def test_approval_without_anchor_does_not_inject_ledger():
    """No anchor in the ledger (first-attempt shape, or a cross-graph
    intent ledger without one) → nothing to re-freeze: the seam must not
    inject a progress_ledger into the result — execute_loop's lazy
    seeding freezes the APPROVED spec on the next entry."""
    state = _state()
    state["progress_ledger"] = {
        "anchor": {}, "state": {"phase": "executing"}, "log": [],
    }
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    assert "progress_ledger" not in result


@pytest.mark.asyncio
async def test_approval_refreeze_composes_with_terminal_phase_reset():
    """O1 and the C1 knife-2 reset write the SAME result key on this
    seam. The reset reads the pre-seam state ledger (retiring the retired
    plan's terminal phase), then O1 reads the RESET's output (result
    first, compose-not-clobber) and re-freezes the anchor spec — both
    effects must survive in the single emitted ledger."""
    from chaos_agent.agent.progress_ledger import (
        freeze_anchor,
        merge_progress_ledger,
    )

    state = _state()
    state["progress_ledger"] = merge_progress_ledger(
        freeze_anchor(_current().to_dict(), goal="g"),
        state_update={"phase": "execution-complete", "current_step": "s3"},
    )
    state["progress_ledger"]["log"] = [{"event": "done", "status": "observed"}]
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    led = result["progress_ledger"]
    # C1 knife-2 effect survived the composition.
    assert led["state"]["phase"] is None
    assert led["state"]["current_step"] is None
    # O1 effect applied on top.
    assert led["anchor"]["fault_spec"]["fault_action"] == "delay"
    assert led["anchor"]["fault_spec"]["revision"] == 4
    # History and goal ride through both writes.
    assert led["anchor"]["goal"] == "g"
    assert led["log"] == [{"event": "done", "status": "observed"}]


@pytest.mark.asyncio
async def test_cli_auto_approval_spends_monotonic_budget():
    """B76 review E1 (probe_b76_round5.py): every auto-approval also resets
    replan/execute budgets and agent_loop_count, so the approval count is
    the ONLY remaining bound on the aligned-proposal loop. It must be
    monotonic (spend +1, never reset by the approval itself) — a reset
    would hand the loop an infinite budget again."""
    first = _node_state(mode="cli")
    first["plan_change_auto_approve_count"] = 0
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("aligned CLI proposal must not interrupt"),
    ):
        result = await plan_change_confirm(first)
    assert result["plan_change_auto_approve_count"] == 1

    # The spend survives into the next round: the approval result contains
    # +1 of the PRE-approval counter, not a fresh zero.
    second = _node_state(mode="cli")
    second["plan_change_auto_approve_count"] = 1
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("aligned CLI proposal must not interrupt"),
    ):
        result = await plan_change_confirm(second)
    assert result["plan_change_auto_approve_count"] == 2


@pytest.mark.asyncio
async def test_cli_terminates_when_auto_approval_budget_exhausted():
    """The aligned-proposal loop cannot converge by definition (each
    replacement failed to execute); after the task-lifetime budget is
    spent the 4th aligned proposal terminates through the same channel as
    rejected-twice — NOT a soft rejection, which would just re-plan under
    the disproven contract one more time."""
    state = _node_state(mode="cli")
    state["plan_change_auto_approve_count"] = settings.max_plan_change_auto_approvals
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("budget exhaustion must terminate, not interrupt"),
    ):
        result = await plan_change_confirm(state)

    assert "fault_spec" not in result
    assert "budget exhausted" in result["failure_detail"]["context"]
    # It is a termination, not a rejection: the reject tally is untouched.
    assert "plan_change_reject_count" not in result


@pytest.mark.asyncio
async def test_tui_approval_does_not_spend_auto_approval_budget():
    """The budget caps the UNATTENDED aligned-approval loop only. A TUI
    approval has a human at the interrupt — the human IS the budget, so
    the counter stays untouched."""
    state = _node_state(mode="tui")
    state["plan_change_auto_approve_count"] = 1
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        return_value="approved",
    ):
        result = await plan_change_confirm(state)

    assert result["fault_spec"] is not None
    assert "plan_change_auto_approve_count" not in result


@pytest.mark.asyncio
async def test_approval_keeps_blade_uid_when_experiment_may_be_live(monkeypatch):
    """Same keep-handle semantics as the replan seam: an experiment that may
    still be live keeps its UID so recovery can reach it."""
    state = _state()
    state["experiment_uid"] = "d6eaa95514305543"
    # The single-slot value proves provenance, so the G sweep treats it as a
    # live liability and dispatches a destroy — stub the carrier (a unit
    # test must never touch a real cluster; the sweep itself is pinned by
    # the G suite below).
    calls = _patch_destroy(monkeypatch, "host_blade")
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    assert calls == ["d6eaa95514305543"]

    # keep_experiment_uid=True means the reset does NOT touch the UID: the key is
    # absent from the result, so LangGraph keeps the state value — same
    # keep-handle semantics as the replan seam.
    assert "experiment_uid" not in result
    assert result["replan_count"] == 0
    assert result["injection_method"] is None


@pytest.mark.asyncio
async def test_approval_releases_destroyed_experiment_uid_across_seam(monkeypatch):
    """Round-28 R1 — the keep predicate's other half: the corpse no longer
    keeps. A destroyed experiment (create receipt + proven destroy + landed
    retired ledger) releases its UID slot at the seam. The committed twin
    kept it — a corpse riding into the next intent's epoch, where the combo
    check licensed a combo on it and durably mis-routed recovery. Identity
    for recovery targeting survives in the retired ledger and message
    history regardless; the G sweep has nothing live to dispatch."""
    uid = "d6eaa95514305543"
    state = _state()
    state["experiment_uid"] = uid
    state["injection_method"] = "kubectl_exec"
    state["retired_experiment_uids"] = [uid]
    # The proven lifecycle rides the history: birth receipt first, then
    # the proven destroy, then the proposal pair (the live-keeping twin
    # above proves the same slot WITHOUT these stays kept).
    state["messages"] = [
        AIMessage(
            content="",
            tool_calls=[{
                "name": "blade_create",
                "args": {"command": "create k8s pod-cpu fullload"},
                "id": "tc-r28-birth",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"%s"}' % uid,
            name="blade_create",
            tool_call_id="tc-r28-birth",
        ),
        AIMessage(
            content="",
            tool_calls=[{
                "name": "blade_destroy",
                "args": {"uid": uid},
                "id": "tc-r28-kill",
                "type": "tool_call",
            }],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"success"}',
            name="blade_destroy",
            tool_call_id="tc-r28-kill",
        ),
        *state["messages"],
    ]
    # Stub the carrier so the test cannot touch a real cluster — and so
    # a corpse-dispatching regression FAILS LOUDLY here (calls non-empty)
    # instead of silently no-op-ing inside a real destroy attempt.
    calls = _patch_destroy(monkeypatch, "kubectl_exec")
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    # keep=False: the UID slot is explicitly released (vs the live twin's
    # key-absent keep above).
    assert result["experiment_uid"] is None
    assert result["injection_method"] is None
    # Nothing live remained — the sweep dispatches no destroy.
    assert calls == []
    # Identity survives for recovery targeting: the retired ledger is
    # kept untouched (no new retirements to append).
    assert "retired_experiment_uids" not in result


@pytest.mark.asyncio
async def test_approval_keeps_native_handle_when_fault_may_be_live():
    """Parity with the blade keep: a live native mutation (attributed at
    issue time, no UID) keeps its fault handle across the contract seam —
    wiping it would orphan a fault the recover graph can no longer identify."""
    state = _state()
    state["injection_method"] = "kubectl_native"
    state["fault_handle"] = {"kind": "native", "method": "kubectl_native"}
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(state)

    # keep=True: the handle key is absent from the result so LangGraph keeps
    # the state value; the method is still cleared for re-detection.
    assert "fault_handle" not in result
    assert result["injection_method"] is None


@pytest.mark.asyncio
async def test_stale_or_noop_proposal_returns_to_react_without_interrupting():
    stale = _state(call=_proposal_call(revision=2))
    result = await plan_change_confirm(stale)
    assert "stale FaultSpec revision" in result["messages"][0].content

    current = _current()
    same_call = {
        "name": "propose_plan_change", "id": "same-1",
        "args": {
            "reason": "no material change",
            "fault_revision": current.revision,
            "proposed_fault": current.to_intent_dict(),
        },
    }
    same = _state(call=same_call)
    result = await plan_change_confirm(same)
    assert "does not change" in result["messages"][0].content


@pytest.mark.asyncio
async def test_cli_and_user_rejections_use_existing_limit():
    first = await plan_change_confirm(_state(mode="cli"))
    assert first["plan_change_reject_count"] == 1
    second = await plan_change_confirm(_state(mode="cli", rejects=1))
    assert second["failure_detail"]["category"] == "execution_failed"

    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="rejected"):
        rejected = await plan_change_confirm(_state())
    assert rejected["plan_change_reject_count"] == 1


# ── B76 ④: CLI non-interactive plan-change escape hatch ──

_NODE_INTENT = (
    "模拟节点宕机：在节点 cn-shanghai-cloudspe.25.209.71.189 上切断该节点与 API Server 的网络通信"
)


def _node_current() -> FaultSpec:
    """The r4 corrupted contract shape: node scope frozen with a Pod label
    (unexecutable — the guard rejects every addressing form for it)."""
    return FaultSpec(
        scope="node",
        namespace="default",
        labels={"app": "drill-nodedown-target"},
        fault_target="network",
        fault_action="drop",
        duration_seconds=240,
        source="cli_nl",
        user_description=_NODE_INTENT,
        objective="simulate node down",
        boundaries=("test only",),
        constraints=("one logical experiment",),
    ).replace(revision=0)


def _node_fix_call() -> dict:
    """The executor's correct fix proposal: re-point the contract at the
    node the user named, dropping the unresolvable Pod label."""
    proposed = _node_current().to_intent_dict() | {
        "names": ["cn-shanghai-cloudspe.25.209.71.189"],
        "labels": {},
    }
    return {
        "name": "propose_plan_change", "id": "fix-1",
        "args": {
            "reason": "approved selector resolves to zero nodes; the user named the node",
            "fault_revision": 0,
            "proposed_fault": proposed,
        },
    }


def _node_state(*, mode="cli", call=None, spec=None) -> dict:
    call = call or _node_fix_call()
    spec = spec or _node_current()
    return {
        "messages": [
            AIMessage(content="", tool_calls=[call]),
            ToolMessage(content="ok", name=call["name"], tool_call_id=call["id"]),
        ],
        "interaction_mode": mode,
        "replan_context": {"error_summary": "selector resolves to zero nodes"},
        "plan_change_reject_count": 0,
        "fault_spec": spec.to_dict(),
        "batch_submit_args": None,
    }


@pytest.mark.asyncio
async def test_cli_auto_applies_proposal_targeting_user_named_node():
    """CLI has no human at the interrupt, but a proposal that re-targets the
    node the user themselves named substitutes no human decision — it aligns
    WITH the user. Before B76 this proposal was a dead end (rejected → re-plan
    under the disproven contract → terminal rejection 44 minutes later)."""
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("interrupt must not fire for an aligned CLI proposal"),
    ):
        result = await plan_change_confirm(_node_state(mode="cli"))

    spec = FaultSpec.from_dict(result["fault_spec"])
    assert spec is not None
    assert spec.revision == 1
    assert spec.names == ("cn-shanghai-cloudspe.25.209.71.189",)
    assert spec.labels == {}
    # Audit: the approval message names the non-interactive path explicitly.
    assert "CLI non-interactive" in result["messages"][0].content


@pytest.mark.asyncio
async def test_cli_still_rejects_proposal_targeting_a_different_resource():
    """Auto-approval is anchor-scoped, not a blanket CLI bypass: a proposal
    naming a node the user did NOT name keeps the dead-end-by-design path."""
    other_node = _node_fix_call()
    other_node["args"]["proposed_fault"]["names"] = ["cn-shanghai-cloudspe.25.209.68.28"]
    result = await plan_change_confirm(_node_state(mode="cli", call=other_node))
    assert result["plan_change_reject_count"] == 1
    assert "cannot confirm" in result["messages"][0].content
    assert "fault_spec" not in result


@pytest.mark.asyncio
async def test_cli_still_rejects_when_intent_names_no_node():
    """Anchorless intent text (the legacy nginx-pod shape) never auto-applies."""
    unanchored = _node_current().replace(
        user_description="validate packet loss on pods",
    )
    result = await plan_change_confirm(_node_state(mode="cli", spec=unanchored))
    assert result["plan_change_reject_count"] == 1
    assert "fault_spec" not in result


@pytest.mark.asyncio
async def test_cli_rejects_fabricated_anchor_in_proposal_description():
    """Trust boundary: the proposal dict is LLM-authored end to end, so a
    candidate echoing its own anchored user_description must NOT unlock the
    auto-approval — only the CURRENT contract's entry-point text counts."""
    unanchored = _node_current().replace(
        user_description="validate packet loss on pods",
    )
    forged = _node_fix_call()
    # The proposal's own description names a node the user never named.
    forged["args"]["proposed_fault"]["user_description"] = (
        "在节点 cn-shanghai-cloudspe.25.209.71.189 上注入（LLM 回显伪造）"
    )
    result = await plan_change_confirm(
        _node_state(mode="cli", call=forged, spec=unanchored)
    )
    assert result["plan_change_reject_count"] == 1
    assert "fault_spec" not in result


@pytest.mark.asyncio
async def test_cli_rejects_kind_confusion_same_name_different_scope():
    """The anchor vocabulary is node-only, so a pod-scope proposal whose pod
    name coincides with the anchored NODE name is a DIFFERENT resource, not
    the named one — the docstring's "different kind keeps the existing
    rejection" must be enforced by a kind check, not assumed from the name
    string (a bare name intersection would auto-approve it)."""
    confused = _node_fix_call()
    confused["args"]["proposed_fault"]["scope"] = "pod"
    result = await plan_change_confirm(_node_state(mode="cli", call=confused))
    assert result["plan_change_reject_count"] == 1
    assert "fault_spec" not in result


@pytest.mark.asyncio
async def test_cli_rejects_target_expansion_beyond_named_nodes():
    """Auto-approval aligns WITH the user: adding a node the user never
    named (even alongside the one they did) is an expansion, not an
    alignment — names must be a subset of the anchor set, not merely
    overlap it."""
    expanded = _node_fix_call()
    expanded["args"]["proposed_fault"]["names"] = [
        "cn-shanghai-cloudspe.25.209.71.189",
        "cn-shanghai-cloudspe.25.209.68.28",
    ]
    result = await plan_change_confirm(_node_state(mode="cli", call=expanded))
    assert result["plan_change_reject_count"] == 1
    assert "fault_spec" not in result


@pytest.mark.asyncio
async def test_tui_mode_still_interrupts_even_for_aligned_proposal():
    """The auto-approval is a CLI-only seam: interactive modes keep the human
    in the loop for every identity change, aligned or not."""
    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        return_value="approved",
    ):
        result = await plan_change_confirm(_node_state(mode="tui"))

    spec = FaultSpec.from_dict(result["fault_spec"])
    assert spec is not None and spec.revision == 1
    # Interactive approval keeps the plain APPROVED wording.
    assert "CLI non-interactive" not in result["messages"][0].content


@pytest.mark.asyncio
async def test_cli_rejects_mechanism_domain_swap_riding_aligned_names():
    """B76 review D1 (probe probe_b76_round4.py): aligned names are a free
    ride for a domain swap — a disk/fill proposal on the user's named node
    passed kind+subset and was silently auto-approved, swapping a
    network-loss drill for a 95% disk fill. The user anchors TWO dimensions
    (the resource they named AND the fault domain they described); a
    different domain is never aligned."""
    hijack = _node_fix_call()
    hijack["args"]["proposed_fault"]["target"] = "disk"
    hijack["args"]["proposed_fault"]["action"] = "fill"
    hijack["args"]["proposed_fault"]["params"] = {"path": "/var/lib/docker", "percent": "95"}

    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("a domain swap must never reach the interrupt"),
    ):
        result = await plan_change_confirm(_node_state(mode="cli", call=hijack))

    assert result["plan_change_reject_count"] == 1
    assert "fault_spec" not in result
    # The receipt points at the violated dimension (mechanism), not identity:
    # the names ARE aligned, so the identity receipt would mislead the LLM.
    assert "swaps the fault domain" in result["messages"][0].content
    assert "network -> disk" in result["messages"][0].content


@pytest.mark.asyncio
async def test_cli_auto_applies_in_domain_action_adjustment():
    """The mechanism check pins the DOMAIN, not the lever: fault_action and
    params stay free so the routine replan path (network loss infeasible →
    network delay, percent tuning, name narrowing) keeps the B76 escape
    hatch. Pinning the action too would resurrect the pre-B76 dead end."""
    in_domain = _node_fix_call()
    in_domain["args"]["proposed_fault"]["action"] = "delay"
    in_domain["args"]["proposed_fault"]["params"] = {"time": "3000"}

    with patch(
        "chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt",
        side_effect=AssertionError("an in-domain adjustment must not interrupt"),
    ):
        result = await plan_change_confirm(_node_state(mode="cli", call=in_domain))

    spec = FaultSpec.from_dict(result["fault_spec"])
    assert spec is not None
    assert spec.fault_target == "network"
    assert spec.fault_action == "delay"
    assert spec.revision == 1


def _candidate(intent: dict) -> FaultSpec:
    """Intent dict -> FaultSpec the same way _extract_proposal builds it."""
    return FaultSpec.from_intent_args(
        strip_timeout_alias(intent), existing=_node_current(),
    )


def test_alignment_violation_tokens_are_pinned():
    """The token vocabulary is the internal contract between the alignment
    check and the CLI receipt renderer — each token selects a receipt, so a
    renamed/silently-added token would strand a violation on the generic
    identity receipt (or crash the renderer). Pin the mapping."""
    current = _node_current()

    # Aligned: identity + mechanism both survive.
    assert _alignment_violation(
        _candidate(_node_fix_call()["args"]["proposed_fault"]), current,
    ) is None

    # anchorless — no node named in the entry-point text
    anchorless = _node_current().replace(user_description="validate packet loss on pods")
    assert _alignment_violation(anchorless, anchorless) == "anchorless"

    # kind — pod-scope proposal echoing the anchored node name
    kind_swap = _node_fix_call()["args"]["proposed_fault"] | {"scope": "pod"}
    assert _alignment_violation(_candidate(kind_swap), current) == "kind"

    # identity — a node the user never named
    other = _node_fix_call()["args"]["proposed_fault"] | {
        "names": ["cn-shanghai-cloudspe.25.209.68.28"],
    }
    assert _alignment_violation(_candidate(other), current) == "identity"

    # mechanism — aligned names, swapped domain (the D1 shape)
    domain_swap = _node_fix_call()["args"]["proposed_fault"] | {
        "target": "disk", "action": "fill",
    }
    assert _alignment_violation(_candidate(domain_swap), current) == "mechanism"


# ── B76 review G: contract-boundary serialization of live experiments ──

def _live_experiment_state(*, mode="tui", method="host_blade") -> dict:
    """Approval-boundary shape: contract-1's experiment is live (message
    provenance + durable slot), the executor proposes an in-domain
    replacement.

    Round-20 Q4 flip: the durable read-side now gates on the UID shape
    (``_UID_SHAPE_RE``), so the fixture carries a legal hex16 UID (the
    pre-r20 placeholder ``uid-e1`` was a non-shaped string the gate now
    refuses — a live-experiment shape must be shape-legitimate)."""
    state = _state()
    create = {
        "name": "blade_create", "id": "create-1",
        "args": {"command": "blade create k8s node-network loss"},
    }
    state["messages"] = [
        AIMessage(content="", tool_calls=[create]),
        ToolMessage(
            content='{"code": 200, "success": true, "result": "a1b2c3d4e5f60719"}',
            name="blade_create", tool_call_id="create-1",
        ),
    ] + state["messages"]
    state["experiment_uid"] = "a1b2c3d4e5f60719"
    state["injection_method"] = method
    return state


def _patch_destroy(monkeypatch, method: str) -> list[str]:
    """Stub the blade carrier's bare destroy, recording dispatched UIDs —
    the sweep must never touch a real cluster from a unit test."""
    import json as _json

    from chaos_agent.agent.providers import FaultProviderRegistry

    provider = FaultProviderRegistry.resolve_by_method(method)
    calls: list[str] = []

    async def _fake_destroy(uid, kubeconfig=""):
        calls.append(uid)
        return _json.dumps({"code": 200, "success": True, "result": uid})

    monkeypatch.setattr(provider, "layer1_raw_destroy", _fake_destroy)
    return calls


@pytest.mark.asyncio
async def test_approval_destroys_superseded_experiment_before_new_contract(monkeypatch):
    """An approval replaces the INTENT, not the cluster state: whatever the
    old contract injected keeps running until proven destroyed, and a live
    superseded experiment both pollutes the new contract's verification (two
    faults stacked on the same target) and falls out of every single-value
    recovery channel the moment the new create lands (the G orphan chain).
    The seam destroys it deterministically and records the retire."""
    calls = _patch_destroy(monkeypatch, "host_blade")
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(_live_experiment_state())

    assert calls == ["a1b2c3d4e5f60719"]
    assert result["retired_experiment_uids"] == ["a1b2c3d4e5f60719"]
    assert "framework destroyed the superseded experiment" in result["messages"][0].content
    assert "a1b2c3d4e5f60719" in result["messages"][0].content


@pytest.mark.asyncio
async def test_approval_in_cluster_delivery_degrades_without_false_retire(monkeypatch):
    """kubectl-exec delivery: the host destroy cannot reach the CRD
    experiment — the seam must NOT retire on a soft failure (a false retire
    hides the live experiment from every future recovery sweep) and tells
    the model the vehicle plus the retry promise instead."""
    calls = _patch_destroy(monkeypatch, "kubectl_exec")
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(
            _live_experiment_state(method="kubectl_exec")
        )

    # The blocks gate fires before any destroy dispatch.
    assert calls == []
    assert "retired_experiment_uids" not in result
    msg = result["messages"][0].content
    assert "in-cluster delivery" in msg
    assert "kubectl exec" in msg
    assert "recovery sweep will retry" in msg


@pytest.mark.asyncio
async def test_approval_without_live_experiments_adds_no_supersede_note():
    """Zero-residual shape (nothing live under the old contract): the sweep
    is a safety net, not a message change — the approval wording stays
    byte-compatible with the pre-G seam."""
    with patch("chaos_agent.agent.nodes.planning.plan_change_confirm.interrupt", return_value="approved"):
        result = await plan_change_confirm(_state())

    assert "retired_experiment_uids" not in result
    assert "superseded experiment" not in result["messages"][0].content
