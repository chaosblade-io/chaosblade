"""Tests for confirmation_gate node."""

from unittest.mock import patch

import pytest

from chaos_agent.agent.nodes.confirmation_gate import confirmation_gate


class TestConfirmationGate:
    """Tests for the confirmation_gate node function."""

    @pytest.mark.asyncio
    async def test_approved_returns_no_confirmation_needed(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = "Delete pod my-pod in namespace default"
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved"):
            result = await confirmation_gate(state)

        assert result["needs_confirmation"] is False
        assert result.get("safety_status") != "rejected"

    @pytest.mark.asyncio
    async def test_rejected_returns_rejected_status(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = "Delete pod my-pod"
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="rejected"):
            result = await confirmation_gate(state)

        assert result["safety_status"] == "rejected"
        assert "rejected" in result["safety_reason"].lower()
        assert result["needs_confirmation"] is False

    @pytest.mark.asyncio
    async def test_interrupt_called_with_confirmation_info(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default", "names": ["my-pod"]}
        state["plan"] = "Delete pod my-pod in namespace default"
        state["safety_status"] = "safe"
        state["safety_reason"] = None

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["skill_name"] == "pod-delete"
        assert call_args["target"] == {"namespace": "default", "names": ["my-pod"]}
        assert "plan_summary" in call_args
        assert call_args["safety_status"] == "safe"

    @pytest.mark.asyncio
    async def test_plan_summary_truncated(self, sample_agent_state):
        long_plan = "x" * 1000
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = long_plan
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert len(call_args["plan_summary"]) == 500

    @pytest.mark.asyncio
    async def test_empty_plan_summary(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = ""
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["plan_summary"] == ""

    @pytest.mark.asyncio
    async def test_none_plan_summary(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = None
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["plan_summary"] == ""

    @pytest.mark.asyncio
    async def test_safety_reason_included(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = "Delete pod"
        state["safety_status"] = "warning"
        state["safety_reason"] = "High blast radius"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["safety_reason"] == "High blast radius"

    @pytest.mark.asyncio
    async def test_default_safety_status(self):
        """When safety_status key is absent from state, defaults to 'safe'."""
        state = {
            "skill_name": "pod-delete",
            "target": {"namespace": "default"},
            "plan": "Plan",
        }

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["safety_status"] == "safe"

    @pytest.mark.asyncio
    async def test_none_target_handled(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = None
        state["plan"] = "Plan"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved") as mock_interrupt:
            await confirmation_gate(state)

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["target"] == {}

    @pytest.mark.asyncio
    async def test_unexpected_decision_rejected(self, sample_agent_state):
        state = sample_agent_state
        state["skill_name"] = "pod-delete"
        state["target"] = {"namespace": "default"}
        state["plan"] = "Plan"
        state["safety_status"] = "safe"

        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="maybe"):
            result = await confirmation_gate(state)

        assert result["safety_status"] == "rejected"
