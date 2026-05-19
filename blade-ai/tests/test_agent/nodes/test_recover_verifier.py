"""Tests for recover_verifier node: two-layer post-recovery verification."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes._injection_detection import (
    _was_kubectl_blade_injection_successful,
    _was_blade_create_attempted,
)
from chaos_agent.agent.nodes.recover_verifier import (
    RecoverLayer1Result,
    _parse_blade_destroy_output,
    _parse_blade_status_destroyed,
    _parse_recovery_verification_result,
    _parse_recovery_checklist_items,
    _has_recovery_checklist,
    _count_recovery_steps_in_skill_case,
    _detect_recovery_checklist_inconsistency,
    _detect_recovery_contradiction,
    _detect_primary_evidence_generic_contradiction,
    _parse_layer1_recovery_result,
    _build_recover_verifier_prompt,
    _build_layer1_recovery_prompt,
    _extract_recovery_verification_section,
    _layer1_to_dict,
    recover_verifier,
    make_recover_verifier,
)
from chaos_agent.agent.prompts.sections.recovery import (
    build_recover_verifier_system_prompt,
)
from chaos_agent.config.settings import settings


# ---------------------------------------------------------------------------
# RecoverLayer1Result
# ---------------------------------------------------------------------------

class TestRecoverLayer1Result:
    def test_is_passed(self):
        assert RecoverLayer1Result(status="passed").is_passed()
        assert not RecoverLayer1Result(status="failed").is_passed()

    def test_is_terminal(self):
        assert RecoverLayer1Result(status="failed").is_terminal()
        assert RecoverLayer1Result(status="error").is_terminal()
        # "skipped" is NOT terminal — it means non-ChaosBlade fault, Layer 1 not applicable
        assert not RecoverLayer1Result(status="skipped").is_terminal()
        assert not RecoverLayer1Result(status="passed").is_terminal()
        assert not RecoverLayer1Result(status="unknown").is_terminal()


# ---------------------------------------------------------------------------
# _parse_blade_destroy_output
# ---------------------------------------------------------------------------

class TestParseBladeDestroyOutput:
    def test_success_json(self):
        raw = json.dumps({"code": 200, "success": True, "result": "abc123"})
        status, details = _parse_blade_destroy_output(raw)
        assert status == "passed"
        assert "success" in details

    def test_failure_json(self):
        raw = json.dumps({"code": 500, "success": False, "error": "not found"})
        status, details = _parse_blade_destroy_output(raw)
        assert status == "failed"

    def test_error_prefix(self):
        raw = "Error: blade destroy failed: connection refused"
        status, details = _parse_blade_destroy_output(raw)
        assert status == "failed"
        assert "Error" in details

    def test_empty_string(self):
        status, details = _parse_blade_destroy_output("")
        assert status == "failed"

    def test_non_json_success_text(self):
        raw = "destroy success"
        status, details = _parse_blade_destroy_output(raw)
        assert status == "passed"

    def test_non_json_gibberish(self):
        raw = "something went wrong"
        status, details = _parse_blade_destroy_output(raw)
        assert status == "failed"


# ---------------------------------------------------------------------------
# _parse_blade_status_destroyed
# ---------------------------------------------------------------------------

class TestParseBladeStatusDestroyed:
    def test_destroyed_status(self):
        raw = json.dumps({"code": 200, "result": {"Status": "Destroyed"}})
        status, details = _parse_blade_status_destroyed(raw)
        assert status == "passed"
        assert "Destroyed" in details

    def test_still_running(self):
        raw = json.dumps({"code": 200, "result": {"Status": "Running"}})
        status, details = _parse_blade_status_destroyed(raw)
        assert status == "failed"
        assert "Running" in details

    def test_not_found_406(self):
        raw = json.dumps({"code": 406, "success": False})
        status, details = _parse_blade_status_destroyed(raw)
        assert status == "passed"
        assert "already destroyed" in details

    def test_non_json_with_destroyed(self):
        raw = "Status: Destroyed"
        status, details = _parse_blade_status_destroyed(raw)
        assert status == "passed"

    def test_non_json_unparseable(self):
        raw = "gibberish text"
        status, details = _parse_blade_status_destroyed(raw)
        assert status == "unknown"


# ---------------------------------------------------------------------------
# _parse_recovery_verification_result
# ---------------------------------------------------------------------------

class TestParseRecoveryVerificationResult:
    def test_full_recovered(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - CPU normal\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "recovered"
        assert result["layer1"]["status"] == "passed"
        assert result["layer2"]["status"] == "passed"

    def test_unrecovered(self):
        text = (
            "- Layer1: passed\n"
            "- Layer2: failed - CPU still high\n"
            "- Overall: unrecovered"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "unrecovered"
        assert result["layer2"]["status"] == "failed"

    def test_no_overall_fallback(self):
        text = "- Layer1: passed - ok\n- Layer2: passed - ok"
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "recovered"

    def test_layer2_skipped_auto_warning(self):
        text = (
            "- Layer1: passed\n"
            "- Layer2: skipped - cannot determine verification\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "skipped"
        assert any("skipped" in w for w in result["warnings"])

    def test_wrong_format_execution_result_success(self):
        """LLM used RECOVERY_EXECUTION_RESULT (Layer 1 format) in Layer 2 with success."""
        text = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: scaled deployment mysql in namespace cms-demo from 0 replicas back to 1 replica\n"
            "- Details: mysql pod is now 1/1 Running with IP 10.0.2.39"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "recovered"
        assert any("RECOVERY_EXECUTION_RESULT" in w for w in result["warnings"])

    def test_wrong_format_execution_result_failed(self):
        """LLM used RECOVERY_EXECUTION_RESULT (Layer 1 format) in Layer 2 with failed."""
        text = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: failed\n"
            "- Actions: attempted to scale deployment\n"
            "- Details: kubectl scale returned error"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"
        assert result["level"] == "unrecovered"
        assert any("RECOVERY_EXECUTION_RESULT" in w for w in result["warnings"])

    def test_layer2_unknown_warning(self):
        """Layer 2 status unknown should produce a warning."""
        text = "Some unclear output without layer2 or overall keywords"
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "unknown"
        assert any("unknown" in w for w in result["warnings"])

    def test_correct_format_not_affected_by_wrong_format_check(self):
        """When both formats appear, RECOVERY_VERIFICATION_RESULT takes precedence."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed\n"
            "- Layer2 (fault-specific): passed - pod running normally\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert result["level"] == "recovered"
        # Should NOT have wrong-format warning
        assert not any("RECOVERY_EXECUTION_RESULT" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# _layer1_to_dict
# ---------------------------------------------------------------------------

class TestLayer1ToDict:
    def test_roundtrip(self):
        original = RecoverLayer1Result(
            status="passed",
            details="blade destroy success",
            raw_output='{"code": 200}',
        )
        d = _layer1_to_dict(original)
        assert d["status"] == "passed"
        assert d["details"] == "blade destroy success"
        assert d["raw_output"] == '{"code": 200}'


# ---------------------------------------------------------------------------
# recover_verifier (no LLM, Layer 1 only)
# ---------------------------------------------------------------------------

class TestRecoverVerifierNoLLM:
    @pytest.mark.asyncio
    async def test_no_blade_uid(self):
        """No blade_uid + no blade_create in messages → non-ChaosBlade, Layer 1 skipped, cannot verify without LLM."""
        state = {"task_id": "t1", "blade_uid": "", "skill_name": "cpu-stress", "kubeconfig": "", "messages": []}
        result = await recover_verifier(state)
        # Non-ChaosBlade fault: Layer 1 skipped, cannot verify without LLM
        assert result["result"]["recovered"] is False
        assert result["recover_verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_successful_recovery(self):
        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success, blade_status confirms: Destroyed",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "blade_uid": "abc123",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
            }
            result = await recover_verifier(state)
            assert result["result"]["recovered"] is True
            assert result["recover_verification"]["level"] == "recovered"
            assert result["recover_verification"]["layer2"]["status"] == "skipped"
            assert len(result["recover_verification"]["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_failed_recovery(self):
        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="failed",
                details="blade_destroy failed",
            )
            state = {
                "task_id": "t1",
                "blade_uid": "abc123",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
            }
            result = await recover_verifier(state)
            assert result["result"]["recovered"] is False
            assert result["recover_verification"]["level"] == "unrecovered"


# ---------------------------------------------------------------------------
# make_recover_verifier (with LLM, two-layer)
# ---------------------------------------------------------------------------

class TestMakeRecoverVerifier:
    @pytest.mark.asyncio
    async def test_returns_simple_verifier_when_no_llm(self):
        node = make_recover_verifier(llm=None, tools=None, registry=None)
        assert node is recover_verifier

    @pytest.mark.asyncio
    async def test_layer1_failed_skips_layer2(self):
        mock_llm = MagicMock()
        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="failed",
                details="blade still running",
            )
            state = {
                "task_id": "t1",
                "blade_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            assert result["result"]["recovered"] is False
            mock_llm.ainvoke.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_instructions_llm_designs_verification(self):
        """Without skill instructions, LLM is called to design verification itself.
        
        On the FIRST Layer 2 iteration, if LLM outputs a conclusion without
        executing any verification commands (no tool_calls), the programmatic
        guard intercepts and injects a mandatory verification prompt.
        The result should NOT contain a final 'result' key — the loop must
        continue so LLM can execute verification commands in the next iteration.
        """
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - CPU normal\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t1",
                "blade_uid": "abc",
                "skill_name": "unknown-skill",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            # First iteration: LLM output conclusion without verification commands
            # → programmatic guard intercepts → no final result yet
            assert "result" not in result, "First iteration should NOT produce final result"
            # Guard should inject mandatory verification prompt
            assert any("rejected" in getattr(m, "content", "") for m in result.get("messages", [])), \
                "Guard should inject a rejection prompt forcing LLM to execute commands"
            # Loop count should increment
            assert result["verifier_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_layer2_final_text(self):
        """LLM outputs final verification result on a non-first Layer 2 iteration
        (after having executed verification commands in earlier iterations).
        
        When verifier_loop_count > 0 and layer2_context_added=True, the first-iteration
        guard is bypassed and LLM's conclusion is parsed normally.
        """
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - CPU normal\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t1",
                "blade_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 2,  # Non-first iteration — guard bypassed
                "layer2_context_added": True,  # Layer 2 context already built
                "recover_phase": "layer2_verification",  # Already in Layer 2 phase
            }
            result = await node(state)
            assert result["result"]["recovered"] is True
            assert result["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_layer2_tool_call_continues_loop(self):
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pods -n default -o json"}}]

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "blade_uid": "abc",
                "skill_name": "pod-kill",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            assert "result" not in result
            assert "messages" in result
            assert result["verifier_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_max_iterations_fallback(self):
        node = make_recover_verifier(llm=AsyncMock(), tools=[], registry=None)
        state = {
            "task_id": "t1",
            "blade_uid": "abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "",
            "verifier_loop_count": settings.max_recover_verifier_loop + 1,
        }
        result = await node(state)
        assert result["recover_verification"]["level"] == "partial"
        assert result["result"]["recovered"] is False


# ---------------------------------------------------------------------------
# _run_recover_layer1
# ---------------------------------------------------------------------------

class TestRunRecoverLayer1:
    @pytest.mark.asyncio
    async def test_no_blade_uid_skipped(self):
        from chaos_agent.agent.nodes.recover_verifier import _run_recover_layer1
        result = await _run_recover_layer1("", "")
        assert result.status == "skipped"


# ---------------------------------------------------------------------------
# _parse_layer1_recovery_result
# ---------------------------------------------------------------------------

class TestParseLayer1RecoveryResult:
    def test_success(self):
        text = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: removed finalizers from pod/xxx\n"
            "- Details: pod deleted successfully"
        )
        result = _parse_layer1_recovery_result(text)
        assert result.status == "passed"
        assert "finalizers" in result.details

    def test_failed(self):
        text = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: failed\n"
            "- Actions: attempted to remove finalizers\n"
            "- Details: pod not found"
        )
        result = _parse_layer1_recovery_result(text)
        assert result.status == "failed"

    def test_no_structured_output_defaults_passed(self):
        text = "I have removed the finalizers from the pod."
        result = _parse_layer1_recovery_result(text)
        assert result.status == "passed"

    def test_result_block_with_error_indicator(self):
        text = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Actions: attempted patch\n"
            "- Details: error: connection refused"
        )
        result = _parse_layer1_recovery_result(text)
        assert result.status == "failed"


# ---------------------------------------------------------------------------
# _build_recover_verifier_prompt
# ---------------------------------------------------------------------------

class TestBuildRecoverVerifierPrompt:
    def test_chaosblade_label(self):
        prompt = _build_recover_verifier_prompt(is_chaosblade=True)
        # The output format line should use "blade_destroy" label
        assert "Layer1 (blade_destroy): passed" in prompt

    def test_non_chaosblade_label(self):
        prompt = _build_recover_verifier_prompt(is_chaosblade=False)
        # The output format line should use "recovery execution" label
        assert "Layer1 (recovery execution): passed" in prompt


# ---------------------------------------------------------------------------
# _build_layer1_recovery_prompt
# ---------------------------------------------------------------------------

class TestBuildLayer1RecoveryPrompt:
    def test_contains_constraints(self):
        prompt = _build_layer1_recovery_prompt()
        assert "DO NOT check for ChaosBlade" in prompt
        assert "DO NOT use interactive commands" in prompt
        assert "DO NOT use `blade_destroy`" in prompt

    def test_contains_programmatic_patterns(self):
        prompt = _build_layer1_recovery_prompt()
        # P2-2: Programmatic Recovery Patterns moved to kubectl docstring;
        # prompt now references tool docstring instead of inline patterns
        assert "see tool docstring" in prompt or "recovery patterns" in prompt


# ---------------------------------------------------------------------------
# make_recover_verifier with non-ChaosBlade Layer 1 (main loop)
# ---------------------------------------------------------------------------

class TestMakeRecoverVerifierNonChaosBlade:
    @pytest.mark.asyncio
    async def test_non_chaosblade_layer1_success_then_layer2(self):
        """Non-ChaosBlade: Layer 1 passes in iteration 1, Layer 2 verifies in iterations 2-3.
        
        The programmatic guard requires LLM to execute at least one verification command
        on the FIRST Layer 2 iteration. So Layer 2 needs two iterations:
        - Iteration 2: LLM executes a kubectl verification command (tool_calls)
        - Iteration 3: LLM outputs final conclusion (no tool_calls)
        """
        # Layer 1 LLM response (no tool calls, final text)
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: removed finalizers from pod/xxx\n"
            "- Details: none"
        )
        mock_l1_response.tool_calls = []

        # Layer 2 first iteration: LLM executes a verification command
        mock_l2_tool_response = MagicMock()
        mock_l2_tool_response.content = ""
        mock_l2_tool_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default -o jsonpath={.status.phase}", "kubeconfig": "/path/to/config"}}]

        # Layer 2 second iteration: LLM outputs final conclusion
        mock_l2_final_response = MagicMock()
        mock_l2_final_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed - removed finalizers\n"
            "- Layer2 (fault-specific): passed - pod deleted\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_l2_final_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_l1_response, mock_l2_tool_response, mock_l2_final_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        # Iteration 1: Layer 1 execution
        state1 = {
            "task_id": "t1",
            "blade_uid": "",
            "skill_name": "pod-terminating",
            "kubeconfig": "/path/to/config",
            "verifier_loop_count": 0,
            "messages": [],
            "target": {"namespace": "default", "names": ["test-pod"]},
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "inject_context": "Injected finalizers on pod test-pod",
        }
        result1 = await node(state1)
        # Layer 1 should have passed and transitioned to Layer 2
        assert result1.get("recover_phase") == "layer2_verification"
        assert result1.get("recover_layer1_cache", {}).get("status") == "passed"

        # Iteration 2: Layer 2 first iteration — LLM executes verification command
        state2 = {
            **state1,
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "messages": result1.get("messages", []),
            "layer2_context_added": False,
        }
        result2 = await node(state2)
        # LLM executed tool_calls → loop continues, no final result yet
        assert "result" not in result2, "Layer 2 tool_call iteration should not produce final result"

        # Iteration 3: Layer 2 second iteration — LLM outputs final conclusion
        # Simulate the ToolMessage from the kubectl call
        tool_msg = MagicMock()
        tool_msg.name = "kubectl"
        tool_msg.content = "Running"
        state3 = {
            **state1,
            "verifier_loop_count": 2,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "messages": result2.get("messages", []) + [tool_msg],
            "layer2_context_added": True,  # Context was built in iteration 2
        }
        result3 = await node(state3)
        assert result3["result"]["recovered"] is True
        assert result3["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_non_chaosblade_layer1_failed_skips_layer2(self):
        """Non-ChaosBlade: Layer 1 fails → skip Layer 2."""
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: failed\n"
            "- Actions: attempted to remove finalizers\n"
            "- Details: pod not found"
        )
        mock_l1_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_l1_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        state = {
            "task_id": "t1",
            "blade_uid": "",
            "skill_name": "pod-terminating",
            "kubeconfig": "",
            "verifier_loop_count": 0,
            "messages": [],
            "target": {"namespace": "default", "names": ["test-pod"]},
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "inject_context": "Injected finalizers on pod test-pod",
        }
        result = await node(state)
        assert result["result"]["recovered"] is False
        assert result["recover_verification"]["level"] == "unrecovered"

    @pytest.mark.asyncio
    async def test_non_chaosblade_no_inject_context_skips_layer1(self):
        """Non-ChaosBlade: no inject context → Layer 1 skipped, Layer 2 proceeds.
        
        Layer 2 first iteration must execute verification commands (programmatic guard).
        Two LLM calls: first executes kubectl, second outputs conclusion.
        """
        # Layer 2 first iteration: executes verification command
        mock_tool_response = MagicMock()
        mock_tool_response.content = ""
        mock_tool_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default", "kubeconfig": ""}}]

        # Layer 2 second iteration: outputs conclusion
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): skipped\n"
            "- Layer2 (fault-specific): passed - ok\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_tool_response, mock_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        # Iteration 1: Layer 1 skipped → Layer 2 first iteration (tool call)
        state1 = {
            "task_id": "t1",
            "blade_uid": "",
            "skill_name": "pod-terminating",
            "kubeconfig": "",
            "verifier_loop_count": 0,
            "messages": [],
            "target": {"namespace": "default", "names": ["test-pod"]},
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        result1 = await node(state1)
        # Layer 1 skipped, transitioned to Layer 2, tool call made
        assert "result" not in result1, "Tool call iteration should not produce final result"

        # Iteration 2: Layer 2 outputs conclusion
        tool_msg = MagicMock()
        tool_msg.name = "kubectl"
        tool_msg.content = "Running"
        state2 = {
            **state1,
            "verifier_loop_count": 2,
            "recover_phase": "layer2_verification",
            "layer2_context_added": True,
            "messages": result1.get("messages", []) + [tool_msg],
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "layer1_iteration_count": 1,
        }
        result2 = await node(state2)
        assert result2["result"]["recovered"] is True

    @pytest.mark.asyncio
    async def test_chaosblade_path_unchanged(self):
        """ChaosBlade fault (has blade_uid): still uses deterministic blade_destroy path.
        
        After deterministic Layer 1, Layer 2 first iteration must execute at least one
        verification command (programmatic guard). Two iterations needed.
        """
        # Layer 2 first iteration: executes kubectl verification command
        mock_tool_response = MagicMock()
        mock_tool_response.content = ""
        mock_tool_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pod -n default", "kubeconfig": ""}}]

        # Layer 2 second iteration: outputs conclusion
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - ok\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_response.tool_calls = []
        mock_response.additional_kwargs = {}

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_tool_response, mock_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success",
                raw_output='{"code": 200}',
            )
            # Iteration 1: deterministic Layer 1 + Layer 2 first iteration (tool call)
            state1 = {
                "task_id": "t1",
                "blade_uid": "abc123",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 0,
                "messages": [],
            }
            result1 = await node(state1)
            mock_l1.assert_called_once()
            assert "result" not in result1, "Tool call iteration should not produce final result"

            # Iteration 2: Layer 2 outputs conclusion
            tool_msg = MagicMock()
            tool_msg.name = "kubectl"
            tool_msg.content = "Running"
            state2 = {
                **state1,
                "verifier_loop_count": 2,
                "recover_phase": "layer2_verification",
                "layer2_context_added": True,
                "messages": result1.get("messages", []) + [tool_msg],
                "recover_layer1_cache": result1.get("recover_layer1_cache"),
                "layer1_iteration_count": 1,
            }
            result2 = await node(state2)
            assert result2["result"]["recovered"] is True


# ---------------------------------------------------------------------------
# _was_kubectl_blade_injection_successful (shared module)
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


class TestWasKubectlBladeInjectionSuccessfulRecover:
    def test_kubectl_exec_blade_create_success(self):
        msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-abc123"}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is True

    def test_kubectl_get_with_chaosblade_json_rejected(self):
        """kubectl get returning ChaosBlade JSON → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default -o json",
            json.dumps({"code": 200, "success": True, "result": "uid-fake"}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_patch_with_chaosblade_json_rejected(self):
        """kubectl patch returning ChaosBlade JSON → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc3", "patch",
            "deployment xxx -p '{}'",
            json.dumps({"code": 200, "success": True, "result": "uid-fake"}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_exec_without_blade_rejected(self):
        """kubectl exec without blade command → False."""
        msgs = _make_kubectl_tool_call_pair(
            "tc4", "exec",
            "my-pod -- top -bn1",
            json.dumps({"code": 200, "success": True, "result": "uid-fake"}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_failure_json(self):
        msgs = _make_kubectl_tool_call_pair(
            "tc5", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 500, "success": False, "error": "failed"}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_kubectl_non_json_content(self):
        msgs = _make_kubectl_tool_call_pair(
            "tc6", "exec",
            "my-pod -- top -bn1",
            "pod list output",
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_blade_create_msg_ignored(self):
        msg = ToolMessage(
            content=json.dumps({"code": 200, "success": True, "result": "uid-abc"}),
            name="blade_create",
            tool_call_id="tc1",
        )
        assert _was_kubectl_blade_injection_successful([msg]) is False

    def test_empty_messages(self):
        assert _was_kubectl_blade_injection_successful([]) is False

    def test_no_result_field(self):
        msgs = _make_kubectl_tool_call_pair(
            "tc7", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True}),
        )
        assert _was_kubectl_blade_injection_successful(msgs) is False

    def test_missing_tool_call_id_fallback(self):
        """Empty tool_call_id → legacy fallback (True)."""
        msg = ToolMessage(
            content=json.dumps({"code": 200, "success": True, "result": "uid-abc123"}),
            name="kubectl",
            tool_call_id="",
        )
        assert _was_kubectl_blade_injection_successful([msg]) is True


# ---------------------------------------------------------------------------
# _was_blade_create_attempted (shared module)
# ---------------------------------------------------------------------------

class TestWasBladeCreateAttemptedRecover:
    def test_no_blade_create_no_kubectl(self):
        assert _was_blade_create_attempted([]) is False

    def test_blade_create_present_no_kubectl_success(self):
        msg = ToolMessage(
            content="some output",
            name="blade_create",
            tool_call_id="tc1",
        )
        assert _was_blade_create_attempted([msg]) is True

    def test_kubectl_exec_success_overrides_blade_create(self):
        """If kubectl exec blade injection succeeded, blade_create is NOT 'attempted and failed'."""
        msg1 = ToolMessage(
            content="blade create output",
            name="blade_create",
            tool_call_id="tc1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc2", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-123"}),
        )
        assert _was_blade_create_attempted([msg1] + kubectl_msgs) is False

    def test_kubectl_get_does_not_override_blade_create(self):
        """kubectl get returning ChaosBlade JSON does NOT override blade_create."""
        msg1 = ToolMessage(
            content="blade create output",
            name="blade_create",
            tool_call_id="tc1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default -o json",
            json.dumps({"code": 200, "success": True, "result": "uid-fake"}),
        )
        # kubectl get is NOT a blade injection, blade_create still counts as "attempted"
        assert _was_blade_create_attempted([msg1] + kubectl_msgs) is True

    def test_kubectl_failure_does_not_override(self):
        """If kubectl exec failed, blade_create is still 'attempted and failed'."""
        msg1 = ToolMessage(
            content="blade create output",
            name="blade_create",
            tool_call_id="tc1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc2", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 500, "success": False, "error": "fail"}),
        )
        assert _was_blade_create_attempted([msg1] + kubectl_msgs) is True


# ---------------------------------------------------------------------------
# _run_recover_layer1 (no degradation logic — kubectl exec handled by routing)
# ---------------------------------------------------------------------------

class TestRunRecoverLayer1KubectlRouting:
    @pytest.mark.asyncio
    async def test_blade_destroy_failed_stays_failed(self):
        """When blade_destroy fails (regardless of kubectl injection),
        Layer 1 stays 'failed' — kubectl exec routing happens before _run_recover_layer1."""
        from chaos_agent.agent.nodes.recover_verifier import _run_recover_layer1

        destroy_output = json.dumps({
            "code": 500, "success": False,
            "error": "record not found, if it's k8s experiment, please add --target k8s flag to retry"
        })

        with patch("chaos_agent.tools.blade.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(return_value=destroy_output)

            result = await _run_recover_layer1(
                "uid-k8s-abc", "/path/to/kubeconfig", messages=[]
            )
            assert result.status == "failed"
            assert result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_destroy_succeeds_normally(self):
        """Normal blade_destroy succeeds."""
        from chaos_agent.agent.nodes.recover_verifier import _run_recover_layer1

        destroy_output = json.dumps({"code": 200, "success": True, "result": "uid-abc"})

        with patch("chaos_agent.tools.blade.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(return_value=destroy_output)
            with patch("chaos_agent.tools.blade.blade_status") as mock_status:
                mock_status.ainvoke = AsyncMock(return_value=json.dumps({"code": 406, "success": False}))

                result = await _run_recover_layer1(
                    "uid-abc", "/path/to/kubeconfig", messages=[]
                )
                assert result.status == "passed"

    @pytest.mark.asyncio
    async def test_blade_tools_exception_stays_error(self):
        """When blade tools throw exceptions, stays 'error'."""
        from chaos_agent.agent.nodes.recover_verifier import _run_recover_layer1

        with patch("chaos_agent.tools.blade.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(side_effect=Exception("blade binary not found"))

            result = await _run_recover_layer1(
                "uid-abc", "/path/to/kubeconfig", messages=[]
            )
            assert result.status == "error"
            assert result.is_terminal()


# ---------------------------------------------------------------------------
# _build_recover_verifier_prompt (no layer1_skipped_kubectl parameter)
# ---------------------------------------------------------------------------

class TestBuildRecoverVerifierPromptSignature:
    def test_chaosblade_prompt(self):
        prompt = _build_recover_verifier_prompt(is_chaosblade=True)
        assert "blade_destroy" in prompt

    def test_non_chaosblade_prompt(self):
        prompt = _build_recover_verifier_prompt(is_chaosblade=False)
        assert "recovery execution" in prompt

    def test_prompt_contains_format_constraint(self):
        """Layer 2 prompt must warn against using RECOVERY_EXECUTION_RESULT format."""
        prompt = _build_recover_verifier_prompt(is_chaosblade=False)
        assert "RECOVERY_EXECUTION_RESULT" in prompt
        assert "RECOVERY_VERIFICATION_RESULT" in prompt
        assert "VERIFICATION phase" in prompt


# ---------------------------------------------------------------------------
# recover_verifier (no LLM) kubectl exec degradation warning
# ---------------------------------------------------------------------------

class TestRecoverVerifierNoLLMKubectlRouting:
    @pytest.mark.asyncio
    async def test_kubectl_exec_injection_skips_blade_destroy(self):
        """When injection was via kubectl exec, simple verifier skips blade_destroy
        and shows kubectl exec-specific warning."""
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state = {
            "task_id": "t1",
            "blade_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "messages": kubectl_msgs,
        }
        result = await recover_verifier(state)
        assert result["result"]["recovered"] is False
        assert result["recover_verification"]["layer1"]["status"] == "skipped"
        warnings = result["recover_verification"]["warnings"]
        # Should contain kubectl exec-specific warning, NOT "Non-ChaosBlade"
        assert any("kubectl exec" in w for w in warnings)
        assert not any("Non-ChaosBlade" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_non_chaosblade_warning_unchanged(self):
        """Non-ChaosBlade fault (no blade_uid) should still show the old warning."""
        state = {
            "task_id": "t1",
            "blade_uid": "",
            "skill_name": "pod-terminating",
            "kubeconfig": "",
            "messages": [],
        }
        result = await recover_verifier(state)
        assert result["result"]["recovered"] is False
        warnings = result["recover_verification"]["warnings"]
        assert any("Non-ChaosBlade" in w for w in warnings)

    @pytest.mark.asyncio
    async def test_normal_chaosblade_still_uses_blade_destroy(self):
        """Normal ChaosBlade injection (host blade_create) still calls _run_recover_layer1."""
        with patch("chaos_agent.agent.nodes._recover_verifier_loop._run_recover_layer1") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "blade_uid": "uid-host-abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "messages": [],
            }
            result = await recover_verifier(state)
            mock_l1.assert_called_once()
            assert result["result"]["recovered"] is True


# ---------------------------------------------------------------------------
# make_recover_verifier with LLM: kubectl exec injection → non-ChaosBlade Layer 1
# ---------------------------------------------------------------------------

class TestMakeRecoverVerifierKubectlExecRouting:
    @pytest.mark.asyncio
    async def test_kubectl_exec_routes_to_non_chaosblade_layer1(self):
        """When injection was via kubectl exec, the LLM version routes to
        the non-ChaosBlade Layer 1 flow (LLM-driven recovery via kubectl tools)
        instead of calling _run_recover_layer1."""
        # Layer 1 LLM response (no tool calls, final text)
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: destroyed ChaosBlade experiment via kubectl exec\n"
            "- Details: blade destroy uid-k8s-abc succeeded"
        )
        mock_l1_response.tool_calls = []

        # Layer 2 LLM response
        mock_l2_response = MagicMock()
        mock_l2_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed - destroyed via kubectl exec\n"
            "- Layer2 (fault-specific): passed - CPU normal\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_l2_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_l1_response, mock_l2_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        # Provide kubectl exec blade create AIMessage+ToolMessage pair
        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state1 = {
            "task_id": "t1",
            "blade_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["test-pod"]},
            "inject_context": "Injected CPU stress via ChaosBlade kubectl exec",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        result1 = await node(state1)
        # Layer 1 should transition to Layer 2
        assert result1.get("recover_phase") == "layer2_verification"
        assert result1.get("recover_layer1_type") == "llm_driven"
        # _run_recover_layer1 should NOT have been called
        # (the LLM was called directly for Layer 1 recovery)

        # Iteration 2: Layer 2 verification (first iteration must execute tool calls)
        # Need three LLM calls: Layer 1, Layer 2 tool call, Layer 2 conclusion
        mock_l2_tool_response = MagicMock()
        mock_l2_tool_response.content = ""
        mock_l2_tool_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default", "kubeconfig": "/path/to/kubeconfig"}}]

        mock_l2_final_response = MagicMock()
        mock_l2_final_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed - destroyed via kubectl exec\n"
            "- Layer2 (fault-specific): passed - CPU normal\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_l2_final_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_l1_response, mock_l2_tool_response, mock_l2_final_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        # Provide kubectl exec blade create AIMessage+ToolMessage pair
        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state1 = {
            "task_id": "t1",
            "blade_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["test-pod"]},
            "inject_context": "Injected CPU stress via ChaosBlade kubectl exec",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        result1 = await node(state1)
        # Layer 1 should transition to Layer 2
        assert result1.get("recover_phase") == "layer2_verification"
        assert result1.get("recover_layer1_type") == "llm_driven"

        # Iteration 2: Layer 2 first iteration — LLM executes verification command
        state2 = {
            **state1,
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "messages": result1.get("messages", []),
            "layer2_context_added": False,
        }
        result2 = await node(state2)
        assert "result" not in result2, "Tool call iteration should not produce final result"

        # Iteration 3: Layer 2 outputs conclusion
        tool_msg = MagicMock()
        tool_msg.name = "kubectl"
        tool_msg.content = "Running"
        state3 = {
            **state1,
            "verifier_loop_count": 2,
            "recover_phase": "layer2_verification",
            "layer2_context_added": True,
            "messages": result2.get("messages", []) + [tool_msg],
            "recover_layer1_cache": result2.get("recover_layer1_cache"),
            "layer1_iteration_count": 1,
        }
        result3 = await node(state3)
        assert result3["result"]["recovered"] is True
        assert result3["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_kubectl_exec_inject_context_contains_destroy_instructions(self):
        """When kubectl exec injection is detected, the inject_context passed to
        the Layer 1 LLM should contain blade destroy instructions."""
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: destroyed experiment\n"
            "- Details: ok"
        )
        mock_l1_response.tool_calls = []

        mock_l2_response = MagicMock()
        mock_l2_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed\n"
            "- Layer2 (fault-specific): passed\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        mock_l2_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_l1_response, mock_l2_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state = {
            "task_id": "t1",
            "blade_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["test-pod"]},
            "inject_context": "Injected CPU stress via ChaosBlade kubectl exec",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        result = await node(state)

        # The LLM's first call should contain blade destroy instructions in inject context
        first_call_args = mock_llm.ainvoke.call_args_list[0]
        messages_arg = first_call_args[0][0]
        # Find any message that contains blade destroy instructions
        all_content = " ".join(getattr(m, "content", "") for m in messages_arg if hasattr(m, "content"))
        assert "uid-k8s-abc" in all_content
        assert "blade destroy" in all_content

    @pytest.mark.asyncio
    async def test_kubectl_exec_layer2_uses_llm_driven_prompt(self):
        """When Layer 1 is llm_driven (kubectl exec injection), Layer 2 should
        use the non-ChaosBlade prompt (is_chaosblade=False).
        
        Layer 2 first iteration must execute verification commands (programmatic guard).
        We check the prompt content on the tool_call iteration.
        """
        # Layer 1 response
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: destroyed experiment\n"
            "- Details: ok"
        )
        mock_l1_response.tool_calls = []

        # Layer 2 first iteration: executes verification command
        mock_l2_tool_response = MagicMock()
        mock_l2_tool_response.content = ""
        mock_l2_tool_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default", "kubeconfig": "/path/to/kubeconfig"}}]

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=[mock_l1_response, mock_l2_tool_response])
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state1 = {
            "task_id": "t1",
            "blade_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["test-pod"]},
            "inject_context": "Injected CPU stress via ChaosBlade kubectl exec",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        result1 = await node(state1)

        # Now simulate Layer 2 first iteration (tool call)
        state2 = {
            **state1,
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "messages": result1.get("messages", []),
            "layer2_context_added": False,
            "recover_layer1_type": "llm_driven",
        }
        result2 = await node(state2)

        # Check that Layer 2 prompt uses "recovery execution" (non-ChaosBlade)
        second_call_args = mock_llm.ainvoke.call_args_list[1]
        system_msg = second_call_args[0][0][0]
        assert "recovery execution" in system_msg.content


# ---------------------------------------------------------------------------
# _parse_recovery_checklist_items
# ---------------------------------------------------------------------------

class TestParseRecoveryChecklistItems:
    def test_checklist_with_all_passed(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [passed] Disk usage below 85%\n"
            "3. [passed] No evicted pods\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
        )
        items = _parse_recovery_checklist_items(text)
        assert len(items) == 3
        assert items[0] == {"step": 1, "status": "passed"}
        assert items[1] == {"step": 2, "status": "passed"}
        assert items[2] == {"step": 3, "status": "passed"}

    def test_checklist_with_skipped(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [skipped] Ingress check (no Ingress configured)\n"
            "3. [passed] No evicted pods\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
        )
        items = _parse_recovery_checklist_items(text)
        assert len(items) == 3
        assert items[1]["status"] == "skipped"

    def test_checklist_with_partial(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [partial] Disk usage at 82% (below 85% but above baseline)\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: partial\n"
        )
        items = _parse_recovery_checklist_items(text)
        assert len(items) == 2
        assert items[1]["status"] == "partial"

    def test_checklist_with_failed(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [failed] DiskPressure still True\n"
            "2. [skipped] Ingress check\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: failed\n"
        )
        items = _parse_recovery_checklist_items(text)
        assert len(items) == 2
        assert items[0]["status"] == "failed"
        assert items[1]["status"] == "skipped"

    def test_no_checklist_section(self):
        text = "No checklist here, just plain text"
        items = _parse_recovery_checklist_items(text)
        assert items == []

    def test_checklist_without_explicit_section_header(self):
        """Checklist items without RECOVERY_VERIFICATION_CHECKLIST header should still parse."""
        text = (
            "1. [passed] DiskPressure=False\n"
            "2. [passed] Disk usage normal\n"
        )
        items = _parse_recovery_checklist_items(text)
        assert len(items) == 2


# ---------------------------------------------------------------------------
# _has_recovery_checklist
# ---------------------------------------------------------------------------

class TestHasRecoveryChecklist:
    def test_with_section_header(self):
        assert _has_recovery_checklist("RECOVERY_VERIFICATION_CHECKLIST:\n1. [passed] ok")

    def test_with_checklist_items(self):
        assert _has_recovery_checklist("1. [passed] something verified")

    def test_no_checklist(self):
        assert not _has_recovery_checklist("Just some random text without checklist")


# ---------------------------------------------------------------------------
# _count_recovery_steps_in_skill_case
# ---------------------------------------------------------------------------

class TestCountRecoveryStepsInSkillCase:
    def test_numbered_steps(self):
        content = (
            "## 故障注入\n1. Do something\n\n"
            "## 恢复验证\n"
            "1. Check DiskPressure is False\n"
            "2. Verify disk usage below threshold\n"
            "3. Confirm no evicted pods\n\n"
            "## 其他\n"
        )
        assert _count_recovery_steps_in_skill_case(content) == 3

    def test_bullet_list_steps(self):
        content = (
            "## 恢复验证\n"
            "- Check DiskPressure is False\n"
            "- Verify disk usage below threshold\n\n"
        )
        assert _count_recovery_steps_in_skill_case(content) == 2

    def test_no_recovery_section(self):
        content = "## 故障注入\n1. Do something"
        assert _count_recovery_steps_in_skill_case(content) == 0

    def test_single_step(self):
        content = "## 恢复验证\n1. Check pod status is Running"
        assert _count_recovery_steps_in_skill_case(content) == 1


# ---------------------------------------------------------------------------
# _detect_recovery_checklist_inconsistency
# ---------------------------------------------------------------------------

class TestDetectRecoveryChecklistInconsistency:
    def test_no_inconsistency_all_passed(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
        ]
        assert _detect_recovery_checklist_inconsistency(items, "passed") is None

    def test_inconsistency_skipped_but_passed(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "skipped"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None
        assert "inconsistency" in warning.lower()
        assert "auto-downgrading" in warning.lower()

    def test_inconsistency_partial_but_passed(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "partial"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None
        assert "partial" in warning.lower()

    def test_no_inconsistency_when_l2_not_passed(self):
        items = [
            {"step": 1, "status": "skipped"},
        ]
        assert _detect_recovery_checklist_inconsistency(items, "failed") is None
        assert _detect_recovery_checklist_inconsistency(items, "partial") is None

    def test_no_inconsistency_when_no_items(self):
        assert _detect_recovery_checklist_inconsistency([], "passed") is None


# ---------------------------------------------------------------------------
# _parse_recovery_verification_result: partial and checklist integration
# ---------------------------------------------------------------------------

class TestParseRecoveryVerificationResultPartial:
    def test_partial_from_overall(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed\n"
            "- Layer2 (fault-specific): partial - disk usage at 82%\n"
            "- Overall: partial\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "partial"
        assert result["layer2"]["status"] == "partial"

    def test_partial_from_l2_fallback(self):
        text = (
            "- Layer1: passed\n"
            "- Layer2: partial - some indicators not fully verified\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "partial"
        assert result["layer2"]["status"] == "partial"

    def test_checklist_parsed_in_result(self):
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [passed] Disk usage normal\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert "checklist" in result
        assert result["checklist"]["total_count"] == 2
        assert result["checklist"]["skipped_count"] == 0

    def test_auto_downgrade_on_inconsistency(self):
        """When checklist has skipped steps but L2 says passed, auto-downgrade to partial."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [skipped] Ingress check (no Ingress configured)\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        # Should auto-downgrade from passed to partial
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"
        assert any("inconsistency" in w.lower() or "auto-downgrading" in w.lower() for w in result["warnings"])

    def test_no_checklist_warning_for_passed(self):
        """When L2 is passed but no checklist, should warn about completeness."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed - everything looks good\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert any("checklist" in w.lower() for w in result["warnings"])

    def test_no_checklist_warning_for_failed(self):
        """When L2 is failed, no checklist warning needed (failure is already clear)."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: failed - fault still active\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert not any("checklist" in w.lower() for w in result["warnings"])

    def test_partial_with_checklist(self):
        """Partial L2 with checklist should record both."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [partial] Disk usage at 82%\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: partial\n"
            "- Overall: partial\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"
        assert result["checklist"]["partial_count"] == 1
        # No auto-downgrade because L2 already says partial
        assert not any("auto-downgrading" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# _detect_recovery_contradiction
# ---------------------------------------------------------------------------

class TestDetectRecoveryContradiction:
    """Tests for recovery-side contradiction detection function."""

    # --- Text-based contradiction ---

    def test_disk_recovery_evidence(self):
        result = _detect_recovery_contradiction("disk usage back to 16%")
        assert result is not None
        assert "recovery effects" in result.lower()

    def test_cpu_recovery_evidence(self):
        result = _detect_recovery_contradiction("cpu usage normal, back to baseline")
        assert result is not None

    def test_diskpressure_false_evidence(self):
        result = _detect_recovery_contradiction("diskpressure is false on node")
        assert result is not None

    def test_network_recovery_evidence(self):
        result = _detect_recovery_contradiction("connectivity restored, no packet loss")
        assert result is not None

    def test_process_recovery_evidence(self):
        result = _detect_recovery_contradiction("pod is running with no restarts")
        assert result is not None

    def test_absence_phrase_blocks(self):
        """Absence phrase 'still elevated' blocks contradiction even with recovery indicator."""
        result = _detect_recovery_contradiction("disk usage back to normal but still elevated")
        assert result is None

    def test_high_percentage_blocks(self):
        """High percentage 'at 95%' blocks contradiction."""
        result = _detect_recovery_contradiction("cpu usage at 95%")
        assert result is None

    def test_diskpressure_true_blocks(self):
        result = _detect_recovery_contradiction("diskpressure is true, disk usage normal")
        assert result is None

    def test_no_indicators(self):
        result = _detect_recovery_contradiction("verification could not complete")
        assert result is None

    def test_empty_details(self):
        result = _detect_recovery_contradiction("")
        assert result is None

    # --- Checklist-based contradiction ---

    def test_all_checklist_passed(self):
        """ALL checklist passed but L2 says failed — structural contradiction."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
            {"step": 3, "status": "passed"},
        ]
        result = _detect_recovery_contradiction("some details", items)
        assert result is not None
        assert "ALL checklist" in result

    def test_all_checklist_passed_no_details(self):
        """ALL checklist passed with no details — still a contradiction."""
        items = [{"step": 1, "status": "passed"}]
        result = _detect_recovery_contradiction("", items)
        assert result is not None
        assert "ALL checklist" in result

    def test_all_checklist_passed_with_absence_blocked(self):
        """ALL checklist passed but details have absence phrase — not a contradiction."""
        items = [{"step": 1, "status": "passed"}]
        result = _detect_recovery_contradiction("cpu still at 95%", items)
        assert result is None

    def test_mixed_checklist_no_contradiction(self):
        """Mixed checklist (passed + failed) — failed item justifies L2=failed."""
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
        ]
        result = _detect_recovery_contradiction("some details", items)
        assert result is None

    # --- No trigger ---

    def test_no_details_no_checklist(self):
        result = _detect_recovery_contradiction("", None)
        assert result is None


# ---------------------------------------------------------------------------
# _detect_primary_evidence_generic_contradiction
# ---------------------------------------------------------------------------

class TestDetectPrimaryEvidenceGenericContradiction:
    """Tests for P2-1: PrimaryEvidenceObserved=true but evidence is all generic."""

    # --- Trigger cases: PrimaryEvidenceObserved=true + generic evidence only ---

    def test_generic_pod_running(self):
        """PrimaryEvidenceObserved=true + 'pod running' = contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "pod is running, no errors"
        )
        assert result is not None
        assert "generic" in result.lower()

    def test_generic_no_restarts(self):
        """PrimaryEvidenceObserved=true + 'no new restarts' = contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "no new restarts, deployment available"
        )
        assert result is not None

    def test_generic_healthy(self):
        """PrimaryEvidenceObserved=true + 'healthy, pods ready' = contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "healthy, pods ready, 1/1"
        )
        assert result is not None

    def test_generic_node_ready(self):
        """PrimaryEvidenceObserved=true + 'node ready' = contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "node ready, not evicted"
        )
        assert result is not None

    # --- No trigger cases: PrimaryEvidenceObserved=false ---

    def test_primary_observed_false_skips(self):
        """PrimaryEvidenceObserved=false → never triggers (already handled elsewhere)."""
        result = _detect_primary_evidence_generic_contradiction(
            False, "pod is running, no errors"
        )
        assert result is None

    # --- No trigger cases: fault-specific evidence present ---

    def test_cpu_evidence_not_generic(self):
        """Fault-specific CPU evidence → no contradiction even if generic also present."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "cpu usage back to baseline, pod running"
        )
        assert result is None

    def test_disk_evidence_not_generic(self):
        """Fault-specific disk evidence → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "disk usage normal, diskpressure=false"
        )
        assert result is None

    def test_memory_evidence_not_generic(self):
        """Fault-specific memory evidence → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "memory usage returned to normal, pod running"
        )
        assert result is None

    def test_network_evidence_not_generic(self):
        """Fault-specific network evidence → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "latency back to normal, no packet loss"
        )
        assert result is None

    def test_io_evidence_not_generic(self):
        """Fault-specific I/O evidence → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "iowait reduced, /proc/diskstats normal"
        )
        assert result is None

    # --- No trigger cases: pod-kill skills (pod running IS primary evidence) ---

    def test_pod_kill_skill_exempt(self):
        """pod-kill skill: 'pod running' IS primary evidence → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "pod is running, no restarts", skill_name="pod-kill"
        )
        assert result is None

    def test_pod_terminating_skill_exempt(self):
        """pod-terminating skill: generic evidence is primary → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "pod running, healthy", skill_name="pod-terminating"
        )
        assert result is None

    def test_pod_delete_skill_exempt(self):
        """pod-delete skill: generic evidence is primary → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "pod running, ready", skill_name="pod-delete"
        )
        assert result is None

    def test_cpu_stress_skill_not_exempt(self):
        """cpu-stress skill: generic evidence triggers contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "pod running, no restarts", skill_name="cpu-stress"
        )
        assert result is not None

    # --- No trigger cases: ambiguous evidence ---

    def test_ambiguous_evidence_no_false_positive(self):
        """No generic indicators AND no fault-specific → skip (avoid false positives)."""
        result = _detect_primary_evidence_generic_contradiction(
            True, "verification completed"
        )
        assert result is None

    def test_empty_details_no_trigger(self):
        """Empty L2 details → no contradiction."""
        result = _detect_primary_evidence_generic_contradiction(
            True, ""
        )
        assert result is None


# ---------------------------------------------------------------------------
# P2-1 integration: PrimaryEvidenceObserved=true + generic → auto-downgrade
# ---------------------------------------------------------------------------

class TestPrimaryEvidenceGenericIntegration:
    """Integration tests: PrimaryEvidenceObserved=true + generic evidence
    triggers downgrade from recovered to partial via _parse_recovery_verification_result."""

    def test_generic_evidence_downgrade_to_partial(self):
        """PrimaryEvidenceObserved=true + generic evidence → level=partial, not recovered."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - pod is running, no new restarts\n"
            "- PrimaryEvidenceObserved: true\n"
            "- BaselineUsed: false\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text, skill_name="cpu-stress")
        assert result["level"] == "partial"
        assert any("generic" in w.lower() for w in result["warnings"])

    def test_fault_specific_evidence_no_downgrade(self):
        """PrimaryEvidenceObserved=true + fault-specific evidence → stays recovered."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - CPU usage back to baseline\n"
            "- PrimaryEvidenceObserved: true\n"
            "- BaselineUsed: true\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text, skill_name="cpu-stress")
        assert result["level"] == "recovered"
        assert not any("generic" in w.lower() for w in result["warnings"])

    def test_pod_kill_no_downgrade(self):
        """pod-kill skill: PrimaryEvidenceObserved=true + generic → stays recovered."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - pod running, no restarts\n"
            "- PrimaryEvidenceObserved: true\n"
            "- BaselineUsed: false\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text, skill_name="pod-kill")
        assert result["level"] == "recovered"

    def test_primary_false_still_downgraded_separately(self):
        """PrimaryEvidenceObserved=false → separate downgrade (not by P2-1)."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - pod is running\n"
            "- PrimaryEvidenceObserved: false\n"
            "- BaselineUsed: false\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text, skill_name="cpu-stress")
        # Downgraded by existing PrimaryEvidenceObserved=false check, NOT by P2-1
        assert result["level"] == "partial"
        assert any("incompatible" in w.lower() for w in result["warnings"])
        assert not any("generic" in w.lower() for w in result["warnings"])

    def test_no_skill_name_defaults_to_checking(self):
        """No skill_name → generic contradiction still detected (pod-kill exemption not applied)."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): passed - pod running, healthy\n"
            "- PrimaryEvidenceObserved: true\n"
            "- Overall: recovered\n"
            "- Warnings: none"
        )
        result = _parse_recovery_verification_result(text)
        # Without skill_name="pod-kill", generic evidence triggers downgrade
        assert result["level"] == "partial"


# ---------------------------------------------------------------------------
# _parse_recovery_verification_result: contradiction detection integration
# ---------------------------------------------------------------------------

class TestRecoveryContradictionIntegration:
    """Integration tests for contradiction detection in _parse_recovery_verification_result."""

    def test_text_contradiction_overrides_l2(self):
        """L2=failed with recovery evidence in details → L2 overridden to partial."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - disk usage back to 16%\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"
        assert any("contradiction" in w.lower() for w in result["warnings"])

    def test_checklist_contradiction_overrides_l2(self):
        """L2=failed but ALL checklist passed → L2 overridden to partial."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [passed] No evicted pods\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - verification incomplete\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"
        assert any("contradiction" in w.lower() for w in result["warnings"])

    def test_absence_phrase_prevents_override(self):
        """L2=failed with absence phrase → no contradiction override."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - cpu still at 95%\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"
        assert not any("contradiction" in w.lower() for w in result["warnings"])

    def test_l2_passed_no_contradiction(self):
        """L2=passed → no contradiction detection (wrong trigger condition)."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - disk usage normal\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "passed"
        assert not any("contradiction" in w.lower() for w in result["warnings"])

    def test_contradiction_after_auto_downgrade_skipped(self):
        """L2 already downgraded to 'partial' → contradiction detection skipped."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [skipped] Ingress check\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed - core indicators recovered\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        # Auto-downgrade from checklist inconsistency first
        assert result["layer2"]["status"] == "partial"
        # No additional contradiction warning (L2 is not "failed")
        assert not any("contradiction" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# Gap A: L2 negation handling via _parse_status_keyword
# ---------------------------------------------------------------------------

class TestRecoveryL2Negation:
    """Tests for negation-aware L2 status parsing in recovery verifier."""

    def test_not_passed_treated_as_failed(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: not passed - some indicators not confirmed\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"

    def test_not_failed_treated_as_passed(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: not failed - all indicators recovered\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "passed"

    def test_not_skipped_treated_as_failed(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: not skipped - verification performed\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "failed"


# ---------------------------------------------------------------------------
# Gap B: Overall negation handling
# ---------------------------------------------------------------------------

class TestRecoveryOverallNegation:
    """Tests for negation-aware Overall parsing in recovery verifier."""

    def test_not_recovered_treated_as_unrecovered(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - fault still present\n"
            "- Overall: not recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "unrecovered"

    def test_unrecovered_still_works(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: failed - fault still present\n"
            "- Overall: unrecovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "unrecovered"

    def test_recovered_without_negation(self):
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: passed - all clear\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "recovered"

    def test_partial_recovered_treated_as_partial(self):
        """'partially recovered' contains both 'recovered' and 'partial'."""
        text = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1: passed\n"
            "- Layer2: partial - some indicators improving\n"
            "- Overall: partially recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["level"] == "partial"


# ---------------------------------------------------------------------------
# Gap C: Checklist inconsistency detection includes "failed" items
# ---------------------------------------------------------------------------

class TestRecoveryChecklistInconsistencyWithFailed:
    """Tests for checklist inconsistency when items are 'failed'."""

    def test_failed_item_triggers_inconsistency(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "failed"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None
        assert "failed" in warning.lower()

    def test_all_passed_no_inconsistency(self):
        items = [
            {"step": 1, "status": "passed"},
            {"step": 2, "status": "passed"},
        ]
        assert _detect_recovery_checklist_inconsistency(items, "passed") is None

    def test_failed_item_auto_downgrade_in_parse(self):
        """Integration: failed checklist item + L2=passed → auto-downgrade."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [passed] DiskPressure=False\n"
            "2. [failed] Disk usage still at 92%\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed - mostly recovered\n"
            "- Overall: recovered\n"
        )
        result = _parse_recovery_verification_result(text)
        assert result["layer2"]["status"] == "partial"
        assert result["level"] == "partial"
        assert any("inconsistency" in w.lower() for w in result["warnings"])


# ---------------------------------------------------------------------------
# TestRecoveryExpectedStatus
# ---------------------------------------------------------------------------

class TestRecoveryExpectedStatus:
    """Tests for 'expected' status in recovery checklist parsing and inconsistency detection."""

    def test_parse_expected_in_step_format(self):
        """'expected' status is parsed from Step N: expected format in recovery checklist."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "Step 1: expected — DiskPressure=False is anticipated after recovery\n"
            "Step 2: passed — disk usage confirmed normal\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: recovered\n"
        )
        items = _parse_recovery_checklist_items(text)
        expected_items = [i for i in items if i["status"] == "expected"]
        assert len(expected_items) == 1
        assert expected_items[0]["step"] == 1

    def test_parse_expected_in_bare_numbered_format(self):
        """'expected' status is parsed from bare numbered list format in recovery."""
        text = (
            "RECOVERY_VERIFICATION_CHECKLIST:\n"
            "1. [expected] DiskPressure=False anticipated\n"
            "2. [passed] disk usage normal\n\n"
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer2: passed\n"
            "- Overall: recovered\n"
        )
        items = _parse_recovery_checklist_items(text)
        expected_items = [i for i in items if i["status"] == "expected"]
        assert len(expected_items) == 1

    def test_expected_does_not_trigger_recovery_inconsistency(self):
        """'expected' status items should NOT trigger recovery inconsistency detection."""
        items = [
            {"step": 1, "status": "expected", "evidence": "DiskPressure=False anticipated"},
            {"step": 2, "status": "passed", "evidence": "disk usage normal"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is None

    def test_expected_with_failed_still_triggers_recovery_inconsistency(self):
        """Mixed expected + failed items: failed should still trigger recovery inconsistency."""
        items = [
            {"step": 1, "status": "expected", "evidence": "DiskPressure=False anticipated"},
            {"step": 2, "status": "failed", "evidence": "disk usage still at 92%"},
        ]
        warning = _detect_recovery_checklist_inconsistency(items, "passed")
        assert warning is not None
        assert "failed" in warning.lower()


# ---------------------------------------------------------------------------
# _extract_recovery_verification_section
# ---------------------------------------------------------------------------

class TestExtractRecoveryVerificationSection:
    """Tests for P1-4: skill case smart extraction — 恢复验证 section extraction."""

    def test_no_recovery_section(self):
        """Skill case without 恢复验证 returns empty string."""
        content = "**注入验证**：\n1. kubectl top node\n2. kubectl describe node\n**注入说明**：\nSome injection notes"
        result = _extract_recovery_verification_section(content)
        assert result == ""

    def test_simple_recovery_section(self):
        """Extract 恢复验证 section with clear end boundary."""
        content = (
            "**注入验证**：\n"
            "1. kubectl top node\n"
            "2. kubectl describe node\n\n"
            "**恢复验证**：\n"
            "1. kubectl top node — confirm CPU back to baseline\n"
            "2. kubectl describe node — confirm conditions normal\n\n"
            "**恢复说明**：\n"
            "Use blade destroy to recover\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "**恢复验证**：" in result
        assert "kubectl top node" in result
        assert "confirm CPU back to baseline" in result
        assert "**恢复说明**：" not in result

    def test_recovery_section_at_end_of_file(self):
        """恢复验证 is the last section — no next heading to delimit."""
        content = (
            "**注入验证**：\n"
            "1. kubectl top node\n\n"
            "**恢复验证**：\n"
            "1. kubectl top node — CPU should be normal\n"
            "2. kubectl describe node — conditions should be false\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "CPU should be normal" in result
        assert "conditions should be false" in result

    def test_cross_reference_tong_inject_verification(self):
        """恢复验证 references '同注入验证' — inject section should be appended."""
        content = (
            "**注入验证**：\n"
            "1. kubectl top node — CPU usage should exceed 80%\n"
            "2. kubectl describe node — MemoryPressure should be True\n\n"
            "**恢复验证**：\n"
            "同注入验证，确认指标恢复到正常水平\n\n"
            "**恢复说明**：\n"
            "blade destroy\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "同注入验证" in result
        assert "**注入验证参考**" in result
        assert "CPU usage should exceed 80%" in result

    def test_cross_reference_pod_level_method(self):
        """恢复验证 references 'Pod 级验证方法中的' — inject section appended."""
        content = (
            "**注入验证**：\n"
            "1. Pod-level: curl endpoint\n\n"
            "**恢复验证**：\n"
            "Pod 级验证方法中的步骤，确认连通性恢复\n\n"
            "**恢复说明**：\n"
            "Remove network policy\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "**注入验证参考**" in result
        assert "curl endpoint" in result

    def test_cross_reference_inject_verification_within(self):
        """恢复验证 references '注入验证中的' — inject section appended."""
        content = (
            "**注入验证**：\n"
            "1. df -h should show disk increase\n\n"
            "**恢复验证**：\n"
            "注入验证中的df命令，确认磁盘使用率恢复\n\n"
            "**恢复说明**：\n"
            "blade destroy\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "**注入验证参考**" in result
        assert "df -h" in result

    def test_no_cross_reference(self):
        """恢复验证 has no cross-reference — only recovery section returned."""
        content = (
            "**恢复验证**：\n"
            "1. kubectl top node\n"
            "2. kubectl describe node\n\n"
            "**恢复说明**：\n"
            "blade destroy\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "**注入验证参考**" not in result
        assert "kubectl top node" in result
        assert "**恢复说明**：" not in result

    def test_empty_recovery_section(self):
        """恢复验证 heading exists but no content beneath it."""
        content = (
            "**恢复验证**：\n\n"
            "**恢复说明**：\n"
            "blade destroy\n"
        )
        result = _extract_recovery_verification_section(content)
        assert "**恢复验证**：" in result

    def test_colon_vs_fullwidth_colon(self):
        """恢复验证 delimiter works with both ：(fullwidth) and :(halfwidth)."""
        content_halfwidth = "**恢复验证**:\n1. kubectl top node\n"
        result = _extract_recovery_verification_section(content_halfwidth)
        assert "kubectl top node" in result


# ---------------------------------------------------------------------------
# build_recover_verifier_system_prompt (U-shaped architecture)
# ---------------------------------------------------------------------------

class TestBuildRecoverVerifierSystemPrompt:
    """Tests for P0-1: U-shaped prompt composition from sections/recovery.py."""

    def test_u_shape_primacy_zone(self):
        """Critical rules appear at the BEGINNING of the prompt (primacy effect)."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        # CRITICAL RULES must appear before the middle-zone sections
        # (knowledge summary, tools, skill priority, kubeconfig)
        rules_pos = prompt.index("CRITICAL RULES")
        knowledge_pos = prompt.index("Domain Knowledge")
        assert rules_pos < knowledge_pos
        assert "Execute kubectl" in prompt[:500]

    def test_u_shape_recency_zone(self):
        """Critical rules reminder appears at the END of the prompt (recency effect)."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        # REMINDER section must appear AFTER all middle-zone sections
        reminder_pos = prompt.index("REMINDER")
        output_format_pos = prompt.index("RECOVERY_VERIFICATION_RESULT")
        assert reminder_pos > output_format_pos
        # Tail of prompt must contain the 3-rule recap
        assert "Critical Rules Recap" in prompt[-500:]
        assert "CURRENT (post-recovery)" in prompt[-300:]

    def test_chaosblade_label(self):
        """is_chaosblade=True → Layer1 label is 'blade_destroy'."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        assert "blade_destroy" in prompt

    def test_non_chaosblade_label(self):
        """is_chaosblade=False → Layer1 label is 'recovery execution'."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=False)
        assert "recovery execution" in prompt

    def test_all_sections_present(self):
        """All 8 section functions are composed in the prompt."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        assert "VERIFICATION phase" in prompt
        assert "CRITICAL RULES" in prompt
        assert "Domain Knowledge" in prompt or "Knowledge" in prompt
        assert "kubectl" in prompt
        assert "Skill Use-Case Priority" in prompt
        assert "Kubeconfig Requirement" in prompt
        assert "RECOVERY_VERIFICATION_RESULT" in prompt
        assert "REMINDER" in prompt

    def test_baseline_integrity_compact(self):
        """Compact baseline integrity rules (4 rules + 1 example) are present."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        assert "Baseline Comparison Rules" in prompt
        assert "SAME resource only" in prompt
        assert "imagefs /dev/vdb" in prompt

    def test_no_full_baseline_integrity(self):
        """Full BASELINE_INTEGRITY_PROMPT content should NOT appear in the compact version."""
        prompt = build_recover_verifier_system_prompt(is_chaosblade=True)
        # The compact version has 4 concise rules; the full version has verbose
        # "FORMAT REQUIREMENT" text — ensure compact doesn't include it
        assert "FORMAT REQUIREMENT" not in prompt
