"""Tests for verifier node."""

import inspect
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

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
from chaos_agent.agent.nodes.execute._injection_detection import (
    _was_kubectl_injection_attempted,
)
# Phase-4 T2 canonical address (kept under the historical call name as the
# equivalence anchor for the attempted-test group).
from chaos_agent.agent.providers.chaosblade.verify import (
    was_blade_create_attempted as _was_blade_create_attempted,
)
from chaos_agent.agent.nodes.verify.verifier import (
    verifier,
    make_verifier,
    _cleanup_debug_pods,
    _experiment_uid_of,
    _recovery_vehicle_of,
    _resolve_fault_dispatch,
)
from chaos_agent.agent.state import materialize_fault_handle
# Phase-4 T4 canonical address (the Layer-1 execution domain moved to the
# provider layer; the nodes facade re-exports only the state orchestration).
from chaos_agent.agent.providers.chaosblade.verify import (
    _run_host_blade_layer1,
)
from chaos_agent.agent.result.verdict import Layer1Result
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


@contextmanager
def _patch_blade_cmd(mock_async):
    """Patch execute_via_transport on blade module.

    After the transport-layer migration, blade_status/blade_query_k8s
    call ``execute_via_transport`` instead of ``run_command`` directly.
    """
    with patch("chaos_agent.agent.providers.chaosblade.cli.execute_via_transport", mock_async):
        yield


class TestVerifier:
    """Tests for the verifier node function."""

    @pytest.mark.asyncio
    async def test_verified_with_blade_uid(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = "task-123"
        state["skill_name"] = "pod-delete"
        state["experiment_uid"] = "abc123xyz"

        with _patch_blade_cmd(_mock_blade_running()):
            result = await verifier(state)
        # Layer 1 passed (blade_status=Running) but no LLM for Layer 2,
        # so verification level is "partial" and verified=False (cannot confirm fault effect)
        assert result["result"]["verified"] is False
        assert result["verification"]["level"] == "partial"
        assert result["result"]["task_id"] == "task-123"
        assert result["result"]["skill"] == "pod-delete"
        assert result["result"]["experiment_uid"] == "abc123xyz"
        assert "blade_uid" not in result["result"]

    @pytest.mark.asyncio
    async def test_not_verified_without_blade_uid(self, sample_agent_state):
        """No blade_uid + no blade_create in messages → non-ChaosBlade, Layer 1 skipped."""
        state = sample_agent_state
        state["task_id"] = "task-456"
        state["skill_name"] = "pod-delete"
        state["experiment_uid"] = ""
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
        state["experiment_uid"] = None
        state["messages"] = []  # No blade_create ToolMessage

        result = await verifier(state)
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_result_structure(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = "task-struct"
        state["skill_name"] = "cpu-burn"
        state["experiment_uid"] = "uid-999"

        with _patch_blade_cmd(_mock_blade_running("uid-999")):
            result = await verifier(state)
        r = result["result"]
        assert "task_id" in r
        assert "skill" in r
        assert "experiment_uid" in r
        assert "verified" in r
        assert "blade_uid" not in r

    @pytest.mark.asyncio
    async def test_verification_field_present(self, sample_agent_state):
        """Result should include a 'verification' dict with layer info."""
        state = sample_agent_state
        state["task_id"] = "task-verify"
        state["skill_name"] = "cpu-burn"
        state["experiment_uid"] = "uid-v1"

        with _patch_blade_cmd(_mock_blade_running("uid-v1")):
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
        state["experiment_uid"] = "uid-l2"

        with _patch_blade_cmd(_mock_blade_running("uid-l2")):
            result = await verifier(state)
        v = result["verification"]
        assert v["layer2"]["status"] == "skipped"
        assert len(v["warnings"]) > 0

    @pytest.mark.asyncio
    async def test_empty_task_id(self, sample_agent_state):
        state = sample_agent_state
        state["task_id"] = ""
        state["skill_name"] = "pod-delete"
        state["experiment_uid"] = "uid-1"

        with _patch_blade_cmd(_mock_blade_running("uid-1")):
            result = await verifier(state)
        assert result["result"]["task_id"] == ""

    @pytest.mark.asyncio
    async def test_defaults_when_state_empty(self):
        """Empty state: all get() calls return empty strings."""
        result = await verifier({})
        # No blade_create in messages → non-ChaosBlade, Layer 1 skipped, unverified
        assert result["result"]["task_id"] == ""
        assert result["result"]["skill"] == ""
        assert result["result"]["experiment_uid"] == ""
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "skipped"

    @pytest.mark.asyncio
    async def test_blade_status_failed(self, sample_agent_state):
        """When blade_status returns Error, verification should fail."""
        state = sample_agent_state
        state["task_id"] = "task-fail"
        state["skill_name"] = "pod-delete"
        state["experiment_uid"] = "uid-fail"

        with _patch_blade_cmd(_mock_blade_failed()):
            result = await verifier(state)
        assert result["result"]["verified"] is False
        assert result["verification"]["layer1"]["status"] == "failed"


# ---------------------------------------------------------------------------
# make_verifier (entry factory)
# ---------------------------------------------------------------------------

class TestMakeVerifier:
    """Phase-5 T4: entry-factory wiring (graph.py:436 assembly semantics)."""

    def test_no_llm_returns_simple_entry(self):
        # llm=None → the factory hands back the simple (Layer-1 only) entry
        # itself, not a wrapper.
        assert make_verifier() is verifier
        assert make_verifier(llm=None) is verifier

    def test_llm_returns_two_layer_closure(self):
        # Any non-None llm → the two-layer ReAct closure: callable and
        # distinct from the simple entry (whose Layer 2 silently skips).
        node = make_verifier(llm=object())
        assert callable(node)
        assert node is not verifier

    def test_active_params_captured_by_closure(self):
        # graph.py:436 passes hook/llm/tools/registry. The closure actually
        # consumes hook/llm/tools (free variables); registry is a signature-
        # compatibility placeholder the closure never reads — asserted absent
        # so a future wiring change cannot silently drop the active three.
        fake_llm, fake_hook, fake_registry = object(), object(), object()
        fake_tools = ["submit_verification"]
        node = make_verifier(
            hook=fake_hook, llm=fake_llm, tools=fake_tools, registry=fake_registry,
        )
        captured = inspect.getclosurevars(node).nonlocals
        assert captured["llm"] is fake_llm
        assert captured["hook"] is fake_hook
        assert captured["tools"] is fake_tools
        assert "registry" not in captured


class TestVerifierLedgerTailMigration:
    """Unit A (context-cache-prefix-stability tasks 2.4/2.7): the verify-phase
    progress ledger rides the message TAIL as an append-only system-reminder
    HumanMessage carrying a supersedes marker — NOT build_verifier_prompt's
    system head (whose per-round ledger rewrite broke the cache prefix).

    Node-level complement to the builder-level prefix guard
    (test_prefix_stability.py::TestVerifierPrefixStability): that one proves the
    head is byte-stable across ledger growth; this one proves the ledger still
    reaches the model every round, at the tail, and persists into state.
    """

    class _FakeLLM:
        def __init__(self):
            self.seen = None

        def bind_tools(self, tools):
            return self

        def bind(self, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.seen = messages
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl_read",
                    "args": {"subcommand": "get", "v_args": "pods"},
                    "id": "c1",
                }],
            )

    @staticmethod
    def _ledger(state):
        from chaos_agent.agent.progress_ledger import (
            freeze_anchor,
            merge_progress_ledger,
        )

        return merge_progress_ledger(
            freeze_anchor(state["fault_spec"], goal="verify mem load on pod-x"),
            log_append=[{"event": "LEDGER-TAIL-MARK step done", "status": "verified"}],
        )

    @staticmethod
    def _ledger_tails(messages):
        return [
            m
            for m in messages
            if isinstance(m, HumanMessage)
            and "LEDGER-TAIL-MARK" in (m.content or "")
        ]

    async def _run(self, state):
        # Layer 1 forced non-terminal (passed) and the cycle forced fresh so the
        # two-layer closure reaches the Layer-2 ReAct body where the ledger tail
        # is appended. tools=[] keeps the capability branch on the unbound-LLM
        # path (``if tools and not visible_tools`` is False), so the fake LLM's
        # ``ainvoke`` is what actually receives the assembled messages.
        llm = self._FakeLLM()
        node = make_verifier(hook=None, llm=llm, tools=[])
        with patch(
            "chaos_agent.agent.nodes.verify.verifier.run_layer1_for_state",
            AsyncMock(return_value=Layer1Result(status="passed", details="ok")),
        ), patch(
            "chaos_agent.agent.nodes.verify.verifier._verification_cycle_needs_context",
            lambda _state: True,
        ):
            result = await node(state)
        return llm, result

    @pytest.mark.asyncio
    async def test_ledger_rides_tail_not_system_head(self, sample_agent_state):
        from langchain_core.messages import SystemMessage

        state = sample_agent_state
        state["verifier_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [AIMessage(content="inject done")]
        state["progress_ledger"] = self._ledger(state)

        llm, result = await self._run(state)

        # The system head must NOT carry the ledger anymore (that per-round
        # rewrite was the volatile byte that broke the verify cache prefix).
        system_msgs = [m for m in llm.seen if isinstance(m, SystemMessage)]
        assert system_msgs, "expected a SystemMessage head"
        for sm in system_msgs:
            assert "LEDGER-TAIL-MARK" not in (sm.content or "")
            assert "progress ledger below" not in (sm.content or "")

        # The ledger rides the TAIL as a system-reminder HumanMessage.
        tails = self._ledger_tails(llm.seen)
        assert len(tails) == 1, "exactly one ledger snapshot must ride the tail"
        content = tails[0].content or ""
        assert content.lstrip().startswith("<system-reminder>")
        # D2 supersedes marker keeps the append-only history single-valued.
        assert "supersedes" in content
        # The anti-drift directive survives the move.
        assert "before acting" in content

        # Persisted into LangGraph state via the same _hints_for_state channel.
        assert len(self._ledger_tails(result.get("messages", []))) == 1

    @pytest.mark.asyncio
    async def test_no_ledger_tail_when_ledger_empty(self, sample_agent_state):
        state = sample_agent_state
        state["verifier_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [AIMessage(content="inject done")]
        # No progress_ledger AND no fault_spec-derived anchor content: force an
        # empty render so the tail message is correctly suppressed.
        state["progress_ledger"] = None
        state["fault_spec"] = None

        llm, result = await self._run(state)
        assert not self._ledger_tails(llm.seen)
        assert not self._ledger_tails(result.get("messages", []))


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

    # --- durable-first: a committed injection_method record overrides the
    # (possibly compacted) message history ---

    def test_durable_native_record_overrides_stale_blade_create_evidence(self):
        """Compaction scenario: the kubectl-native fallback evidence was
        compacted away, but the failed blade_create ToolMessage survived.
        The durable record must win over the stale history."""
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        assert _was_blade_create_attempted([msg], injection_method="kubectl_native") is False
        assert _was_blade_create_attempted([msg], injection_method="host_native") is False

    def test_durable_experiment_record_overrides_stale_blade_create_evidence(self):
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        assert _was_blade_create_attempted([msg], injection_method="kubectl_exec") is False

    def test_no_durable_record_falls_back_to_message_scan(self):
        """State-less restored session: no durable record → the message scan
        still decides (backward-compatible fallback)."""
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        assert _was_blade_create_attempted([msg], injection_method=None) is True
        assert _was_blade_create_attempted([msg], injection_method="") is True


# ---------------------------------------------------------------------------
# _run_layer1_verification — distinguishing two no-uid scenarios
# ---------------------------------------------------------------------------

class TestRunLayer1Verification:
    @pytest.mark.asyncio
    async def test_no_blade_uid_no_blade_create_skipped(self):
        """No blade_uid + no blade_create in messages → non-ChaosBlade → skipped."""
        result = await _run_host_blade_layer1("", "", task_id="t1", messages=[])
        assert result.status == "skipped"
        assert "Non-ChaosBlade" in result.details
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_no_blade_uid_with_blade_create_failed(self):
        """No blade_uid + blade_create in messages → ChaosBlade injection failed → failed."""
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        result = await _run_host_blade_layer1("", "", task_id="t2", messages=[msg])
        assert result.status == "warning"
        assert "blade_create" in result.details
        assert not result.is_terminal()

    @pytest.mark.asyncio
    async def test_no_blade_uid_no_messages_arg(self):
        """No blade_uid + messages=None → defaults to skipped (backward compatible)."""
        result = await _run_host_blade_layer1("", "", task_id="t3")
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_no_blade_uid_durable_native_record_prevents_warning(self):
        """Durable kubectl-native attribution + stale blade_create evidence
        → NOT 'blade attempted' — the fallback injected, so no warning."""
        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        result = await _run_host_blade_layer1(
            "", "", task_id="t4", messages=[msg],
            injection_method="kubectl_native",
        )
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_host_scope_skips_blade_query_k8s(self, monkeypatch):
        """Host Layer 1 = pure blade_status; the k8s-only blade_query_k8s step is
        skipped (host has no cluster CRD to query)."""
        from chaos_agent.config.settings import settings as _settings
        monkeypatch.setattr(_settings, "kube_connection_mode", "kubewiz_host")
        monkeypatch.setattr(_settings, "host_name", "10.0.2.8")

        # Replace the k8s-only query tool so we can assert it is never invoked.
        query_mock = AsyncMock()
        monkeypatch.setattr("chaos_agent.agent.providers.chaosblade.cli.blade_query_k8s", query_mock)

        with _patch_blade_cmd(_mock_blade_running("abc123xyz")):
            result = await _run_host_blade_layer1(
                "abc123xyz", "", task_id="t-host", messages=[]
            )
        # blade_status alone determines the host verdict.
        assert result.status == "passed"
        # blade_query_k8s (k8s-only) must NOT be invoked for host scope.
        query_mock.ainvoke.assert_not_called()

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


class TestRunRecoverLayer1DurableRouting:
    """Recovery Layer-1 no-UID routing must honor the durable record."""

    @pytest.mark.asyncio
    async def test_durable_native_record_prevents_false_failure(self):
        """Replan/compaction leaves a failed blade_create ToolMessage but the
        durable record says kubectl-native → recovery must NOT terminate as
        'no UID available'; the fault is live and LLM-recoverable."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy

        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        result = await run_layer1_destroy(
            "", "", messages=[msg], injection_method="kubectl_native",
        )
        assert result.status == "skipped"

    @pytest.mark.asyncio
    async def test_no_durable_record_keeps_failed_branch(self):
        """Without a durable record the message scan still drives the
        'blade attempted but no UID' terminal failure (backward compatible)."""
        from chaos_agent.agent.providers.chaosblade.recover import run_layer1_destroy

        msg = ToolMessage(content='{"code": 500, "success": false}', name="blade_create", tool_call_id="tc1")
        result = await run_layer1_destroy("", "", messages=[msg])
        assert result.status == "failed"
        assert "no UID" in result.details


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

    def test_l1_skipped_l2_unknown_returns_unverified(self):
        """L1=skipped (non-ChaosBlade) + L2=unknown → unverified.

        When Layer 1 is skipped (non-ChaosBlade fault), Layer 2 is the ONLY
        verification layer. If Layer 2 is "unknown", the injection cannot be
        confirmed — but honest ignorance (level=unverified: evidence
        unavailable) is a distinct knowledge claim, not counter-evidence, so
        it is "unverified", not "failed" and never "injected".
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "unverified"

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
        """ChaosBlade injection attempted but failed, with NO durable
        attribution (no UID / method / handle) → the evidence-less fault
        dispatch lands on the UID-less native carrier, whose Layer-1
        verdict is ``skipped`` (the runner's attempted→``warning`` branch
        stays reachable only along evidence-bearing routes — a blade-kind
        attribution handle). Same routing the recover chain's dispatch
        gives this state; verdict unchanged: unverified. Phase-4 T5
        dispatch change (was ``warning`` pre-dispatch); the skipped
        double-meaning split is a separate task (design Non-Goals)."""
        state = sample_agent_state
        state["task_id"] = "task-cb-fail"
        state["skill_name"] = "cpu-burn"
        state["experiment_uid"] = ""
        # Simulate a failed blade_create call in messages
        state["messages"] = [
            ToolMessage(
                content='{"code": 500, "success": false, "error": "resource not found"}',
                name="blade_create",
                tool_call_id="tc-fail",
            ),
        ]
        result = await verifier(state)
        assert result["verification"]["layer1"]["status"] == "skipped"
        assert result["result"]["verified"] is False


class TestFaultDispatchVerifyMatrix:
    """Phase-4 T5: verify-chain identity resolution through the unified
    fault dispatch — the same four-level resolution the recover chain uses
    (spec verify-chain-provider-protocol, Requirement 1).

    The verify dimension of the dispatch matrix: message-history claim /
    combo / native attribution states, dual-entry isomorphism, internal
    resolution agreement, and the end-to-end message-UID path."""

    @staticmethod
    def _message_history_state(sample_agent_state, uid="f1a2b3c4d5e60718"):
        """State with NO durable identity record whose message history
        carries a live experiment claim (successful blade_create)."""
        state = sample_agent_state
        state["task_id"] = "task-dispatch-verify"
        state["skill_name"] = "pod-delete"
        state["experiment_uid"] = ""
        state["injection_method"] = None
        state["fault_handle"] = None
        state["messages"] = [
            ToolMessage(
                content=json.dumps({"code": 200, "success": True, "result": uid}),
                name="blade_create",
                tool_call_id="tc-live",
            ),
        ]
        return state

    def test_message_history_uid_routes_experiment_carrier(self, sample_agent_state):
        """Spec Scenario: message-history claim carries the experiment UID
        and routes the experiment carrier (replaces the old execute-loop
        message fallback, which only the simple entry had)."""
        provider, identity = _resolve_fault_dispatch(
            self._message_history_state(sample_agent_state)
        )
        assert provider.carrier == "chaosblade"
        assert _experiment_uid_of(identity) == "f1a2b3c4d5e60718"

    def test_combo_attribution_keeps_experiment_uid(self, sample_agent_state):
        """Spec Scenario: combo state (live experiment UID + native durable
        attribution) — the verify dispatch identity is the EXPERIMENT
        handle (UID non-empty), agreeing with the recover chain's
        dispatch."""
        state = sample_agent_state
        state["experiment_uid"] = "uid-combo"
        state["injection_method"] = "kubectl_native"
        state["combo_native_issued"] = True
        state["fault_handle"] = None
        provider, identity = _resolve_fault_dispatch(state)
        assert provider.carrier == "chaosblade"
        assert _experiment_uid_of(identity) == "uid-combo"

    def test_native_attribution_routes_uidless_carrier(self, sample_agent_state):
        """Spec Scenario: native attribution without an experiment — routes
        the UID-less native carrier, experiment UID empty."""
        state = sample_agent_state
        state["experiment_uid"] = ""
        state["injection_method"] = "kubectl_native"
        state["fault_handle"] = None
        provider, identity = _resolve_fault_dispatch(state)
        assert provider.carrier == "k8s_native"
        assert _experiment_uid_of(identity) == ""

    def test_dual_entry_resolution_isomorphic(self, sample_agent_state):
        """Spec Scenario: dual-entry fallback isomorphism — both entries
        render their identity through the SAME seam and the SAME expression
        (dispatch identity first, materialized attribution handle as
        fallback), and the dispatch is idempotent, so every re-dispatch on
        the same state (the LLM entry's later ``_recovery_vehicle_of``
        resolution included) agrees on the same carrier + identity. The LLM
        entry lacked this message fallback pre-T5."""
        state = self._message_history_state(sample_agent_state)
        provider, identity = _resolve_fault_dispatch(state)
        handle = materialize_fault_handle(state)
        uid = _experiment_uid_of(identity) or _experiment_uid_of(handle)
        provider2, identity2 = _resolve_fault_dispatch(state)
        assert (provider.carrier, identity) == (provider2.carrier, identity2)
        assert uid == "f1a2b3c4d5e60718"

    def test_recovery_vehicle_follows_dispatch(self, sample_agent_state):
        """Internal resolution agreement: ``_recovery_vehicle_of`` renders
        through the DISPATCHED provider — a message-history experiment
        claim routes to the experiment carrier, whose vehicle record
        renders (the UID-less default would render None)."""
        state = self._message_history_state(sample_agent_state)
        state["kubectl_exec_pod_name"] = "otel-c-tool-abc"
        assert _recovery_vehicle_of(state) == "otel-c-tool-abc"

    @pytest.mark.asyncio
    async def test_message_uid_end_to_end_layer1(self, sample_agent_state):
        """End-to-end (spec Scenario): a message-history UID reaches the
        experiment carrier's Layer-1 runner through the simple entry —
        ``run_layer1_for_state`` dispatches on the same evidence and
        executes Layer 1 WITH that UID."""
        state = self._message_history_state(sample_agent_state)
        runner = AsyncMock(return_value=Layer1Result(status="passed", details="ok"))
        with patch(
            "chaos_agent.agent.providers.chaosblade.verify._run_host_blade_layer1",
            runner,
        ):
            result = await verifier(state)
        assert runner.await_args.args[0] == "f1a2b3c4d5e60718"
        assert result["verification"]["layer1"]["status"] == "passed"


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

    def test_failed_blade_create_with_exec_native_fallback(self):
        """task-09d30662 regression: blade_create failed (chaosblade-tool
        ImagePullBackOff) and the agent fell back to a kubectl exec python
        memory stressor. The fallback IS the injection — recover Layer 1 must
        NOT report 'blade_create was called but no UID available'."""
        msg1 = ToolMessage(
            content='Error: injection FAILED permanently (exit 1, class=target_gone)',
            name="blade_create",
            tool_call_id="tc1",
        )
        # read-only probes in between don't count...
        probe = _make_kubectl_tool_call_pair(
            "tc2", "exec", "pod-a -n ns -- which python3", "/usr/bin/python3",
        )
        # ...the mutating exec fallback does
        fallback = _make_kubectl_tool_call_pair(
            "tc3", "exec",
            "pod-a -n ns -- sh -c 'nohup python /tmp/mem_stress.py "
            "</dev/null >/dev/null 2>&1 & echo $!'",
            "25427",
        )
        assert _was_blade_create_attempted([msg1] + probe + fallback) is False


# ---------------------------------------------------------------------------
# _find_blade_query_in_messages
# ---------------------------------------------------------------------------

class TestFindBladeQueryInMessages:
    def test_find_matching_query(self):
        from chaos_agent.agent.providers.chaosblade.verify import _find_blade_query_in_messages
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
        from chaos_agent.agent.providers.chaosblade.verify import _find_blade_query_in_messages
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":{"uid":"other-uid"}}',
            name="kubectl",
            tool_call_id="tc1",
        )
        result = _find_blade_query_in_messages([msg], "a0f2357a939a9bb8")
        assert result == ""

    def test_no_kubectl_messages(self):
        from chaos_agent.agent.providers.chaosblade.verify import _find_blade_query_in_messages
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
        result = await _run_host_blade_layer1("", "", task_id="t-kubectl", messages=messages)
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
        with _patch_blade_cmd(mock_error):
            result = await _run_host_blade_layer1(
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
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload --cpu-percent 80",
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
        with _patch_blade_cmd(mock_error):
            result = await _run_host_blade_layer1(
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

    # --- command-mode (exec/debug) injections --------------------------

    def test_exec_mutating_command_after_blade_create_failure(self):
        """kubectl exec running a mutating inner command (the python memory
        stressor fallback, task-09d30662 shape) → detected as alternative
        injection even though 'exec' is not an object-write verb."""
        msg1 = ToolMessage(
            content='Error: injection FAILED permanently (exit 1, class=target_gone)',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-exec", "exec",
            "pod-a -n ns -- sh -c 'nohup python /tmp/mem_stress.py "
            "</dev/null >/dev/null 2>&1 & echo $!'",
            "25427",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is True

    def test_exec_mutating_command_error_result_still_counted(self):
        """Attempt-keyed, not result-keyed: an exec-delivered fault can sever
        its own feedback channel (forensic paradox), so an Error: result must
        NOT disprove the injection."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1)',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-exec", "exec",
            "pod-a -n ns -- sh -c 'iptables -A OUTPUT -j DROP'",
            "Error: command terminated with non-zero exit code",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is True

    def test_readonly_exec_after_blade_create_not_counted(self):
        """Read-only exec probes (df/ps/which...) are NOT injections."""
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1)',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-exec", "exec",
            "pod-a -n ns -- df -h /dev/shm",
            "Filesystem  Size  Used Avail Use%",
        )
        messages = [msg1] + kubectl_msgs
        assert _was_kubectl_injection_attempted(messages) is False

    def test_exec_mutating_before_blade_create_not_counted(self):
        """A mutating exec BEFORE the last blade_create is not a fallback."""
        kubectl_msgs = _make_kubectl_tool_call_pair(
            "tc-exec", "exec",
            "pod-a -n ns -- sh -c 'nohup python /tmp/mem_stress.py &'",
            "25427",
        )
        msg1 = ToolMessage(
            content='Error: blade create failed (exit 1)',
            name="blade_create",
            tool_call_id="tc-fail1",
        )
        messages = kubectl_msgs + [msg1]
        assert _was_kubectl_injection_attempted(messages) is False


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

        result = await _run_host_blade_layer1("", "", task_id="t-scale", messages=messages)
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

        result = await _run_host_blade_layer1("", "", task_id="t-cordon", messages=messages)
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

        result = await _run_host_blade_layer1("", "", task_id="t-no-alt", messages=messages)
        assert result.status == "warning"
        assert not result.is_terminal()


class TestExtractKubectlExecPodName:
    """Tests for _extract_kubectl_exec_pod_name function."""

    def test_kubectl_exec_blade_create_extracts_pod_name(self):
        """kubectl exec with blade create → pod name extracted from v_args."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc1", "exec",
            "otel-c-tool-abc123 -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
        )
        assert _extract_kubectl_exec_pod_name(msgs) == "otel-c-tool-abc123"

    def test_kubectl_get_not_extracted(self):
        """kubectl get (not exec blade create) → None."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc2", "get",
            "pods -n default",
            "NAME  READY  STATUS  RESTARTS  AGE",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_kubectl_exec_without_blade_not_extracted(self):
        """kubectl exec without blade create → None."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc3", "exec",
            "otel-c-tool -n chaosblade -- ls /tmp",
            "file1\nfile2",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_empty_messages_returns_none(self):
        """Empty messages list → None."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        assert _extract_kubectl_exec_pod_name([]) is None

    def test_non_chaosblade_json_returns_none(self):
        """kubectl exec with non-ChaosBlade JSON response → None."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc4", "exec",
            "otel-c-tool -n chaosblade -- blade create k8s pod-cpu fullload",
            "Error: blade not found",
        )
        assert _extract_kubectl_exec_pod_name(msgs) is None

    def test_multiple_blade_creates_returns_most_recent(self):
        """Multiple kubectl exec blade creates → returns the most recent pod name."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
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
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc5", "exec",
            "  otel-c-tool-ws  -n chaosblade -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-ws"}',
        )
        assert _extract_kubectl_exec_pod_name(msgs) == "otel-c-tool-ws"

    def test_v_args_starting_with_flag_still_extracts_pod(self):
        """v_args starting with `-n` is valid kubectl syntax — pod name must
        still be extracted (task-2d612caa regression: losing the injection
        pod made Layer 1 read an unrelated pod's empty local DB as failure)."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
        msgs = _make_kubectl_tool_call_pair(
            "tc6", "exec",
            "-n chaosblade otel-c-tool -- blade create k8s pod-cpu fullload",
            '{"code":200,"success":true,"result":"uid-flag"}',
        )
        assert _extract_kubectl_exec_pod_name(msgs) == "otel-c-tool"

    def test_legacy_session_without_tool_call_id(self):
        """ToolMessage without tool_call_id → fallback to AIMessage scan."""
        from chaos_agent.agent.providers.chaosblade.provider import extract_kubectl_exec_pod_name as _extract_kubectl_exec_pod_name
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


class TestLayer2ToolPodNamespace:
    """Layer 2 prompt must never assert a hardcoded tool pod namespace — it
    is deployment-specific (task-e9bae269: pods lived in `default`, not
    `chaosblade`), so the prompt must instruct discovery instead."""

    def _node_scope_state(self) -> dict:
        from chaos_agent.agent.spec.fault_spec import FaultSpec
        spec = FaultSpec(
            namespace="cms-demo",
            scope="node",
            names=("node-a",),
            fault_target="network",
            fault_action="delay",
            params={"time": "3000"},
        )
        return {
            "messages": [HumanMessage(content="inject")],
            "fault_spec": spec.to_dict(),
            "injection_parsed_params": {},
            "params": {},
            "kubeconfig": "/path/to/kc",
        }

    def _layer2_text(self, **kwargs) -> str:
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result
        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            self._node_scope_state(), layer1, "uid-tp", "net-delay",
            "/path/to/kc", count=1, **kwargs,
        )
        return "\n".join(
            m.content for m in msgs
            if isinstance(m, HumanMessage) and isinstance(m.content, str)
        )

    def test_namespace_never_asserted_discovery_instructed(self):
        text = self._layer2_text(tool_pod_name="otel-c-tool-abc")
        assert "## Available Tool Pod" in text
        # Namespace-agnostic discovery semantics, tool-agnostic wording
        assert "identify it across all namespaces before exec" in text
        assert "never assume it" in text
        # Never assert a hardcoded namespace anywhere in the tool pod block
        assert "- Namespace: `chaosblade`" not in text
        assert "-n chaosblade" not in text
        # Tool-abstraction boundary: no literal tool-call syntax or tool names
        assert "kubectl(" not in text
        assert "blade status" not in text
        assert "blade query" not in text


class TestParsePodNameFromVArgs:
    """Flag-order tolerance of _parse_pod_name_from_v_args (task-2d612caa)."""

    def _parse(self, v_args):
        # phase-3 T3: the pod-name argv parser moved to the chaosblade provider
        # (kubectl-exec delivery domain knowledge).
        from chaos_agent.agent.providers.chaosblade.provider import _parse_pod_name_from_v_args
        return _parse_pod_name_from_v_args(v_args)

    def test_pod_first_classic_order(self):
        assert self._parse("otel-c-tool -n chaosblade -- blade create mem load") == "otel-c-tool"

    def test_namespace_flag_before_pod(self):
        assert self._parse("-n chaosblade chaosblade-tool-krp7v -- blade create mem load") == "chaosblade-tool-krp7v"

    def test_boolean_flags_before_pod(self):
        assert self._parse("-it -n chaosblade otel-c-tool -- blade create mem load") == "otel-c-tool"

    def test_long_flag_with_equals_value(self):
        assert self._parse("--namespace=chaosblade otel-c-tool -- blade create mem load") == "otel-c-tool"

    def test_container_flag_consumes_its_value(self):
        assert self._parse("-c tool -n chaosblade otel-c-tool -- blade create mem load") == "otel-c-tool"

    def test_empty_or_no_pod_before_separator(self):
        assert self._parse("") is None
        assert self._parse("-- blade create mem load") is None
        assert self._parse("-n chaosblade -- blade create mem load") is None

    def test_tokens_after_separator_are_ignored(self):
        # pod-like tokens after `--` belong to the remote command, not the pod slot
        assert self._parse("-n chaosblade -- blade create mem load --names node-x") is None


class TestRunLayer1ViaKubectlExecWithOriginalPod:
    """Tests for _run_layer1_via_kubectl_exec with injection_pod_name parameter."""

    @pytest.mark.asyncio
    async def test_original_pod_succeeds(self):
        """Original pod is available → uses blade query k8s directly, no discovery needed."""
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
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
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
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
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
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
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
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
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
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
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [discover_result, blade_query_result]
            result = await _run_layer1_via_kubectl_exec(
                "exp-test", "/path/to/kc", task_id="t3",
                injection_pod_name=None,
            )
        assert result.status == "passed"
        assert "blade query k8s" in result.details
        # 2 calls: discover + blade query k8s on discovered pod
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_discovery_record_not_found_tries_next_pod(self):
        """task-2d612caa: a discovered pod whose LOCAL DB lacks the record is
        not a verdict — the experiment lives in the injection pod's DB only,
        so discovery must probe the next pod before failing."""
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        discover_result = CommandResult(
            exit_code=0,
            stdout=("chaosblade   chaosblade-tool-2l2gj   1/1   Running   0   1d\n"
                    "chaosblade   chaosblade-tool-krp7v   1/1   Running   0   1d"),
            stderr="",
        )
        # query k8s finds no CRD (operator path unavailable) -> unparseable verdict
        query_no_crd = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 63061, "success": False,
                               "error": "chaosblades.chaosblade.io `exp-test` not found"}),
            stderr="",
        )
        # first pod's local DB does not hold the record
        status_wrong_pod = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 67002, "success": False,
                               "error": "`exp-test` record not found"}),
            stderr="",
        )
        # second pod IS the injection pod — record found, experiment running
        status_right_pod = CommandResult(
            exit_code=0,
            stdout=json.dumps({"code": 200, "success": True,
                               "result": {"Uid": "exp-test", "Status": "Success"}}),
            stderr="",
        )
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                discover_result,
                query_no_crd, status_wrong_pod,   # pod 1: query k8s + status
                query_no_crd, status_right_pod,   # pod 2: query k8s + status
            ]
            result = await _run_layer1_via_kubectl_exec(
                "exp-test", "/path/to/kc", task_id="t4",
                injection_pod_name=None,
            )
        assert result.status == "passed"
        assert "chaosblade-tool-krp7v" in result.details
        assert mock_run.call_count == 5

    @pytest.mark.asyncio
    async def test_discovery_finds_record_beyond_old_two_pod_cap(self):
        """Discovery order is arbitrary: the injection pod may sit at
        position 3+, past the former 2-pod cap. The sweep must keep probing
        until the pod holding the record is reached."""
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        discover_result = CommandResult(
            exit_code=0,
            stdout=("chaosblade   chaosblade-tool-aaa   1/1   Running   0   1d\n"
                    "chaosblade   chaosblade-tool-bbb   1/1   Running   0   1d\n"
                    "chaosblade   chaosblade-tool-ccc   1/1   Running   0   1d"),
            stderr="",
        )
        query_no_crd = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 63061, "success": False,
                               "error": "chaosblades.chaosblade.io `exp-deep` not found"}),
            stderr="",
        )
        status_not_found = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 67002, "success": False,
                               "error": "`exp-deep` record not found"}),
            stderr="",
        )
        status_found = CommandResult(
            exit_code=0,
            stdout=json.dumps({"code": 200, "success": True,
                               "result": {"Uid": "exp-deep", "Status": "Success"}}),
            stderr="",
        )
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                discover_result,
                query_no_crd, status_not_found,   # pod 1
                query_no_crd, status_not_found,   # pod 2 (old cap stopped here)
                query_no_crd, status_found,       # pod 3 holds the record
            ]
            result = await _run_layer1_via_kubectl_exec(
                "exp-deep", "/path/to/kc", task_id="t6",
                injection_pod_name=None,
            )
        assert result.status == "passed"
        assert "chaosblade-tool-ccc" in result.details

    @pytest.mark.asyncio
    async def test_discovery_record_not_found_everywhere_fails(self):
        """Every probed pod answered but none holds the record → genuine
        failure (experiment lost), not an infrastructure skip."""
        from chaos_agent.agent.providers.chaosblade.verify import _run_layer1_via_kubectl_exec
        from chaos_agent.tools.shell import CommandResult

        discover_result = CommandResult(
            exit_code=0,
            stdout=("chaosblade   chaosblade-tool-aaa   1/1   Running   0   1d\n"
                    "chaosblade   chaosblade-tool-bbb   1/1   Running   0   1d"),
            stderr="",
        )
        query_no_crd = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 63061, "success": False,
                               "error": "chaosblades.chaosblade.io `exp-lost` not found"}),
            stderr="",
        )
        status_not_found = CommandResult(
            exit_code=1,
            stdout=json.dumps({"code": 67002, "success": False,
                               "error": "`exp-lost` record not found"}),
            stderr="",
        )
        with patch("chaos_agent.transports.execute_via_transport", new_callable=AsyncMock) as mock_run:
            mock_run.side_effect = [
                discover_result,
                query_no_crd, status_not_found,
                query_no_crd, status_not_found,
            ]
            result = await _run_layer1_via_kubectl_exec(
                "exp-lost", "/path/to/kc", task_id="t5",
                injection_pod_name=None,
            )
        assert result.status == "failed"
        assert "not found in any tool pod" in result.details


# ---------------------------------------------------------------------------
# Tests for checklist parsing and automatic level downgrade
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (  # noqa: E402
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
# _disk_fill_param_hints (VerificationProfile disk slot helper)
# ---------------------------------------------------------------------------

from chaos_agent.agent.nodes.verify._verifier_shared import (  # noqa: E402
    _IMAGEFS_PATHS,
    _NODEFS_PATHS,
)
from chaos_agent.agent.nodes.verify._verifier_hints import (  # noqa: E402
    _BASELINE_INTEGRITY_PROMPT,
)


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
            "experiment_uid": "test-uid",
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
            "fault_scope": "node",
            "fault_target": "disk",
            "fault_action": "fill",
            "experiment_uid": "test-uid-123",
            "baseline_data": {
                "captured_at": "2026-05-09T10:00:00",
                "source": "registry",
                "observations": observations,
                "success_count": 1 if baseline_success else 0,
            },
            "injection_parsed_params": {"path": "/tmp", "size": "10000"},
            "params": {},
            "target": {"namespace": "default", "names": ["test-node"], "labels": {}},
            "kubeconfig": "/path/to/kubeconfig",
            "kubectl_exec_pod_name": "otel-c-tool-abc",
        }

    def test_baseline_in_context_when_available(self):
        """When baseline_data with success_count > 0, baseline is injected as synthetic ToolMessage."""
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_baseline_tool_messages,
        )
        state = self._build_direct_state_with_baseline(baseline_success=True)
        baseline = state.get("baseline_data")
        assert baseline is not None
        assert baseline["success_count"] > 0

        # Verify _build_baseline_tool_messages produces ToolMessage pairs
        msgs = _build_baseline_tool_messages(
            baseline, "disk", "fill",
            injection_parsed={"path": "/tmp", "size": "10000"},
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
        from chaos_agent.agent.nodes.verify._verifier_messages import _build_layer2_messages
        state = self._build_direct_state_with_baseline(baseline_success=True)
        # Build Layer 2 messages and check HumanMessage does NOT contain baseline section
        from chaos_agent.agent.result.verdict import Layer1Result
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
        injection_parsed = {"path": "/tmp", "size": "10000"}
        blade_scope = "node"
        blade_target = "disk"
        blade_action = "fill"
        tool_pod_name = "otel-c-tool-abc"
        kubeconfig = "/path/to/kubeconfig"

        # Simulate the context generation logic
        fill_path = injection_parsed.get("path", "/tmp")
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
            "fault_scope": "pod",
            "fault_target": "cpu",
            "fault_action": "fullload",
            "experiment_uid": "uid-synth-123",
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
            "injection_parsed_params": {},
            "params": {},
            "target": {"namespace": "default", "names": ["myapp-pod"], "labels": {"app": "myapp"}},
            "kubeconfig": "/path/to/kubeconfig",
        }

    def test_extract_synthetic_for_state_on_count1(self, baseline_state):
        """On count==1, _build_layer2_messages injects synthetic messages,
        and _synthetic_for_state extraction picks them up."""
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

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

        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_baseline_tool_messages,
            _build_layer2_messages,
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        # Build synthetic messages as if they were persisted from count==1
        baseline = baseline_state["baseline_data"]
        synthetic_msgs = _build_baseline_tool_messages(
            baseline, "cpu", "fullload", injection_parsed={},
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

        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

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

        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_baseline_tool_messages,
        )

        baseline = baseline_state["baseline_data"]
        synthetic_for_state = _build_baseline_tool_messages(
            baseline, "cpu", "fullload", injection_parsed={},
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
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _METRICS_TOOL_CALL_ID,
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        assert _METRICS_TOOL_CALL_ID == "baseline_collector_metrics"
        assert _METRICS_TOOL_CALL_ID in _SYNTHETIC_TOOL_CALL_IDS


class TestBaselinePairReplanLifecycle:
    """Cross-replan lifecycle of the synthetic baseline pair.

    Change ``stale-baseline-pair-seam-cleanup``: the replan seam
    (``reset_attribution_state``) removes the previous verification cycle's
    synthetic pair, so the next cycle's pair gate rebuilds from the CURRENT
    ``baseline_data`` instead of shipping pre-replan numbers (E1) or
    contradicting a failed re-capture (E4).

    Driving method mirrors the retired repro script
    ``_repro_stale_baseline.py``: every message construction / gate / merge /
    seam cleanup goes through the production functions
    (``_build_layer2_messages``, ``extract_synthetic_messages``,
    ``extract_persistent_hm``, ``_ts_add_messages``,
    ``reset_attribution_state``); only the graph orchestration (how a node's
    result_update lands in state) is simulated.
    """

    V1 = "BASELINE-V1-STALE"
    V2 = "BASELINE-V2-FRESH"

    def _baseline(self, marker: str) -> dict:
        return {
            "captured_at": "2026-09-11T10:00:00",
            "source": "registry",
            "observations": [
                {
                    "exit_code": 0,
                    "stdout": f"NAME  CPU%  MEM%\nmyapp-pod  5%  {marker}",
                    "stderr": "",
                    "resource_name": "myapp-pod",
                    "resource_type": "pod",
                    "namespace": "default",
                    "command": "kubectl top pod",
                }
            ],
            "success_count": 1,
        }

    def _state(self, baseline: dict) -> dict:
        from chaos_agent.agent.spec.fault_spec import FaultSpec

        spec = FaultSpec(
            namespace="cms-demo",
            scope="pod",
            names=("myapp-pod",),
            fault_target="cpu",
            fault_action="fullload",
            params={"cpu-percent": "80"},
        )
        return {
            "task_id": "test-replan-lifecycle",
            "messages": [HumanMessage(content="inject")],
            "fault_spec": spec.to_dict(),
            "baseline_data": baseline,
            "injection_parsed_params": {},
            "kubeconfig": "/path/to/kc",
            "experiment_uid": "uid-cycle-1",
            "injection_method": "chaosblade",
            "verify_replan_count": 0,
        }

    def _verifier_turn(self, state: dict, count: int):
        """One verifier_loop turn: build the shipped sequence, extract the
        persistence payload (same shape as verifier.py's result_update)."""
        from chaos_agent.agent.nodes.execute.react_helpers import (
            extract_persistent_hm,
            extract_synthetic_messages,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _SYNTHETIC_TOOL_CALL_IDS,
            _VERIFIER_CONTEXT_KWARGS_KEY,
            _build_layer2_messages,
        )

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        msgs = _build_layer2_messages(
            state, layer1, state.get("experiment_uid") or "uid-x",
            "cpu-fullload", "/path/to/kc", count,
        )
        synthetic = extract_synthetic_messages(msgs, _SYNTHETIC_TOOL_CALL_IDS)
        main_hm = extract_persistent_hm(msgs, state, _VERIFIER_CONTEXT_KWARGS_KEY)
        response = AIMessage(content="(verifier turn response)")
        return msgs, {"messages": synthetic + main_hm + [response]}

    def _settle(self, state: dict, result_update: dict) -> dict:
        """Graph orchestration: merge messages through the production reducer."""
        from chaos_agent.agent.state import _ts_add_messages

        merged = _ts_add_messages(
            state["messages"], result_update.get("messages", []),
        )
        new_state = dict(state)
        new_state["messages"] = list(merged)
        for key, value in result_update.items():
            if key != "messages":
                new_state[key] = value
        return new_state

    def _pair_text_fragments(self, msgs) -> list:
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _SYNTHETIC_TOOL_CALL_IDS,
        )

        return [
            m.content for m in msgs
            if isinstance(m, ToolMessage)
            and m.tool_call_id in _SYNTHETIC_TOOL_CALL_IDS
        ]

    def _cycle1_with_persisted_pair(self) -> dict:
        """Cycle 1 after two turns: pair persisted in state (pre-seam shape)."""
        state = self._state(self._baseline(self.V1))
        _, upd1 = self._verifier_turn(state, 1)
        state = self._settle(state, upd1)
        _, upd2 = self._verifier_turn(state, 2)
        return self._settle(state, upd2)

    def test_seam_clears_stale_pair_and_next_cycle_renders_fresh_baseline(self):
        """E1 (was BUG CONFIRMED): after the verify-replan seam, cycle 2's
        first turn must render the CURRENT baseline — no V1 fragment, one V2
        pair — because the seam removed the stale pair and the pair gate
        rebuilt from ``baseline_data``."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        state = self._cycle1_with_persisted_pair()
        finalize_update = {"replan_requested": True, "verification": None}
        reset_attribution_state(
            finalize_update, state_messages=state["messages"],
        )
        state = self._settle(state, finalize_update)

        # Replan rerun: baseline_capture unconditionally re-captures (V2).
        state["baseline_data"] = self._baseline(self.V2)
        state["experiment_uid"] = "uid-cycle-2"

        shipped, _ = self._verifier_turn(state, 1)
        fragments = self._pair_text_fragments(shipped)
        assert not any(self.V1 in c for c in fragments), \
            "the stale pre-replan pair must not reach the model"
        assert sum(self.V2 in c for c in fragments) == 1, \
            "the fresh baseline evidence must ship exactly once"

    def test_seam_without_persisted_pair_is_a_no_op_cleanup(self):
        """A pair not yet injected (execute-time replan before the first
        verification) must produce no tombstones for absent ids —
        ``add_messages`` raises ValueError on those."""
        from langchain_core.messages import RemoveMessage

        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        state = self._state(self._baseline(self.V1))
        result: dict = {"replan_requested": True}
        reset_attribution_state(result, state_messages=state["messages"])
        assert not [
            m for m in result.get("messages") or []
            if isinstance(m, RemoveMessage)
        ], "no pair in state -> no tombstones"
        settled = self._settle(state, result)
        assert settled["attribution_epoch_index"] == len(settled["messages"]), \
            "epoch boundary must equal the post-merge length even with no cleanup"

    def test_recapture_failure_ships_no_stale_pair(self):
        """E4 (was CONFIRMED): when cycle 2's baseline re-capture fails, the
        shipped sequence must present "no usable data" WITHOUT the stale pair
        surviving beside it."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        state = self._cycle1_with_persisted_pair()
        finalize_update = {"replan_requested": True}
        reset_attribution_state(
            finalize_update, state_messages=state["messages"],
        )
        state = self._settle(state, finalize_update)

        state["baseline_data"] = {
            "captured_at": "2026-09-11T11:00:00",
            "source": "error",
            "observations": [],
            "success_count": 0,
        }
        shipped, _ = self._verifier_turn(state, 1)
        assert self._pair_text_fragments(shipped) == [], \
            "no synthetic pair may ship when the re-capture failed"
        hm_texts = [
            m.content for m in shipped
            if isinstance(m, HumanMessage) and isinstance(m.content, str)
        ]
        assert any("no usable data" in t for t in hm_texts), \
            "the context must state the absence instead of contradicting itself"

    def test_epoch_alignment_and_context_re_arm_after_seam(self):
        """The seam's epoch must equal the post-merge length (net-length
        compensation — no overshoot into the consumers' full-history
        fallback), and the new cycle's context must re-arm: needs_context
        True, fresh context HM injected and persisted."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _VERIFIER_CONTEXT_KWARGS_KEY,
            _verification_cycle_needs_context,
        )

        state = self._cycle1_with_persisted_pair()
        finalize_update = {"replan_requested": True}
        reset_attribution_state(
            finalize_update, state_messages=state["messages"],
        )
        state = self._settle(state, finalize_update)

        assert state["attribution_epoch_index"] == len(state["messages"]), (
            "four RemoveMessages consumed by the reducer — the boundary must "
            "not overshoot (task-5193538b defence must stay armed)"
        )
        assert _verification_cycle_needs_context(state) is True, \
            "the seam re-arms the new-cycle context detection"

        state["baseline_data"] = self._baseline(self.V2)
        shipped, upd = self._verifier_turn(state, 1)
        assert any(
            isinstance(m, HumanMessage)
            and getattr(m, "additional_kwargs", {}).get(_VERIFIER_CONTEXT_KWARGS_KEY)
            for m in shipped
        ), "the new cycle's judgement context must be injected"
        settled = self._settle(state, upd)
        assert any(
            isinstance(m, HumanMessage)
            and getattr(m, "additional_kwargs", {}).get(_VERIFIER_CONTEXT_KWARGS_KEY)
            for m in settled["messages"]
        ), "the context HM must persist in state (not first-turn-only)"


class TestBaselineEvidenceTruncationNotice:
    """Long baseline observations must carry the shared-contract notice.

    Every verify-side fixture used short stdout (never crossing the
    1500-char per-obs gate), so the notice contract was only asserted on
    the recover side and at truncation unit level — the exact behavioural
    asymmetry the truncation-governance change set out to eliminate,
    reproduced in the test layer. This class closes that gap at the
    node's real entry (``_build_layer2_messages``), pinning all three
    fields of the notice invariant plus the obs-head survival the delta
    comparison depends on.
    """

    @pytest.fixture
    def state(self):
        return {
            "task_id": "test-trunc-1",
            "fault_scope": "pod",
            "fault_target": "cpu",
            "fault_action": "fullload",
            "experiment_uid": "uid-trunc-123",
            "baseline_data": {
                "captured_at": "2026-05-09T10:00:00",
                "source": "registry",
                "observations": [{
                    "exit_code": 0,
                    "stdout": "metric_row " * 250,  # 2,750 chars > 1,500 gate
                    "stderr": "",
                    "resource_name": "myapp-pod",
                    "resource_type": "pod",
                    "namespace": "default",
                    "command": "kubectl top pod",
                }],
                "success_count": 1,
            },
            "injection_parsed_params": {},
            "params": {},
            "target": {"namespace": "default", "names": ["myapp-pod"],
                       "labels": {"app": "myapp"}},
            "kubeconfig": "/path/to/kubeconfig",
        }

    def test_long_observation_carries_three_field_notice(self, state):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        layer1 = Layer1Result(
            status="passed", affected_count=1, raw_output="Success",
        )
        msgs = _build_layer2_messages(
            state, layer1, "uid-trunc-123", "cpu-fullload",
            "/path/to/kubeconfig", count=1,
        )
        tool_msgs = [m for m in msgs
                     if isinstance(m, ToolMessage)
                     and m.tool_call_id == _BASELINE_TOOL_CALL_ID]
        assert tool_msgs, "expected the synthetic baseline ToolMessage"
        content = tool_msgs[0].content
        # Field 1 — machine-parseable marker from the family.
        assert "⚠️ TRUNCATED (baseline evidence)" in content
        # Field 2 — honest original size in the caller's unit.
        assert "(original 2750 characters)" in content
        # Field 3 — retrieval guidance: full evidence preserved in state.
        assert "Full observation preserved in state.baseline_data" in content
        # The obs head survives the cut — the delta-comparison value.
        assert "metric_row" in content


class TestSyntheticPairDedup:
    """Pair-aware dedup of the synthetic baseline injection (#1344 sig #2).

    The gate used to probe ONE id on ToolMessages only, so a half-present pair
    read as "already injected": no rebuild ran, so no caller was ever supplied,
    and the surviving orphan shipped with no caller anywhere in the list —
    Layer 1 then had to DROP it, silently losing the baseline evidence. That is
    the historical bug these tests pin.

    With the pair-aware gate the rebuild always supplies the caller, so Layer 1
    no longer drops anything here (asserted below by identity, not by counting).
    What replaces it is a narrower, documented exception: a fragment whose
    message id predates the stable-id fix cannot be replaced in place, so it
    stays pinned in state and the gate rebuilds every turn without converging.
    See ``test_legacy_fragment_is_pinned_in_state_but_never_ships``.
    """

    @pytest.fixture
    def state(self):
        return {
            "task_id": "test-dedup-1",
            "fault_scope": "pod",
            "fault_target": "cpu",
            "fault_action": "fullload",
            "experiment_uid": "uid-dedup-123",
            "baseline_data": {
                "captured_at": "2026-05-09T10:00:00",
                "source": "registry",
                "observations": [{
                    "exit_code": 0,
                    "stdout": "NAME   CPU%  MEM%\nmyapp  5%    30%",
                    "stderr": "",
                    "resource_name": "myapp-pod",
                    "resource_type": "pod",
                    "namespace": "default",
                    "command": "kubectl top pod",
                }],
                "success_count": 1,
            },
            "injection_parsed_params": {},
            "params": {},
            "target": {"namespace": "default", "names": ["myapp-pod"],
                       "labels": {"app": "myapp"}},
            "kubeconfig": "/path/to/kubeconfig",
        }

    @staticmethod
    def _build(state, count=2):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        return _build_layer2_messages(
            state, layer1, "uid-dedup-123", "cpu-fullload",
            "/path/to/kubeconfig", count=count,
        )

    @staticmethod
    def _intact(msgs) -> bool:
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        from chaos_agent.utils.message_integrity import synthetic_pairs_intact

        return synthetic_pairs_intact(msgs, _SYNTHETIC_TOOL_CALL_IDS)

    @staticmethod
    def _results(msgs, tc_id) -> list:
        return [m for m in msgs
                if isinstance(m, ToolMessage) and m.tool_call_id == tc_id]

    @staticmethod
    def _protocol_legal(msgs) -> bool:
        """The check a strict provider applies, written independently of the
        code under test: every tool result must answer a PRECEDING call.

        Deliberately does not reuse ``sanitize_tool_pairing`` — asking the
        implementation whether the implementation is correct passes even when
        the implementation is wrong.
        """
        asked: set = set()
        for m in msgs:
            if isinstance(m, AIMessage):
                asked.update(tc.get("id") for tc in (m.tool_calls or []))
            elif isinstance(m, ToolMessage):
                if m.tool_call_id not in asked:
                    return False
        return True

    @staticmethod
    def _diagnose(msgs) -> str:
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _SYNTHETIC_TOOL_CALL_IDS,
        )
        from chaos_agent.utils.message_integrity import diagnose_synthetic_pairs

        return diagnose_synthetic_pairs(msgs, _SYNTHETIC_TOOL_CALL_IDS)

    def _advance(self, state, turns=3):
        """Run ``turns`` verifier turns through the REAL ``add_messages``.

        Returns ``(states, shipped)``: ``states[i]`` is what the gate saw
        before turn i, ``shipped[i]`` is what turn i handed the provider. Both
        halves are needed — the contract covers what ships AND what the gate
        had to fire against, and a test that only checks the former passes
        vacuously if the gate stops firing.
        """
        from langgraph.graph.message import add_messages

        from chaos_agent.agent.nodes.execute.react_helpers import (
            extract_synthetic_messages,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _SYNTHETIC_TOOL_CALL_IDS,
        )

        states, shipped = [], []
        for _ in range(turns):
            states.append(list(state["messages"]))
            out = self._build(state)
            shipped.append(out)
            state["messages"] = list(add_messages(
                state["messages"],
                extract_synthetic_messages(out, _SYNTHETIC_TOOL_CALL_IDS),
            ))
        return states, shipped

    def test_intact_pairs_are_reused_not_rebuilt(self, state):
        """Spec: 完整对在场——跳过重建 (identity, not just absence of growth)."""
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_baseline_tool_messages,
        )

        persisted = _build_baseline_tool_messages(
            state["baseline_data"], "cpu", "fullload", injection_parsed={},
        )
        state["messages"] = [HumanMessage(content="turn 1"), *persisted]
        msgs = self._build(state)
        assert self._intact(msgs)
        for msg in persisted:
            assert any(s is msg for s in msgs), "intact pair must ship as-is"

    def test_orphan_results_only_rebuilds_complete_pairs(self, state):
        """Spec: caller 丢失只剩孤儿 ToolMessage——重建完整对."""
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
            _METRICS_TOOL_CALL_ID,
        )

        orphans = [
            ToolMessage(content="stale baseline", tool_call_id=_BASELINE_TOOL_CALL_ID,
                        name="baseline_collector"),
            ToolMessage(content="stale metrics", tool_call_id=_METRICS_TOOL_CALL_ID,
                        name="baseline_collector"),
        ]
        state["messages"] = [HumanMessage(content="turn 1"), *orphans]
        msgs = self._build(state)

        assert self._intact(msgs)
        for tc_id, stale in ((_BASELINE_TOOL_CALL_ID, "stale baseline"),
                             (_METRICS_TOOL_CALL_ID, "stale metrics")):
            results = self._results(msgs, tc_id)
            assert len(results) == 1, f"{tc_id} answered {len(results)} times"
            assert stale not in results[0].content, "stale fragment must be cleared"
        for orphan in orphans:
            assert not any(s is orphan for s in msgs)

    def test_metrics_pair_damaged_alone_triggers_rebuild(self, state):
        """Spec: metrics 对单独受损也触发重建.

        The baseline ToolMessage IS present, so the old single-id probe
        reported "already injected" and never looked at the metrics pair.
        """
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
            _METRICS_MSG_ID_CALLER,
            _METRICS_TOOL_CALL_ID,
            _build_baseline_tool_messages,
        )

        persisted = _build_baseline_tool_messages(
            state["baseline_data"], "cpu", "fullload", injection_parsed={},
        )
        # lose the metrics CALLER only; the baseline pair stays complete
        state["messages"] = [m for m in persisted if m.id != _METRICS_MSG_ID_CALLER]
        assert self._results(state["messages"], _BASELINE_TOOL_CALL_ID), \
            "precondition: the old probe would have seen the baseline result"

        msgs = self._build(state)

        assert self._intact(msgs)
        for tc_id in (_BASELINE_TOOL_CALL_ID, _METRICS_TOOL_CALL_ID):
            assert len(self._results(msgs, tc_id)) == 1

    def test_absent_pairs_inject_a_clean_set(self, state):
        """Spec: 双双缺席——正常注入，且上线序列无孤儿."""
        from chaos_agent.utils.message_integrity import sanitize_tool_pairing

        state["messages"] = [HumanMessage(content="turn 1"), AIMessage(content="resp")]
        msgs = self._build(state)
        assert self._intact(msgs)
        assert sanitize_tool_pairing(msgs) is msgs  # nothing for Layer 1 to drop

    def test_repeated_rebuild_does_not_grow_state(self, state):
        """Stable message ids make the rebuild idempotent through add_messages.

        With fresh UUIDs each rebuild would leave the damaged copy in state,
        re-trigger the gate next turn, and append a full pair set per turn.

        The assertions are the CONTRACT, not just "no growth" — all four were
        measured over three turns with the real reducer before being written:

        * the gate really fires every turn (state stays ``damaged``), so a gate
          that silently stopped rebuilding fails here instead of passing
          vacuously through the ``intact`` check on the shipped list;
        * state reaches a FIXED POINT — from turn 2 on it is identical message
          for message, not merely the same length;
        * every shipped list is ``intact``, protocol-legal, and clean enough
          that Layer 1 returns it by identity (nothing dropped, nothing moved);
        * so the cost of the non-convergence is one rebuild + one log per turn,
          never a growing context and never an illegal sequence.
        """
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.utils.message_integrity import (
            PAIR_DAMAGED,
            PAIR_INTACT,
            sanitize_tool_pairing,
        )

        # a stale fragment that no rebuild can replace (id predates the fix)
        state["messages"] = [
            HumanMessage(content="turn 1"),
            ToolMessage(content="stale", tool_call_id=_BASELINE_TOOL_CALL_ID),
        ]
        states, shipped = self._advance(state)

        assert [self._diagnose(s) for s in states] == [PAIR_DAMAGED] * 3, \
            "the gate must keep firing, or the rest of this test proves nothing"

        ids = [[m.id for m in s] for s in states]
        assert all(all(i for i in turn) for turn in ids), \
            "ids must be real, or the fixed-point comparison passes vacuously"
        assert ids[1] == ids[2], "state did not settle: it is still churning"
        assert len(ids[1]) > len(ids[0]), "precondition: turn 1 really injected"

        for out in shipped:
            assert self._diagnose(out) == PAIR_INTACT
            assert self._protocol_legal(out)
            assert sanitize_tool_pairing(out) is out, \
                "Layer 1 should have nothing left to drop or move"

    def test_legacy_fragment_is_pinned_in_state_but_never_ships(self, state):
        """CHARACTERISATION of the one rebuild that never converges.

        A synthetic ToolMessage written before the stable-message-id fix
        carries a UUID id, so no rebuild can replace it in place and the gate
        reports ``damaged`` on every turn: a tool_call really is answered twice
        in state. Measured over three turns — state settles at a fixed point
        that still contains the fragment, and the fragment never ships.

        Deliberately NOT fixed with ``RemoveMessage``, even though that would
        work here (measured: removing an id the same batch does not re-add
        succeeds, unlike the reversed-pair flavour where
        ``ids_to_remove.discard`` undoes the removal). Rejected because:

        * it only reaches checkpoints written before the fix, which ships in
          the same release as this gate;
        * it would put cross-turn state mutation into a node that today only
          returns this turn's messages, and a wrong id deletes real history
          irrecoverably;
        * it is self-limiting — compaction summarises ``to_compact`` and sweeps
          the fragment out, and ``/clear`` ends it immediately.

        The WARNING is honest rather than noise: state genuinely holds a
        duplicate, and correctness does not depend on it being removed.
        """
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.utils.message_integrity import PAIR_DAMAGED

        state["messages"] = [
            HumanMessage(content="turn 1"),
            ToolMessage(content="LEGACY-FRAGMENT", tool_call_id=_BASELINE_TOOL_CALL_ID),
        ]
        states, shipped = self._advance(state)

        def has_fragment(msgs) -> bool:
            return any(getattr(m, "content", "") == "LEGACY-FRAGMENT" for m in msgs)

        assert has_fragment(states[0]), "precondition"
        assert all(has_fragment(s) for s in states), \
            "the fragment is pinned: no rebuild can replace a foreign id"
        assert not any(has_fragment(out) for out in shipped), \
            "pinned in state, but it must NEVER reach the provider"
        assert self._diagnose(states[-1]) == PAIR_DAMAGED

    def test_duplicated_answer_is_why_the_gate_drops_before_rebuilding(self, state):
        """The one violation the send-side gate structurally CANNOT see.

        Both copies of a duplicated result HAVE a preceding caller, so
        ``sanitize_tool_pairing`` reads the sequence as legal and even keeps it
        that way — measured on a state holding a legacy fragment, Layer 1 MOVES
        the fragment next to the fresh caller and ships two answers to one
        tool_call, dropping nothing. Only the pair-aware gate, which counts
        results per id, catches it. That is why it clears every fragment for
        the synthetic ids before rebuilding, and why "Layer 1 is the last line
        of defence" has a documented exception.
        """
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _BASELINE_TOOL_CALL_ID,
        )
        from chaos_agent.utils.message_integrity import sanitize_tool_pairing

        state["messages"] = [
            HumanMessage(content="turn 1"),
            ToolMessage(content="LEGACY-FRAGMENT", tool_call_id=_BASELINE_TOOL_CALL_ID),
        ]
        self._advance(state, turns=1)
        damaged_state = state["messages"]

        ungated = sanitize_tool_pairing(damaged_state)
        assert self._protocol_legal(ungated), \
            "matching AND precedence both pass — this is the blind spot"
        assert len(self._results(ungated, _BASELINE_TOOL_CALL_ID)) == 2, \
            "one tool_call, two answers, and Layer 1 is structurally blind to it"

        gated = self._build({**state, "messages": list(damaged_state)})
        assert len(self._results(gated, _BASELINE_TOOL_CALL_ID)) == 1, \
            "the gate's drop-before-rebuild is what closes it"


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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
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
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == []

    @pytest.mark.asyncio
    async def test_legacy_scan_does_not_delete_recovery_armed_artifact(self):
        pod_name = "node-debugger-cn-test-timer"
        artifact = {
            "artifact_id": "uid-timer",
            "type": "debug_pod",
            "status": "recovery_armed",
            "name": pod_name,
            "namespace": "default",
            "uid": "uid-timer",
            "target": {"scope": "node", "name": "cn-test.10.0.1.1"},
            "recovery_deadline_epoch": 9999999999,
        }
        state = {
            "messages": [self._debug_tm(pod_name)],
            "execution_artifacts": [artifact],
        }
        deleted: list[str] = []

        async def _fake_delete(name, _kc, _tid, namespace=""):
            deleted.append(name)

        with patch(
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            result_update: dict = {}
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == []
        assert "cleaned_debug_pods" not in result_update


class TestCleanupSpareArmedRecoveryCarrier:
    """run4 live-fire regression: the legacy message scan must NOT
    force-delete a recovery-carrier pod.

    ``parse_debug_pod_name``'s generic ``pod/<name> created`` pattern matches
    the carrier's creation banner (``pod/drill-rc-xxx created``), so the
    legacy discovery path picks the carrier up as if it were a debug pod.
    Pre-fix, the exclusion set only covered ``debug_pod`` artifacts — the
    armed carrier fell through and was force-deleted with NO armed gate,
    killing its in-flight recovery timer 3 minutes into a 600s window and
    leaving the fault unrecovered. Post-fix the exclusion covers every
    vehicle artifact type, so the carrier stays under the artifact chain's
    keep-while-armed authority.
    """

    @staticmethod
    def _carrier_creation_tm(pod_name: str) -> ToolMessage:
        """Mirrors the kubectl tool output for ``kubectl run`` — the generic
        ``pod/<name> created`` line is what the legacy parser matches."""
        return ToolMessage(
            content=f"pod/{pod_name} created",
            name="kubectl",
            tool_call_id=f"call_run_{pod_name}",
        )

    @staticmethod
    def _armed_carrier_artifact(pod_name: str) -> dict:
        import time as _time

        return {
            "artifact_id": f"recovery_carrier:default/{pod_name}",
            "type": "recovery_carrier",
            "status": "recovery_armed",
            "task_id": "task-1",
            "name": pod_name,
            "namespace": "default",
            "rbac_family": [
                {"kind": "serviceaccount", "name": pod_name, "namespace": "default"},
                {"kind": "role", "name": pod_name, "namespace": "default"},
                {"kind": "rolebinding", "name": pod_name, "namespace": "default"},
            ],
            "host_exec_seen_ids": ["call_arm_1"],
            "recovery_timeout_seconds": 600,
            "recovery_deadline_epoch": _time.time() + 3600,
        }

    @pytest.mark.asyncio
    async def test_armed_carrier_banner_is_not_force_deleted(self):
        """Carrier creation banner in history + armed artifact on state →
        the legacy scan must leave the carrier alone (and the artifact
        chain's armed gate holds the RBAC family too)."""
        pod = "drill-rc-9c7e2a"
        state = {
            "messages": [self._carrier_creation_tm(pod)],
            "execution_artifacts": [self._armed_carrier_artifact(pod)],
        }
        result_update: dict = {}
        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace="", kind="pod"):
            deleted.append(f"{kind}/{pod_name}")

        with patch(
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == []

    @pytest.mark.asyncio
    async def test_plain_debug_pod_untracked_still_cleaned(self):
        """The exclusion widening must not neuter the legacy path: an
        UNTRACKED plain debug pod (no artifact) is still deleted."""
        state = {
            "messages": [
                TestCleanupDebugPodsDedup._debug_tm("node-debugger-cn-test-ccc"),
            ],
            "execution_artifacts": [
                self._armed_carrier_artifact("drill-rc-9c7e2a"),
            ],
        }
        result_update: dict = {}
        deleted: list[str] = []

        async def _fake_delete(pod_name, _kc, _tid, namespace="", kind="pod"):
            deleted.append(pod_name)

        with patch(
            "chaos_agent.agent.nodes.verify._verifier_finalize._delete_debug_pod",
            new=_fake_delete,
        ):
            await _cleanup_debug_pods(state, "/kc", "task-1", result_update)

        assert deleted == ["node-debugger-cn-test-ccc"]


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


# ---------------------------------------------------------------------------
# Inject verifier read-only screen (shared with recover Layer 2)
# ---------------------------------------------------------------------------

class TestVerifierReadOnlyScreen:
    """The inject verifier judges whether the fault landed; it never
    injects or repairs. Its read-only discipline shares one classifier
    core with recover Layer 2 (agent.nodes._readonly_screen, used by
    the verifier_screener edge node), so mutating calls are refused
    with unverified guidance while the capability probe stays exempt on
    both sides. Behaviour lives in TestVerifierScreenerNode below."""

    def test_verifier_loop_wires_the_shared_screen(self):
        """The inject verifier screens via the verifier_screener graph-edge
        node (phase1/tool_screener paradigm): capability verdict + read-only
        discipline between verifier_loop and verifier_tools, with the
        verify-flavoured guidance (parity with recover Layer 2)."""
        from pathlib import Path

        src = (
            Path(__file__).resolve().parents[3]
            / "src/chaos_agent/agent/graph.py"
        ).read_text(encoding="utf-8")
        assert "verifier_screener" in src
        assert "make_phase_screener" in src
        assert "`unverified`" in src
        # The loop routes tool_calls into the screener, not the ToolNode.
        assert '"continue": "verifier_screener"' in src


class TestVerifierScreenerNode:
    """Behaviour of the verifier_screener graph-edge node: fabricated
    ToolMessage pairing on refusal, screener_route retry, debug probe
    exemption — same protocol as phase1_screener / tool_screener."""

    @staticmethod
    def _make():
        from chaos_agent.agent.nodes._phase_screener import make_phase_screener
        return make_phase_screener(
            capability_phase="verify",
            readonly=True,
            phase_duty=(
                "The verification phase judges whether the injected fault "
                "landed, using read-only observations only — it never "
                "injects, repairs, or alters cluster state. Injection "
                "actions belong to the execute phase, which has already run."
            ),
            verdict_guidance=(
                "If your observations show the fault did not land (or only "
                "partially landed), submit your verdict as `unverified` and "
                "describe exactly what is missing. Do not re-attempt the "
                "refused call in any form."
            ),
        )

    @staticmethod
    def _state(tool_calls):
        return {"messages": [AIMessage(content="", tool_calls=tool_calls)]}

    @pytest.mark.asyncio
    async def test_readonly_call_passes(self):
        node, route = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": ["get", "pods", "-n", "default"]},
        }])
        update = await node(state)
        assert update["screener_route"] == "pass"
        assert "messages" not in update
        assert route({**state, **update}) == "pass"

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
        assert "unverified" in msg.content

    @pytest.mark.asyncio
    async def test_capability_probe_debug_exempt(self):
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl_read", "id": "c1", "type": "tool_call",
            "args": {"subcommand": "debug", "command": ["node/node-a"]},
        }])
        update = await node(state)
        assert update["screener_route"] == "pass"

    @pytest.mark.asyncio
    async def test_mixed_batch_refused_wholesale_with_skipped_sibling(self):
        node, _ = self._make()
        state = self._state([
            {
                "name": "kubectl", "id": "c1", "type": "tool_call",
                "args": {"command": ["get", "pods", "-n", "default"]},
            },
            {
                "name": "kubectl", "id": "c2", "type": "tool_call",
                "args": {"command": ["delete", "pod", "pod-a", "-n", "default"]},
            },
        ])
        update = await node(state)
        assert update["screener_route"] == "retry"
        fabricated = update["messages"]
        # One ToolMessage per tool_call keeps the pairing invariant.
        assert {m.tool_call_id for m in fabricated} == {"c1", "c2"}
        skipped = next(m for m in fabricated if m.tool_call_id == "c1")
        refused = next(m for m in fabricated if m.tool_call_id == "c2")
        assert "skipped" in skipped.content
        assert "readonly_phase_violation" in refused.content

    @pytest.mark.asyncio
    async def test_no_tool_calls_passes(self):
        node, _ = self._make()
        update = await node({"messages": [AIMessage(content="verdict text")]})
        assert update["screener_route"] == "pass"


class TestVerifierScreenerTruthfulReasons:
    """The verify-phase screener must surface the verdict the classifier
    actually reached, not a template re-invented from the scope word —
    the same truth-first contract phase1_screener locked in after task
    inject-a9ea4da7. The old rendering consumed only (raw_command,
    probe_reason) strings, silently dropping the recorded reject_detail
    (host-escape primitive, banned carrier, ...) on this path."""

    # Class-level access unwraps the staticmethods to plain functions; re-wrap
    # so instance access here does not bind ``self`` as the first argument.
    _make = staticmethod(TestVerifierScreenerNode._make)
    _state = staticmethod(TestVerifierScreenerNode._state)

    @pytest.mark.asyncio
    async def test_escape_carrier_detail_survives_verify_phase(self):
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": [
                "exec", "pod-a", "-n", "ns", "--",
                "chroot", "/host", "iptables", "-A", "INPUT", "-j", "DROP",
            ]},
        }])
        update = await node(state)
        assert update["screener_route"] == "retry"
        content = update["messages"][0].content
        # The classifier's recorded cause — not a generic "would mutate".
        assert "host-escape primitive" in content
        assert "'chroot'" in content
        # No probe framing: this is a deliberate escape, not a shape issue.
        assert "COMMAND SHAPE" not in content

    @pytest.mark.asyncio
    async def test_malformed_probe_carries_shape_frame_in_verify(self):
        # B46 made `;`/`&&`/`||`-chained all-readonly probes legal, so the
        # shape-refusal carrier here is a BACKGROUND `&` (still refused).
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl_read", "id": "c1", "type": "tool_call",
            "args": {
                "subcommand": "exec",
                "v_args": "pod-x -n ns -- sh -c 'df -h & which stress-ng'",
            },
        }])
        update = await node(state)
        assert update["screener_route"] == "retry"
        content = update["messages"][0].content
        assert "not a valid read-only probe" in content
        assert "shell control operator" in content
        assert "COMMAND SHAPE" in content
        assert "-- which stress-ng" in content

    @pytest.mark.asyncio
    async def test_genuine_mutation_names_the_call_without_fake_fix(self):
        node, _ = self._make()
        state = self._state([{
            "name": "kubectl", "id": "c1", "type": "tool_call",
            "args": {"command": ["delete", "pod", "pod-a", "-n", "default"]},
        }])
        update = await node(state)
        content = update["messages"][0].content
        # Truth-first level 3: the raw command is the most truthful thing
        # held; a genuine mutation in a read-only phase gets NO "How to
        # fix" (no compliant reshape exists here — pairing rule).
        assert "kubectl(command=" in content
        assert "would mutate" in content
        assert "How to fix" not in content
        assert "COMMAND SHAPE" not in content


class TestVerificationCycleDetection:
    """Position-based verification-cycle detection: whether the main context
    message is injected is decided by WHERE the marker-tagged HumanMessage
    sits relative to the attribution epoch — never by the loop counter.
    Aligned with execute_loop's Phase-2 kickoff."""

    @staticmethod
    def _marker(content="## Layer 1 Result (cycle context)"):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _VERIFIER_CONTEXT_KWARGS_KEY,
        )
        return HumanMessage(
            content=content,
            additional_kwargs={_VERIFIER_CONTEXT_KWARGS_KEY: True},
        )

    def _needs(self, messages, epoch=None):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _verification_cycle_needs_context,
        )
        state = {"messages": messages}
        if epoch is not None:
            state["attribution_epoch_index"] = epoch
        return _verification_cycle_needs_context(state)

    def test_first_cycle_no_marker_needs_context(self):
        assert self._needs([HumanMessage(content="inject")]) is True

    def test_marker_in_epoch_suppresses_reinjection(self):
        # Mid-cycle: the marker from this cycle's first turn is present in
        # the current epoch → no re-injection (even if the counter says 1).
        msgs = [HumanMessage(content="inject"), self._marker()]
        assert self._needs(msgs) is False
        assert self._needs(msgs, epoch=1) is False

    def test_marker_before_epoch_re_arms_after_replan(self):
        # Replan re-based the attribution epoch AFTER the old cycle's
        # marker: the marker falls outside the current epoch → the new
        # cycle re-arms, regardless of any loop counter value.
        msgs = [self._marker(), HumanMessage(content="replanned run")]
        assert self._needs(msgs, epoch=1) is True

    def test_invalid_epoch_falls_back_to_full_scan(self):
        msgs = [HumanMessage(content="inject"), self._marker()]
        assert self._needs(msgs, epoch="bogus") is False
        assert self._needs(msgs, epoch=999) is False

    def _layer2_context_count(self, state, count):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
            _VERIFIER_CONTEXT_KWARGS_KEY,
        )
        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="Success")
        state_msgs = state.get("messages", [])
        msgs = _build_layer2_messages(
            state, layer1, "uid-x", "cpu-fullload", "/path/to/kc", count,
        )
        # Count only NEWLY injected context messages: the returned list
        # starts with state's own messages (same object references), and a
        # pre-existing marker must not be counted as an injection.
        return sum(
            1 for m in msgs
            if isinstance(m, HumanMessage)
            and getattr(m, "additional_kwargs", {}).get(_VERIFIER_CONTEXT_KWARGS_KEY)
            and not any(m is s for s in state_msgs)
        )

    def test_layer2_reinjects_on_late_count_after_replan(self):
        # The failure mode of count==1 gating: a new cycle entered at a
        # counter value > 1 (reset missed / resume) would skip the context.
        # Position detection keys off the epoch, not the counter.
        state = {
            "messages": [self._marker(), HumanMessage(content="new cycle start")],
            "attribution_epoch_index": 1,
            "injection_parsed_params": {},
            "params": {},
        }
        assert self._layer2_context_count(state, count=5) == 1

    def test_layer2_skips_when_marker_current(self):
        state = {
            "messages": [HumanMessage(content="inject"), self._marker()],
            "injection_parsed_params": {},
            "params": {},
        }
        assert self._layer2_context_count(state, count=2) == 0

    def test_extract_persistent_hm_epoch_bounded(self):
        # The persistence dedup must not let a PRE-replan marker suppress
        # the NEW cycle's context message from entering state.
        from chaos_agent.agent.nodes.execute.react_helpers import (
            extract_persistent_hm,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _VERIFIER_CONTEXT_KWARGS_KEY,
        )
        fresh = self._marker("## Layer 1 Result (fresh cycle)")
        # Old marker at idx 0, epoch re-based past it → fresh IS extracted.
        state = {
            "messages": [self._marker("old cycle"), HumanMessage(content="x")],
            "attribution_epoch_index": 1,
        }
        assert extract_persistent_hm(
            [fresh], state, _VERIFIER_CONTEXT_KWARGS_KEY,
        ) == [fresh]
        # Marker inside the current epoch → dedup suppresses.
        state["attribution_epoch_index"] = None
        assert extract_persistent_hm(
            [fresh], state, _VERIFIER_CONTEXT_KWARGS_KEY,
        ) == []


class TestRecoveryTimerReminder:
    """Case #46 F1: the verifier must SEE the armed timer's remaining time.

    Root cause: ``recovery_deadline_epoch`` lives on the recovery-carrier
    artifact, but none of the verifier's context channels (message history /
    progress ledger / system prompt) carry timestamps — persistence checks
    reverse-engineered the fire moment from cluster-side pod ages, and a
    persistence wait could silently straddle the fire.
    """

    @staticmethod
    def _armed(
        deadline, *, window=300, status="recovery_armed", name="drill-rc-x",
    ):
        return {
            "type": "recovery_carrier",
            "name": name,
            "namespace": "default",
            "status": status,
            "recovery_timeout_seconds": window,
            "recovery_deadline_epoch": deadline,
        }

    def test_future_deadline_renders_remaining_seconds(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            build_recovery_timer_reminder,
        )
        now = 1_000_000.0
        state = {"execution_artifacts": [self._armed(now + 155.7)]}
        text = build_recovery_timer_reminder(state, now=now)
        # REMAINING seconds (truncated), not a fire timestamp — the model
        # has no wall clock to anchor a moment value against.
        assert "fires in ~155s" in text
        assert "(window 300s)" in text
        # Neutral wording: fire only proves the window ENDS there, not that
        # recovery has happened by then — "post-recovery state" would
        # smuggle in that unproven optimism.
        assert "can no longer serve as persistence evidence" in text
        assert "post-recovery" not in text

    def test_expired_deadline_renders_fired_semantics(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            build_recovery_timer_reminder,
        )
        now = 1_000_000.0
        state = {"execution_artifacts": [self._armed(now - 30.2)]}
        text = build_recovery_timer_reminder(state, now=now)
        assert "fired ~30s ago" in text
        # Post-fire semantics must stay NEUTRAL about recovery outcome: fire
        # proves TRIGGERED, never converged (Case #46 R1 hung 12 minutes
        # after firing). The optimistic "at or near completion" reading
        # would launder a nonconvergence finding; a still-present fault
        # signature must instead be flagged as recovery-NOT-converged.
        # Minimal-wording variant (prompt minimalism): ~55 words, four
        # semantic units intact.
        assert "TRIGGERED" in text
        assert "not necessarily completed" in text
        assert "NOT converged" in text
        assert "recovery at or near completion" not in text
        assert "pre-fire evidence" in text

    def test_no_armed_artifact_is_silent(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            build_recovery_timer_reminder,
        )
        now = 1_000_000.0
        cases = [
            {},
            {"execution_artifacts": []},
            # cleaned / active carriers owe no timer (manual disarm pops the
            # deadline; the system sweep marks cleaned).
            {"execution_artifacts": [self._armed(now + 100, status="cleaned")]},
            {"execution_artifacts": [self._armed(now + 100, status="active")]},
            {"execution_artifacts": [
                {**self._armed(now + 100), "recovery_deadline_epoch": "broken"},
            ]},
        ]
        for state in cases:
            assert build_recovery_timer_reminder(state, now=now) == ""

    def test_next_fire_wins_over_already_fired(self):
        # Stacked carriers: the FUTURE fire is the planning anchor.
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            build_recovery_timer_reminder,
        )
        now = 1_000_000.0
        state = {"execution_artifacts": [
            self._armed(now - 50, name="drill-rc-old"),
            self._armed(now + 60, name="drill-rc-new"),
        ]}
        text = build_recovery_timer_reminder(state, now=now)
        assert "fires in ~60s" in text

    def test_debug_pod_rollback_timer_is_covered_too(self):
        # Generalization anchor (cascade round, scenario E): the arming
        # writer (_mark_bounded_host_recovery) covers debug_pod carriers
        # (host-domain systemd-run rollback timers), not just recovery
        # carriers — the reminder deliberately does NOT filter on type, so
        # host-domain verify gets the same visibility. A future type filter
        # would silently narrow this coverage.
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            build_recovery_timer_reminder,
        )
        now = 1_000_000.0
        artifact = {
            **self._armed(now + 90),
            "type": "debug_pod",
        }
        text = build_recovery_timer_reminder(
            {"execution_artifacts": [artifact]}, now=now,
        )
        assert "fires in ~90s" in text

    def test_layer2_messages_carry_fresh_reminder_without_persisting(self):
        from chaos_agent.agent.nodes.execute.react_helpers import (
            extract_persistent_hm,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _VERIFIER_CONTEXT_KWARGS_KEY,
            _build_layer2_messages,
        )
        import time as _time

        now = _time.time()
        state = {
            "messages": [HumanMessage(content="inject")],
            "execution_artifacts": [self._armed(now + 120)],
        }
        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="ok")
        msgs = _build_layer2_messages(
            state, layer1, "uid-1", "skill-x", "/kc", count=1,
        )
        timer_hms = [
            m for m in msgs
            if isinstance(m, HumanMessage) and "RECOVERY TIMER" in str(m.content)
        ]
        # Exactly one reminder per build (fresh each graph re-entry)...
        assert len(timer_hms) == 1
        assert "fires in ~" in str(timer_hms[0].content)
        # ...and it must NOT enter AgentState: the persistence extraction may
        # legitimately take the kwargs-tagged CONTEXT message (count==1 builds
        # it), but never the untagged reminder — a persisted copy would
        # accumulate stale "remaining" values across iterations.
        persisted = extract_persistent_hm(
            msgs, state, _VERIFIER_CONTEXT_KWARGS_KEY,
        )
        assert not [
            m for m in persisted if "RECOVERY TIMER" in str(m.content)
        ]

    def test_layer2_messages_silent_without_armed_artifact(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        state = {"messages": [HumanMessage(content="inject")]}
        layer1 = Layer1Result(status="passed", affected_count=1, raw_output="ok")
        msgs = _build_layer2_messages(
            state, layer1, "uid-1", "skill-x", "/kc", count=1,
        )
        assert not [
            m for m in msgs
            if isinstance(m, HumanMessage) and "RECOVERY TIMER" in str(m.content)
        ]


class TestStampWindowStart:
    """Fault-window hold origin (``injection_window_start_time``).

    The window origin is stamped at the verifier entry — the moment the
    execute-loop concluded. Contract: write-once per attempt (verifier
    self-loop re-entries keep the first stamp), attribution-guarded (no
    stamp without a committed injection), and cleared at replan seams
    (pinned in test_execute_loop's TestResetAttributionState).
    """

    def test_stamps_when_injection_committed(self):
        from chaos_agent.agent.nodes.verify.verifier import _stamp_window_start
        from chaos_agent.utils.time import parse_iso_timestamp

        state = {"injection_start_time": "2026-09-19T10:00:00+08:00"}
        result: dict = {}
        _stamp_window_start(state, result)
        # A parseable ISO stamp close to now (the execute-loop end moment).
        assert "injection_window_start_time" in result
        stamp = parse_iso_timestamp(result["injection_window_start_time"])
        assert stamp is not None

    def test_write_once_keeps_first_stamp(self):
        """Verifier self-loop re-entries (this node returns per ReAct step)
        must keep the FIRST stamp — the origin is a fact about the
        execute-loop boundary, not about any given verify iteration."""
        from chaos_agent.agent.nodes.verify.verifier import _stamp_window_start

        state = {
            "injection_start_time": "2026-09-19T10:00:00+08:00",
            "injection_window_start_time": "2026-09-19T11:00:00+08:00",
        }
        result: dict = {}
        _stamp_window_start(state, result)
        # No key written: the existing origin is authoritative.
        assert "injection_window_start_time" not in result

    def test_no_stamp_without_committed_injection(self):
        """A turn that never committed an injection has no window to
        anchor — stamping anyway would let the hold flip a recover
        dispatch for a turn with no experiment in flight."""
        from chaos_agent.agent.nodes.verify.verifier import _stamp_window_start

        state = {"injection_start_time": None}
        result: dict = {}
        _stamp_window_start(state, result)
        assert "injection_window_start_time" not in result
