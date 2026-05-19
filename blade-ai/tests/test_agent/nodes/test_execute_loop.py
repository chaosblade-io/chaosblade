"""Tests for execute_loop node."""

import pytest
from langchain_core.messages import ToolMessage, AIMessage

from chaos_agent.agent.nodes.execute_loop import execute_loop, _extract_blade_uid_from_messages
from chaos_agent.config.settings import settings


class TestExecuteLoop:
    """Tests for the execute_loop node function."""

    @pytest.mark.asyncio
    async def test_increments_counter(self, sample_agent_state):
        state = sample_agent_state
        state["execute_loop_count"] = 0

        result = await execute_loop(state)
        assert result["execute_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_increments_from_nonzero(self, sample_agent_state):
        state = sample_agent_state
        state["execute_loop_count"] = 7

        result = await execute_loop(state)
        assert result["execute_loop_count"] == 8

    @pytest.mark.asyncio
    async def test_exceeds_max_iterations(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 5)
        import chaos_agent.agent.nodes.execute_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 5)

        state = sample_agent_state
        state["execute_loop_count"] = 5

        result = await execute_loop(state)
        assert "error" in result
        assert "max iterations" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_at_max_iterations_still_ok(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 10)
        import chaos_agent.agent.nodes.execute_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 10)

        state = sample_agent_state
        state["execute_loop_count"] = 9

        result = await execute_loop(state)
        assert result["execute_loop_count"] == 10
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_exceeds_max_by_one(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 2)
        import chaos_agent.agent.nodes.execute_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 2)

        state = sample_agent_state
        state["execute_loop_count"] = 2

        result = await execute_loop(state)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_default_count_missing(self):
        result = await execute_loop({})
        assert result["execute_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_returns_only_relevant_fields(self, sample_agent_state):
        state = sample_agent_state
        state["execute_loop_count"] = 0

        result = await execute_loop(state)
        assert set(result.keys()) == {"execute_loop_count"}

    @pytest.mark.asyncio
    async def test_exceeded_returns_error_field(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 1)
        import chaos_agent.agent.nodes.execute_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 1)

        state = sample_agent_state
        state["execute_loop_count"] = 1

        result = await execute_loop(state)
        assert "error" in result
        assert "1" in result["error"]


class TestExtractBladeUid:
    """Tests for _extract_blade_uid_from_messages helper."""

    def test_no_tool_messages(self):
        messages = [AIMessage(content="hello")]
        assert _extract_blade_uid_from_messages(messages) is None

    def test_empty_messages(self):
        assert _extract_blade_uid_from_messages([]) is None

    def test_blade_create_tool_message(self):
        msg = ToolMessage(
            content='{"code": 200, "success": true, "result": "abc123"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        messages = [AIMessage(content="planning"), msg]
        assert _extract_blade_uid_from_messages(messages) == "abc123"

    def test_blade_create_non_json_content(self):
        msg = ToolMessage(
            content="not json",
            tool_call_id="tc1",
            name="blade_create",
        )
        messages = [msg]
        assert _extract_blade_uid_from_messages(messages) is None

    def test_other_tool_message_ignored(self):
        msg = ToolMessage(
            content='{"code": 200, "success": true, "result": "abc123"}',
            tool_call_id="tc1",
            name="blade_status",
        )
        messages = [msg]
        assert _extract_blade_uid_from_messages(messages) is None

    def test_blade_create_no_result_field(self):
        msg = ToolMessage(
            content='{"code": 200, "success": true}',
            tool_call_id="tc1",
            name="blade_create",
        )
        messages = [msg]
        assert _extract_blade_uid_from_messages(messages) is None

    def test_returns_latest_uid(self):
        msg1 = ToolMessage(
            content='{"code": 200, "success": true, "result": "old-uid"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code": 200, "success": true, "result": "new-uid"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        # reversed scan, so finds msg2 first
        assert _extract_blade_uid_from_messages(messages) == "new-uid"


class TestExtractBladeUidKubectlExec:
    """Tests for _extract_blade_uid_from_messages with kubectl exec blade output."""

    def test_kubectl_exec_blade_success(self):
        """kubectl ToolMessage with ChaosBlade success JSON → extract uid."""
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        assert _extract_blade_uid_from_messages([msg]) == "a0f2357a939a9bb8"

    def test_kubectl_exec_blade_failure(self):
        """kubectl ToolMessage with ChaosBlade failure JSON → None."""
        msg = ToolMessage(
            content='{"code":500,"success":false,"error":"not found"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        assert _extract_blade_uid_from_messages([msg]) is None

    def test_kubectl_non_blade_output(self):
        """kubectl ToolMessage with regular kubectl output → None."""
        msg = ToolMessage(
            content='NAME   STATUS   AGE\npod1   Running  5d',
            tool_call_id="tc1",
            name="kubectl",
        )
        assert _extract_blade_uid_from_messages([msg]) is None

    def test_blade_create_priority_over_kubectl(self):
        """blade_create result takes priority over kubectl result."""
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"kubectl-uid"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"blade-uid"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        # Reversed scan: msg2 (blade_create) is checked first and returned
        assert _extract_blade_uid_from_messages(messages) == "blade-uid"

    def test_failed_blade_create_with_kubectl_success(self):
        """Failed blade_create + successful kubectl exec → kubectl uid as fallback."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            tool_call_id="tc2",
            name="kubectl",
        )
        messages = [msg1, msg2]
        # msg1 is not valid JSON, msg2 provides the fallback uid
        assert _extract_blade_uid_from_messages(messages) == "a0f2357a939a9bb8"

    def test_multiple_kubectl_results_uses_latest(self):
        """Multiple kubectl exec blade results → returns the latest one."""
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"old-kubectl-uid"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"new-kubectl-uid"}',
            tool_call_id="tc2",
            name="kubectl",
        )
        messages = [msg1, msg2]
        # Reversed scan: msg2 is found first
        assert _extract_blade_uid_from_messages(messages) == "new-kubectl-uid"

    def test_kubectl_query_output_not_extracted(self):
        """kubectl exec blade query k8s output has dict result → not extracted as uid."""
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":{"uid":"abc123","success":true}}',
            tool_call_id="tc1",
            name="kubectl",
        )
        # result is a dict, not a string → should not be extracted as blade_uid
        assert _extract_blade_uid_from_messages([msg]) is None


class TestParseBladeUidFromContent:
    """Tests for _parse_blade_uid_from_content helper."""

    def test_valid_success_json(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":"abc123"}') == "abc123"

    def test_failure_json(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":500,"success":false,"error":"fail"}') is None

    def test_non_string_result(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":{"uid":"abc"}}') is None

    def test_empty_result(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":""}') is None

    def test_non_json_content(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content("not json") is None

    def test_non_string_input(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content(None) is None


class TestParseBladeCreateFromVArgs:
    """Tests for _parse_blade_create_from_v_args helper."""

    def test_network_loss(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_create_from_v_args
        v_args = (
            "otel-c-tool-xxx -n chaosblade -- blade create k8s pod-network loss "
            "--percent 100 --interface eth0 --namespace cms-demo "
            "--names mysql-79794985d4-7zl5p --kubeconfig /root/.kube/config"
        )
        result = _parse_blade_create_from_v_args(v_args)
        assert result == {
            "scope": "pod", "target": "network", "action": "loss",
            "flags": "--percent 100 --interface eth0 --namespace cms-demo "
                     "--names mysql-79794985d4-7zl5p --kubeconfig /root/.kube/config",
        }

    def test_cpu_fullload(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_create_from_v_args
        v_args = (
            "otel-c-tool-xxx -n chaosblade -- blade create k8s node-cpu fullload "
            "--cpu-percent 80 --names worker-1"
        )
        result = _parse_blade_create_from_v_args(v_args)
        assert result == {
            "scope": "node", "target": "cpu", "action": "fullload",
            "flags": "--cpu-percent 80 --names worker-1",
        }

    def test_no_blade_create(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_create_from_v_args
        v_args = "otel-c-tool-xxx -n chaosblade -- blade destroy abc123"
        result = _parse_blade_create_from_v_args(v_args)
        assert result is None

    def test_non_blade_kubectl(self):
        from chaos_agent.agent.nodes.execute_loop import _parse_blade_create_from_v_args
        v_args = "some-pod -n default -- cat /etc/hosts"
        result = _parse_blade_create_from_v_args(v_args)
        assert result is None
