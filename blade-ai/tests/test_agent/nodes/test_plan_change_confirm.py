"""Tests for explicit FaultSpec plan-change confirmation."""

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.planning.plan_change_confirm import _extract_proposal, plan_change_confirm
from chaos_agent.agent.spec.fault_spec import FaultSpec


def _current() -> FaultSpec:
    return FaultSpec.from_intent_args({
        "objective": "validate packet loss", "scope": "pod", "target": "network",
        "action": "drop", "namespace": "default", "names": ["nginx"],
        "params": {"timeout": "60"}, "boundaries": ["staging only"],
        "constraints": ["one logical experiment"],
    }).replace(revision=3)


def _proposal_call(*, revision: int = 3, action: str = "delay") -> dict:
    proposed = _current().to_intent_dict() | {
        "action": action,
        "labels": {"app": "web"},
        "params": {"timeout": "60", "time": "3000"},
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
    assert candidate.blade_action == "delay"
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
    assert spec.blade_action == "delay"
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
    assert changed.blade_action == "delay"
    assert changed.revision == 4


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
