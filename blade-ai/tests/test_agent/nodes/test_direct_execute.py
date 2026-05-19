"""Tests for direct_execute node — bug fixes A, B, D."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.nodes.direct_execute import (
    _parse_blade_uid_from_content,
    _build_blade_command_for_exec,
)


# ---------------------------------------------------------------------------
# Fix A: _parse_blade_uid_from_content must reject code 54000 + success=false
# ---------------------------------------------------------------------------


class TestParseBladeUidCode54000:
    """Verify that code 54000 responses with success=false are rejected."""

    def test_code_200_success_returns_uid(self):
        payload = json.dumps({"code": 200, "success": True, "result": "abc123"})
        assert _parse_blade_uid_from_content(payload) == "abc123"

    def test_code_54000_success_true_returns_uid(self):
        payload = json.dumps({
            "code": 54000, "success": True,
            "result": {"uid": "race-uid-1", "status": "created"},
        })
        assert _parse_blade_uid_from_content(payload) == "race-uid-1"

    def test_code_54000_success_false_returns_empty(self):
        payload = json.dumps({
            "code": 54000, "success": False,
            "result": {"uid": "f020bfe94bb83137", "status": "failed"},
        })
        assert _parse_blade_uid_from_content(payload) == ""

    def test_code_54000_no_success_field_returns_uid(self):
        payload = json.dumps({"code": 54000, "result": {"uid": "compat-uid"}})
        assert _parse_blade_uid_from_content(payload) == "compat-uid"

    def test_code_54000_success_none_returns_uid(self):
        payload = json.dumps({
            "code": 54000, "success": None,
            "result": {"uid": "null-success-uid"},
        })
        assert _parse_blade_uid_from_content(payload) == "null-success-uid"

    def test_code_54000_result_not_dict_skips(self):
        payload = json.dumps({"code": 54000, "success": False, "result": "str-uid"})
        assert _parse_blade_uid_from_content(payload) == ""

    def test_error_wrapped_code_54000_success_false(self):
        inner = json.dumps({
            "code": 54000, "success": False,
            "result": {"uid": "wrapped-fail-uid"},
        })
        payload = f"Error: blade create failed (exit 1): {inner}\nlog"
        assert _parse_blade_uid_from_content(payload) == ""

    def test_empty_input(self):
        assert _parse_blade_uid_from_content("") == ""
        assert _parse_blade_uid_from_content(None) == ""


# ---------------------------------------------------------------------------
# Fix B: Pre-flight check for node-scope DaemonSet pod availability
# ---------------------------------------------------------------------------


def _make_blade_create_mock(return_value: str):
    """Create a mock blade_create with a working .ainvoke() method.

    blade_create is a LangChain StructuredTool. We replace the module-level
    reference with a MagicMock whose .ainvoke is an AsyncMock.
    """
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value=return_value)
    return mock


class TestPreflightNodeScopeCheck:
    """Verify direct_execute node-scope DaemonSet pod pre-flight check."""

    @pytest.mark.asyncio
    async def test_preflight_missing_node_returns_prerequisite_failed(self):
        """When target node has no DaemonSet pod, injection should fail with
        PREREQUISITE_FAILED."""
        from chaos_agent.agent.nodes.direct_execute import direct_execute

        state = {
            "blade_scope": "node", "blade_target": "disk",
            "blade_action": "burn",
            "target": {"namespace": "", "names": ["cn-hongkong.10.0.1.120"]},
            "task_id": "test-task-001", "messages": [],
        }

        mock_result = MagicMock()
        mock_result.stdout = (
            "NAME              READY   STATUS    RESTARTS   AGE   IP           NODE\n"
            "otel-c-tool-abc   1/1     Running   0          1h    10.0.1.50   other-node\n"
        )
        mock_result.exit_code = 0

        with patch(
            "chaos_agent.agent.nodes.direct_execute.run_command",
            new_callable=AsyncMock, return_value=mock_result,
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.blade_create",
            _make_blade_create_mock('{"code":200,"success":true,"result":"uid"}'),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_tracker",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_global_session_store",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_node_status_to_session",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_to_store",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.build_blade_create_args",
            return_value={"flags": "--path /tmp --timeout 120"},
        ):
            result = await direct_execute(state)

        assert "error" in result
        assert "prerequisite_failed" in result.get("failure_reason", "")

    @pytest.mark.asyncio
    async def test_preflight_available_node_proceeds(self):
        """When target node has a DaemonSet pod, injection should proceed
        to blade_create and extract the uid."""
        from chaos_agent.agent.nodes.direct_execute import direct_execute

        state = {
            "blade_scope": "node", "blade_target": "disk",
            "blade_action": "burn",
            "target": {"namespace": "", "names": ["cn-hongkong.10.0.1.120"]},
            "task_id": "test-task-002", "messages": [],
        }

        mock_kubectl = MagicMock()
        mock_kubectl.stdout = (
            "NAME              READY   STATUS    RESTARTS   AGE   IP           NODE\n"
            "otel-c-tool-abc   1/1     Running   0          1h    10.0.1.50   cn-hongkong.10.0.1.120\n"
        )
        mock_kubectl.exit_code = 0

        with patch(
            "chaos_agent.agent.nodes.direct_execute.run_command",
            new_callable=AsyncMock, return_value=mock_kubectl,
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.blade_create",
            _make_blade_create_mock('{"code":200,"success":true,"result":"uid-123"}'),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_tracker",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_global_session_store",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_node_status_to_session",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_to_store",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.build_blade_create_args",
            return_value={"flags": "--path /tmp --timeout 120"},
        ):
            result = await direct_execute(state)

        assert result.get("blade_uid") == "uid-123", (
            f"Expected blade_uid=uid-123, got: {result.get('blade_uid')}, "
            f"full: {result}"
        )

    @pytest.mark.asyncio
    async def test_preflight_check_failure_does_not_block(self):
        """If kubectl get pods throws (network error), injection should still
        proceed (fail-open) — NOT return PREREQUISITE_FAILED."""
        from chaos_agent.agent.nodes.direct_execute import direct_execute

        state = {
            "blade_scope": "node", "blade_target": "disk",
            "blade_action": "burn",
            "target": {"namespace": "", "names": ["cn-hongkong.10.0.1.120"]},
            "task_id": "test-task-003", "messages": [],
        }

        call_count = 0

        async def _run_cmd_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise Exception("network error")  # pre-flight fails
            # fallback kubectl calls
            r = MagicMock()
            r.stdout = ""
            r.stderr = ""
            r.exit_code = 1
            return r

        with patch(
            "chaos_agent.agent.nodes.direct_execute.run_command",
            new_callable=AsyncMock, side_effect=_run_cmd_side_effect,
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.blade_create",
            _make_blade_create_mock('{"code":200,"success":true,"result":"uid-456"}'),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_tracker",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_global_session_store",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_node_status_to_session",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_to_store",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.build_blade_create_args",
            return_value={"flags": "--path /tmp --timeout 120"},
        ):
            result = await direct_execute(state)

        # Fail-open: pre-flight exception must NOT produce PREREQUISITE_FAILED
        assert "prerequisite_failed" not in result.get("failure_reason", ""), (
            f"Fail-open violated: got PREREQUISITE_FAILED "
            f"but pre-flight exception should not block injection"
        )
        assert result.get("blade_uid") == "uid-456", (
            f"Expected blade_uid=uid-456, got: {result}"
        )

    @pytest.mark.asyncio
    async def test_pod_scope_no_preflight(self):
        """Pod-scope should not trigger the node DaemonSet pre-flight check
        (distinguished by `kubectl get pods ... -o wide`)."""
        from chaos_agent.agent.nodes.direct_execute import direct_execute

        state = {
            "blade_scope": "pod", "blade_target": "cpu",
            "blade_action": "fullload",
            "target": {
                "namespace": "cms-demo",
                "names": ["accounting-abc"],
                "labels": {"app": "accounting"},
            },
            "task_id": "test-task-004", "messages": [],
        }

        run_command_calls = []

        async def mock_run_cmd(cmd, **kwargs):
            run_command_calls.append(cmd)
            r = MagicMock()
            r.stdout = ""
            r.stderr = ""
            r.exit_code = 1
            return r

        with patch(
            "chaos_agent.agent.nodes.direct_execute.run_command",
            new_callable=AsyncMock, side_effect=mock_run_cmd,
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.blade_create",
            _make_blade_create_mock('{"code":200,"success":true,"result":"uid-pod"}'),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_tracker",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.get_global_session_store",
            return_value=MagicMock(),
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_node_status_to_session",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.sync_to_store",
        ), patch(
            "chaos_agent.agent.nodes.direct_execute.build_blade_create_args",
            return_value={"flags": "--cpu-percent 80 --timeout 120"},
        ):
            result = await direct_execute(state)

        # Pre-flight check uses `kubectl get pods -l app=otel-c-tool -o wide`
        # Pod scope should NOT issue this command
        for cmd in run_command_calls:
            cmd_str = " ".join(cmd)
            is_preflight = "otel-c-tool" in cmd_str and "-o" in cmd_str and "wide" in cmd_str
            assert not is_preflight, (
                f"Pre-flight should not run for pod scope: {cmd_str}"
            )


# ---------------------------------------------------------------------------
# Fix D: _build_blade_command_for_exec omits --namespace for node scope
# ---------------------------------------------------------------------------


class TestBuildBladeCommandForExecNodeScope:
    """Verify _build_blade_command_for_exec omits --namespace for node scope."""

    def test_node_scope_omits_namespace(self):
        cmd = _build_blade_command_for_exec(
            scope="node", target="disk", action="burn",
            namespace="default", names="node-1", labels="app=test",
            flags="--path /tmp --read",
        )
        assert "--namespace" not in cmd
        assert "--labels" not in cmd
        assert "--names" in cmd

    def test_pod_scope_includes_namespace(self):
        cmd = _build_blade_command_for_exec(
            scope="pod", target="cpu", action="fullload",
            namespace="cms-demo", names="pod-1", labels="app=myapp",
            flags="--cpu-percent 80",
        )
        assert "--namespace" in cmd
        assert "--labels" in cmd
