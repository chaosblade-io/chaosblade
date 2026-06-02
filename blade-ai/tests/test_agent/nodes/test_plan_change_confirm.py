"""Tests for ``chaos_agent.agent.nodes.plan_change_confirm``.

Covers:
  - _extract_proposal from AIMessage tool_calls
  - CLI auto-reject with counting
  - TUI approve path (spec update + skill_name reset)
  - TUI reject path with counting + second-reject termination
  - Missing proposal → no-op
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.plan_change_confirm import (
    _extract_proposal,
    plan_change_confirm,
)


def _make_state(
    *,
    tool_calls=None,
    interaction_mode="tui",
    replan_context=None,
    plan_change_reject_count=0,
    fault_spec=None,
):
    messages = []
    if tool_calls:
        messages.append(AIMessage(content="", tool_calls=tool_calls))
        for tc in tool_calls:
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            messages.append(ToolMessage(content="ok", name=name, tool_call_id=tc_id))
    if fault_spec is None:
        fault_spec = {
            "namespace": "default",
            "scope": "pod",
            "names": ("nginx",),
            "labels": {},
            "blade_target": "network",
            "blade_action": "drop",
            "params": {},
        }
    return {
        "messages": messages,
        "interaction_mode": interaction_mode,
        "replan_context": replan_context or {"error_summary": "test"},
        "plan_change_reject_count": plan_change_reject_count,
        "fault_spec": fault_spec,
    }


PROPOSE_TC = {
    "name": "propose_plan_change",
    "id": "tc_1",
    "args": {
        "reason": "iptables not available, delay works",
        "scope": "pod",
        "target": "network",
        "action": "delay",
    },
}


class TestExtractProposal:
    def test_extracts_from_ai_message(self):
        state = _make_state(tool_calls=[PROPOSE_TC])
        result = _extract_proposal(state)
        assert result is not None
        assert result["reason"] == "iptables not available, delay works"
        assert result["scope"] == "pod"
        assert result["target"] == "network"
        assert result["action"] == "delay"

    def test_returns_none_when_no_propose(self):
        state = _make_state(tool_calls=[{"name": "read_file", "id": "tc_2", "args": {}}])
        assert _extract_proposal(state) is None

    def test_returns_none_on_empty_messages(self):
        assert _extract_proposal({"messages": []}) is None

    def test_only_checks_most_recent_ai_message(self):
        older_ai = AIMessage(content="", tool_calls=[PROPOSE_TC])
        newer_ai = AIMessage(content="", tool_calls=[{"name": "read_file", "id": "tc_3", "args": {}}])
        state = {
            "messages": [
                older_ai,
                ToolMessage(content="ok", name="propose_plan_change", tool_call_id="tc_1"),
                newer_ai,
                ToolMessage(content="ok", name="read_file", tool_call_id="tc_3"),
            ],
        }
        assert _extract_proposal(state) is None


class TestCLIAutoReject:
    @pytest.mark.asyncio
    async def test_first_reject_increments_count(self):
        state = _make_state(tool_calls=[PROPOSE_TC], interaction_mode="cli")
        result = await plan_change_confirm(state)
        assert result["plan_change_reject_count"] == 1
        assert any("[PLAN CHANGE REJECTED]" in m.content for m in result["messages"])
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_second_reject_terminates(self):
        state = _make_state(
            tool_calls=[PROPOSE_TC],
            interaction_mode="cli",
            plan_change_reject_count=1,
        )
        result = await plan_change_confirm(state)
        assert result["plan_change_reject_count"] == 2
        assert "error" in result


class TestTUIApprove:
    @pytest.mark.asyncio
    async def test_approve_updates_spec_and_resets(self):
        state = _make_state(tool_calls=[PROPOSE_TC], plan_change_reject_count=1)
        with patch(
            "chaos_agent.agent.nodes.plan_change_confirm.interrupt",
            return_value="approved",
        ):
            result = await plan_change_confirm(state)
        assert result["plan_change_reject_count"] == 0
        assert result["skill_name"] is None
        spec = result["fault_spec"]
        assert spec["blade_target"] == "network"
        assert spec["blade_action"] == "delay"

    @pytest.mark.asyncio
    async def test_approve_clears_stale_planning_state(self):
        """Approve must clear plan/plan_path/is_complex/matched_use_case_path
        so extract_planning_metadata can write the new plan and agent_loop
        re-resolves the catalogue for the new fault type."""
        state = _make_state(tool_calls=[PROPOSE_TC])
        with patch(
            "chaos_agent.agent.nodes.plan_change_confirm.interrupt",
            return_value="approved",
        ):
            result = await plan_change_confirm(state)
        assert result["plan"] is None
        assert result["plan_path"] is None
        assert result["is_complex"] is False
        assert result["matched_use_case_path"] is None


class TestTUIReject:
    @pytest.mark.asyncio
    async def test_first_reject_continues(self):
        state = _make_state(tool_calls=[PROPOSE_TC])
        with patch(
            "chaos_agent.agent.nodes.plan_change_confirm.interrupt",
            return_value="rejected",
        ):
            result = await plan_change_confirm(state)
        assert result["plan_change_reject_count"] == 1
        assert "error" not in result
        assert any("[PLAN CHANGE REJECTED]" in m.content for m in result["messages"])

    @pytest.mark.asyncio
    async def test_second_reject_terminates(self):
        state = _make_state(
            tool_calls=[PROPOSE_TC],
            plan_change_reject_count=1,
        )
        with patch(
            "chaos_agent.agent.nodes.plan_change_confirm.interrupt",
            return_value="rejected",
        ):
            result = await plan_change_confirm(state)
        assert result["plan_change_reject_count"] == 2
        assert "error" in result


class TestNoOpCases:
    @pytest.mark.asyncio
    async def test_no_proposal_returns_empty(self):
        state = _make_state(tool_calls=[{"name": "read_file", "id": "tc_x", "args": {}}])
        result = await plan_change_confirm(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_no_spec_returns_empty(self):
        state = _make_state(tool_calls=[PROPOSE_TC], fault_spec={})
        result = await plan_change_confirm(state)
        assert result == {}
