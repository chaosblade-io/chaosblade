"""Integration test: Inject graph flow (load_memory → agent_loop → safety_check → ... → save_memory)."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.agent_loop import agent_loop
from chaos_agent.agent.nodes.execute_loop import execute_loop
from chaos_agent.agent.nodes.memory_nodes import load_memory, save_memory
from chaos_agent.agent.nodes.reject import reject
from chaos_agent.agent.nodes.safety_check import safety_check
from chaos_agent.agent.nodes.verifier import verifier
from chaos_agent.agent.router import (
    should_continue_agent_loop,
    should_continue_execute_loop,
    route_after_safety,
    route_after_confirmation,
    route_after_baseline,
)
from chaos_agent.config.settings import settings


class TestInjectGraphFlow:
    """Integration test for the inject graph node sequence."""

    @pytest.mark.asyncio
    async def test_successful_inject_flow(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        """Test a successful inject flow through all nodes."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "max_agent_loop", 10)
        monkeypatch.setattr(settings, "max_execute_loop", 15)
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        import chaos_agent.agent.nodes.agent_loop as al_mod
        import chaos_agent.agent.nodes.execute_loop as el_mod
        monkeypatch.setattr(al_mod, "MAX_AGENT_LOOP", 10)
        monkeypatch.setattr(el_mod, "MAX_EXECUTE_LOOP", 15)

        state = sample_agent_state.copy()

        # Step 1: load_memory
        result = await load_memory(state)
        state.update(result)
        assert "operational_notes" in state
        assert "experiment_history" in state

        # Step 2: agent_loop (simulates LLM planning)
        result = await agent_loop(state)
        state.update(result)
        assert state["agent_loop_count"] == 1

        # Simulate LLM producing a plan
        state["plan"] = "Delete pod my-pod using pod-delete skill"
        state["skill_name"] = "pod-delete"

        # Step 3: Route after agent_loop → extract_planning_metadata
        route = should_continue_agent_loop(state)
        assert route == "extract_planning_metadata"

        # Step 4: safety_check
        result = await safety_check(state)
        state.update(result)
        # No kubeconfig in test → conflict check skipped → warning
        assert state["safety_status"] == "warning"

        # Step 5: Route after safety → confirmation_gate (warning needs confirmation)
        route = route_after_safety(state)
        assert route == "confirmation_gate"

        # Step 6: baseline_capture (shared across all modes)
        # In this test we skip actual baseline_capture execution (it requires kubectl access).
        # Just verify the routing: baseline_capture → route_after_baseline → execute_loop
        route = route_after_baseline(state)
        assert route == "execute_loop"  # NL mode (direct=False)

        # Step 7: execute_loop (simulates LLM executing blade commands)
        result = await execute_loop(state)
        state.update(result)
        assert state["execute_loop_count"] == 1

        # Simulate blade returning a UID
        state["blade_uid"] = "exp-abc123"

        # Step 8: Route after execute_loop → verifier
        route = should_continue_execute_loop(state)
        assert route == "verifier"

        # Step 9: verifier (with mocked blade_status returning Running)
        with patch("chaos_agent.tools.blade.run_command", new_callable=AsyncMock) as mock_run:
            from chaos_agent.tools.shell import CommandResult
            mock_run.return_value = CommandResult(
                exit_code=0,
                stdout=json.dumps({
                    "code": 200, "success": True,
                    "result": {"Uid": "exp-abc123", "Status": "Running"}
                }),
                stderr="",
            )
            result = await verifier(state)
            state.update(result)
        assert state["result"]["verified"] is False  # No LLM → Layer2 skipped → partial verification
        assert state["verification"]["level"] == "partial"
        assert state["result"]["blade_uid"] == "exp-abc123"

        # Step 10: save_memory
        result = await save_memory(state)
        assert "finished_at" in result
        assert result["finished_at"]

    @pytest.mark.asyncio
    async def test_rejected_inject_flow(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        """Test a rejected inject flow (blacklisted namespace)."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "max_agent_loop", 10)
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system,kube-public")

        import chaos_agent.agent.nodes.agent_loop as al_mod
        monkeypatch.setattr(al_mod, "MAX_AGENT_LOOP", 10)

        state = sample_agent_state.copy()
        # Override fault_spec to target the blacklisted namespace —
        # the fixture's default spec is in "default" namespace which
        # would pass the blacklist check.
        from chaos_agent.agent.fault_spec import FaultSpec
        state["fault_spec"] = FaultSpec.from_cli_structured({
            "scope": "pod", "target": "kill", "action": "delete",
            "namespace": "kube-system", "target_name": "coredns",
        }).to_dict()
        state["target"] = {"namespace": "kube-system", "names": ["coredns"]}
        state["plan"] = "Inject fault into kube-system"
        state["skill_name"] = "pod-delete"

        # agent_loop
        result = await agent_loop(state)
        state.update(result)

        # Route → extract_planning_metadata
        route = should_continue_agent_loop(state)
        assert route == "extract_planning_metadata"

        # safety_check should reject
        result = await safety_check(state)
        state.update(result)
        assert state["safety_status"] == "rejected"
        assert "blacklist" in state["safety_reason"]

        # Route after safety → reject
        route = route_after_safety(state)
        assert route == "reject"

        # reject node
        result = await reject(state)
        assert result["result"]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_confirmation_flow(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        """Test inject flow with confirmation gate."""
        monkeypatch.setattr(settings, "kubeconfig_path", "")
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "max_agent_loop", 10)
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system")

        import chaos_agent.agent.nodes.agent_loop as al_mod
        monkeypatch.setattr(al_mod, "MAX_AGENT_LOOP", 10)

        state = sample_agent_state.copy()
        state["plan"] = "Delete pod my-pod"
        state["skill_name"] = "pod-delete"
        state["needs_confirmation"] = True

        # safety_check: no kubeconfig → conflict check skipped → warning
        result = await safety_check(state)
        state.update(result)
        assert state["safety_status"] == "warning"

        # Route after safety → confirmation_gate (warning + needs_confirmation)
        route = route_after_safety(state)
        assert route == "confirmation_gate"

        # Simulate user approval
        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="approved"):
            from chaos_agent.agent.nodes.confirmation_gate import confirmation_gate
            result = await confirmation_gate(state)
            state.update(result)

        assert state["needs_confirmation"] is False

        # Route after confirmation → baseline_capture (all modes share baseline_capture)
        route = route_after_confirmation(state)
        assert route == "baseline_capture"

        # After baseline_capture, route_after_baseline dispatches by mode
        route = route_after_baseline(state)
        assert route == "execute_loop"  # NL mode

    @pytest.mark.asyncio
    async def test_user_rejection_flow(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        """Test inject flow where user rejects at confirmation gate."""
        monkeypatch.setattr(settings, "working_dir", tmp_memory_dir.parent)
        monkeypatch.setattr(settings, "safety_blacklist_namespaces", "kube-system")

        state = sample_agent_state.copy()
        state["plan"] = "Delete pod my-pod"
        state["skill_name"] = "pod-delete"
        state["needs_confirmation"] = True
        state["safety_status"] = "safe"

        # Simulate user rejection
        with patch("chaos_agent.agent.nodes.confirmation_gate.interrupt", return_value="rejected"):
            from chaos_agent.agent.nodes.confirmation_gate import confirmation_gate
            result = await confirmation_gate(state)
            state.update(result)

        assert state["safety_status"] == "rejected"

        # Route after confirmation → reject
        route = route_after_confirmation(state)
        assert route == "reject"

        # reject node
        result = await reject(state)
        assert result["result"]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_max_loop_exceeded_flow(self, sample_agent_state, monkeypatch):
        """Test inject flow when agent_loop exceeds max iterations with a skill active."""
        monkeypatch.setattr(settings, "max_agent_loop", 2)
        import chaos_agent.agent.nodes.agent_loop as al_mod
        monkeypatch.setattr(al_mod, "MAX_AGENT_LOOP", 2)

        state = sample_agent_state.copy()
        state["agent_loop_count"] = 2  # Already at max
        state["skill_name"] = "pod-kill"  # Skill active but max iterations hit

        # agent_loop should reject
        result = await agent_loop(state)
        state.update(result)
        assert state["safety_status"] == "rejected"

        # Route → reject
        route = should_continue_agent_loop(state)
        assert route == "reject"

        result = await reject(state)
        assert result["result"]["status"] == "rejected"

    @pytest.mark.asyncio
    async def test_max_loop_no_skill_treated_as_reject(self, sample_agent_state, monkeypatch):
        """Test inject flow when agent_loop exceeds max iterations without skill → reject."""
        monkeypatch.setattr(settings, "max_agent_loop", 2)

        state = sample_agent_state.copy()
        state["agent_loop_count"] = 2  # Already at max
        # No skill_name → now treated as reject (not chat)

        # Route → reject (no longer chat)
        route = should_continue_agent_loop(state)
        assert route == "reject"
