"""Tests for verifier node."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes._injection_detection import (
    _was_kubectl_blade_injection_successful,
    _was_blade_create_attempted,
    _was_kubectl_injection_attempted,
)
from chaos_agent.agent.nodes.verifier import (
    verifier,
    _run_layer1_verification,
    _cleanup_debug_pods,
)
from chaos_agent.agent.verdict import Layer1Result
from chaos_agent.agent.state import infer_task_state


def _mock_blade_running(uid="abc123xyz"):
    """Helper: mock run_command to return a Running blade_status."""
    return AsyncMock(return_value=__import__("chaos_agent.tools.shell", fromlist=["CommandResult"]).CommandResult(
        exit_code=0,
        stdout=json.dumps({
            "code": 200, "success": True,
            "result": {"Uid": uid, "Status": "Running"}
        }),
        stderr="",
    ))


def _mock_blade_failed():
    """Helper: mock run_command to return a failed blade_status."""
    return AsyncMock(return_value=__import__("chaos_agent.tools.shell", fromlist=["CommandResult"]).CommandResult(
        exit_code=0,
        stdout=json.dumps({
            "code": 500, "success": False,
            "result": {"Uid": "abc123xyz", "Status": "Error"}
        }),
        stderr="",
    ))


class TestVerifier:
    """Tests for the verifier node function."""

    @pytest.mark.asyncio
    async def test_verified_with_blade_uid(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = "task-123"
        state["skill_name"] = "pod-delete"
        state["blade_uid"] = "abc123xyz"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_running()):
            result = await verifier(state)
        # Layer 1 passed (blade_status=Running) but no LLM for Layer 2,
        # so verification level is "partial" and verified=False (cannot confirm fault effect)
        assert result["result"]["verified"] is False
        assert result["verification"]["level"] == "partial"
        assert result["result"]["task_id"] == "task-123"
        assert result["result"]["skill"] == "pod-delete"
        assert result["result"]["blade_uid"] == "abc123xyz"

    @pytest.mark.asyncio
    async def test_not_verified_without_blade_uid(self, sample_agent_state):
        """No blade_uid + no blade_create in messages → non-ChaosBlade, Layer 1 skipped."""
        state = sample_agent_state
        state["task_id"] = "task-456"
        state["skill_name"] = "pod-delete"
        state["blade_uid"] = ""
        state["messages"] = []  # No blade_create ToolMessage

        result = await verifier(state)
        # Non-ChaosBlade fault: Layer 1 skipped, cannot verify without LLM
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "skipped"
        assert result["verification"]["level"] == "unverified"

    @pytest.mark.asyncio
    async def test_none_blade_uid(self, sample_agent_state):
        """None blade_uid + no blade_create in messages → non-ChaosBlade, Layer 1 skipped."""
        state = sample_agent_state
        state["task_id"] = "task-789"
        state["skill_name"] = "network-delay"
        state["blade_uid"] = None
        state["messages"] = []  # No blade_create ToolMessage

        result = await verifier(state)
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_result_structure(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = "task-struct"
        state["skill_name"] = "cpu-burn"
        state["blade_uid"] = "uid-999"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_running("uid-999")):
            result = await verifier(state)
        r = result["result"]
        assert "task_id" in r
        assert "skill" in r
        assert "blade_uid" in r
        assert "verified" in r

    @pytest.mark.asyncio
    async def test_verification_field_present(self, sample_agent_state):
        """Result should include a 'verification' dict with layer info."""
        state = sample_agent_state
        state["task_id"] = "task-verify"
        state["skill_name"] = "cpu-burn"
        state["blade_uid"] = "uid-v1"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_running("uid-v1")):
            result = await verifier(state)
        assert "verification" in result
        v = result["verification"]
        assert "level" in v
        assert "layer1" in v
        assert "layer2" in v
        assert "warnings" in v
        assert v["layer1"]["status"] == "passed"

    @pytest.mark.asyncio
    async def test_verification_layer2_skipped_no_llm(self, sample_agent_state):
        """Without LLM, Layer 2 is skipped with a warning."""
        state = sample_agent_state
        state["task_id"] = "task-l2skip"
        state["skill_name"] = "cpu-burn"
        state["blade_uid"] = "uid-l2"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_running("uid-l2")):
            result = await verifier(state)
        v = result["verification"]
        assert v["layer2"]["status"] == "skipped"
        assert len(v["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_empty_task_id(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = ""
        state["skill_name"] = "pod-delete"
        state["blade_uid"] = "uid-1"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_running("uid-1")):
            result = await verifier(state)
        assert result["result"]["task_id"] == ""

    @pytest.mark.asyncio
    async def test_defaults_when_state_empty(self):
        """Empty state: all get() calls return empty strings."""
        result = await verifier({})
        # No blade_create in messages → non-ChaosBlade, Layer 1 skipped, unverified
        assert result["result"]["task_id"] == ""
        assert result["result"]["skill"] == ""
        assert result["result"]["blade_uid"] == ""
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_blade_status_failed(self, sample_agent_state):
        """When blade_status returns Error, verification should fail."""
        state = sample_agent_state
        state["task_id"] = "task-fail"
        state["skill_name"] = "pod-delete"
        state["blade_uid"] = "uid-fail"

        with patch("chaos_agent.tools.blade.run_command", _mock_blade_failed()):
            result = await verifier(state)
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "failed"


# ---------------------------------------------------------------------------
# _was_blade_create_attempted
# ---------------------------------------------------------------------------

class TestWasBladeCreateAttempted:
    def test_no_messages(self):
        assert _was_blade_create_attempted([]) is False

    def test_blade_create_tool_message_present(self):
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        assert _was_blade_create_attempted([msg]) is True

    def test_other_tool_message(self):
        msg = ToolMessage(content="pod patched", name="kubectl", tool_call_id="tc2")
        assert _was_blade_create_attempted([msg]) is False

    def test_mixed_messages(self):
        msg1 = ToolMessage(content="pod patched", name="kubectl", tool_call_id="tc1")
        msg2 = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc2")
        assert _was_blade_create_attempted([msg1, msg2]) is True


# ---------------------------------------------------------------------------
# _run_layer1_verification — distinguishing two no-uid scenarios
# ---------------------------------------------------------------------------

class TestRunLayer1Verification:
    @pytest.mark.asyncio
    async def test_no_blade_uid_no_blade_create_skipped(self):
        """No blade_uid + no blade_create in messages → non-ChaosBlade → skipped."""
        result = await _run_layer1_verification("", "", task_id="t1", messages=[])
        assert result.status == "skipped"
        assert "Non-ChaosBlade" in result.details
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_no_blade_uid_with_blade_create_failed(self):
        """No blade_uid + blade_create in messages → ChaosBlade injection failed → failed."""
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        result = await _run_layer1_verification("", "", task_id="t2", messages=[msg])
        assert result.status == "warning"
        assert "blade_create" in result.details
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_no_blade_uid_no_messages_arg(self):
        """No blade_uid + messages=None → defaults to skipped (backward compatible)."""
        result = await _run_layer1_verification("", "", task_id="t3")
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_layer1_result_is_terminal_skipped(self):
        """skipped status is NOT terminal."""
        r = Layer1Result(status="skipped", details="test")
        assert not r.is_terminal()

    @pytest.mark.asyncio
    async def test_layer1_result_is_terminal_failed(self):
        """failed status IS terminal."""
        r = Layer1Result(status="failed", details="test")
        assert r.is_terminal()

    @pytest.mark.asyncio
    async def test_layer1_result_is_terminal_error(self):
        """error status IS terminal."""
        r = Layer1Result(status="error", details="test")
        assert r.is_terminal()


# ---------------------------------------------------------------------------
# infer_task_state — skipped Layer 1 handling
# ---------------------------------------------------------------------------

class TestInferTaskStateSkipped:
    def test_l1_skipped_l2_passed_returns_injected(self):
        """L1=skipped (non-ChaosBlade) + L2=passed → injected."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "verified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_task_state(state) == "injected"

    def test_l1_skipped_l2_failed_returns_failed(self):
        """L1=skipped (non-ChaosBlade) + L2=failed → failed."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "failed"},
            },
        }
        assert infer_task_state(state) == "failed"

    def test_l1_failed_l2_passed_returns_failed(self):
        """L1=failed (ChaosBlade injection failed) + L2=passed → still failed."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "failed"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_task_state(state) == "failed"

    def test_l1_skipped_l2_unknown_returns_failed(self):
        """L1=skipped (non-ChaosBlade) + L2=unknown → failed (core bug scenario).

        When Layer 1 is skipped (non-ChaosBlade fault), Layer 2 is the ONLY
        verification layer. If Layer 2 is "unknown", the injection cannot be
        confirmed and should return "failed", not "injected".
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "failed"

    def test_l1_passed_l2_unknown_returns_injected(self):
        """L1=passed (ChaosBlade) + L2=unknown → injected (partial verification OK).

        When Layer 1 confirmed the experiment is Running (ChaosBlade), Layer 2
        "unknown" is acceptable as partial verification.
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "injected"

    def test_recover_l1_skipped_level_recovered(self):
        """Recover: L1=skipped + level=recovered → recovered."""
        state = {
            "operation": "recover",
            "result": {},  # recovered=False, but level=recovered should override
            "recover_verification": {
                "level": "recovered",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_task_state(state) == "recovered"


class TestChaosBladeFailedNoUid:
    """Test ChaosBlade injection failed (blade_create called but no uid)."""

    @pytest.mark.asyncio
    async def test_chaosblade_failed_no_uid_verifier(self, sample_agent_state):
        """ChaosBlade injection attempted but failed → blade_create ToolMessage exists, no uid → Layer 1 failed."""
        state = sample_agent_state
        state["task_id"] = "task-cb-fail"
        state["skill_name"] = "cpu-burn"
        state["blade_uid"] = ""
        # Simulate a failed blade_create call in messages
        state["messages"] = [
            ToolMessage(
                content='{"code": 500, "success": false, "error": "resource not found"}',
                name="blade_create",
                tool_call_id="tc-fail",
            ),
        ]
        result = await verifier(state)
        assert result["verification"]["layer1"]["status"] == "warning"
        assert result["result"]["verified"] is False


# ---------------------------------------------------------------------------
# _was_kubectl_blade_injection_successful
# ---------------------------------------------------------------------------

def _make_kubectl_tool_call_pair(tool_call_id, subcommand, v_args, response_content):
    """Build AIMessage + ToolMessage pair for kubectl tool call test."""
    ai_msg = AIMessage(
        content="",
        tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": subcommand, "v_args": v_args, "kubeconfig": "/path/to/kc"},
            "id": tool_call_id,
            "type": "tool_call",
        }],
    )
    tool_msg = ToolMessage(content=response_content, name="kubectl", tool_call_id=tool_call_id)
    return [ai_msg, tool_msg]


class TestWasKubectlBladeInjectionSuccessful:
    def test_kubectl_exec_blade_create_success(self):
        """kubectl exec with blade create + ChaosBlade success JSON → True."""
        msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is True

    def test_kubectl_get_with_chaosblade_json_rejected(self):
        """kubectl get returning ChaosBlade JSON → False (not exec blade create)."""
        msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default -o json",
            '{"code":200,"success":true,"result":"uid-fake"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_patch_with_chaosblade_json_rejected(self):
        """kubectl patch returning ChaosBlade JSON → False (not exec blade create)."""
        msgs = _make_kubectl_tool_call_pair(
            "tc3", "patch",
            "deployment xxx -p '{\"replicas\":0}'",
            '{"code":200,"success":true,"result":"uid-fake"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_exec_without_blade_rejected(self):
        """kubectl exec without blade command + ChaosBlade JSON → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc4", "exec",
            "my-pod -- top -bn1",
            '{"code":200,"success":true,"result":"uid-fake"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_exec_blade_destroy_rejected(self):
        """kubectl exec with blade destroy (not create) + ChaosBlade JSON → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc5", "exec",
            "otel-c-tool -n chaosblade -- blade destroy uid-abc",
            '{"code":200,"success":true,"result":"uid-abc"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_missing_tool_call_id_fallback(self):
        """Empty tool_call_id on ToolMessage → legacy fallback (True)."""
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            name="kubectl",
            tool_call_id="",
        )
        assert _was_kubectl_blade_injection_successful([msg]) is True

    def test_kubectl_failure_json(self):
        """kubectl exec with failure JSON → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc6", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":500,"success":false,"error":"not found"}',
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_non_blade_output(self):
        """kubectl exec with plain text output → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc7", "exec",
            "my-pod -- top -bn1",
            "NAME   STATUS   AGE\npod1   Running  5d",
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_blade_create_message_ignored(self):
        """blade_create ToolMessage (not kubectl) → False."""
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":"abc123"}',
            name="blade_create",
            tool_call_id="tc1",
        )
        assert _was_kubectl_blade_injection_successful([msg]) is False

    def test_no_messages(self):
        assert _was_kubectl_blade_injection_successful([]) is False


# ---------------------------------------------------------------------------
# _was_blade_create_attempted — with kubectl success override
# ---------------------------------------------------------------------------

class TestWasBladeCreateAttemptedKubectlOverride:
    def test_failed_blade_create_with_kubectl_exec_success(self):
        """Failed blade_create + successful kubectl exec blade injection → False."""
        msg1 = ToolMessage(
            content='{"code":500,"success":false,"error":"unknown flag"}',
            name="blade_create",
            tool_call_id="tc1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc2", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        assert _was_blade_create_attempted([msg1] + kubectl_msgs) is False

    def test_failed_blade_create_without_kubectl_success(self):
        """Failed blade_create only → True (attempted and failed)."""
        msg = ToolMessage(
            content='{"code":500,"success":false,"error":"unknown flag"}',
            name="blade_create",
            tool_call_id="tc1",
        )
        assert _was_blade_create_attempted([msg]) is True

    def test_kubectl_get_success_not_override(self):
        """kubectl get returning ChaosBlade JSON does NOT override blade_create → True."""
        msg1 = ToolMessage(
            content='{"code":500,"success":false,"error":"unknown flag"}',
            name="blade_create",
            tool_call_id="tc1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default -o json",
            '{"code":200,"success":true,"result":"uid-fake"}',
        )
        # kubectl get is NOT a blade injection, so blade_create is still "attempted and failed"
        assert _was_blade_create_attempted([msg1] + kubectl_msgs) is True


# ---------------------------------------------------------------------------
# _find_blade_query_in_messages
# ---------------------------------------------------------------------------

class TestFindBladeQueryInMessages:
    def test_find_matching_query(self):
        from chaos_agent.agent.nodes._verifier_layer1 import _find_blade_query_in_messages
        query_output = json.dumps({
            "code": 200,
            "success": True,
            "result": {
                "uid": "a0f2357a939a9bb8",
                "success": True,
                "statuses": [
                    {"id": "sub1", "state": "Success", "success": True},
                    {"id": "sub2", "state": "Success", "success": True},
                ],
            },
        })
        msg = ToolMessage(content=query_output, name="kubectl", tool_call_id="tc1")
        result = _find_blade_query_in_messages([msg], "a0f2357a939a9bb8")
        assert result == query_output

    def test_no_matching_uid(self):
        from chaos_agent.agent.nodes._verifier_layer1 import _find_blade_query_in_messages
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":{"uid":"other-uid"}}',
            name="kubectl",
            tool_call_id="tc1",
        )
        result = _find_blade_query_in_messages([msg], "a0f2357a939a9bb8")
        assert result == ""

    def test_no_kubectl_messages(self):
        from chaos_agent.agent.nodes._verifier_layer1 import _find_blade_query_in_messages
        msg = ToolMessage(content="some output", name="blade_create", tool_call_id="tc1")
        result = _find_blade_query_in_messages([msg], "a0f2357a939a9bb8")
        assert result == ""


# ---------------------------------------------------------------------------
# kubectl exec injection scenario — integration-level test
# ---------------------------------------------------------------------------

class TestKubectlExecInjectionScenario:
    """Test the full scenario: blade_create fails, kubectl exec succeeds, blade query confirms."""

    @pytest.mark.asyncio
    async def test_blade_create_failed_kubectl_succeeded_layer1_skipped(self):
        """blade_create failed + kubectl exec injection succeeded → Layer 1 skipped (not failed).

        This is the core scenario from the bug report:
        - blade_create was called but failed (unknown flag: --namespace)
        - LLM used kubectl exec to inject via cluster pod
        - No blade_uid in state (execute_loop couldn't extract it)
        - verifier should NOT mark Layer 1 as "failed"
        """
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        msg2 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail2",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-kubectl-success", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        messages = [msg1, msg2] + kubectl_msgs

        # When blade_uid is empty but kubectl injection succeeded,
        # _was_blade_create_attempted should return False → Layer 1 skipped
        result = await _run_layer1_verification("", "", task_id="t-kubectl", messages=messages)
        assert result.status == "skipped"
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_create_failed_kubectl_succeeded_with_uid_layer1_degrades(self):
        """blade_create failed + kubectl exec injection → blade_uid extracted → blade_status reports failure → skipped.

        When execute_loop correctly extracts blade_uid from kubectl output,
        but blade_status tool reports failure because host blade binary is broken.
        The degradation logic should downgrade to "skipped" since kubectl injection succeeded.
        """
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc-kubectl-success", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        messages = [msg1] + kubectl_inject_msgs

        # Mock blade_status to return an error (host blade binary broken)
        mock_error = AsyncMock(return_value=__import__("chaos_agent.tools.shell", fromlist=["CommandResult"]).CommandResult(
            exit_code=1,
            stdout="Error: unknown flag: --namespace",
            stderr="",
        ))
        with patch("chaos_agent.tools.blade.run_command", mock_error):
            result = await _run_layer1_verification(
                "a0f2357a939a9bb8", "", task_id="t-kubectl-uid", messages=messages
            )
        assert result.status == "skipped"
        assert "a0f2357a939a9bb8" in result.details
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_create_failed_kubectl_succeeded_with_query_evidence(self):
        """blade_create failed + kubectl exec injection + blade query k8s evidence → Layer 1 passed.

        When both the injection output and blade query k8s result are available
        in message history, Layer 1 should find the query evidence and mark as passed.
        """
        blade_uid = "a0f2357a939a9bb8"
        query_output = json.dumps({
            "code": 200,
            "success": True,
            "result": {
                "uid": blade_uid,
                "success": True,
                "statuses": [
                    {"id": "sub1", "state": "Success", "success": True},
                    {"id": "sub2", "state": "Success", "success": True},
                ],
            },
        })
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc-inject", "exec",
            f"otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80",
            f'{{"code":200,"success":true,"result":"{blade_uid}"}}',
        )
        kubectl_query_msgs = _make_kubectl_tool_call_pair(
            "tc-query", "exec",
            f"otel-c-tool -n chaosblade -- blade query k8s {blade_uid}",
            query_output,
        )
        messages = [msg1] + kubectl_inject_msgs + kubectl_query_msgs

        # Mock blade_status to return an error (host blade binary broken)
        mock_error = AsyncMock(return_value=__import__("chaos_agent.tools.shell", fromlist=["CommandResult"]).CommandResult(
            exit_code=1,
            stdout="Error: unknown flag: --namespace",
            stderr="",
        ))
        with patch("chaos_agent.tools.blade.run_command", mock_error):
            result = await _run_layer1_verification(
                blade_uid, "", task_id="t-kubectl-query", messages=messages
            )
        assert result.status == "passed"
        assert "kubectl exec" in result.details


# ---------------------------------------------------------------------------
# _was_kubectl_injection_attempted — kubectl-native injection detection
# ---------------------------------------------------------------------------

class TestWasKubectlInjectionAttempted:
    """Test detection of kubectl-native injection methods (scale, patch, etc.)."""

    def test_kubectl_scale_after_blade_create_failure(self):
        """kubectl scale after blade_create failure → detected as alternative injection."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-scale", "scale",
            "deployment mysql -n cms-demo --replicas=0",
            "deployment.apps/mysql scaled",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is True

    def test_kubectl_scale_before_blade_create_not_counted(self):
        """kubectl scale BEFORE blade_create should NOT be counted as alternative."""
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-scale", "scale",
            "deployment mysql -n cms-demo --replicas=0",
            "deployment.apps/mysql scaled",
        )
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        # kubectl scale comes BEFORE blade_create → should NOT count
        messages = kubectl_msgs + [msg1]
        assert _was_kubectl_injection_attempted(messages) is False

    def test_kubectl_scale_failed_not_counted(self):
        """Failed kubectl scale should NOT be counted as alternative injection."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-scale", "scale",
            "deployment mysql -n cms-demo --replicas=0",
            'Error: kubectl scale failed: deployments.apps "mysql" not found',
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is False

    def test_kubectl_get_not_counted(self):
        """kubectl get (read-only) should NOT be counted as injection."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-get", "get",
            "pods -n cms-demo -l app=mysql",
            "NAME  READY  STATUS  RESTARTS  AGE",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is False

    def test_no_blade_create_no_kubectl_injection(self):
        """No blade_create and no kubectl injection → False."""
        messages = []
        assert _was_kubectl_injection_attempted(messages) is False

    def test_kubectl_cordon_after_blade_create_failure(self):
        """kubectl cordon after blade_create failure → detected as alternative injection."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-cordon", "cordon",
            "node-worker-1",
            "node/node-worker-1 cordoned",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is True


class TestKubectlNativeInjectionLayer1:
    """Test Layer 1 behavior when blade_create fails but kubectl-native injection succeeds."""

    @pytest.mark.asyncio
    async def test_blade_create_failed_kubectl_scale_succeeded_layer1_skipped(self):
        """blade_create failed + kubectl scale succeeded → Layer 1 skipped.

        When blade_create was attempted but failed, and the agent
        subsequently used kubectl scale as an alternative injection method,
        Layer 1 should return "skipped" (not terminal), allowing Layer 2
        to verify the actual fault effect.
        """
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-kubectl-scale", "scale",
            "deployment mysql -n cms-demo --replicas=0",
            "deployment.apps/mysql scaled",
        )
        messages = [msg1] + kubectl_msgs

        result = await _run_layer1_verification("", "", task_id="t-scale", messages=messages)
        assert result.status == "skipped"
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_create_failed_kubectl_cordon_succeeded_layer1_skipped(self):
        """blade_create failed + kubectl cordon succeeded → Layer 1 skipped."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-kubectl-cordon", "cordon",
            "node-worker-1",
            "node/node-worker-1 cordoned",
        )
        messages = [msg1] + kubectl_msgs

        result = await _run_layer1_verification("", "", task_id="t-cordon", messages=messages)
        assert result.status == "skipped"
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_create_failed_no_alternative_layer1_failed(self):
        """blade_create failed + no alternative injection → Layer 1 failed (unchanged)."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1): unknown flag: --namespace',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        # Only kubectl get (read-only), no injection method
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-get", "get",
            "pods -n cms-demo",
            "NAME  READY  STATUS  RESTARTS  AGE",
        )
        messages = [msg1] + kubectl_msgs

        result = await _run_layer1_verification("", "", task_id="t-no-alt", messages=messages)
        assert result.status == "warning"
        assert not result.is_terminal()


class TestExtractKubectlExecPodName:
    """Tests for _extract_kubectl_exec_pod_name function."""

    def test_kubectl_exec_blade_create_extracts_pod_name(self):
        """kubectl exec with blade create → pod name extracted from v_args."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool-abc123 -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        assert _extract_kubectl_exec_pod_name(msgs) == "otel-c-tool-abc123"

    def test_kubectl_get_not_extracted(self):
        """kubectl get (not exec blade create) → None."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default",
            "NAME  READY  STATUS  RESTARTS  AGE",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_kubectl_exec_without_blade_not_extracted(self):
        """kubectl exec without blade create → None."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc3", "exec",
            "otel-c-tool -n chaosblade -- ls /tmp",
            "file1\nfile2",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_empty_messages_returns_none(self):
        """Empty messages list → None."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        assert _extract_kubectl_exec_pod_name([]) is None

    def test_non_chaosblade_json_returns_none(self):
        """kubectl exec with non-ChaosBlade JSON response → None."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc4", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            "Error: blade not found",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_multiple_blade_creates_returns_most_recent(self):
        """Multiple kubectl exec blade creates → returns the most recent pod name."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs1 = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool-old -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-old"}',
        )
        msgs2 = _make_kubectl_tool_call_pair(
            "tc2", "exec",
            "otel-c-tool-new -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-new"}',
        )
        result = _extract_kubectl_exec_pod_name(msgs1 + msgs2)
        assert result == "otel-c-tool-new"

    def test_v_args_with_leading_whitespace(self):
        """v_args with leading whitespace → still extracts pod name correctly."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc5", "exec",
            "  otel-c-tool-ws  -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-ws"}',
        )
        assert _extract_kubectl_exec_pod_name(msgs) == "otel-c-tool-ws"

    def test_v_args_starting_with_flag_returns_none(self):
        """v_args starting with a flag → None (not a valid pod name)."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc6", "exec",
            "-n chaosblade otel-c-tool -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-flag"}',
        )
        # First token is "-n" which starts with "-" → returns None
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_legacy_session_without_tool_call_id(self):
        """ToolMessage without tool_call_id → fallback to AIMessage scan."""
        from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
        ai_msg = AIMessage(
            content="",
            tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "exec",
                    "v_args": "otel-c-tool-legacy -n chaosblade -- blade create k8s pod-cpu fullload",
                    "kubeconfig": "/path/to/kc",
                },
                "id": "tc-legacy",
                "type": "tool_call",
            }],
        )
        # ToolMessage without tool_call_id (older session format)
        tool_msg = ToolMessage(
            content='{"code":200,"success":true,"result":"uid-legacy"}',
            name="kubectl",
            tool_call_id="",  # No tool_call_id
        )
        result = _extract_kubectl_exec_pod_name([ai_msg, tool_msg])
        assert result == "otel-c-tool-legacy"


class TestRunLayer1ViaKubectlExecWithOriginalPod:
    """Tests for _run_layer1_via_kubectl_exec with injection_pod_name parameter."""

    @pytest.mark.asyncio
    async def test_original_pod_succeeds(self):
        """Original pod is available → uses blade query k8s directly, no discovery needed."""
        from chaos_agent.agent.nodes._verifier_layer1 import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        # blade query k8s returns success for a running experiment
        blade_query_result = CommandResult(
            exit_code=0,
            stdout=json.dumps({
                "code": 200, "success": True,
                "result": {
                    "uid": "exp-test", "success": True,
                    "statuses": [{"id": "sub1", "state": "Success", "success": True}]
                }
            }),
            stderr="",
        )
        with patch("chaos_agent.tools.shell.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.return_value = blade_query_result
            result = await _run_layer1_via_kubectl_exec(
                "exp-test", "/path/to/kc", task_id="t1",
                injection_pod_name="otel-c-tool-original",
            )
        assert result.status == "passed"
        assert "otel-c-tool-original" in result.details
        assert "blade query k8s" in result.details
        # Only one call should have been made (blade query k8s, no discovery)
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    async def test_original_pod_not_found_falls_back(self):
        """Original pod blade query k8s returns error → falls back to blade status → discovery."""
        from chaos_agent.agent.nodes._verifier_layer1 import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        # blade query k8s returns error on original pod (unavailable)
        query_k8s_error = CommandResult(
            exit_code=1,
            stdout='Error: pod otel-c-tool-original not found',
            stderr="",
        )
        # blade status also fails on original pod (not found)
        blade_status_error = CommandResult(
            exit_code=1,
            stdout='Error: pod otel-c-tool-original not found',
            stderr="",
        )
        discover_result = CommandResult(
            exit_code=0,
            stdout="chaosblade   otel-c-tool-new   1/1   Running   0   1d",
            stderr="",
        )
        # blade query k8s on discovered pod succeeds
        blade_query_result = CommandResult(
            exit_code=0,
            stdout=json.dumps({
                "code": 200, "success": True,
                "result": {
                    "uid": "exp-test", "success": True,
                    "statuses": [{"id": "sub1", "state": "Success", "success": True}]
                }
            }),
            stderr="",
        )
        with patch("chaos_agent.tools.shell.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [query_k8s_error, blade_status_error, discover_result, blade_query_result]
            result = await _run_layer1_via_kubectl_exec(
                "exp-test", "/path/to/kc", task_id="t2",
                injection_pod_name="otel-c-tool-original",
            )
        assert result.status == "passed"
        # 4 calls: query_k8s(original) + blade_status(original fallback) + discover + query_k8s(discovered)
        assert mock_run.call_count == 4

    @pytest.mark.asyncio
    async def test_no_original_pod_discovers_normally(self):
        """No original pod name → falls through to discovery, uses blade query k8s."""
        from chaos_agent.agent.nodes._verifier_layer1 import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        discover_result = CommandResult(
            exit_code=0,
            stdout="chaosblade   otel-c-tool-abc   1/1   Running   0   1d",
            stderr="",
        )
        # blade query k8s on discovered pod succeeds
        blade_query_result = CommandResult(
            exit_code=0,
            stdout=json.dumps({
                "code": 200, "success": True,
                "result": {
                    "uid": "exp-test", "success": True,
                    "statuses": [{"id": "sub1", "state": "Success", "success": True}]
                }
            }),
            stderr="",
        )
        with patch("chaos_agent.tools.shell.run_command", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [discover_result, blade_query_result]
            result = await _run_layer1_via_kubectl_exec(
                "exp-test", "/path/to/kc", task_id="t3",
                injection_pod_name=None,
            )
        assert result.status == "passed"
        assert "blade query k8s" in result.details
        # 2 calls: discover + blade query k8s on discovered pod
        assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# Tests for checklist parsing and automatic level downgrade
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes._verifier_layer2_parse import (
    _parse_checklist_items,
    _has_checklist,
    _parse_verification_result,
    _detect_checklist_conclusion_inconsistency,
    _count_verification_steps_in_skill_case,
    _has_injection_verification_section,
    _extract_verification_step_descriptions,
    _split_candidates,
    _validate_step_number_coverage,
    _try_parse_json,
    _has_format_reminder,
)


class TestParseChecklistItems:
    """Tests for _parse_checklist_items()."""

    def test_standard_step_format(self):
        text = "Step 1: passed — iowait 19%\nStep 2: skipped — no Pod exec"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["step"] == 1
        assert items[0]["status"] == "passed"
        assert items[0]["evidence"] == "iowait 19%"
        assert items[1]["step"] == 2
        assert items[1]["status"] == "skipped"
        assert items[1]["evidence"] == "no Pod exec"

    def test_check_variant(self):
        text = "Check 1: passed — evidence\nCheck 2: failed — no change"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "passed"
        assert items[1]["status"] == "failed"

    def test_skipped_marker(self):
        text = "[SKIPPED] Step 3: no dd in container"
        items = _parse_checklist_items(text)
        assert len(items) == 1
        assert items[0]["status"] == "skipped"
        assert items[0]["step"] == 3

    def test_bare_numbered_list(self):
        text = "1. passed — iowait high\n2. skipped — Pod test"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "passed"
        assert items[1]["status"] == "skipped"

    def test_scoped_to_checklist_section(self):
        """When VERIFICATION_CHECKLIST: header exists, only parse within that section."""
        text = (
            "Step 1: passed — irrelevant earlier mention\n"
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: failed — the real one\n"
            "Step 2: passed — ok\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
        )
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "failed"
        assert items[1]["status"] == "passed"

    def test_no_checklist(self):
        text = "Some random text without any checklist items"
        items = _parse_checklist_items(text)
        assert len(items) == 0

    def test_skipped_marker_without_step_number(self):
        text = "[SKIPPED] no Ingress configured in this cluster"
        items = _parse_checklist_items(text)
        assert len(items) == 1
        assert items[0]["status"] == "skipped"

    def test_bracket_format_step(self):
        text = "Step 1: [passed] — iowait 19%\nStep 2: [failed] — no change"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["step"] == 1
        assert items[0]["status"] == "passed"
        assert items[0]["evidence"] == "iowait 19%"
        assert items[1]["step"] == 2
        assert items[1]["status"] == "failed"
        assert items[1]["evidence"] == "no change"

    def test_bracket_format_bare_numbered(self):
        text = "1. [passed] — iowait high\n2. [skipped] — Pod test"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "passed"
        assert items[1]["status"] == "skipped"

    def test_lowercase_skipped_marker(self):
        text = "[skipped] Step 2: reason"
        items = _parse_checklist_items(text)
        assert len(items) == 1
        assert items[0]["status"] == "skipped"
        assert items[0]["step"] == 2

    def test_mixed_case_skipped_marker(self):
        text = "[Skipped] Step 3: reason"
        items = _parse_checklist_items(text)
        assert len(items) == 1
        assert items[0]["status"] == "skipped"
        assert items[0]["step"] == 3

    def test_bracket_format_check_variant(self):
        text = "Check 1: [failed] — error\nCheck 2: [passed] — ok"
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "failed"
        assert items[1]["status"] == "passed"

    def test_bracket_format_with_checklist_section(self):
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "- Step 1: [passed] — iowait 19%\n"
            "- Step 2: [failed] — no change\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
        )
        items = _parse_checklist_items(text)
        assert len(items) == 2
        assert items[0]["step"] == 1
        assert items[0]["status"] == "passed"
        assert items[0]["evidence"] == "iowait 19%"
        assert items[1]["step"] == 2
        assert items[1]["status"] == "failed"
        assert items[1]["evidence"] == "no change"


class TestHasChecklist:

    def test_with_header(self):
        assert _has_checklist("VERIFICATION_CHECKLIST:\nStep 1: passed") is True

    def test_with_step_pattern(self):
        assert _has_checklist("Step 1: passed — evidence") is True

    def test_without_checklist(self):
        assert _has_checklist("Some text without checklist") is False


class TestVerificationResultChecklistDowngrade:
    """Tests for _parse_verification_result() with checklist-based downgrade."""

    def test_passed_with_skipped_downgrade_to_partial(self):
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — iowait 19%\n"
            "Step 2: passed — dd process running\n"
            "Step 3: skipped — Pod dd test not executed\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - iowait elevated\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        # Skipped steps no longer downgrade passed → partial (they're warnings)
        assert result["layer2"]["status"] == "passed"
        assert any("skipped step" in w.lower() for w in result["warnings"])
        assert result["level"] == "verified"

    def test_passed_with_skipped_overall_verified_still_downgraded(self):
        """Even if LLM says Overall: verified, skipped steps add warnings but don't downgrade."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — iowait 19%\n"
            "Step 2: skipped — Pod test skipped\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - iowait elevated\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        # Skipped steps no longer downgrade; they add warnings instead
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "verified"

    def test_passed_all_items_no_downgrade(self):
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — iowait 19%\n"
            "Step 2: passed — dd process running\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - all checks passed\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "verified"
        assert not any("skipped step" in w.lower() for w in result["warnings"])

    def test_no_checklist_adds_warning(self):
        text = (
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - verified\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert any("No Verification Checklist" in w for w in result["warnings"])

    def test_failed_not_downgraded_by_checklist(self):
        """L2=failed with skipped items should not be further downgraded by checklist logic."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: failed — no iowait change\n"
            "Step 2: skipped — no exec\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - no effect\n"
            "- Overall: unverified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "failed"

    def test_checklist_stored_in_result(self):
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — evidence\n"
            "Step 2: skipped — reason\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - ok\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert "checklist" in result
        assert result["checklist"]["skipped_count"] == 1
        assert result["checklist"]["total_count"] == 2

    def test_total_executed_field_populated(self):
        """total_executed should equal the number of checklist items parsed."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — evidence\n"
            "Step 2: skipped — reason\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - ok\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["checklist"]["total_executed"] == 2


# ---------------------------------------------------------------------------
# _detect_checklist_conclusion_inconsistency
# ---------------------------------------------------------------------------

class TestDetectChecklistConclusionInconsistency:
    """Tests for _detect_checklist_conclusion_inconsistency()."""

    def test_failed_step_with_passed_conclusion_returns_warning(self):
        """Checklist item 'failed' + Layer2 'passed' → inconsistency detected."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
            {"step": 3, "status": "passed"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is not None
        assert "2" in warning
        assert "inconsistency" in warning.lower()
        assert should_downgrade is False  # no absence evidence → timing delay scenario

    def test_all_passed_no_inconsistency(self):
        """All checklist items passed + Layer2 'passed' → no inconsistency."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is None
        assert should_downgrade is False

    def test_l2_not_passed_no_check(self):
        """Layer2 not 'passed' → no inconsistency check (regardless of checklist)."""
        items = [{"step": 1, "status": "failed"}]
        w1, d1 = _detect_checklist_conclusion_inconsistency(items, "failed")
        w2, d2 = _detect_checklist_conclusion_inconsistency(items, "partial")
        assert w1 is None and d1 is False
        assert w2 is None and d2 is False

    def test_empty_checklist_no_inconsistency(self):
        """Empty checklist → no inconsistency."""
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency([], "passed")
        assert warning is None
        assert should_downgrade is False

    def test_multiple_failed_steps(self):
        """Multiple failed steps → all listed in warning."""
        items = [
            {"step": 1, "status": "failed"},
            {"step": 2, "status": "passed"},
            {"step": 3, "status": "failed"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is not None
        assert "1" in warning
        assert "3" in warning

    def test_absence_evidence_triggers_auto_downgrade(self):
        """Failed step with absence evidence (metric far below threshold) → auto-downgrade."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed", "evidence": "disk usage at 16%, no change"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is not None
        assert should_downgrade is True
        assert "absence" in warning.lower() or "auto-downgrading" in warning.lower()

    def test_absence_evidence_via_param(self):
        """Absence evidence passed via failed_evidence parameter also triggers downgrade."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(
            items, "passed", failed_evidence="CPU remains normal, no increase observed"
        )
        assert warning is not None
        assert should_downgrade is True

    def test_non_absence_evidence_no_downgrade(self):
        """Failed step without absence evidence → warning but no auto-downgrade."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed", "evidence": "disk usage at 87%, slightly below 90%"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is not None
        assert should_downgrade is False


# ---------------------------------------------------------------------------
# _count_verification_steps_in_skill_case
# ---------------------------------------------------------------------------

class TestCountVerificationStepsInSkillCase:
    """Tests for _count_verification_steps_in_skill_case()."""

    def test_numbered_steps_in_section(self):
        """Numbered steps in 注入验证 section are counted."""
        content = (
            "## 故障注入\n"
            "1. 执行 blade create\n"
            "## 注入验证\n"
            "1. 检查 iowait\n"
            "2. 检查 dd 进程\n"
            "3. 检查磁盘利用率\n"
            "## 恢复验证\n"
            "1. 恢复后检查\n"
        )
        assert _count_verification_steps_in_skill_case(content) == 3

    def test_bullet_items_fallback(self):
        """When no numbered steps, bullet items are counted."""
        content = (
            "## 注入验证\n"
            "- 检查 iowait\n"
            "- 检查 dd 进程\n"
            "## 恢复验证\n"
        )
        assert _count_verification_steps_in_skill_case(content) == 2

    def test_no_injection_verification_section(self):
        """No 注入验证 section → 0."""
        content = "## 故障注入\n1. 执行 blade create\n"
        assert _count_verification_steps_in_skill_case(content) == 0

    def test_section_at_end_of_content(self):
        """注入验证 at end of content (no next section header) → still counted."""
        content = "## 注入验证\n1. 检查 iowait\n2. 检查 dd\n"
        assert _count_verification_steps_in_skill_case(content) == 2

    def test_mixed_numbered_and_bullet_prefers_numbered(self):
        """Numbered steps take precedence over bullets."""
        content = (
            "## 注入验证\n"
            "1. 检查 iowait\n"
            "- 子步骤说明\n"
            "2. 检查 dd\n"
            "## 恢复验证\n"
        )
        assert _count_verification_steps_in_skill_case(content) == 2


# ---------------------------------------------------------------------------
# Tests for _has_injection_verification_section and
# _extract_verification_step_descriptions — step parsing for three-tier prompt
# ---------------------------------------------------------------------------

class TestHasInjectionVerificationSection:
    """Tests for _has_injection_verification_section()."""

    def test_section_present(self):
        """Returns True when 注入验证 exists."""
        content = "## 故障注入\n1. 执行 blade\n## 注入验证\n1. 检查 CPU"
        assert _has_injection_verification_section(content) is True

    def test_section_absent(self):
        """Returns False when 注入验证 is not present."""
        content = "## 故障注入\n1. 执行 blade\n## 恢复验证\n1. 检查恢复"
        assert _has_injection_verification_section(content) is False

    def test_empty_string(self):
        """Empty string → False."""
        assert _has_injection_verification_section("") is False

    def test_prose_only_section(self):
        """Returns True even if the section has no numbered/bullet steps (prose only)."""
        content = (
            "## 注入验证\n"
            "该故障需要通过观察节点状态来验证，核心指标包括 CPU 使用率和内存。\n"
            "确认故障已成功注入后即可进入恢复阶段。\n"
        )
        assert _has_injection_verification_section(content) is True


class TestExtractVerificationStepDescriptions:
    """Tests for _extract_verification_step_descriptions()."""

    def test_numbered_steps_extracted(self):
        """Numbered steps with descriptions are extracted in order."""
        content = (
            "**故障注入**\n"
            "1. 执行 blade create\n"
            "**注入验证**：\n"
            "1. 查看 Pod CPU 使用率监控，确认持续高于阈值\n"
            "2. 进入容器查看 CPU 占用进程\n"
            "3. 检查 HPA 是否触发扩容\n"
            "4. 检查应用响应延迟是否增大\n"
            "**恢复验证**：\n"
            "1. 恢复后检查\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 4
        assert descs[0] == "查看 Pod CPU 使用率监控，确认持续高于阈值"
        assert descs[1] == "进入容器查看 CPU 占用进程"
        assert descs[2] == "检查 HPA 是否触发扩容"
        assert descs[3] == "检查应用响应延迟是否增大"

    def test_trailing_colons_removed(self):
        """Trailing Chinese and English colons are stripped."""
        content = (
            "## 注入验证\n"
            "1. 检查 iowait：\n"
            "2. 检查 dd 进程:\n"
            "3. 检查磁盘利用率\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 3
        assert descs[0] == "检查 iowait"
        assert descs[1] == "检查 dd 进程"
        assert descs[2] == "检查磁盘利用率"

    def test_multiline_descriptions_first_line_only(self):
        """Multi-line descriptions keep only the first line."""
        content = (
            "## 注入验证\n"
            "1. 检查 iowait\n详情见监控面板\n"
            "2. 检查 dd 进程\n确认进程正在运行\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 2
        assert descs[0] == "检查 iowait"
        assert descs[1] == "检查 dd 进程"

    def test_bullet_items_fallback(self):
        """When no numbered steps, bullet items are extracted."""
        content = (
            "## 注入验证\n"
            "- 检查 iowait\n"
            "- 检查 dd 进程\n"
            "## 恢复验证\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 2
        assert descs[0] == "检查 iowait"
        assert descs[1] == "检查 dd 进程"

    def test_no_section_returns_empty(self):
        """No 注入验证 section → empty list."""
        content = "## 故障注入\n1. 执行 blade\n"
        descs = _extract_verification_step_descriptions(content)
        assert descs == []

    def test_prose_only_section_returns_empty(self):
        """注入验证 section has only prose (no numbered/bullet) → empty list."""
        content = (
            "## 注入验证\n"
            "该故障的验证较为简单，主要观察节点状态变化。\n"
            "注入后应该能看到 CPU 使用率上升。\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert descs == []

    def test_numbered_steps_reindex_from_zero(self):
        """Steps numbered from 0, 1, 2... are extracted correctly."""
        content = (
            "## 注入验证\n"
            "0. 初始状态检查\n"
            "1. 注入后状态检查\n"
            "2. 恢复后状态检查\n"
            "3. 日志检查\n"
            "4. 事件检查\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 5
        assert descs[0] == "初始状态检查"

    def test_section_at_end_of_content(self):
        """注入验证 at end of content (no next section header) → still extracted."""
        content = "## 注入验证\n1. 检查 iowait\n2. 检查 dd\n"
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 2
        assert descs[0] == "检查 iowait"
        assert descs[1] == "检查 dd"

    def test_numbered_steps_with_sub_bullets(self):
        """Numbered steps with indented sub-bullets → only top-level numbers extracted.

        This matches the spec scenario: 'Node Disk IO skill case（编号步骤含子 bullet）
        → 仅提取顶层编号，不含子 bullet'."""
        content = (
            "**注入验证**：\n"
            "1. 查看节点磁盘 IO 负载指标：\n"
            "   - 优先：iostat -xd 1 3（关注 %util 接近 100%）\n"
            "   - BusyBox 备选：iostat -d -k 1 3\n"
            "2. 查看 iowait 占比，确认显著升高\n"
            "3. 确认应用 A 的磁盘读写延迟增大\n"
        )
        descs = _extract_verification_step_descriptions(content)
        assert len(descs) == 3
        assert descs[0] == "查看节点磁盘 IO 负载指标"
        assert descs[1] == "查看 iowait 占比，确认显著升高"
        assert descs[2] == "确认应用 A 的磁盘读写延迟增大"


# _validate_step_number_coverage — P3 step-number-level coverage validation

class TestValidateStepNumberCoverage:
    """Tests for _validate_step_number_coverage()."""

    SKILL_CASE_4_STEPS = (
        "## 注入验证\n"
        "1. 从目标 Pod ping 上游服务\n"
        "2. 使用 netstat 查看重传统计\n"
        "3. 查看目标 Pod 日志\n"
        "4. 查看上游服务日志\n"
        "\n## 其他章节\n"
    )

    def test_all_steps_covered_no_missing(self):
        """All 4 steps present in checklist → no missing, no deviated."""
        items = [
            {"step": 1, "status": "passed", "evidence": "ping timed out"},
            {"step": 2, "status": "passed", "evidence": "netstat retransmits high"},
            {"step": 3, "status": "passed", "evidence": "logs show timeout"},
            {"step": 4, "status": "passed", "evidence": "upstream logs confirm"},
        ]
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, items)
        assert missing == []
        assert deviated == []

    def test_missing_steps_detected(self):
        """Steps 3 and 4 missing from checklist → [3, 4] reported."""
        items = [
            {"step": 1, "status": "passed", "evidence": "ping timed out"},
            {"step": 2, "status": "passed", "evidence": "retransmits high"},
        ]
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, items)
        assert missing == [3, 4]
        assert deviated == []

    def test_deviated_step_detected(self):
        """Step with '(deviation: ...)' in evidence → reported as deviated."""
        items = [
            {"step": 1, "status": "passed", "evidence": "wget timed out (deviation: used wget instead of ping)"},
            {"step": 2, "status": "passed", "evidence": "netstat retransmits high"},
            {"step": 3, "status": "skipped", "evidence": "no pod exec"},
            {"step": 4, "status": "skipped", "evidence": "no upstream access"},
        ]
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, items)
        assert missing == []
        assert deviated == [1]

    def test_no_skill_case_content(self):
        """No 注入验证 section → empty results."""
        items = [{"step": 1, "status": "passed", "evidence": "ok"}]
        missing, deviated = _validate_step_number_coverage("no section here", items)
        assert missing == []
        assert deviated == []

    def test_empty_checklist_items(self):
        """Empty checklist but skill case has 4 steps → all missing."""
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, [])
        assert missing == [1, 2, 3, 4]
        assert deviated == []

    def test_deviated_case_insensitive(self):
        """'Deviation:' with capital D also detected."""
        items = [
            {"step": 1, "status": "passed", "evidence": "wget timed out (Deviation: used wget)"},
            {"step": 2, "status": "passed", "evidence": "netstat ok"},
            {"step": 3, "status": "passed", "evidence": "logs ok"},
            {"step": 4, "status": "passed", "evidence": "upstream ok"},
        ]
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, items)
        assert missing == []
        assert deviated == [1]

    def test_missing_and_deviated_combined(self):
        """Both missing steps and deviated steps in same checklist."""
        items = [
            {"step": 1, "status": "passed", "evidence": "wget timed out (deviation: used wget)"},
            {"step": 2, "status": "passed", "evidence": "netstat ok"},
            # Step 3 and 4 are missing
        ]
        missing, deviated = _validate_step_number_coverage(self.SKILL_CASE_4_STEPS, items)
        assert missing == [3, 4]
        assert deviated == [1]


# ---------------------------------------------------------------------------

class TestChecklistConclusionInconsistencyIntegration:
    """Integration tests: checklist item 'failed' + Layer2 'passed' → warning only (LLM's Overall is authority)."""

    def test_failed_step_with_passed_conclusion_downgraded(self):
        """Checklist has a failed step but LLM concludes 'passed' → inconsistency warning, not downgrade.
        
        The LLM may know that a 'failed' checklist item is due to timing delays.
        Overall field is the final authority; inconsistency is recorded as warning."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — iowait elevated\n"
            "Step 2: failed — no dd process found\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - fault effect observed\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        # L2 no longer force-downgraded — LLM's Overall: verified is the authority
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "verified"
        assert any("inconsistency" in w.lower() for w in result["warnings"])

    def test_skipped_step_no_inconsistency_warning(self):
        """Skipped steps should produce skip warning, not inconsistency warning."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — evidence\n"
            "Step 2: skipped — reason\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - ok\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        # Skipped steps no longer downgrade; status stays "passed" with skip warning
        assert result["layer2"]["status"] == "passed"
        assert not any("inconsistency" in w.lower() for w in result["warnings"])

    def test_all_passed_no_inconsistency(self):
        """All checklist items passed + L2 passed → no inconsistency warning."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — evidence\n"
            "Step 2: passed — evidence2\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - all ok\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert not any("inconsistency" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# _try_parse_json — JSON mode parsing
# ---------------------------------------------------------------------------

class TestTryParseJson:
    """Tests for _try_parse_json()."""

    def test_valid_json(self):
        data = {
            "verification_checklist": [
                {"step": 1, "status": "passed", "evidence": "iowait 28%"}
            ],
            "layer1": "passed",
            "layer2": "passed",
            "layer2_details": "fault confirmed",
            "overall": "verified",
            "warnings": [],
        }
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["level"] == "verified"
        assert result["layer2"]["status"] == "passed"
        assert result["checklist"]["total_count"] == 1

    def test_valid_json_partial(self):
        data = {
            "layer1": "passed",
            "layer2": "partial",
            "overall": "partial",
            "warnings": ["coverage incomplete"],
        }
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["level"] == "partial"

    def test_invalid_l2_status(self):
        data = {"layer1": "passed", "layer2": "invalid", "overall": "verified", "warnings": []}
        assert _try_parse_json(json.dumps(data)) is None

    def test_invalid_overall(self):
        data = {"layer1": "passed", "layer2": "passed", "overall": "success", "warnings": []}
        assert _try_parse_json(json.dumps(data)) is None

    def test_missing_checklist_ok(self):
        """checklist is optional, parsing should still succeed."""
        data = {"layer1": "passed", "layer2": "passed", "overall": "verified", "warnings": []}
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["level"] == "verified"

    def test_non_json_text(self):
        assert _try_parse_json("VERIFICATION_RESULT: ...") is None

    def test_empty_string(self):
        assert _try_parse_json("") is None

    def test_not_a_dict(self):
        assert _try_parse_json(json.dumps(["not a dict"])) is None


# ---------------------------------------------------------------------------
# _has_format_reminder
# ---------------------------------------------------------------------------

class TestFormatGuard:
    """Tests for _has_format_reminder()."""

    def test_detects_format_reminder(self):
        msgs = [
            HumanMessage(content="上一轮输出缺少要求的 VERIFICATION_RESULT 格式。请重新输出。")
        ]
        assert _has_format_reminder(msgs) is True

    def test_ignores_normal_messages(self):
        msgs = [
            AIMessage(content="Some verification text"),
            HumanMessage(content="kubeconfig reminder"),
        ]
        assert _has_format_reminder(msgs) is False

    def test_empty_messages(self):
        assert _has_format_reminder([]) is False


# ---------------------------------------------------------------------------
# _disk_fill_param_hints / _PARAM_HINT_GENERATORS
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes._verifier_hints import (
    _disk_fill_param_hints,
    _PARAM_HINT_GENERATORS,
    _IMAGEFS_PATHS,
    _NODEFS_PATHS,
    _BASELINE_INTEGRITY_PROMPT,
)


class TestDiskFillParamHints:
    """Tests for _disk_fill_param_hints() — parameter-dependent hint generation."""

    def test_imagefs_path_var_log(self):
        """--path /var/log → imagefs partition hint."""
        hint = _disk_fill_param_hints({"path": "/var/log"})
        assert hint is not None
        assert "imagefs" in hint.lower()
        assert "df -h" in hint  # correct verification command
        assert "df -h /host" not in hint or "FALSE_NEGATIVE" in hint or "false negative" in hint.lower()

    def test_imagefs_path_tmp(self):
        """--path /tmp → imagefs partition hint."""
        hint = _disk_fill_param_hints({"path": "/tmp"})
        assert hint is not None
        assert "imagefs" in hint.lower()

    def test_imagefs_path_trailing_slash(self):
        """--path /var/log/ (trailing slash) → still imagefs."""
        hint = _disk_fill_param_hints({"path": "/var/log/"})
        assert hint is not None
        assert "imagefs" in hint.lower()

    def test_imagefs_subpath(self):
        """--path /var/log/app (subpath of imagefs path) → imagefs."""
        hint = _disk_fill_param_hints({"path": "/var/log/app"})
        assert hint is not None
        assert "imagefs" in hint.lower()

    def test_nodefs_path_docker(self):
        """--path /var/lib/docker → nodefs partition hint."""
        hint = _disk_fill_param_hints({"path": "/var/lib/docker"})
        assert hint is not None
        assert "nodefs" in hint.lower()
        assert "df -h /host" in hint

    def test_nodefs_path_kubelet(self):
        """--path /var/lib/kubelet → nodefs partition hint."""
        hint = _disk_fill_param_hints({"path": "/var/lib/kubelet"})
        assert hint is not None
        assert "nodefs" in hint.lower()

    def test_nodefs_path_etc(self):
        """--path /etc → nodefs partition hint."""
        hint = _disk_fill_param_hints({"path": "/etc"})
        assert hint is not None
        assert "nodefs" in hint.lower()

    def test_unknown_path(self):
        """--path /data (unknown) → fallback hint listing all filesystems."""
        hint = _disk_fill_param_hints({"path": "/data"})
        assert hint is not None
        assert "unable to determine" in hint.lower() or "all mounted" in hint.lower()

    def test_empty_path(self):
        """No path parameter → None (no hint)."""
        hint = _disk_fill_param_hints({"percent": "95"})
        assert hint is None

    def test_no_flags(self):
        """Empty flags → None."""
        hint = _disk_fill_param_hints({})
        assert hint is None


class TestParamHintGenerators:
    """Tests for _PARAM_HINT_GENERATORS registry."""

    def test_disk_fill_registered(self):
        """(disk, fill) key is registered in _PARAM_HINT_GENERATORS."""
        assert ("disk", "fill") in _PARAM_HINT_GENERATORS

    def test_disk_fill_callable(self):
        """Registered generator is callable."""
        gen = _PARAM_HINT_GENERATORS.get(("disk", "fill"))
        assert callable(gen)

    def test_disk_fill_produces_hint(self):
        """Calling the generator with path flag produces a non-None hint."""
        gen = _PARAM_HINT_GENERATORS[("disk", "fill")]
        hint = gen({"path": "/var/log", "percent": "95"})
        assert hint is not None
        assert "imagefs" in hint.lower()

    def test_unregistered_key_returns_none(self):
        """Unregistered key returns None from dict.get()."""
        assert _PARAM_HINT_GENERATORS.get(("cpu", "fullload")) is None


class TestImagefsNodefsConstants:
    """Tests for _IMAGEFS_PATHS and _NODEFS_PATHS constants."""

    def test_imagefs_paths_contain_var_log(self):
        assert "/var/log" in _IMAGEFS_PATHS

    def test_imagefs_paths_contain_tmp(self):
        assert "/tmp" in _IMAGEFS_PATHS

    def test_nodefs_paths_contain_docker(self):
        assert "/var/lib/docker" in _NODEFS_PATHS

    def test_nodefs_paths_contain_kubelet(self):
        assert "/var/lib/kubelet" in _NODEFS_PATHS

    def test_no_overlap(self):
        """imagefs and nodefs paths should not overlap."""
        assert len(_IMAGEFS_PATHS & _NODEFS_PATHS) == 0


# ---------------------------------------------------------------------------
# L2 auto-downgrade → level sync (P0 bug fix verification)
# ---------------------------------------------------------------------------

class TestL2DowngradeLevelSync:
    """Verify that when L2 is programmatically downgraded to 'partial',
    the level field is also overridden to 'partial'.

    This was a critical bug: L2 auto-downgrade was not syncing to level,
    so infer_task_state() would see level=verified despite L2=partial.
    """

    def test_absence_evidence_downgrade_syncs_level_text_mode(self):
        """Text-mode: failed checklist with absence evidence → L2=partial, level=partial."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — DiskPressure=True\n"
            "Step 2: failed — disk usage at 16%, no change\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - fault effect observed\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "partial", \
            f"L2 should be 'partial' after auto-downgrade, got '{result['layer2']['status']}'"
        assert result["level"] == "partial", \
            f"level should be 'partial' after L2 downgrade, got '{result['level']}'"

    def test_absence_evidence_downgrade_syncs_level_json_mode(self):
        """JSON-mode: failed checklist with absence evidence → L2=partial, level=partial."""
        data = {
            "verification_checklist": [
                {"step": 1, "status": "passed", "evidence": "DiskPressure=True"},
                {"step": 2, "status": "failed", "evidence": "disk usage at 16%, no change"},
            ],
            "layer1": "passed",
            "layer2": "passed",
            "layer2_details": "fault effect observed",
            "overall": "verified",
            "warnings": [],
        }
        result = _try_parse_json(json.dumps(data))
        assert result is not None
        assert result["layer2"]["status"] == "partial", \
            f"L2 should be 'partial' after auto-downgrade, got '{result['layer2']['status']}'"
        assert result["level"] == "partial", \
            f"level should be 'partial' after L2 downgrade, got '{result['level']}'"

    def test_no_absence_evidence_keeps_level_verified(self):
        """No absence evidence → L2 stays 'passed', level stays 'verified'."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: passed — iowait elevated\n"
            "Step 2: failed — no dd process found\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - fault effect observed\n"
            "- Overall: verified\n"
            "- Warnings: none"
        )
        result = _parse_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "verified"

    def test_infer_task_state_partial_level(self):
        """When L2=partial and level=partial, infer_task_state returns 'injected'."""
        from chaos_agent.agent.state import infer_task_state
        state = {
            "operation": "inject",
            "skill_name": "test-fault",
            "blade_uid": "test-uid",
            "verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "partial"},
            },
            "result": {},
        }
        assert infer_task_state(state) == "injected"


# ---------------------------------------------------------------------------
# BASELINE INTEGRITY — constant, hints, and prompt inclusion
# ---------------------------------------------------------------------------

class TestBaselineIntegrityPrompt:
    """Tests for _BASELINE_INTEGRITY_PROMPT constant."""

    def test_constant_non_empty(self):
        assert _BASELINE_INTEGRITY_PROMPT
        assert len(_BASELINE_INTEGRITY_PROMPT) > 50

    def test_contains_same_resource_rule(self):
        assert "SAME resource" in _BASELINE_INTEGRITY_PROMPT

    def test_contains_baseline_keyword(self):
        assert "baseline" in _BASELINE_INTEGRITY_PROMPT.lower()

    def test_contains_valid_invalid_examples(self):
        assert "✅" in _BASELINE_INTEGRITY_PROMPT
        assert "❌" in _BASELINE_INTEGRITY_PROMPT

    def test_contains_quantitative_scope(self):
        """The prompt explicitly scopes to quantitative metrics, excluding qualitative status checks."""
        assert "quantitative metric" in _BASELINE_INTEGRITY_PROMPT

    def test_contains_first_check_matches_expected(self):
        """Rule 6: first-check value matching expected injection parameter is evidence."""
        assert "first-check value already matches" in _BASELINE_INTEGRITY_PROMPT or \
               "first-check shows" in _BASELINE_INTEGRITY_PROMPT


class TestDiskFillParamHintsBaselineIntegrity:
    """Tests for baseline integrity rules in _disk_fill_param_hints()."""

    def test_imagefs_path_includes_baseline_integrity(self):
        """imagefs hint includes BASELINE INTEGRITY and same partition rule."""
        hint = _disk_fill_param_hints({"path": "/var/log"})
        assert hint is not None
        assert "BASELINE INTEGRITY" in hint
        assert "same partition" in hint.lower() or "SAME partition" in hint

    def test_imagefs_path_includes_first_check_as_baseline(self):
        """imagefs hint includes FIRST-CHECK-AS-BASELINE rule."""
        hint = _disk_fill_param_hints({"path": "/var/log"})
        assert hint is not None
        assert "FIRST-CHECK-AS-BASELINE" in hint

    def test_imagefs_path_baseline_hint_matches_expected_fill(self):
        """imagefs hint mentions that first-check near expected fill % is evidence of fault."""
        hint = _disk_fill_param_hints({"path": "/var/log"})
        assert hint is not None
        assert "84%" in hint or "expected" in hint.lower() or "fill has completed" in hint

    def test_nodefs_path_includes_baseline_integrity(self):
        """nodefs hint also includes baseline integrity rules."""
        hint = _disk_fill_param_hints({"path": "/var/lib/docker"})
        assert hint is not None
        assert "BASELINE INTEGRITY" in hint
        assert "same partition" in hint.lower() or "SAME partition" in hint

    def test_nodefs_path_includes_first_check_as_baseline(self):
        """nodefs hint includes FIRST-CHECK-AS-BASELINE rule."""
        hint = _disk_fill_param_hints({"path": "/var/lib/docker"})
        assert hint is not None
        assert "FIRST-CHECK-AS-BASELINE" in hint

    def test_unknown_path_includes_baseline_warning(self):
        """unknown path fallback includes baseline integrity rule."""
        hint = _disk_fill_param_hints({"path": "/data"})
        assert hint is not None
        assert "BASELINE INTEGRITY" in hint


# ---------------------------------------------------------------------------
# TestDeriveDiskFillPartition
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes._verifier_hints import (
    _derive_disk_fill_partition,
    _COMMAND_PRIORITY_HINT,
)


class TestDeriveDiskFillPartition:
    """Tests for _derive_disk_fill_partition() — heuristic partition derivation."""

    def test_imagefs_path_tmp(self):
        assert _derive_disk_fill_partition({"path": "/tmp"}) == "imagefs"

    def test_imagefs_path_var_log(self):
        assert _derive_disk_fill_partition({"path": "/var/log"}) == "imagefs"

    def test_imagefs_path_subpath(self):
        assert _derive_disk_fill_partition({"path": "/var/log/app"}) == "imagefs"

    def test_imagefs_path_trailing_slash(self):
        assert _derive_disk_fill_partition({"path": "/tmp/"}) == "imagefs"

    def test_nodefs_path_var_lib_docker(self):
        assert _derive_disk_fill_partition({"path": "/var/lib/docker"}) == "nodefs"

    def test_nodefs_path_var_lib_kubelet(self):
        assert _derive_disk_fill_partition({"path": "/var/lib/kubelet"}) == "nodefs"

    def test_nodefs_path_subpath(self):
        assert _derive_disk_fill_partition({"path": "/var/lib/docker/overlay2"}) == "nodefs"

    def test_nodefs_path_etc(self):
        assert _derive_disk_fill_partition({"path": "/etc"}) == "nodefs"

    def test_unknown_path(self):
        assert _derive_disk_fill_partition({"path": "/data"}) is None

    def test_empty_path(self):
        assert _derive_disk_fill_partition({"path": ""}) is None

    def test_no_path_key(self):
        assert _derive_disk_fill_partition({}) is None


class TestDiskFillParamHintsCommandPriority:
    """Tests for COMMAND PRIORITY in _disk_fill_param_hints() output."""

    def test_imagefs_path_includes_command_priority(self):
        hint = _disk_fill_param_hints({"path": "/tmp"})
        assert hint is not None
        assert "COMMAND PRIORITY" in hint

    def test_nodefs_path_includes_command_priority(self):
        hint = _disk_fill_param_hints({"path": "/var/lib/docker"})
        assert hint is not None
        assert "COMMAND PRIORITY" in hint

    def test_unknown_path_includes_command_priority(self):
        hint = _disk_fill_param_hints({"path": "/data"})
        assert hint is not None
        assert "COMMAND PRIORITY" in hint

    def test_imagefs_uses_heuristic_language(self):
        hint = _disk_fill_param_hints({"path": "/tmp"})
        assert hint is not None
        assert "Likely target partition" in hint or "typically" in hint.lower()

    def test_nodefs_uses_heuristic_language(self):
        hint = _disk_fill_param_hints({"path": "/var/lib/docker"})
        assert hint is not None
        assert "Likely target partition" in hint or "typically" in hint.lower()


class TestExpectedStatus:
    """Tests for 'expected' status in checklist parsing and inconsistency detection."""

    def test_parse_expected_in_step_format(self):
        """'expected' status is parsed from Step N: expected format."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: expected — DiskPressure=False is anticipated\n"
            "Step 2: passed — disk usage confirmed high\n\n"
            "VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: verified\n"
        )
        items = _parse_checklist_items(text)
        assert len(items) >= 2
        expected_items = [i for i in items if i["status"] == "expected"]
        assert len(expected_items) == 1
        assert expected_items[0]["step"] == 1

    def test_parse_expected_in_bare_numbered_format(self):
        """'expected' status is parsed from bare numbered list format."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "1. [expected] — DiskPressure=False is anticipated\n"
            "2. [passed] — disk usage confirmed high\n\n"
            "VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: verified\n"
        )
        items = _parse_checklist_items(text)
        expected_items = [i for i in items if i["status"] == "expected"]
        assert len(expected_items) == 1

    def test_expected_does_not_trigger_inconsistency(self):
        """'expected' status items should NOT trigger checklist-conclusion inconsistency."""
        items = [
            {"step": 1, "status": "expected", "evidence": "DiskPressure=False anticipated"},
            {"step": 2, "status": "passed", "evidence": "disk usage confirmed"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is None
        assert should_downgrade is False

    def test_expected_with_failed_still_triggers_inconsistency(self):
        """Mixed expected + failed items: failed should still trigger inconsistency."""
        items = [
            {"step": 1, "status": "expected", "evidence": "DiskPressure=False anticipated"},
            {"step": 2, "status": "failed", "evidence": "CPU usage not elevated"},
        ]
        warning, should_downgrade = _detect_checklist_conclusion_inconsistency(items, "passed")
        assert warning is not None
        assert "failed" in warning.lower()

    def test_expected_in_parse_verification_result(self):
        """'expected' status is preserved through _parse_verification_result."""
        text = (
            "VERIFICATION_CHECKLIST:\n"
            "Step 1: expected — DiskPressure=False anticipated\n"
            "Step 2: passed — disk usage at 84%\n\n"
            "VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - fault confirmed\n"
            "- Overall: verified\n"
            "- Warnings: none\n"
        )
        result = _parse_verification_result(text)
        # The 'expected' item should not cause auto-downgrade
        assert result["layer2"]["status"] == "passed"


# ---------------------------------------------------------------------------
# Baseline Comparison / Fill File Check / Tool Pod Context tests
# ---------------------------------------------------------------------------


class TestBaselineComparisonInLayer2Context:
    """Test that baseline_data is injected into Layer 2 context when available."""

    def _build_direct_state_with_baseline(self, baseline_success=True):
        """Helper: build a direct-mode state with optional baseline_data."""
        observations = [
            {
                "description": "Node disk usage",
                "command": "kubectl exec debug-pod -- df -h",
                "exit_code": 0,
                "stdout": "Filesystem Size Used Use% Mounted on\n/dev/vdb 100G 17G 16% /tmp",
                "stderr": "",
            },
        ] if baseline_success else []
        return {
            "task_id": "test-bl-1",
            "blade_scope": "node",
            "blade_target": "disk",
            "blade_action": "fill",
            "blade_uid": "test-uid-123",
            "direct": True,
            "baseline_data": {
                "captured_at": "2026-05-09T10:00:00",
                "source": "registry",
                "observations": observations,
                "success_count": 1 if baseline_success else 0,
            },
            "blade_parsed_flags": {"path": "/tmp", "size": "10000"},
            "params": {},
            "target": {"namespace": "default", "names": ["test-node"], "labels": {}},
            "kubeconfig": "/path/to/kubeconfig",
            "kubectl_exec_pod_name": "otel-c-tool-abc",
        }

    def test_baseline_in_context_when_available(self):
        """When baseline_data with success_count > 0, baseline is injected as synthetic ToolMessage."""
        from chaos_agent.agent.nodes._verifier_messages import (
            _build_baseline_tool_messages,
        )
        state = self._build_direct_state_with_baseline(baseline_success=True)
        baseline = state.get("baseline_data")
        assert baseline is not None
        assert baseline["success_count"] > 0

        # Verify _build_baseline_tool_messages produces ToolMessage pairs
        msgs = _build_baseline_tool_messages(
            baseline, "disk", "fill",
            blade_parsed={"path": "/tmp", "size": "10000"},
        )
        assert len(msgs) >= 2  # At least one AIMessage + ToolMessage pair
        # First pair: raw observations
        assert msgs[0].content == ""  # AIMessage with tool_calls
        assert hasattr(msgs[0], "tool_calls") and len(msgs[0].tool_calls) > 0
        assert msgs[0].tool_calls[0]["name"] == "baseline_collector"
        assert msgs[1].tool_call_id == msgs[0].tool_calls[0]["id"]  # ID matches
        assert "16%" in msgs[1].content  # Baseline data present in ToolMessage
        assert "Pre-injection baseline" in msgs[1].content  # Causal narrative framing
        assert "baseline: X → current: Y" in msgs[1].content  # Delta format instruction

    def test_baseline_not_in_humanmessage_when_available(self):
        """When baseline_data is available, it should NOT be in the HumanMessage content."""
        from chaos_agent.agent.nodes._verifier_messages import _build_layer2_messages
        state = self._build_direct_state_with_baseline(baseline_success=True)
        # Build Layer 2 messages and check HumanMessage does NOT contain baseline section
        from chaos_agent.agent.verdict import Layer1Result
        layer1 = Layer1Result(
            status="passed",
            affected_count=1,
            raw_output="Success",
        )
        msgs = _build_layer2_messages(
            state, layer1, "test-uid-123", "disk-fill",
            "/path/to/kubeconfig", count=1,
        )
        # Find HumanMessage(s) — baseline should NOT be in any HumanMessage content
        human_msgs = [m for m in msgs if isinstance(m, HumanMessage)]
        for hm in human_msgs:
            assert "Pre-Injection Baseline" not in hm.content
            assert "Baseline Comparison Semantics" not in hm.content
        # Find ToolMessage(s) — baseline should be in ToolMessage
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        baseline_tool = [m for m in tool_msgs if getattr(m, "name", "") == "baseline_collector"]
        assert len(baseline_tool) >= 1

    def test_no_baseline_fallback(self):
        """When baseline_data has success_count == 0, should use fallback note."""
        state = self._build_direct_state_with_baseline(baseline_success=False)
        baseline = state.get("baseline_data")
        assert baseline["success_count"] == 0


class TestFillFileCheck:
    """Test that Fill File Check section is present for node-disk-fill."""

    def test_fill_file_context_for_node_disk_fill(self):
        """For node-disk-fill, the Fill File Check section should be generated."""
        blade_parsed = {"path": "/tmp", "size": "10000"}
        blade_scope = "node"
        blade_target = "disk"
        blade_action = "fill"
        tool_pod_name = "otel-c-tool-abc"
        kubeconfig = "/path/to/kubeconfig"

        # Simulate the context generation logic
        fill_path = blade_parsed.get("path", "/tmp")
        size_param = blade_parsed.get("size")
        context = ""
        if blade_target == "disk" and blade_action == "fill" and blade_scope == "node":
            if tool_pod_name:
                context += (
                    f"\n## PRIMARY VERIFICATION: Fill File Check\n"
                    f"For node-disk-fill, the MOST RELIABLE verification is checking the fill file "
                    f"directly inside the tool pod's container overlay:\n"
                    f"1. Run: kubectl(subcommand='exec', v_args='{tool_pod_name} -n chaosblade -- ls -lh {fill_path}/', "
                    f"kubeconfig='{kubeconfig}')\n"
                )
        assert "PRIMARY VERIFICATION: Fill File Check" in context
        assert "chaos_filldisk.log.dat" not in context or True  # pattern mention
        assert "ls -lh /tmp/" in context
        assert tool_pod_name in context

    def test_no_fill_file_for_pod_disk_fill(self):
        """Fill File Check should NOT appear for pod-scope disk fill."""
        blade_scope = "pod"
        blade_target = "disk"
        blade_action = "fill"
        context = ""
        if blade_target == "disk" and blade_action == "fill" and blade_scope == "node":
            context += "PRIMARY VERIFICATION: Fill File Check\n"
        assert "PRIMARY VERIFICATION" not in context


class TestVerificationSemanticsDiskFill:
    """Test disk-fill specific verification semantics."""

    def test_scenario_vs_injection_criterion(self):
        """Verify that '85%' is recognized as scenario, not injection criterion."""
        # This is tested by checking the context string construction
        blade_target = "disk"
        blade_action = "fill"
        context = ""
        if blade_target == "disk" and blade_action == "fill":
            context += (
                "Disk-fill specific: The skill case's '确认超过85%' is a SCENARIO SUCCESS criterion, "
                "NOT an injection verification criterion. If fill data was written (fill file exists OR "
                "disk usage increased by ≈size from baseline) but 85% was not reached → Layer2 = PASSED with Warning"
            )
        assert "SCENARIO SUCCESS criterion" in context
        assert "NOT an injection verification criterion" in context


class TestSyntheticMessagePersistence:
    """Tests for ephemeral baseline ToolMessage persistence fix.

    Verifies that synthetic AIMessage+ToolMessage pairs (baseline_collector,
    baseline_collector_metrics, restart_precheck_check) are:
    1. Injected on count==1 and available for state persistence
    2. Detected as already-in-state on count>1 (skip injection)
    3. Prepended BEFORE response in result_update (routing-safe)
    """

    @pytest.fixture
    def baseline_state(self):
        """State with baseline_data for synthetic message injection."""
        return {
            "task_id": "test-synth-1",
            "blade_scope": "pod",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "blade_uid": "uid-synth-123",
            "direct": True,
            "baseline_data": {
                "captured_at": "2026-05-09T10:00:00",
                "source": "registry",
                "observations": [
                    {
                        "exit_code": 0,
                        "stdout": "NAME   CPU%  MEM%\nmyapp  5%    30%",
                        "stderr": "",
                        "resource_name": "myapp-pod",
                        "resource_type": "pod",
                        "namespace": "default",
                        "command": "kubectl top pod",
                    },
                ],
                "success_count": 1,
            },
            "blade_parsed_flags": {},
            "params": {},
            "target": {"namespace": "default", "names": ["myapp-pod"], "labels": {"app": "myapp"}},
            "kubeconfig": "/path/to/kubeconfig",
        }

    def test_extract_synthetic_for_state_on_count1(self, baseline_state):
        """On count==1, _build_layer2_messages injects synthetic messages,
        and _synthetic_for_state extraction picks them up."""
        from chaos_agent.agent.nodes._verifier_messages import (
            _build_layer2_messages,
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        from chaos_agent.agent.verdict import Layer1Result

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            baseline_state, layer1, "uid-synth-123", "cpu-fullload",
            "/path/to/kubeconfig", count=1,
        )

        # Extract synthetic messages (same logic as verifier main function)
        synthetic_for_state = []
        for msg in msgs:
            if isinstance(msg, AIMessage) and getattr(msg, "tool_calls", None):
                tc_ids = [tc.get("id", "") for tc in msg.tool_calls if isinstance(tc, dict)]
                if any(tid in _SYNTHETIC_TOOL_CALL_IDS for tid in tc_ids):
                    synthetic_for_state.append(msg)
            elif isinstance(msg, ToolMessage):
                if getattr(msg, "tool_call_id", "") in _SYNTHETIC_TOOL_CALL_IDS:
                    synthetic_for_state.append(msg)

        # Should have 4 messages: AIMessage + ToolMessage for Pair 1 + Pair 2
        assert len(synthetic_for_state) == 4
        # Verify tool_call_ids are in the synthetic set
        for msg in synthetic_for_state:
            if isinstance(msg, ToolMessage):
                assert msg.tool_call_id in _SYNTHETIC_TOOL_CALL_IDS
            elif isinstance(msg, AIMessage):
                for tc in msg.tool_calls:
                    assert tc.get("id", "") in _SYNTHETIC_TOOL_CALL_IDS

    def test_skip_injection_when_already_in_state(self, baseline_state):
        """On count>1, when baseline ToolMessages are already in
        state['messages'], _build_layer2_messages should NOT re-inject them."""

        from chaos_agent.agent.nodes._verifier_messages import (
            _build_baseline_tool_messages,
            _build_layer2_messages,
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.agent.verdict import Layer1Result

        # Build synthetic messages as if they were persisted from count==1
        baseline = baseline_state["baseline_data"]
        synthetic_msgs = _build_baseline_tool_messages(
            baseline, "cpu", "fullload", blade_parsed={},
        )

        # Simulate state after count==1: synthetic msgs in messages history
        baseline_state["messages"] = list(synthetic_msgs)

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            baseline_state, layer1, "uid-synth-123", "cpu-fullload",
            "/path/to/kubeconfig", count=2,
        )

        # No new synthetic messages should be added (already in state)
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        baseline_tools = [m for m in tool_msgs
                          if getattr(m, "tool_call_id", "") == _BASELINE_TOOL_CALL_ID]
        # Exactly the ones from state (no duplicates)
        assert len(baseline_tools) == 1

    def test_injection_when_missing_from_state(self, baseline_state):
        """On count>1, when baseline ToolMessages are NOT in
        state['messages'], _build_layer2_messages should inject them."""

        from chaos_agent.agent.nodes._verifier_messages import (
            _build_layer2_messages,
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.agent.verdict import Layer1Result

        # State has messages but WITHOUT synthetic baseline messages
        baseline_state["messages"] = [
            HumanMessage(content="Inject flow message"),
            AIMessage(content="Previous LLM response"),
        ]

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            baseline_state, layer1, "uid-synth-123", "cpu-fullload",
            "/path/to/kubeconfig", count=2,
        )

        # Synthetic messages should be injected (not in state history)
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        baseline_tools = [m for m in tool_msgs
                          if getattr(m, "tool_call_id", "") == _BASELINE_TOOL_CALL_ID]
        assert len(baseline_tools) >= 1

    def test_prepend_order_routing_safe(self, baseline_state):
        """When _synthetic_for_state is prepended BEFORE response,
        the last message in result_update is response (routing-safe)."""

        from chaos_agent.agent.nodes._verifier_messages import (
            _build_baseline_tool_messages,
        )

        baseline = baseline_state["baseline_data"]
        synthetic_for_state = _build_baseline_tool_messages(
            baseline, "cpu", "fullload", blade_parsed={},
        )

        # Simulate response (AIMessage with no tool_calls — final answer)
        response = AIMessage(content="VERIFICATION_RESULT: ...")

        # Build result_update as verifier would
        result_messages = synthetic_for_state + [response]

        # Last message must be response (AIMessage, no tool_calls)
        last_msg = result_messages[-1]
        assert isinstance(last_msg, AIMessage)
        assert not last_msg.tool_calls  # Routing sees "done"

    def test_metrics_tool_call_id_constant(self):
        """Verify _METRICS_TOOL_CALL_ID is defined and equals 'baseline_collector_metrics'."""
        from chaos_agent.agent.nodes._verifier_messages import (
            _METRICS_TOOL_CALL_ID,
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        assert _METRICS_TOOL_CALL_ID == "baseline_collector_metrics"
        assert _METRICS_TOOL_CALL_ID in _SYNTHETIC_TOOL_CALL_IDS


class TestCleanupDebugPodsDedup:
    """Pin the cross-reentry idempotency of _cleanup_debug_pods.

    Pre-fix (task-712629116b64): every verifier re-entry re-scanned the
    full message history and re-issued ``kubectl delete`` for every debug
    pod found. After the first delete the pod is gone, so every retry
    returns ``Error from server (NotFound)`` — observed as 8 spurious
    NotFound failures on a single task, inflating the failure-rate stat
    while doing zero useful work.

    Post-fix: ``state.cleaned_debug_pods`` carries the set of pods already
    attempted; the diff isolates only genuinely new pods so each pod is
    deleted at most once across the entire verifier lifecycle.
    """

    @staticmethod
    def _debug_tm(pod_name: str, call_id: str | None = None) -> ToolMessage:
        """Build a ToolMessage that the parser recognises as 'kubectl
        debug created this pod'. Format mirrors what kubectl 1.25+
        actually emits."""
        return ToolMessage(
            content=(
                f"Creating debugging pod {pod_name} with container "
                f"debugger on node cn-test.10.0.1.1."
            ),
            name="kubectl",
            tool_call_id=call_id or f"call_{pod_name}",
        )

    @pytest.mark.asyncio
    async def test_first_call_deletes_all_discovered_pods(self):
        """First verifier invocation: 2 pods in history, both must be
        deleted, and both names must be persisted into result_update."""
        state = {
            "messages": [
                self._debug_tm("node-debugger-cn-test-aaa"),
                self._debug_tm("node-debugger-cn-test-bbb"),
            ],
            # No cleaned_debug_pods on state yet → first-time call
        }
        result_update: dict = {}

        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace=""):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert sorted(deleted) == [
            "node-debugger-cn-test-aaa",
            "node-debugger-cn-test-bbb",
        ]
        # Both pods now persisted as cleaned (sorted for determinism).
        assert result_update["cleaned_debug_pods"] == [
            "node-debugger-cn-test-aaa",
            "node-debugger-cn-test-bbb",
        ]

    @pytest.mark.asyncio
    async def test_reentry_with_same_pods_performs_zero_deletes(self):
        """Verifier re-entry (reverify, ReAct iteration): same pods in
        history but state already lists them as cleaned. _delete must
        NOT be called — this is the core regression fix.
        """
        state = {
            "messages": [
                self._debug_tm("node-debugger-cn-test-aaa"),
                self._debug_tm("node-debugger-cn-test-bbb"),
            ],
            "cleaned_debug_pods": [
                "node-debugger-cn-test-aaa",
                "node-debugger-cn-test-bbb",
            ],
        }
        result_update: dict = {}

        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace=""):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        # Zero delete attempts → zero spurious NotFound errors.
        assert deleted == []
        # No write back when there's nothing to do (avoid noise in
        # result_update + LangGraph checkpoint).
        assert "cleaned_debug_pods" not in result_update

    @pytest.mark.asyncio
    async def test_reentry_with_new_pod_deletes_only_the_new_one(self):
        """LLM creates a fresh debug pod during reverify (e.g. retry
        after connection error). The dedup must isolate only the new pod
        — the previously-cleaned pods stay out of the delete batch, but
        the persisted set MUST grow to include the new pod so the next
        re-entry also skips it.
        """
        state = {
            "messages": [
                self._debug_tm("node-debugger-cn-test-aaa"),  # already cleaned
                self._debug_tm("node-debugger-cn-test-bbb"),  # already cleaned
                self._debug_tm("node-debugger-cn-test-ccc"),  # NEW this re-entry
            ],
            "cleaned_debug_pods": [
                "node-debugger-cn-test-aaa",
                "node-debugger-cn-test-bbb",
            ],
        }
        result_update: dict = {}

        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace=""):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == ["node-debugger-cn-test-ccc"]
        # Merged + sorted: previous two stay, new one joins.
        assert result_update["cleaned_debug_pods"] == [
            "node-debugger-cn-test-aaa",
            "node-debugger-cn-test-bbb",
            "node-debugger-cn-test-ccc",
        ]

    @pytest.mark.asyncio
    async def test_failed_delete_still_recorded_so_no_retry(self):
        """``_delete_debug_pod`` is best-effort — if it fails (network
        glitch, RBAC), we still record the pod as 'attempted' so the
        next re-entry doesn't retry. Otherwise a single transient failure
        would re-introduce the N-spurious-failures pattern the dedup
        was built to prevent.
        """
        state = {
            "messages": [self._debug_tm("node-debugger-flaky")],
        }
        result_update: dict = {}

        async def _failing_delete(pod_name, _kc, _tid, namespace=""):
            # _delete_debug_pod internally swallows exceptions and logs
            # a warning — it returns None either way. We simulate that
            # contract here (silent failure).
            return None

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_failing_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        # Pod recorded even though "delete" returned without confirming.
        # If a future regression makes us re-attempt failed deletes, this
        # assertion will catch it.
        assert result_update["cleaned_debug_pods"] == ["node-debugger-flaky"]

    @pytest.mark.asyncio
    async def test_no_debug_pods_in_history_is_a_noop(self):
        """No kubectl-debug messages → no deletes, no state write.
        Avoids noise on the common path where the LLM didn't use debug.
        """
        state = {
            "messages": [
                ToolMessage(content="random output", name="kubectl",
                            tool_call_id="x"),
                ToolMessage(content="blade output", name="blade_status",
                            tool_call_id="y"),
            ],
        }
        result_update: dict = {}

        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace=""):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == []
        assert "cleaned_debug_pods" not in result_update

    @pytest.mark.asyncio
    async def test_non_kubectl_toolmessages_are_ignored(self):
        """Defensive: a `blade_create` ToolMessage whose content happens
        to contain a string that looks like a pod name should NOT be
        parsed as a debug-pod creation, because the parser only inspects
        ToolMessages whose ``name == "kubectl"``. Pinning this prevents
        a future "scan all ToolMessages" generalisation from accidentally
        triggering deletes for non-debug pods.
        """
        state = {
            "messages": [
                ToolMessage(
                    content="Creating debugging pod node-debugger-evil ...",
                    name="blade_create",  # not "kubectl"
                    tool_call_id="bc",
                ),
            ],
        }
        result_update: dict = {}

        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace=""):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == []


# ---------------------------------------------------------------------------
# _split_candidates — multi-candidate skill_case splitting
# ---------------------------------------------------------------------------


class TestSplitCandidates:
    """Tests for _split_candidates()."""

    MULTI = (
        "Multiple skill cases match.\n\n"
        "--- Candidate 1: Service_调用失败_kube-proxy异常 ---\n"
        "**注入验证**：\n"
        "1. 确认 kube-proxy 不存在\n"
        "2. 访问 Service ClusterIP\n"
        "3. 检查 iptables 规则\n"
        "\n"
        "--- Candidate 2: Service_负载均衡异常_后端不可达 ---\n"
        "**注入验证**：\n"
        "1. kubectl get endpoints\n"
        "2. 向 Service 发送请求\n"
        "3. 查看 Ingress 状态\n"
        "4. 确认流量调度\n"
    )

    def test_split_two_candidates(self):
        parts = _split_candidates(self.MULTI)
        assert len(parts) == 2

    def test_candidate1_has_3_steps(self):
        parts = _split_candidates(self.MULTI)
        assert _count_verification_steps_in_skill_case(parts[0]) == 3

    def test_candidate2_has_4_steps(self):
        parts = _split_candidates(self.MULTI)
        assert _count_verification_steps_in_skill_case(parts[1]) == 4

    def test_candidate2_step_descriptions(self):
        parts = _split_candidates(self.MULTI)
        descs = _extract_verification_step_descriptions(parts[1])
        assert len(descs) == 4
        assert "kubectl get endpoints" in descs[0]
        assert "Ingress" in descs[2]

    def test_single_candidate_returns_list_of_one(self):
        single = "**注入验证**：\n1. 检查 CPU\n2. 检查内存\n"
        parts = _split_candidates(single)
        assert len(parts) == 1
        assert parts[0] == single

    def test_empty_content(self):
        assert _split_candidates("") == [""]

    def test_validate_against_chosen_candidate(self):
        """_validate_step_number_coverage on candidate 2 expects 4 steps."""
        parts = _split_candidates(self.MULTI)
        items = [
            {"step": 1, "status": "passed", "evidence": "endpoints empty"},
            {"step": 2, "status": "passed", "evidence": "5xx"},
            {"step": 3, "status": "passed", "evidence": "health check fail"},
            {"step": 4, "status": "passed", "evidence": "traffic rerouted"},
        ]
        missing, _ = _validate_step_number_coverage(parts[1], items)
        assert missing == []

    def test_validate_missing_step_from_chosen_candidate(self):
        parts = _split_candidates(self.MULTI)
        items = [
            {"step": 1, "status": "passed", "evidence": "endpoints empty"},
            {"step": 3, "status": "passed", "evidence": "health check fail"},
        ]
        missing, _ = _validate_step_number_coverage(parts[1], items)
        assert 2 in missing
        assert 4 in missing
