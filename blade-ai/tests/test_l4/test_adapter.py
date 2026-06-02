"""Tests for chaos_agent.l4.adapter — TestTask ↔ AgentState conversions."""

from unittest.mock import patch

from chaos_agent.l4 import adapter as _adapter_mod
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask

# Use underscore-prefixed aliases to prevent pytest from collecting
# source functions whose names start with 'test_'.
_to_initial_state = _adapter_mod.test_task_to_initial_state
_to_task_result = _adapter_mod.state_to_task_result
_build_recover = _adapter_mod.build_recover_initial_state
_make_traj_id = _adapter_mod.make_trajectory_id


class TestTestTaskToInitialState:
    """Test inbound conversion: L4TestTask → inject graph initial_state."""

    def test_basic_conversion(self):
        task = L4TestTask(
            task_id="t-001",
            intent="inject pod cpu fault",
            payload={
                "fault_scope": "pod",
                "fault_target": "cpu",
                "fault_action": "fullload",
                "namespace": "cms-demo",
                "target_names": ["app=myapp"],
                "params": {"cpu-percent": "80"},
                "duration": 300,
                "kubeconfig": "/home/user/.kube/config",
            },
        )
        state = _to_initial_state(task)

        assert state["task_id"] == "t-001"
        assert state["operation"] == "inject"
        assert state["interaction_mode"] == "l4"
        assert state["direct"] is False
        assert state["kubeconfig"] == "/home/user/.kube/config"

        fs = state["fault_spec"]
        assert fs["namespace"] == "cms-demo"
        assert fs["scope"] == "pod"
        assert fs["blade_target"] == "cpu"
        assert fs["blade_action"] == "fullload"
        assert fs["names"] == ["app=myapp"]
        assert fs["params"] == {"cpu-percent": "80"}
        assert fs["duration_seconds"] == 300
        assert fs["source"] == "l4_sdk"
        assert fs["user_description"] == "inject pod cpu fault"

    def test_none_payload_safety(self):
        """payload=None should not crash."""
        task = L4TestTask(task_id="t-002", intent="test", payload=None)
        state = _to_initial_state(task)
        assert state["fault_spec"]["namespace"] == "default"
        assert state["direct"] is False

    def test_empty_payload_defaults(self):
        task = L4TestTask(task_id="t-003", intent="test")
        state = _to_initial_state(task)
        assert state["fault_spec"]["scope"] == "pod"
        assert state["fault_spec"]["blade_target"] == "cpu"
        assert state["fault_spec"]["blade_action"] == "fullload"
        assert state["fault_spec"]["duration_seconds"] == 600

    def test_direct_false_override(self):
        task = L4TestTask(
            task_id="t-004",
            intent="test",
            payload={"direct": False},
        )
        state = _to_initial_state(task)
        assert state["direct"] is False

    def test_messages_empty(self):
        task = L4TestTask(task_id="t-005", intent="test")
        state = _to_initial_state(task)
        assert state["messages"] == []

    def test_safety_status_pending(self):
        task = L4TestTask(task_id="t-006", intent="test")
        state = _to_initial_state(task)
        assert state["safety_status"] == "pending"


class TestStateToTaskResult:
    """Test outbound conversion: graph final state → L4TaskResult."""

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_injected_maps_to_passed(self, mock_infer, mock_build):
        mock_infer.return_value = "injected"
        mock_build.return_value = {"fault_type": "pod-cpu", "phase": "verify"}

        values = {"blade_uid": "uid-123", "safety_status": "safe"}
        result = _to_task_result(values, "t-001", "traj-001")

        assert isinstance(result, L4TaskResult)
        assert result.status == "passed"
        assert result.task_id == "t-001"
        assert result.trajectory_id == "traj-001"
        assert result.error is None
        assert "pod-cpu" in result.summary

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_failed_maps_to_failed_with_error(self, mock_infer, mock_build):
        mock_infer.return_value = "failed"
        mock_build.return_value = {"fault_type": "pod-network"}

        values = {"error": "connection timed out"}
        result = _to_task_result(values, "t-002")

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "AGENT_TIMEOUT"

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_partial_recovered_maps_to_degraded(self, mock_infer, mock_build):
        mock_infer.return_value = "partial_recovered"
        mock_build.return_value = {"fault_type": "node-disk"}

        result = _to_task_result({}, "t-003")
        assert result.status == "degraded"

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_extras_contain_status_data(self, mock_infer, mock_build):
        mock_infer.return_value = "recovered"
        mock_build.return_value = {
            "fault_type": "pod-cpu",
            "phase": "recovery",
            "duration_ms": 5000,
            "task_id": "t-004",  # should be excluded
            "stage": "done",  # should be excluded
            "status": "ok",  # should be excluded
        }

        values = {"blade_uid": "abc", "safety_status": "safe"}
        result = _to_task_result(values, "t-004")

        # Explicit fields
        assert result.extras["blade_uid"] == "abc"
        assert result.extras["safety"] == "safe"
        # Spread from status_data (excluding task_id, stage, status)
        assert result.extras["fault_type"] == "pod-cpu"
        assert result.extras.get("task_state") == "recovered"


class TestBuildRecoverInitialState:
    """Test recover graph initial state construction."""

    @patch("chaos_agent.utils.inject_context.build_inject_context")
    def test_basic_fields(self, mock_ctx):
        mock_ctx.return_value = "inject context summary"

        inject_values = {
            "tui_session_id": "sess-1",
            "blade_uid": "uid-abc",
            "skill_name": "pod-cpu-fullload",
            "skill_case_content": "steps...",
            "inject_verification_summary": "verified OK",
            "fault_spec": {"scope": "pod"},
            "kubeconfig": "/path/to/kube",
            "messages": [{"role": "ai", "content": "done"}],
        }
        result = _build_recover(inject_values, "t-001")

        assert result["task_id"] == "recover-t-001"
        assert result["parent_task_id"] == "t-001"
        assert result["operation"] == "recover"
        assert result["blade_uid"] == "uid-abc"
        assert result["inject_context"] == "inject context summary"
        assert result["fault_spec"] == {"scope": "pod"}
        assert result["messages"] == []  # Fresh messages
        assert result["verification"] is None
        assert result["recover_verification"] is None
        assert result["verifier_loop_count"] == 0

    @patch("chaos_agent.utils.inject_context.build_inject_context")
    def test_missing_fields_use_defaults(self, mock_ctx):
        mock_ctx.return_value = ""
        result = _build_recover({}, "t-002")
        assert result["blade_uid"] == ""
        assert result["skill_name"] == ""
        assert result["kubeconfig"] == ""


class TestMakeTrajectoryId:
    """Test trajectory ID generation."""

    def test_format(self):
        tid = _make_traj_id("task-001")
        assert tid.startswith("traj-task-001-")
        # UUID hex 8 chars suffix
        suffix = tid.split("-", 3)[-1]
        assert len(suffix) == 8

    def test_uniqueness(self):
        ids = {_make_traj_id("t-001") for _ in range(100)}
        assert len(ids) == 100
