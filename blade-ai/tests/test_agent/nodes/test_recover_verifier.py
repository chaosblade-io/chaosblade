"""Tests for recover_verifier node: two-layer post-recovery verification."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

# Phase-13 T2: the blade-success scan's canonical address (the generic-layer
# shim was retired with the detection import; kept under the historical call
# name as the equivalence anchor for the successful-test group). Phase-14 G1:
# the scan's physical address moved from the retired chaosblade/detection.py
# to chaosblade/verify.py.
from chaos_agent.agent.providers.chaosblade.verify import (
    scan_kubectl_blade_success as _was_kubectl_blade_injection_successful,
)
# Phase-4 T2 canonical address (kept under the historical call name as the
# equivalence anchor for the attempted-test group).
from chaos_agent.agent.providers.chaosblade.verify import (
    was_blade_create_attempted as _was_blade_create_attempted,
)
from chaos_agent.agent.nodes.recover.recover_verifier import (
    RecoverLayer1Result,
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
    recover_verifier,
    make_recover_verifier,
)
# Phase-9: the carrier-owned destroy-output parsers are imported from their
# canonical provider home — the recover_verifier facade no longer re-exports
# carrier symbols (generic-layer direction violation; this test file was the
# sole consumer of those re-exports).
from chaos_agent.agent.providers.chaosblade.recover import (
    parse_blade_destroy_output as _parse_blade_destroy_output,
    parse_blade_status_destroyed as _parse_blade_status_destroyed,
)
# Phase-7 T1: the Layer-1 storage-shape helper lives beside the data class
# (result/verdict.py) — the recover_verifier facade no longer re-exports it.
from chaos_agent.agent.result.verdict import layer1_to_dict as _layer1_to_dict
from chaos_agent.agent.prompts.sections.recovery import (
    build_recover_verifier_system_prompt,
)
from chaos_agent.config.settings import settings


async def _drive_finalize(state: dict, loop_result: dict) -> dict:
    """Scheme B helper: run finalize_recover_verification on a loop result.

    The recover_verifier_loop node no longer finalizes the Layer 2 verdict
    inline — it persists the response and routing hands off to the
    finalize_recover_verification node. This helper mirrors that hand-off so
    tests can assert the final verdict: merge the loop's result_update into
    state (messages append, other keys overwrite), then invoke the finalize
    node.
    """
    from chaos_agent.agent.nodes.recover._recover_finalize import (
        make_finalize_recover_verification,
    )
    merged = {**state, **{k: v for k, v in loop_result.items() if k != "messages"}}
    merged["messages"] = list(state.get("messages", [])) + list(loop_result.get("messages", []))
    fnode = make_finalize_recover_verification()
    return await fnode(merged)


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

    def test_is_in_progress(self):
        assert RecoverLayer1Result(status="in_progress").is_in_progress()
        assert not RecoverLayer1Result(status="passed").is_in_progress()
        assert not RecoverLayer1Result(status="unknown").is_in_progress()
        # IN_PROGRESS is NOT terminal — Layer 1 is still running
        assert not RecoverLayer1Result(status="in_progress").is_terminal()


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
        state = {"task_id": "t1", "experiment_uid": "", "skill_name": "cpu-stress", "kubeconfig": "", "messages": []}
        result = await recover_verifier(state)
        # Non-ChaosBlade fault: Layer 1 skipped, cannot verify without LLM
        assert result["result"]["recovered"] is False
        assert result["recover_verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_successful_recovery(self):
        # No-LLM path now delegates to ChaosbladeProvider.recover, which imports
        # run_layer1_destroy from its canonical module at call time — patch there.
        with patch("chaos_agent.agent.providers.chaosblade.recover.run_layer1_destroy") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success, blade_status confirms: Destroyed",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "experiment_uid": "abc123",
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
        # No-LLM path delegates to ChaosbladeProvider.recover — patch the canonical module.
        with patch("chaos_agent.agent.providers.chaosblade.recover.run_layer1_destroy") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="failed",
                details="blade_destroy failed",
            )
            state = {
                "task_id": "t1",
                "experiment_uid": "abc123",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
            }
            result = await recover_verifier(state)
            assert result["result"]["recovered"] is False
            assert result["recover_verification"]["level"] == "unrecovered"

    @pytest.mark.asyncio
    async def test_uid_from_message_history_routes_destroy(self):
        """Legacy/compacted checkpoint (no durable uid/method facts): the
        experiment UID recovered from the message history must still route
        to the experiment carrier's deterministic destroy (the dispatch's
        message-history claim), never to the UID-less native default —
        preserving the pre-dispatch ``if blade_uid:`` contract."""
        from langchain_core.messages import ToolMessage

        with patch("chaos_agent.agent.providers.chaosblade.recover.run_layer1_destroy") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success, blade_status confirms: Destroyed",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "messages": [
                    ToolMessage(
                        content='{"code":200,"success":true,"result":"a1b2c3d4e5f60719"}',
                        name="blade_create", tool_call_id="c1",
                    ),
                ],
            }
            result = await recover_verifier(state)
            assert mock_l1.await_count == 1
            assert mock_l1.await_args.args[0] == "a1b2c3d4e5f60719"
            assert result["result"]["recovered"] is True


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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="failed",
                details="blade still running",
            )
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "unknown-skill",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            # Loop count increments; loop persists the conclusion + first-Layer2 flag.
            assert result["verifier_loop_count"] == 1
            assert result.get("recover_layer2_first") is True
            # finalize_recover_verification's guard intercepts the first-turn
            # conclusion (no kubectl verification ran) → no final result, inject
            # a rejection prompt forcing the LLM to verify.
            fin = await _drive_finalize(state, result)
            assert "recover_verification" not in fin, "First-turn conclusion must be rejected, not finalized"
            assert any("rejected" in getattr(m, "content", "") for m in fin.get("messages", [])), \
                "Guard should inject a rejection prompt forcing LLM to execute commands"

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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 2,  # Non-first iteration — guard bypassed
                "layer2_context_added": True,  # Layer 2 context already built
                "recover_phase": "layer2_verification",  # Already in Layer 2 phase
            }
            result = await node(state)
            # Non-first iteration → guard bypassed; finalize parses the verdict.
            assert result.get("recover_layer2_first") is False
            fin = await _drive_finalize(state, result)
            assert fin["result"]["recovered"] is True
            assert fin["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_ledger_rides_tail_not_system_head(self):
        """Unit A (context-cache-prefix-stability tasks 2.5/2.7): the recover
        progress ledger rides the message TAIL as an append-only system-reminder
        HumanMessage carrying a supersedes marker — NOT the recover verifier
        system head (whose per-round ledger rewrite broke the cache prefix).

        Node-level complement to test_prefix_stability.py's
        TestRecoverVerifierPrefixStability builder guard: that one proves the
        head is byte-stable across ledger growth; this one proves the ledger
        still reaches the model every round, at the tail, and persists.
        """
        from langchain_core.messages import SystemMessage
        from chaos_agent.agent.progress_ledger import (
            freeze_anchor,
            merge_progress_ledger,
        )

        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": "pods -n default"}}
        ]
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)
        # Production recover inherits the anchor frozen during execute, so the
        # recover ledger is ANCHORED — the tail must render the anchored
        # directive variant ("...MUST serve its immutable ANCHOR..."), NOT the
        # no-anchor one planning uses. Pinning the anchored shape here keeps the
        # test faithful to the real recover path (the old no-anchor fixture
        # silently rendered the other variant while its "before acting"
        # assertion — shared by BOTH variants — still passed).
        ledger = merge_progress_ledger(
            freeze_anchor(
                {"scope": "pod", "fault_target": "cpu", "fault_action": "fullload",
                 "namespace": "default", "names": ["p0"]},
                goal="inject cpu fullload on pod p0",
            ),
            log_append=[{"event": "LEDGER-TAIL-MARK step done", "status": "verified"}],
        )
        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 2,
                "layer2_context_added": True,
                "recover_phase": "layer2_verification",
                "progress_ledger": ledger,
                "messages": [],
            }
            result = await node(state)

        sent = mock_llm.ainvoke.call_args[0][0]
        # The system head must NOT carry the ledger anymore (that per-round
        # rewrite was the volatile byte that broke the recover cache prefix).
        for sm in [m for m in sent if isinstance(m, SystemMessage)]:
            assert "LEDGER-TAIL-MARK" not in (sm.content or "")
            assert "progress ledger below" not in (sm.content or "")
        # The ledger rides the TAIL as a system-reminder HumanMessage.
        tails = [
            m for m in sent
            if isinstance(m, HumanMessage) and "LEDGER-TAIL-MARK" in (m.content or "")
        ]
        assert len(tails) == 1, "exactly one ledger snapshot must ride the tail"
        content = tails[0].content or ""
        assert content.lstrip().startswith("<system-reminder>")
        assert "supersedes" in content      # D2 marker
        assert "before acting" in content    # anti-drift directive survives
        # ...and it is the ANCHORED variant specifically ("immutable ANCHOR" is
        # unique to _LEDGER_DIRECTIVE; the no-anchor variant instead says
        # "do not re-derive what is already established").
        assert "immutable ANCHOR" in content
        assert "do not re-derive" not in content
        # Persisted into LangGraph state (append-only) via result_update.
        persisted = [
            m for m in result.get("messages", [])
            if isinstance(m, HumanMessage) and "LEDGER-TAIL-MARK" in (m.content or "")
        ]
        assert len(persisted) == 1

    @pytest.mark.asyncio
    async def test_no_ledger_tail_when_ledger_empty(self):
        """Empty ledger → the tail message is suppressed (no empty
        system-reminder noise)."""
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": "pods -n default"}}
        ]
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)
        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 2,
                "layer2_context_added": True,
                "recover_phase": "layer2_verification",
                "progress_ledger": None,
                "messages": [],
            }
            result = await node(state)
        sent = mock_llm.ainvoke.call_args[0][0]
        assert not [
            m for m in sent
            if isinstance(m, HumanMessage) and "LEDGER-TAIL-MARK" in (m.content or "")
        ]
        assert not [
            m for m in result.get("messages", [])
            if isinstance(m, HumanMessage) and "LEDGER-TAIL-MARK" in (m.content or "")
        ]

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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "pod-kill",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            assert "result" not in result
            assert "messages" in result
            assert result["verifier_loop_count"] == 1


    @pytest.mark.asyncio
    async def test_b51_convergence_hint_silent_on_first_layer2_after_long_layer1(self):
        """B51 (case #33): Layer 1 ran 7 observation iterations, so the shared
        ``verifier_loop_count`` satisfied the old task-level ``count >= 4``
        gate on Layer 2's FIRST iteration — the convergence hint ("conclude
        now") then collided with the ``recover_layer2_first`` anti-laziness
        guard (first-turn conclusions rejected) in the same turn. The hint
        must be gated on the Layer-2-LOCAL iteration count instead."""
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "node n1"}}]

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 7,  # Layer 1 consumed 7 iterations
                "recover_phase": "layer2_verification",  # first Layer 2 iteration
            }
            result = await node(state)

        assert "result" not in result  # ReAct tool call continues the loop
        assert result.get("recover_layer2_first") is True
        # First Layer 2 iteration pins the counter for later local counting.
        assert result["layer2_start_count"] == 8
        sent = mock_llm.ainvoke.call_args[0][0]
        assert not any("sufficient CURRENT" in getattr(m, "content", "") for m in sent), (
            "convergence hint must NOT fire on the first Layer 2 iteration "
            "even when Layer 1 iterations already pushed the shared count past 4 (B51)"
        )

    @pytest.mark.asyncio
    async def test_b51_convergence_hint_fires_at_layer2_local_iteration_4(self):
        """With the Layer-2 start pinned, the hint fires on the Layer-2-LOCAL
        4th iteration (shared count 11, start 8) — Layer-1 iterations no
        longer accelerate the hint."""
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "node n1"}}]

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 11,  # shared
                "layer2_start_count": 8,    # Layer 2 began at shared count 8
                "layer2_context_added": True,
                "recover_phase": "layer2_verification",  # Layer-2-local = 11-8+1 = 4
            }
            await node(state)

        sent = mock_llm.ainvoke.call_args[0][0]
        assert any("sufficient CURRENT" in getattr(m, "content", "") for m in sent)

    @pytest.mark.asyncio
    async def test_b51_legacy_state_without_start_pin_falls_back_to_shared_count(self):
        """Legacy checkpoints predate ``layer2_start_count``: without the pin
        the local count falls back to the shared counter (pre-B51 behavior
        preserved for count >= 4)."""
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [{"name": "kubectl", "args": {"subcommand": "get", "v_args": "node n1"}}]

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 4,
                "layer2_context_added": True,  # no layer2_start_count (legacy)
                "recover_phase": "layer2_verification",
            }
            await node(state)

        sent = mock_llm.ainvoke.call_args[0][0]
        assert any("sufficient CURRENT" in getattr(m, "content", "") for m in sent)

    def test_b51_pin_is_a_declared_state_channel(self):
        """B51 review finding (2026-09-10): LangGraph silently DROPS
        node-update keys absent from the AgentState schema — an undeclared
        pin never persists across real graph iterations while hand-built
        state unit tests stay green. The pin MUST be declared (and lifecycle
        registered), or the hint gate silently reverts to the shared counter
        after Layer 2's first iteration."""
        from chaos_agent.agent.state import AgentState

        assert "layer2_start_count" in AgentState.__annotations__, (
            "layer2_start_count must be a declared AgentState channel; "
            "undeclared node updates are silently dropped by LangGraph"
        )

    def test_b51_pin_survives_a_stategraph_round_trip(self):
        """The teeth for the drop class of bug: route the pin through a REAL
        StateGraph(AgentState) round-trip, not a hand-built state dict —
        the mechanism the three b51 tests above bypass entirely."""
        from langgraph.graph import END, START, StateGraph

        from chaos_agent.agent.state import AgentState

        def writer(state):
            return {"verifier_loop_count": 8, "layer2_start_count": 8}

        graph = StateGraph(AgentState)
        graph.add_node("writer", writer)
        graph.add_edge(START, "writer")
        graph.add_edge("writer", END)
        out = graph.compile().invoke({"messages": []})
        assert out.get("layer2_start_count") == 8, (
            "the B51 pin must survive the LangGraph channel merge"
        )

    @pytest.mark.asyncio
    async def test_max_iterations_fallback(self):
        """Round-32 K2 修复验证：max-iterations 守卫的诚实词是 'unverified'
        （无法确认 ≠ 部分恢复）。旧词 'partial' 经 recovery_task_state_from_level
        落 'failed'——旧查询集对 failed 失明，在这个警告明说「故障可能
        仍活跃」的行上永久关闭恢复入口。"""
        node = make_recover_verifier(llm=AsyncMock(), tools=[], registry=None)
        state = {
            "task_id": "t1",
            "experiment_uid": "abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "",
            "verifier_loop_count": settings.max_recover_verifier_loop + 1,
        }
        result = await node(state)
        assert result["recover_verification"]["level"] == "unverified"
        assert result["result"]["recovered"] is False
        # the verdict's downstream task_state word, on both axes:
        #   word axis — 'unverified' is the honest verdict word;
        #   liability axis — it does NOT clear the ledger (verdict-terminal
        #     is not liability-clearing), so the row stays recoverable.
        from chaos_agent.agent.state import (
            TASK_STATE_ACTIVE_VALUES,
            TASK_STATE_CLEARED_VALUES,
            recovery_task_state_from_level,
        )

        new_state = recovery_task_state_from_level(
            "unverified", recovered=False
        )
        assert new_state == "unverified"
        assert new_state not in TASK_STATE_CLEARED_VALUES
        # and the old word's mapping is exactly the bug this replaces:
        # 'partial' fed the terminal 'failed' branch, which even the OLD
        # word-based query set could not see (the K2 blinding mechanism)
        old_state = recovery_task_state_from_level(
            "partial", recovered=False
        )
        assert old_state == "failed"
        assert old_state not in TASK_STATE_ACTIVE_VALUES


class TestRecoverBaselineGateEndToEnd:
    """The recover-side MIRROR of verify's TestBaselineComparisonInLayer2Context.

    ``apply_synthetic_pair_gate`` is unit-tested where it lives, and its wiring
    into ``_run_layer2_verification`` is AST-asserted in
    test_synthetic_pair_gate_contract.py. What neither covers is the gate
    actually EXECUTING inside the node: reading ``baseline_data``, rebuilding
    the pair, and handing it to the LLM. The node is a 600-line async function,
    but it is NOT unreachable — driving it with a mocked LLM and a baseline in
    state opens the gate exactly as production does. This asserts the synthetic
    pair reaches ``ainvoke``, the end-to-end coverage verify has always had.
    """

    @pytest.mark.asyncio
    async def test_baseline_gate_injects_recover_pair_that_reaches_the_llm(self):
        from chaos_agent.agent.nodes.recover._recover_layer1 import (
            _RECOVER_BASELINE_TOOL_CALL_ID as TC_ID,
        )

        # A tool_call response keeps the loop on the layer2 path. The gate runs
        # BEFORE the LLM call, so what the model replies does not matter here —
        # only that the injected pair is in what it was handed.
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [{
            "name": "kubectl",
            "args": {"subcommand": "get", "v_args": "pods -n default -o json"},
        }]
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        baseline = {
            "captured_at": "2026-05-09T10:00:00",
            "source": "registry",
            "success_count": 1,
            "total_count": 1,
            "observations": [{
                "exit_code": 0,
                "stdout": "NAME   CPU%  MEM%\nmyapp  5%  30%",
                "description": "pod resources",
                "command": "kubectl top pod",
            }],
        }
        with patch(
            "chaos_agent.agent.nodes.recover._recover_verifier_loop."
            "_layer1_destroy_via_provider"
        ) as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 0,
                "baseline_data": baseline,  # opens the gate (success_count > 0)
            }
            await node(state)

        # Every message object handed to ainvoke, across all calls.
        seen = []
        for call in mock_llm.ainvoke.call_args_list:
            for arg in list(call.args) + list(call.kwargs.values()):
                if isinstance(arg, list):
                    seen.extend(arg)

        assert mock_llm.ainvoke.call_args_list, "the node never reached the LLM"
        assert any(
            isinstance(m, AIMessage)
            and any(tc.get("id") == TC_ID for tc in (m.tool_calls or []))
            for m in seen
        ), "the gate's synthetic CALLER never reached the LLM"
        assert any(
            isinstance(m, ToolMessage) and m.tool_call_id == TC_ID for m in seen
        ), "the gate's synthetic RESULT never reached the LLM"


# ---------------------------------------------------------------------------
# _run_recover_layer1
# ---------------------------------------------------------------------------

class TestRunRecoverLayer1:
    @pytest.mark.asyncio
    async def test_no_blade_uid_skipped(self):
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy as _run_recover_layer1
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
        prompt = _build_recover_verifier_prompt(layer1_label="deterministic destroy")
        # Scheme B: verdict submitted via submit_recover_verification; the
        # deterministic-destroy label still appears in the text fallback block.
        assert "deterministic destroy" in prompt
        assert "submit_recover_verification" in prompt

    def test_non_chaosblade_label(self):
        prompt = _build_recover_verifier_prompt(layer1_label="recovery execution")
        assert "recovery execution" in prompt
        assert "submit_recover_verification" in prompt


# ---------------------------------------------------------------------------
# _build_layer1_recovery_prompt
# ---------------------------------------------------------------------------

class TestBuildLayer1RecoveryPrompt:
    def test_contains_constraints(self):
        prompt = _build_layer1_recovery_prompt()
        # Generic (native-carrier) recovery: Layer 2 owns verification, no
        # experiment carrier here, and no TTY-bound interactive commands.
        assert "Do NOT verify the fault has been removed" in prompt
        assert "Do NOT use interactive commands" in prompt
        assert "there is no experiment carrier" in prompt

    def test_undo_effect_evidence_taught(self):
        # inject-aac02265 (#35): Layer 1 issued the undo in the SAME batch
        # as a consequence read and claimed success — the programmatic
        # success guard correctly bounced it (no post-undo observation),
        # costing ~36s. The guard stays authoritative; this prompt line
        # teaches the SEMANTIC criterion, not the guard's mechanical
        # form: a receipt proves issuance, not effect — a success claim
        # needs direct evidence the undo is actually taking effect
        # (fault cause revoked / visibly being removed), read
        # immediately. Layer 1 never waits for recovery to finish.
        # Scoped to the generic branch only — the kubectl-blade branch's
        # destroy classifies READONLY and is exempt from the guard.
        prompt = _build_layer1_recovery_prompt()
        assert "proves the undo was issued, not that it took effect" in prompt
        assert "fault cause is revoked or" in prompt
        assert "visibly being removed" in prompt
        assert "not a wait for recovery" in prompt
        kubectl_blade = _build_layer1_recovery_prompt(is_kubectl_blade=True)
        assert "not that it took effect" not in kubectl_blade

    def test_landed_reminder_kept_but_no_heuristic_gate(self):
        """Batch 2 (revised): enforcement is programmatic (Layer 1 success
        guard), not prompt rules. The prompt keeps a ONE-line intent
        reminder only — no heuristic Completion Gate rule block."""
        for prompt in (
            _build_layer1_recovery_prompt(),
            _build_layer1_recovery_prompt(is_kubectl_blade=True),
        ):
            assert "Completion Gate" not in prompt
            assert "confirmed landed at the API" in prompt

    def test_contains_programmatic_patterns(self):
        prompt = _build_layer1_recovery_prompt()
        # Interactive commands must be translated into programmatic equivalents.
        assert "programmatic equivalents" in prompt

    def test_kubectl_blade_recovery_prompt_is_tool_agnostic(self):
        """The kubectl-exec recovery prompt follows the tool-abstraction
        boundary: namespace-agnostic discovery semantics in GENERIC terms,
        no literal tool-call syntax (concrete forms live in knowledge docs).

        task-e9bae269: the cluster's tool pods lived in `default`, so the
        legacy 'look in the chaosblade namespace' instruction found nothing
        and recovery could never reach the experiment.
        """
        prompt = _build_layer1_recovery_prompt(is_kubectl_blade=True)
        # Namespace-agnostic discovery semantics
        assert "NEVER assume" in prompt
        assert "ACROSS ALL NAMESPACES" in prompt
        assert "deployment-specific" in prompt
        # Recovery must mirror the in-cluster injection channel — pinned on
        # the CRITICAL CONSTRAINTS wording (its body carrier since pass-5
        # removed the REMEMBER bullet that carried the shorter phrase; the
        # phrase spans a line break, so both halves are pinned).
        assert "MUST go through the same in-cluster" in prompt
        assert "channel that performed the injection" in prompt
        # The recovery context message carries no persisted injection-pod
        # field, so the template must not promise one unconditionally —
        # only a conditional "when it is named" reference is honest.
        assert "The recovery context below names the" not in prompt
        assert "when it is named" in prompt
        assert "may name no pod at all" in prompt
        # First-principles command authority: the tool's own help/usage and
        # runtime output are the ground truth — docs/history may lag
        assert "help/usage" in prompt
        assert "runtime behavior is the ground truth" in prompt
        # Documentation must never be positioned as the command authority
        assert "knowledge reading tool" not in prompt
        # No dangling references to sections that don't carry the namespace
        assert "shows the exact pod (and its namespace)" not in prompt
        # No hardcoded namespace assumption anywhere
        assert "pods -n chaosblade" not in prompt
        assert "namespace='chaosblade'" not in prompt
        # Tool-agnostic: no literal tool-call syntax or tool names
        assert "kubectl(" not in prompt
        assert "blade destroy" not in prompt
        assert "blade_destroy" not in prompt
        assert "blade_status" not in prompt


class TestLayer1SuccessGuard:
    """Programmatic acceptance gate for Layer 1 success claims (mirrors the
    Layer 2 anti-laziness guard): a success with no post-mutation read-only
    observation is rejected once and bounced back into the ReAct loop."""

    @staticmethod
    def _ai(tool_calls):
        return AIMessage(content="", tool_calls=tool_calls)

    @staticmethod
    def _tc(cmd_id, *cmd):
        return {
            "name": "kubectl",
            "args": {"command": list(cmd)},
            "id": cmd_id,
            "type": "tool_call",
        }

    def test_success_with_post_mutation_observation_passes(self):
        from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
            _layer1_success_guard_feedback,
        )

        messages = [
            self._ai([self._tc("c1", "scale", "deployment/app", "--replicas", "3", "-n", "default")]),
            self._ai([self._tc("c2", "get", "deployment", "app", "-n", "default")]),
        ]
        layer1 = RecoverLayer1Result(status="passed", details="")
        assert _layer1_success_guard_feedback(layer1, {"messages": messages}) is None

    def test_success_without_observation_rejected_once(self):
        from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
            _layer1_success_guard_feedback,
        )

        messages = [
            self._ai([self._tc("c1", "scale", "deployment/app", "--replicas", "3", "-n", "default")]),
        ]
        layer1 = RecoverLayer1Result(status="passed", details="")
        state = {"messages": messages}
        feedback = _layer1_success_guard_feedback(layer1, state)
        assert feedback is not None
        assert "RECOVERY EXECUTION GUARD" in feedback
        # One-shot: the second claim passes (Layer 2 still verifies truth).
        state["_layer1_success_guard_fired"] = True
        assert _layer1_success_guard_feedback(layer1, state) is None

    def test_readonly_only_layer1_passes(self):
        from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
            _layer1_success_guard_feedback,
        )

        # No mutating call at all (e.g., pure observation) — nothing to
        # confirm landed.
        messages = [
            self._ai([self._tc("c1", "get", "pods", "-n", "default")]),
        ]
        layer1 = RecoverLayer1Result(status="passed", details="")
        assert _layer1_success_guard_feedback(layer1, {"messages": messages}) is None

    def test_failed_status_not_gated(self):
        from chaos_agent.agent.nodes.recover._recover_verifier_loop import (
            _layer1_success_guard_feedback,
        )

        messages = [
            self._ai([self._tc("c1", "scale", "deployment/app", "--replicas", "3", "-n", "default")]),
        ]
        layer1 = RecoverLayer1Result(status="failed", details="422")
        assert _layer1_success_guard_feedback(layer1, {"messages": messages}) is None


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
            "experiment_uid": "",
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
        # Layer 2 conclusion (non-first iteration) → guard bypassed; finalize verdict.
        fin = await _drive_finalize(state3, result3)
        assert fin["result"]["recovered"] is True
        assert fin["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_non_chaosblade_layer1_failed_continues_to_layer2(self):
        """Non-ChaosBlade: Layer 1 fails → continues to Layer 2 verification.

        Layer 1 failure (e.g. kubectl patch 422) should NOT skip Layer 2,
        because the fault may have self-recovered (e.g. Operator restored the Pod).
        The result should transition to layer2_verification phase.
        """
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
            "experiment_uid": "",
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
        # Should NOT produce a final result — transitions to Layer 2 instead
        assert "result" not in result, "Layer 1 failure should not produce final result for non-ChaosBlade"
        assert result["recover_phase"] == "layer2_verification"
        assert result["recover_layer1_type"] == "llm_driven"
        # Layer 1 cache should reflect the failure
        cache = result.get("recover_layer1_cache", {})
        assert cache.get("status") == "failed"

    @pytest.mark.asyncio
    async def test_non_chaosblade_layer1_llm_exception_empty_str_backfills_class_name(self):
        """Non-ChaosBlade: Layer 1 LLM call raises an EMPTY-str exception
        (asyncio.TimeoutError/CancelledError shape, e.g. a 29-min read-timeout
        window) — the error details must not end with a blank tail: the
        exception class name is backfilled so the failure stays attributable.

        Fall-through shape mirrors the live recover-c801f489 behaviour: the
        Layer-1 error does NOT terminalize the node — the same invocation
        proceeds into Layer 2, which times out on its own and finalizes.
        """
        mock_llm = AsyncMock()
        # TimeoutError() has an empty str() — the exact shape observed in
        # task recover-c801f489 ("LLM call failed: " with nothing after it).
        mock_llm.ainvoke = AsyncMock(side_effect=TimeoutError())
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        state = {
            "task_id": "t1",
            "experiment_uid": "",
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
        # Layer 1 error falls through: the finalization comes from Layer 2's
        # own timeout, not from the Layer-1 error itself.
        assert result.get("error", "").startswith("recovery_verification_timeout"), (
            f"expected Layer 2 timeout finalization, got {result.get('error')!r}"
        )
        cache = result.get("recover_layer1_cache", {})
        assert cache.get("status") == "error"
        details = str(cache.get("details", ""))
        # The fix: no blank tail — the class name is backfilled
        assert details.strip() != "LLM call failed:", "details must not end with a blank tail"
        assert details.endswith("TimeoutError"), f"expected backfilled class name, got {details!r}"
        assert str(cache.get("raw_output", "")).strip() != "", "raw_output must not be blank"

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
            "experiment_uid": "",
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
        fin = await _drive_finalize(state2, result2)
        assert fin["result"]["recovered"] is True

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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success",
                raw_output='{"code": 200}',
            )
            # Iteration 1: deterministic Layer 1 + Layer 2 first iteration (tool call)
            state1 = {
                "task_id": "t1",
                "experiment_uid": "abc123",
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
            fin = await _drive_finalize(state2, result2)
            assert fin["result"]["recovered"] is True


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
    async def test_blade_destroy_failed_but_experiment_gone_passes(self):
        """When blade_destroy fails but experiment CRD is already gone (not found),
        Layer 1 passes — the experiment was auto-recovered (timeout expiry)."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy as _run_recover_layer1

        destroy_output = json.dumps({
            "code": 500, "success": False,
            "error": "record not found, if it's k8s experiment, please add --target k8s flag to retry"
        })
        status_output = json.dumps({
            "code": 63061, "success": False,
            "error": "Error from server (NotFound): chaosblades.chaosblade.io \"uid-k8s-abc\" not found"
        })

        with patch("chaos_agent.agent.providers.chaosblade.cli.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(return_value=destroy_output)
            with patch("chaos_agent.agent.providers.chaosblade.cli.blade_status") as mock_status:
                mock_status.ainvoke = AsyncMock(return_value=status_output)

                result = await _run_recover_layer1(
                    "uid-k8s-abc", "/path/to/kubeconfig", messages=[]
                )
                assert result.status == "passed"

    @pytest.mark.asyncio
    async def test_blade_destroy_failed_experiment_still_running(self):
        """When blade_destroy fails and experiment is still Running,
        Layer 1 stays 'failed'."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy as _run_recover_layer1

        destroy_output = json.dumps({
            "code": 500, "success": False,
            "error": "kubewiz guard blocked kubectl delete chaosblade"
        })
        status_output = json.dumps({
            "code": 200, "success": True,
            "result": {"uid": "uid-k8s-abc", "status": "Running"}
        })

        with patch("chaos_agent.agent.providers.chaosblade.cli.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(return_value=destroy_output)
            with patch("chaos_agent.agent.providers.chaosblade.cli.blade_status") as mock_status:
                mock_status.ainvoke = AsyncMock(return_value=status_output)

                result = await _run_recover_layer1(
                    "uid-k8s-abc", "/path/to/kubeconfig", messages=[]
                )
                assert result.status == "failed"
                assert result.is_terminal()

    @pytest.mark.asyncio
    async def test_blade_destroy_succeeds_normally(self):
        """Normal blade_destroy succeeds."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy as _run_recover_layer1

        destroy_output = json.dumps({"code": 200, "success": True, "result": "uid-abc"})

        with patch("chaos_agent.agent.providers.chaosblade.cli.blade_destroy") as mock_destroy:
            mock_destroy.ainvoke = AsyncMock(return_value=destroy_output)
            with patch("chaos_agent.agent.providers.chaosblade.cli.blade_status") as mock_status:
                mock_status.ainvoke = AsyncMock(return_value=json.dumps({"code": 406, "success": False}))

                result = await _run_recover_layer1(
                    "uid-abc", "/path/to/kubeconfig", messages=[]
                )
                assert result.status == "passed"

    @pytest.mark.asyncio
    async def test_blade_tools_exception_stays_error(self):
        """When blade tools throw exceptions, stays 'error'."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy as _run_recover_layer1

        with patch("chaos_agent.agent.providers.chaosblade.cli.blade_destroy") as mock_destroy:
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
        prompt = _build_recover_verifier_prompt(layer1_label="deterministic destroy")
        assert "deterministic destroy" in prompt

    def test_non_chaosblade_prompt(self):
        prompt = _build_recover_verifier_prompt(layer1_label="recovery execution")
        assert "recovery execution" in prompt

    def test_prompt_contains_format_constraint(self):
        """Recover verifier prompt must use submit_recover_verification, not RECOVERY_EXECUTION_RESULT."""
        prompt = _build_recover_verifier_prompt(layer1_label="recovery execution")
        assert "submit_recover_verification" in prompt
        assert "RECOVERY_VERIFICATION_RESULT" in prompt
        assert "successfully recovered" in prompt


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
            "experiment_uid": "uid-k8s-abc",
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
            "experiment_uid": "",
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
        """Normal ChaosBlade injection (host blade_create) still runs the deterministic destroy."""
        # No-LLM path delegates to ChaosbladeProvider.recover, which imports
        # run_layer1_destroy from its canonical module at call time — patch there.
        with patch("chaos_agent.agent.providers.chaosblade.recover.run_layer1_destroy") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "experiment_uid": "uid-host-abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "messages": [],
            }
            result = await recover_verifier(state)
            mock_l1.assert_called_once()
            assert result["result"]["recovered"] is True


# ---------------------------------------------------------------------------
# recover_layer1_type materialization (phase-4 T1.4)
# ---------------------------------------------------------------------------

class TestRecoverLayer1TypeMaterialization:
    """The Layer-1 type must become explicit on state once a deterministic
    destroy ran (simple entry writes it post-destroy; the LLM flow's Layer-2
    result materializes the inferred value), while the readers' None
    fallback keeps serving legacy checkpoints unchanged."""

    @pytest.mark.asyncio
    async def test_simple_entry_deterministic_write(self):
        """Deterministic destroy executed in the no-LLM entry → the type is
        written explicitly instead of being left to reader-side inference."""
        with patch("chaos_agent.agent.providers.chaosblade.recover.run_layer1_destroy") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(
                status="passed",
                details="blade_destroy: success",
                raw_output='{"code": 200}',
            )
            state = {
                "task_id": "t1",
                "experiment_uid": "uid-host-abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "messages": [],
            }
            result = await recover_verifier(state)
            mock_l1.assert_called_once()
            assert result["recover_layer1_type"] == "deterministic"

    @pytest.mark.asyncio
    async def test_simple_entry_skipped_not_written(self):
        """kubectl-exec delivery: Layer-1 "skipped" (deterministic destroy not
        applicable) → the field stays untouched (None default)."""
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )
        state = {
            "task_id": "t1",
            "experiment_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "messages": kubectl_msgs,
        }
        result = await recover_verifier(state)
        assert result["recover_verification"]["layer1"]["status"] == "skipped"
        assert "recover_layer1_type" not in result

    @pytest.mark.asyncio
    async def test_llm_flow_layer2_materializes_deterministic(self):
        """Deterministic Layer-1 in the LLM flow never writes the field at its
        transition — the first Layer-2 result materializes the inferred
        "deterministic" value."""
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (deterministic destroy): passed - success\n"
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

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t1",
                "experiment_uid": "abc",
                "skill_name": "unknown-skill",
                "kubeconfig": "",
                "verifier_loop_count": 0,
            }
            result = await node(state)
            mock_l1.assert_called_once()
            assert result["recover_layer1_type"] == "deterministic"

    @pytest.mark.asyncio
    async def test_llm_flow_explicit_type_not_overwritten(self):
        """A pre-existing explicit type (e.g. "llm_driven" written at the
        Layer-1 transition) must survive the Layer-2 result — no re-inference,
        no overwrite."""
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (recovery execution): passed\n"
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

        state = {
            "task_id": "t1",
            "experiment_uid": "abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "",
            "verifier_loop_count": 2,  # Non-first iteration
            "layer2_context_added": True,
            "recover_phase": "layer2_verification",
            "recover_layer1_type": "llm_driven",
        }
        result = await node(state)
        # Absent from the update = state's explicit value survives the merge.
        assert "recover_layer1_type" not in result

    @pytest.fixture
    def _l2_failed_state(self):
        """State reaching finalize with a failed Layer-2 verdict on a
        deterministic Layer-1 (retry-eligible: loop budget available, no
        retry marker yet). ``layer1_type`` set per-test."""
        return {
            "task_id": "t1",
            "experiment_uid": "abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "",
            "verifier_loop_count": 1,
            "layer2_context_added": True,
            "recover_phase": "layer2_verification",
            "recover_layer2_first": False,
            "recover_layer1_cache": {"status": "passed", "details": "ok", "raw_output": "ok"},
            "messages": [
                AIMessage(content=(
                    "RECOVERY_VERIFICATION_RESULT:\n"
                    "- Layer1 (deterministic destroy): passed - success\n"
                    "- Layer2 (fault-specific): failed - CPU still at 95%\n"
                    "- Overall: unrecovered\n"
                    "- Warnings: none"
                )),
            ],
        }

    @pytest.mark.asyncio
    async def test_finalize_explicit_deterministic_takes_retry_destroy(self, _l2_failed_state):
        """Explicit "deterministic" on state → finalize retry takes the
        deterministic-destroy branch directly (no None re-derivation)."""
        from chaos_agent.agent.nodes.recover._recover_finalize import (
            make_finalize_recover_verification,
        )

        state = {**_l2_failed_state, "recover_layer1_type": "deterministic"}
        with patch("chaos_agent.agent.providers.chaosblade.recover.raw_destroy", new_callable=AsyncMock) as mock_raw:
            mock_raw.return_value = '{"code": 200, "success": true}'
            fnode = make_finalize_recover_verification()
            result = await fnode(state)
            mock_raw.assert_called_once()
            assert any(
                "layer-1 destroy output:" in (getattr(m, "content", "") or "")
                for m in result.get("messages", [])
            )
            assert "recover_verification" not in result  # loop back, not finalized

    @pytest.mark.asyncio
    async def test_finalize_none_fallback_takes_retry_destroy(self, _l2_failed_state):
        """Legacy-checkpoint None → the reader-side fallback still derives
        "deterministic" and the retry branch fires identically."""
        from chaos_agent.agent.nodes.recover._recover_finalize import (
            make_finalize_recover_verification,
        )

        state = {**_l2_failed_state}  # no recover_layer1_type key at all
        with patch("chaos_agent.agent.providers.chaosblade.recover.raw_destroy", new_callable=AsyncMock) as mock_raw:
            mock_raw.return_value = '{"code": 200, "success": true}'
            fnode = make_finalize_recover_verification()
            result = await fnode(state)
            mock_raw.assert_called_once()
            assert any(
                "layer-1 destroy output:" in (getattr(m, "content", "") or "")
                for m in result.get("messages", [])
            )


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
        # Real runs observe the target after injection (verify phase); the
        # Layer 1 success guard requires a read-only call after the last
        # mutation, so model that observation here.
        kubectl_inject_msgs += _make_kubectl_tool_call_pair(
            "tc2", "get", "pods -n default", "NAME  READY  STATUS",
        )

        state1 = {
            "task_id": "t1",
            "experiment_uid": "uid-k8s-abc",
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
        # Post-injection observation (see the first part of this test).
        kubectl_inject_msgs += _make_kubectl_tool_call_pair(
            "tc2", "get", "pods -n default", "NAME  READY  STATUS",
        )

        state1 = {
            "task_id": "t1",
            "experiment_uid": "uid-k8s-abc",
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
        # Layer 2 conclusion (non-first iteration) → guard bypassed; finalize verdict.
        fin = await _drive_finalize(state3, result3)
        assert fin["result"]["recovered"] is True
        assert fin["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_layer1_success_without_observation_bounces_back(self):
        """Programmatic success guard wiring: a Layer 1 success claim with no
        read-only observation after the last mutation is rejected once and
        bounced back into the Layer 1 ReAct loop instead of transitioning to
        Layer 2 (mirrors the Layer 2 anti-laziness guard)."""
        mock_l1_response = MagicMock()
        mock_l1_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: destroyed experiment\n"
            "- Details: none"
        )
        mock_l1_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_l1_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        # Injection mutation with NO post-mutation observation anywhere.
        kubectl_inject_msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        state1 = {
            "task_id": "t1",
            "experiment_uid": "uid-k8s-abc",
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

        # No Layer 2 transition — the claim was rejected.
        assert result1.get("recover_phase") != "layer2_verification"
        assert result1.get("_layer1_success_guard_fired") is True
        # Layer 1 stays in progress for the bounce-back iteration.
        assert result1["recover_layer1_cache"]["status"] == "in_progress"
        # Guidance message appended after the LLM's conclusion.
        msgs = result1["messages"]
        assert isinstance(msgs[-1], HumanMessage)
        assert "RECOVERY EXECUTION GUARD" in msgs[-1].content

    @pytest.mark.asyncio
    async def test_kubectl_exec_inject_context_contains_destroy_instructions(self):
        """When kubectl exec injection is detected, the inject_context passed to
        the Layer 1 LLM should contain tool-agnostic destroy instructions
        (in-cluster channel semantics, no literal tool syntax)."""
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
            "experiment_uid": "uid-k8s-abc",
            "skill_name": "cpu-stress",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["test-pod"]},
            "inject_context": "Injected CPU stress via ChaosBlade kubectl exec",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
        }
        await node(state)

        # The LLM's first call should contain tool-agnostic destroy instructions
        first_call_args = mock_llm.ainvoke.call_args_list[0]
        messages_arg = first_call_args[0][0]
        all_content = " ".join(getattr(m, "content", "") for m in messages_arg if hasattr(m, "content"))
        assert "uid-k8s-abc" in all_content
        assert "experiment-destroy command" in all_content
        assert "in-cluster channel" in all_content
        assert "do NOT assume" in all_content
        # Tool-agnostic: no literal tool-call syntax
        assert "kubectl(subcommand=" not in all_content
        # Kubeconfig mandate wording is tool-agnostic too
        assert "EVERY kubectl tool call" not in all_content

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
            "experiment_uid": "uid-k8s-abc",
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
        await node(state2)

        # Check that Layer 2 prompt uses "recovery execution" (non-ChaosBlade)
        second_call_args = mock_llm.ainvoke.call_args_list[1]
        system_msg = second_call_args[0][0][0]
        assert "recovery execution" in system_msg.content

    @pytest.mark.asyncio
    async def test_kubectl_exec_layer2_tool_pod_block_is_tool_agnostic(self):
        """The recover Layer-2 tool pod context must follow the same boundary
        as the inject verifier: namespace-agnostic discovery semantics and no
        literal tool-call syntax (task-e9bae269; concrete forms live in
        knowledge docs)."""
        from chaos_agent.agent.spec.fault_spec import FaultSpec

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
            "otel-c-tool -n chaosblade -- blade create k8s node-disk fill",
            json.dumps({"code": 200, "success": True, "result": "uid-k8s-abc"}),
        )

        spec = FaultSpec(
            namespace="cms-demo",
            scope="node",
            names=("node-a",),
            fault_target="disk",
            fault_action="fill",
            params={"path": "/", "percent": "80"},
        )

        state1 = {
            "task_id": "t1",
            "experiment_uid": "uid-k8s-abc",
            "skill_name": "disk-fill",
            "kubeconfig": "/path/to/kubeconfig",
            "verifier_loop_count": 0,
            "messages": kubectl_inject_msgs,
            "target": {"namespace": "default", "names": ["node-a"]},
            "inject_context": "Injected disk fill via in-cluster tool pod",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "fault_spec": spec.to_dict(),
            "kubectl_exec_pod_name": "otel-c-tool-x",
        }
        result1 = await node(state1)

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
        await node(state2)

        # The Layer-2 iteration call carries the tool pod context block
        l2_call_args = mock_llm.ainvoke.call_args_list[1]
        all_content = " ".join(
            getattr(m, "content", "") for m in l2_call_args[0][0]
            if hasattr(m, "content") and isinstance(m.content, str)
        )
        assert "## Available Tool Pod" in all_content
        # Namespace-agnostic discovery semantics
        assert "identify it across all namespaces before exec" in all_content
        # No hardcoded namespace assertion anywhere in the block
        assert "- Namespace: `chaosblade`" not in all_content
        # Tool-agnostic: no literal tool-call syntax or injection-tool names
        assert "kubectl(subcommand=" not in all_content
        assert "blade status" not in all_content
        assert "blade query" not in all_content
        # Kubeconfig mandate is tool-agnostic too
        assert "EVERY kubectl tool call" not in all_content
        assert "cluster tool call" in all_content


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
        assert "**Injection-verification reference**" in result
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
        assert "**Injection-verification reference**" in result
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
        assert "**Injection-verification reference**" in result
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
        assert "**Injection-verification reference**" not in result
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
        """Core Principles appear at the BEGINNING of the prompt (primacy effect)."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        # Core Principles must appear before the middle-zone sections
        # (knowledge summary, tools, skill priority, kubeconfig)
        rules_pos = prompt.index("Core Principles")
        knowledge_pos = prompt.index("Domain Knowledge")
        assert rules_pos < knowledge_pos
        assert "Evidence MUST come from" in prompt[:600]

    def test_u_shape_recency_zone(self):
        """pass-5 (2026-09-20): the REMEMBER recency mirror was deleted —
        all seven bullets restated named carriers, and the recover
        verifier's ReAct loop owns recency at the message tail (tool
        results + conditional reminders). The prompt now closes on the
        machine-parsed Output contract, the same shape as the pass-4
        verifier; what must NOT come back is a prompt-end REMEMBER recap
        pretending to hold the recency position."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "# REMEMBER" not in prompt
        # Output contract IS the closing block now
        output_format_pos = prompt.index("RECOVERY_VERIFICATION_RESULT")
        assert prompt.rstrip().endswith("parsed programmatically.")
        assert output_format_pos > prompt.index("Core Principles")
        # Carrier spot-checks: the two bullets whose carriers sat farthest
        # from the head remain stated in their functional sections.
        assert "stale" in prompt[: prompt.index("Converging State")]
        assert "NOT generic health" in prompt

    def test_chaosblade_label(self):
        """is_chaosblade=True → Layer1 label is 'blade_destroy'."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "blade_destroy" in prompt

    def test_non_chaosblade_label(self):
        """is_chaosblade=False → Layer1 label is 'recovery execution'."""
        prompt = build_recover_verifier_system_prompt(layer1_label="recovery execution")
        assert "recovery execution" in prompt

    def test_all_sections_present(self):
        """All section functions are composed in the prompt (REMEMBER
        removed in pass-5 — see test_u_shape_recency_zone)."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "successfully recovered" in prompt
        assert "Core Principles" in prompt
        assert "Domain Knowledge" in prompt or "Knowledge" in prompt
        assert "kubectl" in prompt
        assert "Skill Use-Case Priority" in prompt
        assert "RECOVERY_VERIFICATION_RESULT" in prompt

    def test_baseline_integrity_compact(self):
        """Compact baseline integrity rules (4 rules + 1 example) are present."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "Baseline Comparison Rules" in prompt
        assert "SAME resource only" in prompt
        assert "imagefs /dev/vdb" in prompt

    def test_no_full_baseline_integrity(self):
        """Full BASELINE_INTEGRITY_PROMPT content should NOT appear in the compact version."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        # The compact version has 4 concise rules; the full version has verbose
        # "FORMAT REQUIREMENT" text — ensure compact doesn't include it
        assert "FORMAT REQUIREMENT" not in prompt

    def test_three_level_degradation_in_principles(self):
        """Core Principles mention healthy-state comparison as middle degradation level."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "healthy state" in prompt.lower() or "healthy-state" in prompt.lower()
        assert "cross-validate" in prompt.lower() or "cross-validation" in prompt.lower()

    def test_output_format_has_status_definitions(self):
        """Output Format defines Overall and per-step status values."""
        prompt = build_recover_verifier_system_prompt(layer1_label="blade_destroy")
        assert "recovered" in prompt
        assert "unrecovered" in prompt
        assert "partial" in prompt


# ---------------------------------------------------------------------------
# finalize_recover_verification: IN_PROGRESS defense-in-depth
# ---------------------------------------------------------------------------

class TestFinalizeInProgressDefense:
    """Tests for the defense-in-depth handling of ``in_progress`` cache.

    After the root-cause fix (Layer 1 filters out submit_recover_verification),
    the cache should never be ``in_progress`` when finalize runs.  But if it
    does happen (e.g. routing bug), finalize should default to ``unknown``
    instead of crashing with a Pydantic ValidationError.
    """

    @pytest.mark.asyncio
    async def test_in_progress_cache_defaults_to_unknown(self):
        """When cache is in_progress, finalize should default to unknown,
        not crash with ValidationError."""
        from chaos_agent.agent.nodes.verify._verifier_submit import SUBMIT_RECOVER_VERIFICATION_TOOL_NAME

        # Simulate: Layer 1 cache is in_progress (should not happen after fix,
        # but defense-in-depth must handle it).
        submit_msg = AIMessage(
            content="Some recovery text without RECOVERY_EXECUTION_RESULT",
            tool_calls=[{
                "name": SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
                "args": {
                    "overall": "recovered",
                    "layer2_status": "passed",
                    "layer2_details": "Pod is no longer stuck",
                },
                "id": "tc_submit",
                "type": "tool_call",
            }],
        )
        tool_msg = ToolMessage(
            content="Recovery verdict recorded.",
            tool_call_id="tc_submit",
            name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )

        state = {
            "task_id": "task-test",
            "experiment_uid": "",
            "skill_name": "pod-terminating",
            "kubeconfig": "",
            "verifier_loop_count": 3,
            "messages": [submit_msg, tool_msg],
            "recover_layer1_cache": {
                "status": "in_progress",
                "details": "",
                "raw_output": "",
                "system_prompt": "test prompt",
            },
            "recover_layer2_first": False,
            "layer2_context_added": True,
        }

        fin = await _drive_finalize(state, {})

        # finalize should succeed (no ValidationError)
        assert "recover_verification" in fin
        # Layer 1 should be unknown (defense-in-depth), not in_progress
        assert fin["recover_verification"]["layer1"]["status"] == "unknown"


# ---------------------------------------------------------------------------
# finalize_recover_verification: combo experiment-uid external contract
# ---------------------------------------------------------------------------

class TestFinalizeComboUidContract:
    @pytest.mark.asyncio
    async def test_combo_result_keeps_experiment_uid(self):
        """Combo task: the external ``result["experiment_uid"]`` contract keeps
        the EXPERIMENT uid even though the durable attribution is native —
        the finalize reads the combo-safe dispatch identity (a plain
        materialization would return the native handle and blank the uid).
        (Legacy-spelling input pins the hydrate path.)"""
        state = {
            "task_id": "t-combo",
            "skill_name": "pod-delete",
            "kubeconfig": "",
            "experiment_uid": "uid-combo",
            "injection_method": "kubectl_native",
            "combo_native_issued": True,
            "recover_layer1_type": "llm_driven",
            "verifier_loop_count": 2,
            "messages": [],
        }
        fin = await _drive_finalize(state, {})
        # The external uid contract survives the native attribution (the
        # verdict itself comes from the messages — out of scope here).
        assert fin["result"]["experiment_uid"] == "uid-combo"
        assert "blade_uid" not in fin["result"]


# ---------------------------------------------------------------------------
# finalize_recover_verification: mark original inject task as recovered
# ---------------------------------------------------------------------------

class TestFinalizeMarksInjectTask:
    """Tests that finalize_recover_verification marks the ORIGINAL inject task
    (recover_task_id) as recovered in the TaskStore, so it disappears from
    query_active_experiments.
    """

    @pytest.mark.asyncio
    async def test_marks_inject_task_recovered_on_success(self):
        """On successful recovery, the original inject task should be updated
        with operation='recover' and recover_verification so infer_task_state
        returns 'recovered'."""
        from chaos_agent.agent.nodes.verify._verifier_submit import SUBMIT_RECOVER_VERIFICATION_TOOL_NAME

        submit_msg = AIMessage(
            content="Recovery verified.",
            tool_calls=[{
                "name": SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
                "args": {
                    "overall": "recovered",
                    "layer2_status": "passed",
                    "layer2_details": "All clear",
                },
                "id": "tc_submit",
                "type": "tool_call",
            }],
        )
        tool_msg = ToolMessage(
            content="Recovery verdict recorded.",
            tool_call_id="tc_submit",
            name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )

        inject_task_id = "task-inject-aaa"
        recover_task_id = "task-recover-bbb"

        state = {
            "task_id": recover_task_id,
            "recover_task_id": inject_task_id,
            "experiment_uid": "uid-123",
            "skill_name": "pod-cpu-fullload",
            "kubeconfig": "",
            "verifier_loop_count": 3,
            "messages": [submit_msg, tool_msg],
            "recover_layer1_cache": {
                "status": "passed",
                "details": "blade_destroy succeeded",
                "raw_output": "success",
                "system_prompt": "test",
            },
            "recover_layer2_first": False,
            "layer2_context_added": True,
        }

        upsert_calls = []
        update_state_calls = []

        class _FakeStore:
            async def upsert(self, task_id, **fields):
                upsert_calls.append((task_id, fields))

            async def update_task_state(self, task_id, task_state, *, recover_verification=None):
                update_state_calls.append((task_id, task_state, recover_verification))

        with patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new_callable=AsyncMock,
            return_value=_FakeStore(),
        ):
            await _drive_finalize(state, {})

        # The original inject task should be marked via update_task_state
        inject_updates = [c for c in update_state_calls if c[0] == inject_task_id]
        assert len(inject_updates) == 1, f"Expected 1 update_task_state for inject task, got {len(inject_updates)}"
        assert inject_updates[0][1] == "recovered"
        # round-33b single-source: the clearance verdict MUST travel with the
        # CLEARED word onto the SAME (inject) row — a bare word left the row a
        # permanent "completed-but-uncleared" ghost under the fail-closed
        # predicate (UID-less native carriers the ledger is blind to).
        assert inject_updates[0][2] is not None, (
            "verdict must be propagated onto the inject row with the word"
        )
        assert inject_updates[0][2]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_no_inject_task_update_when_recover_task_id_missing(self):
        """When recover_task_id is not set (e.g. direct API call without
        intent_clarification), finalize should not crash."""
        from chaos_agent.agent.nodes.verify._verifier_submit import SUBMIT_RECOVER_VERIFICATION_TOOL_NAME

        submit_msg = AIMessage(
            content="Recovery verified.",
            tool_calls=[{
                "name": SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
                "args": {
                    "overall": "recovered",
                    "layer2_status": "passed",
                    "layer2_details": "All clear",
                },
                "id": "tc_submit",
                "type": "tool_call",
            }],
        )
        tool_msg = ToolMessage(
            content="Recovery verdict recorded.",
            tool_call_id="tc_submit",
            name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )

        recover_task_id = "task-recover-bbb"
        state = {
            "task_id": recover_task_id,
            # NO recover_task_id — simulate direct API recover
            "experiment_uid": "uid-123",
            "skill_name": "pod-cpu-fullload",
            "kubeconfig": "",
            "verifier_loop_count": 3,
            "messages": [submit_msg, tool_msg],
            "recover_layer1_cache": {
                "status": "passed",
                "details": "blade_destroy succeeded",
                "raw_output": "success",
                "system_prompt": "test",
            },
            "recover_layer2_first": False,
            "layer2_context_added": True,
        }

        update_state_calls = []

        class _FakeStore:
            async def upsert(self, task_id, **fields):
                pass

            async def update_task_state(self, task_id, task_state, *, recover_verification=None):
                update_state_calls.append((task_id, task_state, recover_verification))

        with patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new_callable=AsyncMock,
            return_value=_FakeStore(),
        ):
            # Should not crash
            await _drive_finalize(state, {})

        # No update_task_state should happen (recover_task_id is empty)
        assert len(update_state_calls) == 0, f"Expected no state update, got {update_state_calls}"


# ---------------------------------------------------------------------------
# Combo injection recovery (blade experiment + kubectl-native component)
# ---------------------------------------------------------------------------

class TestMergeComboBladePart:
    """Composite Layer-1 verdict: either part failing fails the whole."""

    def _merge(self, layer1, blade_part=None, state=None):
        # phase-3 T4: the composite combo verdict moved to the experiment
        # carrier (``merge_deterministic_recover_verdict``).
        from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider

        return ChaosbladeProvider().merge_deterministic_recover_verdict(
            layer1, state or {}, part_override=blade_part
        )

    def test_no_blade_part_is_identity(self):
        l1 = RecoverLayer1Result(status="passed", details="native undone")
        assert self._merge(l1) is l1

    def test_blade_passed_annotates_native_verdict(self):
        from chaos_agent.agent.result.verdict import Layer1Status

        l1 = RecoverLayer1Result(status="passed", details="native undone")
        # model_dump keeps enum objects — normalization must handle them.
        merged = self._merge(l1, blade_part={"status": Layer1Status.PASSED, "details": ""})
        assert str(getattr(merged.status, "value", merged.status)) == "passed"
        assert "[blade experiment destroyed deterministically]" in merged.details

    def test_blade_failed_fails_composite(self):
        l1 = RecoverLayer1Result(status="passed", details="native undone")
        merged = self._merge(l1, blade_part={"status": "failed", "details": "destroy err"})
        assert str(getattr(merged.status, "value", merged.status)) == "failed"
        assert "Combo recovery failed" in merged.details
        assert "destroy err" in merged.details


class TestComboRecoveryRouting:
    """Combo injection must run the deterministic blade destroy FIRST, then
    route to the LLM-driven Layer-1 flow for the native undo — deterministic
    recovery alone would leak the native mutation."""

    def _combo_state(self):
        return {
            "task_id": "t1",
            "experiment_uid": "uid-combo",
            "skill_name": "combo-skill",
            "kubeconfig": "/path/to/config",
            "verifier_loop_count": 0,
            "messages": [],
            "target": {"namespace": "default", "names": ["test-pod"]},
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "injection_method": "host_blade",
            "combo_native_issued": True,
            "inject_context": "Scaled deploy/foo to 0 replicas via kubectl patch",
        }

    def _mock_l1_llm(self):
        resp = MagicMock()
        resp.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: reverted kubectl patch on deploy/foo\n"
            "- Details: none"
        )
        resp.tool_calls = []
        llm = AsyncMock()
        llm.ainvoke = AsyncMock(return_value=resp)
        llm.bind_tools = MagicMock(return_value=llm)
        llm.bind = MagicMock(return_value=llm)
        return llm

    @pytest.mark.asyncio
    async def test_combo_routes_to_llm_after_deterministic_blade_destroy(self):
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="passed", details="destroyed")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ) as mock_l1:
            result = await node(self._combo_state())

        # Blade part destroyed deterministically first...
        mock_l1.assert_awaited_once()
        # ...then the LLM flow was entered (not the deterministic terminal).
        assert llm.ainvoke.await_count >= 1
        bp = result.get("combo_blade_part")
        assert bp is not None
        assert str(getattr(bp.get("status"), "value", bp.get("status"))) == "passed"
        # Composite verdict: native passed, blade part annotated.
        cache = result.get("recover_layer1_cache", {})
        assert "[blade experiment destroyed deterministically]" in cache.get("details", "")
        # Combo context reached the LLM messages.
        msgs = result.get("messages", [])
        assert any(
            "handled by the framework" in str(getattr(m, "content", "")) for m in msgs
        )

    @pytest.mark.asyncio
    async def test_combo_blade_destroy_failure_fails_composite(self):
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="failed", details="destroy error xyz")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ):
            result = await node(self._combo_state())

        cache = result.get("recover_layer1_cache", {})
        assert str(getattr(cache.get("status"), "value", cache.get("status"))) == "failed"
        assert "Combo recovery failed" in cache.get("details", "")
        assert "destroy error xyz" in cache.get("details", "")

    @pytest.mark.asyncio
    async def test_implicit_combo_by_durable_cross_check(self):
        """Marker absent, but durable cross-check proves the combo: a
        native-family attribution alongside a live blade_uid cannot arise
        legitimately — recovery must still destroy the blade part first and
        route the native undo to the LLM flow."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="passed", details="destroyed")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        state = self._combo_state()
        del state["combo_native_issued"]  # marker never landed
        # The cross-check evidence itself: native-family attribution with a
        # live blade_uid.
        state["injection_method"] = "kubectl_native"
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ) as mock_l1:
            result = await node(state)

        mock_l1.assert_awaited_once()
        assert llm.ainvoke.await_count >= 1
        assert result.get("combo_blade_part") is not None
        cache = result.get("recover_layer1_cache", {})
        assert "[blade experiment destroyed deterministically]" in cache.get("details", "")

    @pytest.mark.asyncio
    async def test_stale_combo_blade_part_cleared_for_non_combo_run(self):
        """Defense against cross-run contamination: a blade-part verdict
        inherited in state from an earlier recover run must be cleared at
        the LLM-flow entry of a NON-combo run — otherwise the composite
        merge would mis-report this run as a combo failure."""
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        state = self._combo_state()
        # Pure native run: no blade UID, no marker — but a stale verdict
        # lingers in the inherited state.
        del state["combo_native_issued"]
        state["experiment_uid"] = None
        state["injection_method"] = "kubectl_native"
        state["combo_blade_part"] = {"status": "failed", "details": "stale verdict"}
        result = await node(state)

        assert result.get("combo_blade_part") is None
        cache = result.get("recover_layer1_cache", {})
        assert "Combo recovery failed" not in cache.get("details", "")
        assert "stale verdict" not in cache.get("details", "")

    @pytest.mark.asyncio
    async def test_pure_blade_not_implicit_combo(self):
        """Experiment-method attribution + UID is NOT a combo: the
        cross-check only fires for native-family methods, so pure-blade
        tasks keep the zero-LLM deterministic route."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="passed", details="destroyed")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        state = self._combo_state()
        del state["combo_native_issued"]
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ) as mock_l1:
            result = await node(state)

        mock_l1.assert_awaited_once()
        # The combo flow did NOT run: no blade-part record and no composite
        # annotation on the Layer-1 verdict (the LLM call count is not a
        # discriminator — a deterministic Layer 1 still transitions to the
        # LLM-driven Layer 2 verification within the same invocation).
        assert result.get("combo_blade_part") is None
        cache = result.get("recover_layer1_cache", {})
        assert "[blade experiment destroyed deterministically]" not in cache.get("details", "")

    @pytest.mark.asyncio
    async def test_combo_not_stealed_by_stale_blade_message_without_method(self):
        """Regression for the elif-steal edge: a combo marked ONLY by the
        durable flag (injection_method cleared by a keep_experiment_uid replan
        seam) with a stale failed blade_create ToolMessage still in the
        history must NOT fall into the terminal "blade attempted, no UID"
        branch — the UID exists, the blade part was pre-destroyed, and the
        native undo must reach the LLM flow."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="passed", details="destroyed")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        state = self._combo_state()
        state["injection_method"] = None  # cleared by the replan seam
        # Stale evidence: blade_create was called (and failed) before the
        # successful re-attribution; no durable method backs the scan.
        state["messages"] = [
            ToolMessage(content="Error: create failed", name="blade_create", tool_call_id="tc1"),
        ]
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ) as mock_l1:
            result = await node(state)

        # Pre-destroy ran and the LLM flow was entered (not the terminal
        # "no UID available" failure).
        mock_l1.assert_awaited_once()
        assert llm.ainvoke.await_count >= 1
        assert result.get("combo_blade_part") is not None
        cache = result.get("recover_layer1_cache", {})
        assert "no UID available" not in cache.get("details", "")

    @pytest.mark.asyncio
    async def test_implicit_cross_check_host_native(self):
        """The cross-check is provider-driven, not kubectl-specific: a
        host_native attribution (HostShellProvider, is_multi_step=True)
        alongside a live blade_uid is combo evidence on the host scope too."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        blade_result = RecoverLayer1Result(status="passed", details="destroyed")
        llm = self._mock_l1_llm()
        node = make_recover_verifier(llm=llm, tools=[], registry=None)
        state = self._combo_state()
        del state["combo_native_issued"]
        state["injection_method"] = "host_native"
        with patch.object(
            rvl, "_layer1_destroy_via_provider", new_callable=AsyncMock,
            return_value=blade_result,
        ) as mock_l1:
            result = await node(state)

        mock_l1.assert_awaited_once()
        assert llm.ainvoke.await_count >= 1
        assert result.get("combo_blade_part") is not None
        cache = result.get("recover_layer1_cache", {})
        assert "[blade experiment destroyed deterministically]" in cache.get("details", "")


# ---------------------------------------------------------------------------
# Layer 2 read-only screen (problem B)
# ---------------------------------------------------------------------------

class TestLayer2ReadOnlyScreen:
    """The verifier observes; it never repairs. Mutating tool calls in a
    Layer 2 turn are refused by the recover_verifier_screener graph-edge
    node (phase1/tool_screener paradigm) with unrecovered guidance.
    Layer 1 still repairs: the read-only gate keys off recover_phase."""

    @staticmethod
    def _make():
        from chaos_agent.agent.nodes._phase_screener import make_phase_screener
        return make_phase_screener(
            capability_phase="recover_verify",
            readonly=lambda s: s.get("recover_phase", "layer1_recovery") == "layer2_verification",
            phase_duty=(
                "Layer 2 verifies recovery outcome with read-only observations "
                "only — it never repairs. Recovery actions belong to Layer 1, "
                "which has already run."
            ),
            verdict_guidance=(
                "If your observations show residual fault effects, submit your "
                "verdict as `unrecovered` and describe in details exactly which "
                "recovery action is needed. If the residual matches a recorded "
                "side effect, report it as a warning. Do not re-attempt the "
                "refused call in any form."
            ),
        )

    @staticmethod
    def _state(tool_calls, *, layer2=True):
        return {
            "messages": [AIMessage(content="", tool_calls=tool_calls)],
            "recover_phase": "layer2_verification" if layer2 else "layer1_recovery",
        }

    @pytest.mark.asyncio
    async def test_read_only_batch_passes(self):
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": ["get", "pods", "-n", "default"]},
        }])
        update = await node(state)
        assert update["screener_route"] == "pass"
        assert "messages" not in update

    @pytest.mark.asyncio
    async def test_mutating_call_refused_with_paired_feedback(self):
        node, route = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": ["delete", "pod", "pod-a", "-n", "default"]},
        }])
        update = await node(state)
        assert update["screener_route"] == "retry"
        assert route({**state, **update}) == "retry"
        fabricated = update["messages"]
        assert len(fabricated) == 1
        msg = fabricated[0]
        assert msg.tool_call_id == "c1"
        assert getattr(msg, "status", None) == "error"
        assert "readonly_phase_violation" in msg.content
        assert "unrecovered" in msg.content

    @pytest.mark.asyncio
    async def test_layer1_mutating_call_allowed(self):
        """Layer 1 repairs — the read-only gate must NOT fire while
        recover_phase is layer1_recovery (the Layer 1/2 branch is the
        whole point of the state-driven readonly predicate)."""
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": ["delete", "pod", "pod-a", "-n", "default"]},
        }], layer2=False)
        update = await node(state)
        assert update["screener_route"] == "pass"

    @pytest.mark.asyncio
    async def test_submit_verdict_tool_passes(self):
        node, _ = self._make()
        state = self._state([{
            "name": "submit_recover_verification", "id": "c1", "type": "tool_call",
            "args": {"overall": "recovered"},
        }])
        update = await node(state)
        assert update["screener_route"] == "pass"

    @pytest.mark.asyncio
    async def test_capability_probe_debug_exempt(self):
        """kubectl_read debug is the capability-probe exception (shared
        with phase1_screener): verification may create the ephemeral,
        self-gated probe pod to inspect host-level state. The classifier
        scopes it node/pod, so without the exemption Layer 2 probing
        would be wrongly refused."""
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl_read", "id": "c1", "type": "tool_call",
            "args": {"subcommand": "debug", "command": ["node/node-a"]},
        }])
        update = await node(state)
        assert update["screener_route"] == "pass"

    def test_recover_graph_wires_the_screener(self):
        """The recover graph routes tool_calls through
        recover_verifier_screener before recover_verifier_tools."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[3]
            / "src/chaos_agent/agent/graph.py"
        ).read_text(encoding="utf-8")
        assert "recover_verifier_screener" in src
        assert '"continue": "recover_verifier_screener"' in src
        assert "layer2_verification" in src


# ---------------------------------------------------------------------------
# Side-effect reconciliation contract (problems E, ①, ③)
# ---------------------------------------------------------------------------

def test_recover_prompts_carry_side_effect_reconciliation_contract():
    """Layer 1 must undo/reconcile recorded side effects; Layer 2 must
    verify each one. Keyword assertion on the loop source (the context
    assembly is inline in the node)."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[3]
        / "src/chaos_agent/agent/nodes/recover/_recover_verifier_loop.py"
    ).read_text(encoding="utf-8")
    assert "Recorded Side Effects (must be undone or reconciled)" in src
    assert "Side-Effect Reconciliation (verification duty)" in src
    assert "the overall verdict must be unrecovered" in src


# ---------------------------------------------------------------------------
# Baseline rendering (problem D; round-41 retirement: the compactor-cache
# restore bridge was removed — see TestRecoverCacheBridgeRetirement)
# ---------------------------------------------------------------------------

class TestRecoverBaselineTruncationRestore:
    def _baseline(self, stdout: str) -> dict:
        return {
            "success_count": 1,
            "total_count": 1,
            "captured_at": "2026-07-01T00:00:00",
            "source": "skill",
            "observations": [{
                "exit_code": 0,
                "description": "pod describe",
                "command": "kubectl describe pod pod-a",
                "stdout": stdout,
            }],
        }

    def test_untruncated_baseline_unchanged(self):
        from chaos_agent.agent.nodes.recover.recover_verifier import (
            _build_recover_baseline_tool_messages,
        )

        msgs = _build_recover_baseline_tool_messages(
            self._baseline("plain short output")
        )
        assert msgs
        content = msgs[-1].content
        assert "plain short output" in content
        assert "restored from compactor cache" not in content

    def test_legacy_baseline_without_total_count_renders_honest_denominator(self):
        """Recover-layer header mirrors the verifier contract: baselines
        persisted before the total_count column (#13/#10 audits) fall back
        to len(observations), never to the "1/0" cosmetic receipt."""
        from chaos_agent.agent.nodes.recover.recover_verifier import (
            _build_recover_baseline_tool_messages,
        )

        legacy = self._baseline("plain short output")
        del legacy["total_count"]
        msgs = _build_recover_baseline_tool_messages(legacy)
        assert msgs
        assert "1/1 succeeded" in msgs[-1].content
        assert "/0 succeeded" not in msgs[-1].content


class TestRecoverCacheBridgeRetirement:
    """Round-41 retirement guard: the compactor-cache restore bridge is
    GONE. Rationale (adversarially confirmed before deletion): no producer
    ever writes compactor notices into baseline observation stdout — the
    baseline executors store raw result.stdout, and compactor notices only
    live in conversation ToolMessages, a different data path — so the
    bridge's genuine branch was dead code, while its spoof branch parsed
    `Cache:` paths out of workload-controlled text (kubectl echoes pod
    annotations verbatim) and read files on the operator machine. Absent
    code cannot be spoofed, bypassed, or regress. These anchors keep the
    "untrusted stdout → filesystem read" surface from creeping back."""

    _SRC = Path(__file__).resolve().parents[3] / "src" / "chaos_agent" / \
        "agent" / "nodes" / "recover" / "_recover_layer1.py"

    def _baseline(self, stdout: str) -> dict:
        return {
            "success_count": 1,
            "total_count": 1,
            "captured_at": "2026-09-16T00:00:00",
            "source": "skill",
            "observations": [{
                "exit_code": 0,
                "description": "pod describe",
                "command": "kubectl describe pod victim -n cms-demo",
                "stdout": stdout,
            }],
        }

    def _content(self, baseline: dict) -> str:
        from chaos_agent.agent.nodes.recover.recover_verifier import (
            _build_recover_baseline_tool_messages,
        )
        msgs = _build_recover_baseline_tool_messages(baseline)
        assert msgs
        return msgs[-1].content

    def test_bridge_absent_from_source(self):
        """The restore bridge, its pin, and its read primitive are gone;
        the retirement NOTE explains why."""
        src = self._SRC.read_text(encoding="utf-8")
        assert "Restored from compactor cache" not in src
        assert "_read_baseline_cache_content" not in src
        assert "_recover_baseline_cache_path" not in src
        assert "_is_compactor_cache_path" not in src
        assert "TRUNCATION_CACHE_RE" not in src
        assert "round-41 retirement" in src

    def test_spoofed_marker_and_planted_path_render_honest_head_only(
        self, tmp_path
    ):
        """The strongest spoof (⚠️ marker copied + planted path, target file
        EXISTS on the operator machine) now renders exactly like any other
        observation: honest head echo — no parse, no read, no retrieval-path
        advertisement, no degraded-branch wording, no special casing."""
        secret = tmp_path / "secret.txt"
        secret.write_text("OPERATOR SECRET\n" * 500, encoding="utf-8")
        stdout = (
            "Name: victim-pod\n"
            "Annotations: ops.note=rotate policy: logs ⚠️ TRUNCATED at 2GB, "
            "Cache: " + str(secret) + "\n"
        )
        content = self._content(self._baseline(stdout))
        assert "OPERATOR SECRET" not in content  # no read — no bridge at all
        assert "Restored from compactor cache" not in content
        assert f"full output at {secret}" not in content
        assert "evidence incomplete" not in content
        assert "Cache: " + str(secret) in content  # honest raw head echo

    def test_marker_text_gets_same_oversize_notice_as_any_text(self):
        """Marker-bearing text >1500 chars is CONTENT, not a notice: same
        head cap + shared baseline-evidence notice as any other oversized
        observation — no branch on marker presence."""
        stdout = (
            "app log line one\nCache: /etc/passwd\n" + "x" * 2000
            + "\n⚠️ OUTPUT_TRUNCATED: output was reduced (original 20000bytes)"
            + "\nFull output cached at: /nowhere/ab12cd34.txt"
        )
        content = self._content(self._baseline(stdout))
        assert "Restored from compactor cache" not in content
        assert "evidence incomplete" not in content
        assert f"{len(stdout)} characters" in content  # shared honest notice
        assert "state.baseline_data" in content


# ---------------------------------------------------------------------------
# R3: recover_verifier hands a materialized fault handle to provider.recover
# ---------------------------------------------------------------------------

class TestRecoverVerifierHandleSeam:
    """The deterministic recover entry routes through the registry's recover
    dispatch and hands the dispatched IDENTITY handle to ``provider.recover``:
    ``materialize_fault_handle(state)`` (with legacy hydration for pre-handle
    checkpoints) — or, for a combo task, the experiment claim that outranks
    the native attribution."""

    def _fake_provider(self, captured: dict):
        from chaos_agent.agent.providers.base import RecoverResult

        class _Fake:
            async def recover(self, state, handle, **kwargs):
                captured["handle"] = handle
                captured["kwargs"] = kwargs
                # Phase-6: mirrors the real providers — the handle is the
                # single identity source (experiment_uid rendered from
                # handle['value'], empty for UID-less handles).
                return RecoverResult(
                    recovered=True, level="recovered",
                    experiment_uid=str((handle or {}).get("value") or ""),
                )
        return _Fake()

    def _patch_dispatch(self, rvl, captured):
        """Install the fake provider while keeping the REAL identity
        derivation (the registry dispatch) in the loop — the hydration chain
        stays covered end-to-end."""
        from chaos_agent.agent.providers import FaultProviderRegistry

        fake = self._fake_provider(captured)
        return patch.object(
            rvl,
            "_resolve_recover_dispatch",
            side_effect=lambda state: (
                fake,
                FaultProviderRegistry.resolve_fault_dispatch(state)[1],
            ),
        )

    @pytest.mark.asyncio
    async def test_explicit_handle_passes_through(self):
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        captured: dict = {}
        handle = {"kind": "experiment_uid", "value": "uid-1", "method": "host_blade"}
        state = {
            "task_id": "t1", "experiment_uid": "uid-1",
            "fault_handle": handle, "messages": [],
        }
        with self._patch_dispatch(rvl, captured):
            await recover_verifier(state)
        assert captured["handle"] == handle

    @pytest.mark.asyncio
    async def test_legacy_blade_checkpoint_hydrates_handle(self):
        """Pre-handle checkpoint (blade_uid only) → registry hydration builds
        the blade handle; the provider never sees a bare None."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        captured: dict = {}
        state = {
            "task_id": "t1", "experiment_uid": "abc123",
            "injection_method": "kubectl_exec", "messages": [],
        }
        with self._patch_dispatch(rvl, captured):
            await recover_verifier(state)
        assert captured["handle"] == {
            "kind": "experiment_uid", "value": "abc123", "method": "kubectl_exec",
        }

    @pytest.mark.asyncio
    async def test_legacy_native_checkpoint_hydrates_handle(self):
        """UID-less native attribution (no blade_uid anywhere) still yields a
        handle — the whole reason the seam exists."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        captured: dict = {}
        state = {
            "task_id": "t1", "experiment_uid": "",
            "injection_method": "kubectl_native", "messages": [],
        }
        with self._patch_dispatch(rvl, captured):
            await recover_verifier(state)
        assert captured["handle"] == {"kind": "native", "method": "kubectl_native"}

    @pytest.mark.asyncio
    async def test_no_attribution_still_calls_with_none(self):
        """No attribution facts at all → materialize returns None and the node
        still delegates (skipped verdict comes from the provider, unchanged)."""
        from chaos_agent.agent.nodes.recover import _recover_verifier_loop as rvl

        captured: dict = {}
        state = {"task_id": "t1", "experiment_uid": "", "messages": []}
        with self._patch_dispatch(rvl, captured):
            await recover_verifier(state)
        assert captured["handle"] is None

    @pytest.mark.asyncio
    async def test_combo_dispatches_experiment_identity_not_native(self):
        """Combo task: attribution is native, yet the dispatch identity is the
        EXPERIMENT claim (the deterministic destroy must reach the experiment
        carrier) — the ownership-order contract of ``resolve_fault_dispatch``.
        """
        from chaos_agent.agent.providers import FaultProviderRegistry

        state = {
            "task_id": "t1", "experiment_uid": "uid-combo",
            "injection_method": "kubectl_native",
            "combo_native_issued": True, "messages": [],
        }
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(state)
        assert provider.handle_kind == "experiment_uid"
        assert identity == {
            "kind": "experiment_uid", "value": "uid-combo", "method": "kubectl_native",
        }


class TestUnverifiedRecoveryFinalize:
    """Recovery unconfirmed: honest ignorance is not RECOVERY_FAILED.

    fail_state writes a RECOVERY_FAILED diagnostic that directs operators to
    debug the recovery chain — but "unverified" means the observation channel
    was unavailable, and the right follow-up is to restore observability and
    re-confirm. The verdict must flow through without a failure_reason.
    """

    @pytest.mark.asyncio
    async def test_unverified_recovery_no_fail_state(self):
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): unknown - metrics query forbidden (403)\n"
            "- Overall: unverified\n"
            "- Warnings: observation channel unavailable"
        )
        mock_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t-uv",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 2,
                "layer2_context_added": True,
                "recover_phase": "layer2_verification",
            }
            result = await node(state)
            fin = await _drive_finalize(state, result)
            # Honest verdict: never claim recovery without evidence...
            assert fin["result"]["recovered"] is False
            assert fin["result"]["recovery_level"] == "unverified"
            assert fin["recover_verification"]["level"] == "unverified"
            # ...but no RECOVERY_FAILED diagnostic either — unconfirmed is not
            # failed (operators should re-observe, not debug the chain).
            assert not fin.get("failure_detail")
            assert not fin.get("error")

    @pytest.mark.asyncio
    async def test_unrecovered_recovery_still_records_failure(self):
        """Counter-evidence (fault still present) keeps the failure signal —
        the unverified exemption must not swallow real recovery failures."""
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_VERIFICATION_RESULT:\n"
            "- Layer1 (blade_destroy): passed - success\n"
            "- Layer2 (fault-specific): failed - CPU still at 95%\n"
            "- Overall: unrecovered\n"
            "- Warnings: fault persists"
        )
        mock_response.tool_calls = []

        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)

        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        with patch("chaos_agent.agent.nodes.recover._recover_verifier_loop._layer1_destroy_via_provider") as mock_l1:
            mock_l1.return_value = RecoverLayer1Result(status="passed", details="ok", raw_output="ok")
            state = {
                "task_id": "t-ur",
                "experiment_uid": "abc",
                "skill_name": "cpu-stress",
                "kubeconfig": "",
                "verifier_loop_count": 3,
                "layer2_context_added": True,
                "recover_phase": "layer2_verification",
                # Retry-recovery already consumed (marker in message history)
                # so l2=failed goes straight to fail_state, not loop-back.
                "messages": [HumanMessage(content="recovery retry already executed")],
            }
            result = await node(state)
            fin = await _drive_finalize(state, result)
            assert fin["result"]["recovered"] is False
            assert fin.get("failure_detail"), "real recovery failure keeps its diagnostic"


# ---------------------------------------------------------------------------
# truncation-governance-consistency: baseline injection notices follow the
# shared three-field contract (marker + honest size + retrieval guidance),
# and the cache-path parsing is the shared constant's duality.
# ---------------------------------------------------------------------------

class TestBaselineInjectionNoticeContract:
    def _baseline(self, stdout: str) -> dict:
        return {
            "success_count": 1,
            "total_count": 1,
            "captured_at": "2026-07-01T00:00:00",
            "source": "skill",
            "observations": [{
                "exit_code": 0,
                "description": "pod describe",
                "command": "kubectl describe pod pod-a",
                "stdout": stdout,
            }],
        }

    def test_recover_side_notice_three_fields(self, tmp_path):
        """A >1500-char obs: the injected content carries the shared notice
        — marker, honest size, retrieval guidance (state.baseline_data)."""
        from chaos_agent.agent.nodes.recover.recover_verifier import (
            _build_recover_baseline_tool_messages,
        )
        stdout = "line\n" * 600  # > 1500 chars, no TRUNCATED marker
        msgs = _build_recover_baseline_tool_messages(self._baseline(stdout))
        content = msgs[-1].content
        assert "⚠️ TRUNCATED" in content
        assert f"{len(stdout)} characters" in content   # honest size
        assert "state.baseline_data" in content       # retrieval guidance

    def test_verify_side_notice_three_fields(self):
        """Verify-side baseline injection: same shared notice, with the
        state.baseline_data retrieval guidance."""
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_baseline_tool_messages,
        )
        baseline = {
            "success_count": 1,
            "total_count": 1,
            "captured_at": "2026-07-01T00:00:00",
            "source": "skill",
            "observations": [{
                "exit_code": 0,
                "description": "pod describe",
                "command": "kubectl describe pod pod-a",
                "stdout": "line\n" * 600,  # > 1500 chars
            }],
        }
        msgs = _build_baseline_tool_messages(
            baseline, fault_target="app", fault_action="cpu",
            injection_parsed=None,
        )
        tool_contents = [
            m.content for m in msgs if getattr(m, "type", "") == "tool"
        ]
        joined = "\n".join(tool_contents)
        assert "⚠️ TRUNCATED" in joined
        obs_len = len(baseline["observations"][0]["stdout"])
        assert f"{obs_len} characters" in joined
        assert "state.baseline_data" in joined


class TestGraftBoundaryDeclaration:
    """Anchor the inject→recover graft boundary declaration.

    Openspec phase-boundary-declaration; #29 first-run evidence
    (recover-98caf0cd msg[7]-[11]): three ``kubectl_read`` calls copied
    from the grafted inject history were rejected by ToolNode before
    self-correction. The declaration rides the Layer 1 state write
    (``msg_list``), so it reaches every later L1 iteration, the Layer 2
    message stream (built from ``state["messages"]``, L1040 of the loop),
    and the task-JSON audit record — unlike the re-built expired-context
    note it carries no ``NO_SESSION_MARKER``.
    """

    _DECL_MARKER = "**PHASE BOUNDARY"

    def _grafted_history(self) -> list:
        """A minimal stand-in for the completed inject task's history."""
        return [
            HumanMessage(content="Injected cpu fullload on pod/test-pod"),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl_read",
                "args": {"subcommand": "get", "v_args": "pod test-pod"},
                "id": "graft_call_1", "type": "tool_call",
            }]),
        ]

    def _base_state(self) -> dict:
        return {
            "task_id": "t1",
            "experiment_uid": "",
            "skill_name": "pod-cpu-fullload",
            "kubeconfig": "/path/to/config",
            "verifier_loop_count": 0,
            "messages": self._grafted_history(),
            "target": {"namespace": "default", "names": ["test-pod"]},
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "inject_context": "Injected cpu fullload on pod/test-pod",
        }

    @staticmethod
    def _llm_with(responses: list) -> AsyncMock:
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(side_effect=responses)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)
        return mock_llm

    @pytest.mark.asyncio
    async def test_layer1_state_write_carries_declaration(self):
        """Tool-call branch: msg_list = declaration → expired note → task context → response."""
        mock_l1_response = MagicMock()
        mock_l1_response.content = ""
        mock_l1_response.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default"},
            "id": "l1_call_1",
        }]
        mock_l1_response.additional_kwargs = {}

        mock_llm = self._llm_with([mock_l1_response])
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)
        result = await node(self._base_state())

        msgs = result.get("messages", [])
        assert msgs, "tool-call iteration must persist messages"
        first = msgs[0]
        assert isinstance(first, HumanMessage)
        decl = first.content
        # Wrapped per the kickoff precedent: a phase-transition directive.
        assert "<system-reminder>" in decl and "</system-reminder>" in decl
        # The three axes are locatable in the declaration text.
        assert self._DECL_MARKER in decl
        assert "RECOVERY task" in decl                    # role axis
        assert "BEFORE or DURING injection" in decl       # evidence-tense axis
        assert "tools currently bound" in decl            # tool-surface axis
        # Reading order: past (grafted history) → boundary → present.
        # The expired-context note and the Layer 1 task context come after.
        contents = [str(m.content) for m in msgs]
        assert any("EXPIRED" in c for c in contents[1:])
        assert any("Recovery" in c or "Fault" in c for c in contents[1:])
        # Persistence semantics: no NO_SESSION_MARKER — the declaration
        # must land in the task JSON exactly once (audit-record contract).
        assert not getattr(first, "additional_kwargs", {}).get("_no_session")

    @pytest.mark.asyncio
    async def test_layer1_llm_sees_declaration_after_grafted_history(self):
        """The LLM call itself: SystemMessage → grafted history → declaration."""
        mock_l1_response = MagicMock()
        mock_l1_response.content = ""
        mock_l1_response.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod -n default"},
            "id": "l1_call_1",
        }]
        mock_l1_response.additional_kwargs = {}

        mock_llm = self._llm_with([mock_l1_response])
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)
        await node(self._base_state())

        sent = mock_llm.ainvoke.call_args[0][0]
        # sent[0] is the system prompt; sent[1:3] is the grafted history.
        decl_positions = [
            i for i, m in enumerate(sent)
            if isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
        ]
        assert decl_positions, "declaration missing from the LLM call"
        assert decl_positions[0] >= 3, (
            "declaration must sit AFTER the grafted history "
            f"(found at {decl_positions[0]}, grafted history ends at 2)"
        )
        # And BEFORE the Layer 1 task context message.
        task_ctx_positions = [
            i for i, m in enumerate(sent)
            if isinstance(m, HumanMessage) and self._DECL_MARKER not in str(m.content)
        ]
        if task_ctx_positions:
            assert decl_positions[0] < task_ctx_positions[-1]

    @pytest.mark.asyncio
    async def test_layer2_first_iteration_inherits_declaration(self):
        """Layer 2 builds from state.messages — the declaration rides along."""
        # L1 turn 1: tool call (persist declaration into state).
        mock_l1_response = MagicMock()
        mock_l1_response.content = ""
        mock_l1_response.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod test-pod -n default"},
            "id": "l1_call_1",
        }]
        mock_l1_response.additional_kwargs = {}
        # L1 turn 2 (after the tool result): final execution text.
        mock_l1_final = MagicMock()
        mock_l1_final.content = (
            "RECOVERY_EXECUTION_RESULT:\n- Status: success\n"
            "- Actions: removed the fault\n- Details: none"
        )
        mock_l1_final.tool_calls = []
        mock_l1_final.additional_kwargs = {}
        # L2 turn 1: a verification tool call (keeps the loop alive).
        mock_l2_tool = MagicMock()
        mock_l2_tool.content = ""
        mock_l2_tool.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod -n default"},
            "id": "l2_call_1",
        }]
        mock_l2_tool.additional_kwargs = {}

        mock_llm = self._llm_with([mock_l1_response, mock_l1_final, mock_l2_tool])
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        state1 = self._base_state()
        result1 = await node(state1)
        tool_msg = MagicMock()
        tool_msg.name = "kubectl"
        tool_msg.content = "Running"
        state2 = {
            **state1,
            "verifier_loop_count": 1,
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result1.get("recover_layer1_cache"),
            "messages": result1.get("messages", []) + [tool_msg],
        }
        result2 = await node(state2)
        assert result2.get("recover_phase") == "layer2_verification"

        # LangGraph's add_messages reducer APPENDS result2's [response] onto
        # state2.messages — it does not replace them. The declaration that
        # iteration 1 persisted is still in the stream.
        state3 = {
            **state2,
            "verifier_loop_count": 2,
            "recover_phase": "layer2_verification",
            "layer2_context_added": False,
            "layer1_iteration_count": 1,
            "recover_layer1_cache": result2.get("recover_layer1_cache"),
            "messages": state2["messages"] + result2.get("messages", []) + [MagicMock()],
        }
        await node(state3)

        # The LAST LLM call is Layer 2's first iteration: its message list
        # is built from state["messages"] and must contain the declaration
        # the Layer 1 write persisted.
        sent_l2 = mock_llm.ainvoke.call_args_list[-1][0][0]
        assert any(
            isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
            for m in sent_l2
        ), "Layer 2 first iteration lost the graft boundary declaration"

    def test_injection_point_is_the_graph_node_not_entry_sites(self):
        """Every recover entry path (CLI ×2, L4, HTTP/TUI streaming — the
        baseline_messages bootstrap sites) converges on the recover graph's
        message stream; the declaration is injected inside
        recover_verifier_loop — the single graph-internal choke point every
        entry path must cross. Structural assertion: the node module
        sources the constant, the entry sites stay declaration-free (they
        graft history only).
        """
        import inspect

        import chaos_agent.agent.nodes.recover._recover_verifier_loop as rvl
        import chaos_agent.cli.runner as runner_mod
        import chaos_agent.l4.recovery as recovery_mod
        from chaos_agent.agent.prompts.boundary import (
            INJECT_TO_RECOVER_BOUNDARY_DECLARATION,
        )

        assert INJECT_TO_RECOVER_BOUNDARY_DECLARATION in vars(rvl).values() or hasattr(
            rvl, "INJECT_TO_RECOVER_BOUNDARY_DECLARATION"
        )
        assert "INJECT_TO_RECOVER_BOUNDARY_DECLARATION" not in inspect.getsource(runner_mod)
        assert "INJECT_TO_RECOVER_BOUNDARY_DECLARATION" not in inspect.getsource(recovery_mod)

    @pytest.mark.asyncio
    async def test_layer2_first_iteration_injects_when_l1_was_deterministic(self):
        """Deterministic Layer 1 (provider destroy, no LLM) writes no
        messages, so Layer 2's first iteration is the recovery's FIRST
        LLM loop — the graft declaration must be injected there, after
        the grafted history and before the expired-context note, and ride
        the Layer 2 state write (task-JSON audit contract)."""
        mock_l2_tool = MagicMock()
        mock_l2_tool.content = ""
        mock_l2_tool.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod -n default"},
            "id": "l2_call_det_1",
        }]
        mock_l2_tool.additional_kwargs = {}

        mock_llm = self._llm_with([mock_l2_tool])
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        # Deterministic shape: the grafted inject history is the ONLY
        # content of state["messages"] — Layer 1 never appended anything.
        state = {
            **self._base_state(),
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "layer2_context_added": False,
            "recover_layer1_type": "deterministic",
            "recover_layer1_cache": {
                "status": "passed", "details": "destroyed", "raw_output": "",
            },
        }
        result = await node(state)

        # The Layer 2 state write persists the declaration exactly once,
        # without a NO_SESSION_MARKER (audit-record contract).
        msgs = result.get("messages", [])
        decls = [
            m for m in msgs
            if isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
        ]
        assert len(decls) == 1, (
            f"deterministic-L1 shape must declare exactly once, got {len(decls)}"
        )
        assert not getattr(decls[0], "additional_kwargs", {}).get("_no_session")
        # Head-of-block ordering: the declaration rides FIRST in the Layer 2
        # write, so the next iteration reads [grafted history] → declaration
        # → [current-recovery context] — "the history above" stays the
        # completed inject task, never the current task's own context.
        assert msgs[0] is decls[0], (
            "declaration must lead the Layer 2 state write block"
        )
        # Reading order: the declaration precedes the expired-context note.
        contents = [str(m.content) for m in msgs]
        decl_idx = contents.index(str(decls[0].content))
        expired = [i for i, c in enumerate(contents) if "EXPIRED" in c]
        if expired:
            assert decl_idx < expired[0]

        # The LLM saw it after the grafted history (SystemMessage + 2
        # grafted messages) and before the task instructions.
        sent = mock_llm.ainvoke.call_args[0][0]
        sent_decls = [
            i for i, m in enumerate(sent)
            if isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
        ]
        assert len(sent_decls) == 1
        assert sent_decls[0] >= 3

    @pytest.mark.asyncio
    async def test_layer2_does_not_redeclare_when_already_present(self):
        """Idempotence: when the LLM-driven Layer 1 already persisted the
        declaration into state["messages"], the Layer 2 marker probe finds
        it and injects nothing — exactly one declaration per recovery
        context, whichever layer landed it."""
        from chaos_agent.agent.prompts.boundary import (
            INJECT_TO_RECOVER_BOUNDARY_DECLARATION,
        )
        from chaos_agent.agent.prompts.reminder import wrap_system_reminder

        mock_l2_tool = MagicMock()
        mock_l2_tool.content = ""
        mock_l2_tool.tool_calls = [{
            "name": "kubectl", "args": {"subcommand": "get", "v_args": "pod -n default"},
            "id": "l2_call_idem_1",
        }]
        mock_l2_tool.additional_kwargs = {}

        mock_llm = self._llm_with([mock_l2_tool])
        node = make_recover_verifier(llm=mock_llm, tools=["kubectl"], registry=None)

        state = {
            **self._base_state(),
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "layer2_context_added": False,
            "recover_layer1_type": "llm_driven",
            "recover_layer1_cache": {
                "status": "passed", "details": "", "raw_output": "",
            },
            # The LLM-driven Layer 1 already persisted its declaration.
            "messages": self._grafted_history() + [HumanMessage(content=wrap_system_reminder(
                INJECT_TO_RECOVER_BOUNDARY_DECLARATION
            ))],
        }
        result = await node(state)

        # The LLM call carried the inherited declaration — and only it.
        sent = mock_llm.ainvoke.call_args[0][0]
        sent_decls = [
            m for m in sent
            if isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
        ]
        assert len(sent_decls) == 1, "a second declaration leaked into the L2 call"
        # The Layer 2 state write added no fresh copy.
        fresh_decls = [
            m for m in result.get("messages", [])
            if isinstance(m, HumanMessage) and self._DECL_MARKER in str(m.content)
        ]
        assert not fresh_decls, "L2 re-declared an already-declared seam"


# ---------------------------------------------------------------------------
# truncation-debt-cleanup (3.2): side-effect injection speaks the shared
# truncation dialect in both recover layers
# ---------------------------------------------------------------------------

class TestSideEffectInjectionSharedDialect:
    """Oversized side-effect entries (>500 chars) injected into BOTH recover
    layers must speak the shared truncation dialect — a both-ends preview
    (head/tail anchors survive) plus the state-evidence notice (marker +
    honest original size + retrieval guidance back to state.side_effects) —
    replacing the old head-only ``[:500] + "...(truncated)"`` cut that hid
    the tail and named no way back. Entries within the 500-char budget are
    injected VERBATIM (zero truncation noise)."""

    @staticmethod
    def _oversized_side_effect() -> dict:
        # ~1.3K chars serialized — well over the 500-char injection cut.
        # The head carries the resource anchor + leading A-run; the tail
        # carries the trailing Z-run: the both-ends assertions key on them.
        return {
            "resource": "configmap/app-config",
            "before": "A" * 600,
            "after": "Z" * 600,
        }

    @staticmethod
    def _mock_llm() -> AsyncMock:
        mock_response = MagicMock()
        mock_response.content = (
            "RECOVERY_EXECUTION_RESULT:\n"
            "- Status: success\n"
            "- Actions: restored the mutated configmap\n"
            "- Details: ok"
        )
        mock_response.tool_calls = []
        mock_llm = AsyncMock()
        mock_llm.ainvoke = AsyncMock(return_value=mock_response)
        mock_llm.bind_tools = MagicMock(return_value=mock_llm)
        mock_llm.bind = MagicMock(return_value=mock_llm)
        return mock_llm

    @staticmethod
    def _message_texts(call) -> list:
        # ainvoke(messages) — flatten message contents for content search.
        messages = call.args[0] if call.args else call.kwargs.get("messages", [])
        return [str(m.content) for m in messages if hasattr(m, "content")]

    def _assert_shared_dialect(self, text: str, original_len: int, entry_key: str):
        # Three-field notice contract.
        assert "⚠️ TRUNCATED (state evidence):" in text
        assert f"(original {original_len} characters)." in text
        assert "state.side_effects" in text
        # Both-ends preview: head and tail anchors survive, the middle run
        # is quantified as elided exactly once.
        assert '"resource": "configmap/app-config"' in text
        assert "A" * 300 in text      # head window keeps the leading A-run
        assert "Z" * 100 in text      # tail window keeps the trailing Z-run
        assert "A" * 400 not in text  # the middle is actually elided
        assert "Z" * 200 not in text
        assert text.count("chars elided") == 1
        # The old head-only dialect is gone.
        assert "...(truncated)" not in text
        # Per-entry injection growth is BOUNDED (design risk register:
        # preview 500 + marker + notice ≈ 750 chars vs the old 514) —
        # pinned so a future notice-wording change cannot silently balloon
        # the per-entry prompt cost.
        entry_line = next(
            ln for ln in text.split("\n") if ln.startswith(f"- {entry_key}: ")
        )
        assert len(entry_line) <= 800, (
            f"per-entry injection cost unbounded: {len(entry_line)} chars"
        )

    @pytest.mark.asyncio
    async def test_layer1_oversized_side_effect_shared_dialect(self):
        """Layer 1 human message: the side-effects section speaks the shared
        dialect when an entry exceeds the 500-char cut."""
        se_val = self._oversized_side_effect()
        original_len = len(json.dumps(se_val, ensure_ascii=False))
        mock_llm = self._mock_llm()
        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        state = {
            "task_id": "t1",
            "experiment_uid": "",  # no deterministic destroy → LLM-driven L1
            "skill_name": "config-patch",
            "kubeconfig": "",
            "verifier_loop_count": 0,
            "inject_context": "Patched configmap app-config during injection",
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "side_effects": {"patched-config": se_val},
        }
        await node(state)

        # The FIRST ainvoke is the Layer 1 recovery call — its messages
        # carry the human content with the side-effects section.
        assert mock_llm.ainvoke.await_count >= 1
        first_texts = self._message_texts(mock_llm.ainvoke.await_args_list[0])
        se_text = next(t for t in first_texts if "## Recorded Side Effects" in t)
        assert "- patched-config: " in se_text
        self._assert_shared_dialect(se_text, original_len, "patched-config")

    @pytest.mark.asyncio
    async def test_layer2_oversized_side_effect_shared_dialect(self):
        """Layer 2 verification context (first iteration): the same shared
        dialect, the same contract — the two layers inject identically."""
        se_val = self._oversized_side_effect()
        original_len = len(json.dumps(se_val, ensure_ascii=False))
        mock_llm = self._mock_llm()
        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        state = {
            "task_id": "t1",
            "experiment_uid": "",
            "skill_name": "config-patch",
            "kubeconfig": "",
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "layer2_context_added": False,  # first L2 iteration → context built
            "side_effects": {"patched-config": se_val},
        }
        await node(state)

        assert mock_llm.ainvoke.await_count >= 1
        first_texts = self._message_texts(mock_llm.ainvoke.await_args_list[0])
        se_text = next(t for t in first_texts if "## Side-Effect Reconciliation" in t)
        assert "- patched-config: " in se_text
        self._assert_shared_dialect(se_text, original_len, "patched-config")

    @pytest.mark.asyncio
    async def test_within_budget_side_effect_verbatim(self):
        """Entries within the 500-char budget inject VERBATIM — no marker,
        no elision noise, the full record stays inline."""
        small_val = {"resource": "configmap/app-config", "note": "x" * 200}
        serialized = json.dumps(small_val, ensure_ascii=False)
        assert len(serialized) < 500
        mock_llm = self._mock_llm()
        node = make_recover_verifier(llm=mock_llm, tools=[], registry=None)

        state = {
            "task_id": "t1",
            "experiment_uid": "",
            "skill_name": "config-patch",
            "kubeconfig": "",
            "verifier_loop_count": 1,
            "recover_phase": "layer2_verification",
            "layer1_iteration_count": 1,
            "layer2_context_added": False,
            "side_effects": {"small-note": small_val},
        }
        await node(state)

        first_texts = self._message_texts(mock_llm.ainvoke.await_args_list[0])
        se_text = next(t for t in first_texts if "## Side-Effect Reconciliation" in t)
        assert f"- small-note: {serialized}" in se_text  # verbatim, single line
        assert "TRUNCATED" not in se_text
        assert "elided" not in se_text


# ---------------------------------------------------------------------------
# finalize_recover_verification: residual liability sweep (B76 review G)
# ---------------------------------------------------------------------------

class TestFinalizeResidualLiabilitySweep:
    """The recover finale's safety net: destroy every live experiment the
    task still owes a destroy for, EXCEPT the identity UID the main Layer-1
    flow (and the retry below) already owns. Whichever seam let a superseded
    experiment survive (approval-time destroy failure, an execute-replan
    build-on-top, compaction blinding the destroy whitelist), THIS is the
    last point where the framework still holds the full ownership record."""

    def _state(self, *, owned, destroy_output='{"code":200,"success":true}'):
        from chaos_agent.agent.nodes.verify._verifier_submit import (
            SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )

        submit_msg = AIMessage(
            content="Recovery verified.",
            tool_calls=[{
                "name": SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
                "args": {
                    "overall": "recovered",
                    "layer2_status": "passed",
                    "layer2_details": "fault effect absent, baseline restored",
                },
                "id": "tc_submit",
                "type": "tool_call",
            }],
        )
        tool_msg = ToolMessage(
            content="Recovery verdict recorded.",
            tool_call_id="tc_submit",
            name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )
        state = {
            "task_id": "t-sweep",
            "experiment_uid": "uid-main",
            "injection_method": "host_blade",
            "owned_experiment_uids": list(owned),
            "skill_name": "pod-cpu-fullload",
            "kubeconfig": "",
            "verifier_loop_count": 3,
            "messages": [submit_msg, tool_msg],
            "recover_layer1_cache": {
                "status": "passed",
                "details": "destroyed",
                "raw_output": "",
                "system_prompt": "test",
            },
            "recover_layer2_first": False,
        }
        return state, destroy_output

    def _patch_destroy(self, monkeypatch, destroy_output):
        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method("host_blade")
        calls: list[str] = []

        async def _fake_destroy(uid, kubeconfig=""):
            calls.append(uid)
            return destroy_output

        monkeypatch.setattr(provider, "layer1_raw_destroy", _fake_destroy)
        return calls

    @pytest.mark.asyncio
    async def test_finalize_sweeps_residual_experiments_and_retires(self, monkeypatch):
        """The superseded experiment that survived the approval seam is
        destroyed at the finale; the identity UID stays excluded (the main
        Layer-1 flow owns it — the sweep must not race the destroy already
        recorded in the Layer-1 cache). Round-32 death-wing completion:
        the FULL verdict settles the proven identity UID too — retired
        carries BOTH wings (sweep's destroy + verdict's proof), so the
        persisted ledger ends balanced instead of haunting query_active."""
        state, output = self._state(owned=["uid-old", "uid-main"])
        calls = self._patch_destroy(monkeypatch, output)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-old"]
        assert fin["retired_experiment_uids"] == ["uid-main", "uid-old"]
        # A successful sweep leaves the verdict itself untouched.
        assert fin["recover_verification"]["level"] == "recovered"
        assert not any(
            "survived the final destroy sweep" in w
            for w in fin["recover_verification"].get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_finalize_failed_sweep_warns_without_false_retire(self, monkeypatch):
        """False-retire guard at the finale: an Error destroy output (NOT
        the not-found form — that one routes through the sweep's status
        recheck valve) keeps the UID in the liability record and surfaces a
        warning (the verdict stays recoverable but the residue is on the
        record for the operator). Round-32 death-wing completion must NOT
        swallow the failed residual: a destroy error is LIVE evidence, and
        the full verdict settles only the PROVEN owned — the identity UID
        (Layer-1 passed) retires, the failed residual never does."""
        state, output = self._state(
            owned=["uid-old", "uid-main"],
            destroy_output="Error: blade daemon unreachable",
        )
        calls = self._patch_destroy(monkeypatch, output)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-old"]
        # uid-old: failed destroy — stays LIVE in the ledger despite the
        # full verdict (false-settle guard). uid-main: destroy proven by
        # the passed Layer-1 — the death wing settles exactly it.
        assert fin["retired_experiment_uids"] == ["uid-main"]
        assert any(
            "survived the final destroy sweep" in w
            for w in fin["recover_verification"]["warnings"]
        )

    @pytest.mark.asyncio
    async def test_finalize_not_found_failure_rechecks_status_and_retires(
        self, monkeypatch,
    ):
        """B76 review I2/I2b — the convergence valve at the finale: a
        record-not-found destroy failure (the repeat-destroy signature of an
        already-dead experiment) triggers the carrier's status recheck; a
        proven death retires instead of warning forever."""
        state, output = self._state(
            owned=["uid-old", "uid-main"],
            destroy_output="Error: record not found",
        )
        calls = self._patch_destroy(monkeypatch, output)
        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method("host_blade")

        async def _destroyed_true(uid, kubeconfig=""):
            return True

        monkeypatch.setattr(provider, "experiment_destroyed", _destroyed_true)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-old"]
        # both wings settled: the recheck-proven residual (sweep) + the
        # Layer-1-proven identity UID (round-32 death wing)
        assert fin["retired_experiment_uids"] == ["uid-main", "uid-old"]
        assert not any(
            "survived the final destroy sweep" in w
            for w in fin["recover_verification"].get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_finalize_single_experiment_task_is_untouched(self, monkeypatch):
        """Normal single-experiment shape: the live set minus the identity
        UID is empty — zero destroy dispatches, no warnings (the sweep is a
        safety net, not a behaviour change). The single retire write is the
        round-32 death wing: the passed Layer-1 IS the destroy proof the
        persisted ledger lacked — without it the recovered row's
        ``owned − retired`` stays unbalanced and haunts query_active
        forever."""
        state, output = self._state(owned=["uid-main"])
        calls = self._patch_destroy(monkeypatch, output)

        fin = await _drive_finalize(state, {})

        assert calls == []
        assert fin["retired_experiment_uids"] == ["uid-main"]
        assert fin["recover_verification"]["level"] == "recovered"


# ---------------------------------------------------------------------------
# finalize_recover_verification: identity-UID exemption gate (B76 review M2)
# ---------------------------------------------------------------------------

class TestFinalizeIdentityExemptionGate:
    """The identity UID's exemption from the final sweep is EARNED by the
    Layer-1 verdict, not unconditional (B76 review M2 — the exempt orphan).

    ``exclude_uid`` exists to prevent a duplicate destroy of the experiment
    the main Layer-1 flow owns. A failed/error/unknown Layer-1 means that
    ownership never paid out — the sweep must CATCH the identity UID
    instead of skipping it. The pre-fix chain: destroy failed (experiment
    proven alive), Layer 2 passed, retry gate L2-only (never fires), sweep
    unconditionally exempt → the experiment ended UNDESTROYED and UNWARNED
    four ways while the task closed as "recovered"."""

    def _state(self, *, layer1_status: str, owned=("uid-main",)):
        from chaos_agent.agent.nodes.verify._verifier_submit import (
            SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )

        submit_msg = AIMessage(
            content="Recovery verified.",
            tool_calls=[{
                "name": SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
                "args": {
                    "overall": "recovered",
                    "layer2_status": "passed",
                    "layer2_details": "fault effect absent, baseline restored",
                },
                "id": "tc_submit",
                "type": "tool_call",
            }],
        )
        tool_msg = ToolMessage(
            content="Recovery verdict recorded.",
            tool_call_id="tc_submit",
            name=SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
        )
        return {
            "task_id": "t-exempt",
            "experiment_uid": "uid-main",
            "injection_method": "host_blade",
            "owned_experiment_uids": list(owned),
            "skill_name": "pod-cpu-fullload",
            "kubeconfig": "",
            "verifier_loop_count": 3,
            "messages": [submit_msg, tool_msg],
            "recover_layer1_cache": {
                "status": layer1_status,
                "details": "",
                "raw_output": "",
                "system_prompt": "test",
            },
            "recover_layer2_first": False,
        }

    def _patch_destroy(self, monkeypatch):
        from chaos_agent.agent.providers import FaultProviderRegistry

        provider = FaultProviderRegistry.resolve_by_method("host_blade")
        calls: list[str] = []

        async def _fake_destroy(uid, kubeconfig=""):
            calls.append(uid)
            return '{"code":200,"success":true}'

        monkeypatch.setattr(provider, "layer1_raw_destroy", _fake_destroy)
        return calls

    @pytest.mark.asyncio
    async def test_failed_layer1_revokes_exemption_identity_uid_swept(
        self, monkeypatch,
    ):
        """M2 MAIN SCENARIO — the main-flow destroy failed (experiment
        proven alive) but Layer 2 passed: the exemption must NOT hold. The
        sweep destroys + retires the identity UID, closing the four-way
        silent orphan."""
        state = self._state(layer1_status="failed")
        calls = self._patch_destroy(monkeypatch)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-main"]
        assert fin["retired_experiment_uids"] == ["uid-main"]
        # The verdict axes stay decoupled: Layer 2 passed, so the level is
        # recovered — the fix closes the LEDGER hole, it does not re-open
        # the "Layer-1 one-vote veto" bug (inject-e47de3e8).
        assert fin["recover_verification"]["level"] == "recovered"
        assert not any(
            "survived the final destroy sweep" in w
            for w in fin["recover_verification"].get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_error_layer1_revokes_exemption(self, monkeypatch):
        """An errored main flow (exception during the deterministic destroy)
        is equally an unproven destroy — the sweep catches the UID."""
        state = self._state(layer1_status="error")
        calls = self._patch_destroy(monkeypatch)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-main"]
        assert fin["retired_experiment_uids"] == ["uid-main"]

    @pytest.mark.asyncio
    async def test_missing_cache_defaults_unknown_and_sweeps(self, monkeypatch):
        """Legacy checkpoint without a Layer-1 cache: the finalize's default
        verdict is ``unknown`` — never a proven destroy, so the exemption
        must not hold (fail-closed for every non-proving status)."""
        state = self._state(layer1_status="unknown")
        state.pop("recover_layer1_cache")
        calls = self._patch_destroy(monkeypatch)

        fin = await _drive_finalize(state, {})

        assert calls == ["uid-main"]
        assert fin["retired_experiment_uids"] == ["uid-main"]

    @pytest.mark.asyncio
    async def test_passed_layer1_keeps_exemption_no_duplicate_destroy(
        self, monkeypatch,
    ):
        """Mirror control: a passed Layer-1 proves the destroy already
        happened — the exemption holds and the sweep must NOT dispatch a
        duplicate destroy of the identity UID. The retire write is NOT a
        duplicate destroy: it is the round-32 death wing recording the
        proof the passed Layer-1 already earned (ledger-only, no dispatch)."""
        state = self._state(layer1_status="passed")
        calls = self._patch_destroy(monkeypatch)

        fin = await _drive_finalize(state, {})

        assert calls == []
        assert fin["retired_experiment_uids"] == ["uid-main"]
        assert fin["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_skipped_layer1_keeps_exemption(self, monkeypatch):
        """Skipped = no deterministic destroy applies (UID typically empty) —
        a degenerate non-empty UID under skipped still exempts; the sweep is
        not a second opinion on the main flow's N/A verdict. The full verdict
        still settles the degenerate UID on the ledger (round-32 death wing:
        ``skipped`` itself says no experiment to destroy, so retiring the
        name is bookkeeping, not a claim of a dispatched destroy)."""
        state = self._state(layer1_status="skipped")
        calls = self._patch_destroy(monkeypatch)

        fin = await _drive_finalize(state, {})

        assert calls == []
        assert fin["retired_experiment_uids"] == ["uid-main"]

    def test_helper_predicate_is_fail_closed(self):
        """The gate's single decision table: only proving statuses exempt.
        Every non-proving vocabulary (failed/error/unknown/in_progress/
        empty/None) sweeps — pinned so a future status addition cannot
        silently inherit the exemption."""
        from chaos_agent.agent.nodes.recover._recover_finalize import (
            _sweep_exempts_identity_uid,
        )
        from chaos_agent.agent.result.verdict import Layer1Status

        # BOTH arms: the enum (what the finalize's cache-restored object
        # actually carries) and the plain string (dict payloads).
        assert _sweep_exempts_identity_uid(Layer1Status.PASSED) is True
        assert _sweep_exempts_identity_uid(Layer1Status.SKIPPED) is True
        assert _sweep_exempts_identity_uid("passed") is True
        assert _sweep_exempts_identity_uid("skipped") is True
        for status in (
            Layer1Status.FAILED,
            Layer1Status.ERROR,
            Layer1Status.UNKNOWN,
            Layer1Status.IN_PROGRESS,
            "failed",
            "error",
            "unknown",
            "in_progress",
            "",
            None,
        ):
            assert _sweep_exempts_identity_uid(status) is False, status
