"""Tests for execute_loop node."""

import pytest
from langchain_core.messages import ToolMessage, AIMessage, HumanMessage

from chaos_agent.agent.nodes.execute.execute_loop import (
    execute_loop,
    _detect_injection_method,
    _should_redetect_injection_method,
)

# Phase-5 canonical address (kept under the historical call name).
from chaos_agent.agent.providers.chaosblade.verify import (
    extract_experiment_uid_from_messages as _extract_blade_uid_from_messages,
)
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
        import chaos_agent.agent.nodes.execute.execute_loop as loop_mod

        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 5)

        state = sample_agent_state
        state["execute_loop_count"] = 5

        result = await execute_loop(state)
        assert "error" in result
        assert "execution_timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_at_max_iterations_still_ok(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 10)
        import chaos_agent.agent.nodes.execute.execute_loop as loop_mod

        monkeypatch.setattr(loop_mod, "MAX_EXECUTE_LOOP", 10)

        state = sample_agent_state
        state["execute_loop_count"] = 9

        result = await execute_loop(state)
        assert result["execute_loop_count"] == 10
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_exceeds_max_by_one(self, sample_agent_state, monkeypatch):
        monkeypatch.setattr(settings, "max_execute_loop", 2)
        import chaos_agent.agent.nodes.execute.execute_loop as loop_mod

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
        import chaos_agent.agent.nodes.execute.execute_loop as loop_mod

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
            content='{"code": 200, "success": true, "result": "abc123def4567890"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        messages = [AIMessage(content="planning"), msg]
        assert _extract_blade_uid_from_messages(messages) == "abc123def4567890"

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
            content='{"code": 200, "success": true, "result": "abc123def4567890"}',
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
            content='{"code": 200, "success": true, "result": "0d1a2b3c4d5e6f71"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code": 200, "success": true, "result": "e1f2a3b4c5d60718"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        # reversed scan, so finds msg2 first
        assert _extract_blade_uid_from_messages(messages) == "e1f2a3b4c5d60718"

    def test_destroyed_uid_not_returned(self):
        """Root-cause guard: a UID sent to blade_destroy is cleaned-up/residual
        and must NOT be returned as the active injection, even if blade_create
        originally reported it. (Contrast with test_blade_create_tool_message,
        where the same create result IS returned when no destroy happened.)"""
        ai_destroy = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "blade_destroy",
                    "args": {"uid": "deadbeef00000001"},
                    "id": "bd1",
                },
            ],
        )
        create_ok = ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            tool_call_id="bc1",
            name="blade_create",
        )
        destroy_ok = ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            tool_call_id="bd1",
            name="blade_destroy",
        )
        messages = [create_ok, ai_destroy, destroy_ok]
        assert _extract_blade_uid_from_messages(messages) is None


class TestExtractBladeUidKubectlExec:
    """Tests for _extract_blade_uid_from_messages with kubectl exec blade output."""

    def test_kubectl_exec_blade_success(self):
        """kubectl exec blade ToolMessage with ChaosBlade success JSON → extract uid."""
        from langchain_core.messages import AIMessage

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "id": "tc1",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "pod1 -n chaosblade -- blade create k8s pod-cpu fullload",
                    },
                }
            ],
        )
        tool_msg = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        assert (
            _extract_blade_uid_from_messages([ai_msg, tool_msg]) == "a0f2357a939a9bb8"
        )

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
            content="NAME   STATUS   AGE\npod1   Running  5d",
            tool_call_id="tc1",
            name="kubectl",
        )
        assert _extract_blade_uid_from_messages([msg]) is None

    def test_blade_create_priority_over_kubectl(self):
        """blade_create result takes priority over kubectl result."""
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"cafeba5e00000003"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"b1ade5eed00000004"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        # Reversed scan: msg2 (blade_create) is checked first and returned
        assert _extract_blade_uid_from_messages(messages) == "b1ade5eed00000004"

    def test_failed_blade_create_with_kubectl_success(self):
        """Failed blade_create + successful kubectl exec → kubectl uid as fallback."""
        from langchain_core.messages import AIMessage

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "id": "tc2",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "pod1 -n chaosblade -- blade create k8s pod-cpu fullload",
                    },
                }
            ],
        )
        msg1 = ToolMessage(
            content="Error: blade create failed (exit 1): unknown flag: --namespace",
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            tool_call_id="tc2",
            name="kubectl",
        )
        messages = [ai_msg, msg1, msg2]
        # msg1 is not valid JSON, msg2 provides the fallback uid
        assert _extract_blade_uid_from_messages(messages) == "a0f2357a939a9bb8"

    def test_multiple_kubectl_results_uses_latest(self):
        """Multiple kubectl exec blade results → returns the latest one."""
        from langchain_core.messages import AIMessage

        ai_msg = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "id": "tc1",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "pod1 -- blade create k8s pod-cpu fullload",
                    },
                },
                {
                    "name": "kubectl",
                    "id": "tc2",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "pod1 -- blade create k8s pod-cpu fullload",
                    },
                },
            ],
        )
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"0dcafeba5e000005"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"cafeba5e00000006"}',
            tool_call_id="tc2",
            name="kubectl",
        )
        messages = [ai_msg, msg1, msg2]
        # Reversed scan: msg2 is found first
        assert _extract_blade_uid_from_messages(messages) == "cafeba5e00000006"

    def test_kubectl_query_output_not_extracted(self):
        """kubectl exec blade query k8s output has dict result → not extracted as uid."""
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":{"uid":"abc123def4567890","success":true}}',
            tool_call_id="tc1",
            name="kubectl",
        )
        # result is a dict, not a string → should not be extracted as blade_uid
        assert _extract_blade_uid_from_messages([msg]) is None


class TestExtractBladeUidRetired:
    """Tests for the ``retired`` filter of _extract_blade_uid_from_messages.

    Framework-side verify-replan cleanup destroys experiments in CODE (no
    blade_destroy ToolMessage), so retired_experiment_uids is the only record
    that a UID is dead. Extraction must treat retired UIDs exactly like destroyed
    ones (task-29848471).
    """

    def test_retired_uid_not_returned(self):
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        messages = [msg]
        # Without retired filter the UID is live; with it, it must vanish.
        assert _extract_blade_uid_from_messages(messages) == "deadbeef00000001"
        assert (
            _extract_blade_uid_from_messages(messages, retired=["deadbeef00000001"])
            is None
        )
        assert (
            _extract_blade_uid_from_messages(messages, retired={"deadbeef00000001"})
            is None
        )

    def test_retired_does_not_mask_live_uid(self):
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"0d1a2b3c4d5e6f71"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"e1f2a3b4c5d60718"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        # Only the old UID was retired by cleanup; the re-injected UID stays.
        assert (
            _extract_blade_uid_from_messages(
                messages,
                retired=["0d1a2b3c4d5e6f71"],
            )
            == "e1f2a3b4c5d60718"
        )

    def test_retired_skips_over_dead_uid_to_live_one(self):
        """Retired latest UID → fall through to the older live UID."""
        msg1 = ToolMessage(
            content='{"code":200,"success":true,"result":"1eafbeef00000002"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        msg2 = ToolMessage(
            content='{"code":200,"success":true,"result":"deadbeef00000001"}',
            tool_call_id="tc2",
            name="blade_create",
        )
        messages = [msg1, msg2]
        assert (
            _extract_blade_uid_from_messages(
                messages,
                retired=["deadbeef00000001"],
            )
            == "1eafbeef00000002"
        )

    def test_retired_empty_is_noop(self):
        msg = ToolMessage(
            content='{"code":200,"success":true,"result":"b1c2d3e4f5a60789"}',
            tool_call_id="tc1",
            name="blade_create",
        )
        assert (
            _extract_blade_uid_from_messages([msg], retired=None) == "b1c2d3e4f5a60789"
        )
        assert _extract_blade_uid_from_messages([msg], retired=[]) == "b1c2d3e4f5a60789"


class TestResetAttributionState:
    """Tests for reset_attribution_state (shared replan-seam reset)."""

    def _populated(self) -> dict:
        return {
            "experiment_uid": "a1b2c3d4e5f60718",
            "fault_handle": {
                "kind": "blade_uid",
                "value": "a1b2c3d4e5f60718",
                "method": "kubectl_exec",
            },
            "fault_readback_verified": "cms-demo/fd-x",
            "injection_method": "kubectl_exec",
            "combo_native_issued": True,
            "kubectl_exec_pod_name": "tool-pod",
            "inject_layer1_cache": {"status": "passed"},
            "injection_start_time": 12345.0,
            "injection_window_start_time": "2026-09-19T10:00:00+08:00",
            "unrelated_field": "keep-me",
        }

    def test_default_clears_all_attribution(self):
        result = self._populated()
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        reset_attribution_state(result)
        assert result["experiment_uid"] is None
        assert result["fault_handle"] is None
        assert result["injection_method"] is None
        assert result["combo_native_issued"] is None
        assert result["kubectl_exec_pod_name"] is None
        assert result["inject_layer1_cache"] is None
        assert result["injection_start_time"] is None
        # Fault-window hold origin: retired with the attribution — the
        # replanned attempt's window re-anchors at its own execute-loop end.
        assert result["injection_window_start_time"] is None
        assert result["unrelated_field"] == "keep-me"

    def test_keep_experiment_uid_preserves_live_experiment(self):
        """Execute-replan with existing_experiment_uids keeps the UID (recover must
        still reach it) while re-arming method re-detection."""
        result = self._populated()
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        reset_attribution_state(result, keep_experiment_uid=True)
        assert result["experiment_uid"] == "a1b2c3d4e5f60718"
        # A live experiment keeps its handle for the recover graph.
        assert result["fault_handle"]["value"] == "a1b2c3d4e5f60718"
        assert result["injection_method"] is None
        assert result["kubectl_exec_pod_name"] is None
        # The window origin does NOT survive even with a live experiment: a
        # keep-UID seam is still a replan, and the next attempt's window must
        # re-anchor at its own verifier entry (execute-loop end).
        assert result["injection_window_start_time"] is None

    def test_keep_experiment_uid_preserves_combo_marker(self):
        """A live experiment keeps its native companion: the combo marker
        belongs to the same attribution as the UID."""
        result = self._populated()
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        reset_attribution_state(result, keep_experiment_uid=True)
        assert result.get("combo_native_issued") is True

    def test_accepts_partial_dict(self):
        """Verify-replan result_update starts sparse; missing keys are fine."""
        result: dict = {"replan_requested": True}
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        reset_attribution_state(result)
        assert result["experiment_uid"] is None
        assert result["injection_method"] is None

    def test_state_messages_records_epoch_boundary(self):
        """The replan seam records the attribution epoch boundary so the
        RESUME re-detection scan cannot resurrect pre-seam attempts
        (task-5193538b). With no synthetic pair in state the boundary is the
        post-merge length itself."""
        from langchain_core.messages import HumanMessage

        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        state_messages = [HumanMessage(content=f"m{i}") for i in range(7)]
        result = self._populated()
        reset_attribution_state(result, state_messages=state_messages)
        assert result["attribution_epoch_index"] == 7

    def test_no_state_messages_leaves_boundary_unset(self):
        """Callers that pass no state_messages keep the pre-fix behaviour
        (full-history scan) and no pair cleanup."""
        result = self._populated()
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        reset_attribution_state(result)
        assert "attribution_epoch_index" not in result
        assert "messages" not in result

    def test_state_messages_removes_persisted_pair_and_aligns_epoch(self):
        """The replan seam removes the previous verification cycle's
        synthetic baseline pair by its stable ids (change
        ``stale-baseline-pair-seam-cleanup``): every PRESENT id gets exactly
        one RemoveMessage (referencing the exported constant, not literals),
        and the epoch boundary equals the POST-MERGE length — the four
        deletions must not overshoot into the epoch consumers' full-history
        fallback (task-5193538b defence stays armed)."""
        from langchain_core.messages import (
            AIMessage,
            HumanMessage,
            RemoveMessage,
            ToolMessage,
        )

        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            BASELINE_PAIR_MESSAGE_IDS,
        )
        from chaos_agent.agent.state import _ts_add_messages

        assert len(BASELINE_PAIR_MESSAGE_IDS) == 4, "caller/result x baseline/metrics"

        # Build a state holding the full pair set, generically from the
        # exported constant so an id rename cannot silently disarm this pin.
        state_messages: list = [HumanMessage(content="h", id="real-1")]
        for mid in sorted(BASELINE_PAIR_MESSAGE_IDS):
            if mid.endswith(":caller"):
                state_messages.append(AIMessage(id=mid, content=""))
            else:
                state_messages.append(
                    ToolMessage(id=mid, content="x", tool_call_id="tc")
                )
        state_messages.append(HumanMessage(content="t", id="real-2"))

        result: dict = {"replan_requested": True}
        reset_attribution_state(result, state_messages=state_messages)

        rm_ids = sorted(
            m.id for m in result["messages"] if isinstance(m, RemoveMessage)
        )
        assert rm_ids == sorted(BASELINE_PAIR_MESSAGE_IDS), (
            "one tombstone per present stable id"
        )
        merged = _ts_add_messages(state_messages, result["messages"])
        assert [m.id for m in merged] == ["real-1", "real-2"], (
            "the pair is gone; everything else survives"
        )
        assert result["attribution_epoch_index"] == len(merged), (
            "epoch boundary == post-merge length (net-length compensation)"
        )

    def test_state_messages_partial_pair_presence(self):
        """Partial presence (a pair persisted mid-flight, or an execute-time
        replan before the metrics pair landed) still aligns: tombstones only
        for the PRESENT ids (absent ids raise ValueError in add_messages),
        epoch == post-merge length."""
        from langchain_core.messages import (
            HumanMessage,
            RemoveMessage,
            ToolMessage,
        )

        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            BASELINE_PAIR_MESSAGE_IDS,
        )
        from chaos_agent.agent.state import _ts_add_messages

        present = sorted(BASELINE_PAIR_MESSAGE_IDS)[:2]
        state_messages: list = [HumanMessage(content="h", id="real-1")]
        for mid in present:
            state_messages.append(ToolMessage(id=mid, content="x", tool_call_id="tc"))
        state_messages.append(HumanMessage(content="t", id="real-2"))

        result: dict = {"replan_requested": True}
        reset_attribution_state(result, state_messages=state_messages)

        rm_ids = sorted(
            m.id for m in result["messages"] if isinstance(m, RemoveMessage)
        )
        assert rm_ids == present, "tombstones only for ids actually in state"
        merged = _ts_add_messages(state_messages, result["messages"])
        assert result["attribution_epoch_index"] == len(merged)

    def test_state_retires_terminal_ledger_phase(self):
        """C1 knife-2 + O2: the ledger's execution-complete and the plan's
        step pointer are facts about THE PLAN THAT JUST RAN — a replan
        retires that plan, so neither must survive the seam. Without this
        reset a stale marker satisfies the stall gate and the router's
        text-only branch for the NEXT plan, letting it reach the verifier
        with zero steps executed (stale-fact-across-a-seam,
        task-5193538b family)."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )
        from chaos_agent.agent.progress_ledger import (
            freeze_anchor,
            merge_progress_ledger,
        )
        from chaos_agent.tools.progress import (
            ledger_declares_execution_complete,
        )

        ledger = merge_progress_ledger(
            freeze_anchor({"scope": "pod"}, goal="演练"),
            state_update={"phase": "execution-complete", "current_step": "s3"},
        )
        ledger["log"] = [{"event": "done", "status": "observed"}]

        result = self._populated()
        reset_attribution_state(
            result, state={"progress_ledger": ledger},
        )

        merged = result["progress_ledger"]
        # Both plan-scoped fields retired...
        assert merged["state"]["phase"] is None
        assert merged["state"]["current_step"] is None
        # ...and the gates re-arm for the next plan.
        assert not ledger_declares_execution_complete(
            {"progress_ledger": merged},
        )
        # History survives the seam: the retired plan's facts stay
        # readable (the next plan may build on them).
        assert merged["anchor"]["goal"] == "演练"
        assert merged["log"] == [{"event": "done", "status": "observed"}]

    def test_state_retires_current_step_even_mid_plan(self):
        """O2: current_step is the retired plan's pointer regardless of
        phase — a mid-plan replan (phase=executing) still retires the
        plan, so the stale "step s3" must not render as the NEW plan's
        current step. phase is NOT terminal here, so it must survive
        untouched (it describes a live phase, not a completed plan)."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )
        from chaos_agent.agent.progress_ledger import merge_progress_ledger

        ledger = merge_progress_ledger(
            None,
            state_update={"phase": "executing", "current_step": "s2"},
        )

        result = self._populated()
        reset_attribution_state(
            result, state={"progress_ledger": ledger},
        )

        merged = result["progress_ledger"]
        assert merged["state"]["current_step"] is None
        assert merged["state"]["phase"] == "executing"

    def test_state_leaves_non_terminal_ledger_untouched(self):
        """No plan-scoped fields present (terminal marker absent AND
        current_step absent) → nothing to retire: the seam must not
        emit a ledger override at all (an override with a live phase
        would still be a needless write racing the next round's
        update_progress)."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        result = self._populated()
        reset_attribution_state(
            result,
            state={"progress_ledger": {
                "anchor": {}, "state": {"phase": "executing"}, "log": [],
            }},
        )
        assert "progress_ledger" not in result

    def test_state_shapes_missing_ledger_are_inert(self):
        """state omitted (default), empty, or holding an ill-shaped ledger
        → no reset, no crash: the normal first-attempt shape."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        # state omitted entirely.
        result = self._populated()
        reset_attribution_state(result)
        assert "progress_ledger" not in result
        # state without a ledger.
        reset_attribution_state(result, state={})
        assert "progress_ledger" not in result
        # ledger whose state layer is not a mapping.
        reset_attribution_state(
            result, state={"progress_ledger": {"state": "oops"}},
        )
        assert "progress_ledger" not in result
        # ledger with an ABSENT phase (never written) is not terminal.
        reset_attribution_state(
            result, state={"progress_ledger": {"state": {}, "log": []}},
        )
        assert "progress_ledger" not in result

    def test_default_clears_readback_bookkeeping_with_handle(self):
        """faultdrill-cr-channel task 2.1: the readback bookkeeping mirrors
        the handle's fate — cleared at the seam, because the next attempt's
        re-apply may carry a DIFFERENT recipe under the same carrier name
        and a stale verification would wave a stripped landing through."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        result = self._populated()
        reset_attribution_state(result)
        assert result["fault_handle"] is None
        assert result["fault_readback_verified"] is None

    def test_keep_experiment_uid_preserves_readback_bookkeeping(self):
        """A kept handle keeps its verified landing: keep_experiment_uid
        carries a live fault whose landing was already proven intact — the
        readback gate must not re-bill it on the next epoch's iterations."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            reset_attribution_state,
        )

        result = self._populated()
        reset_attribution_state(result, keep_experiment_uid=True)
        assert result["fault_handle"]["value"] == "a1b2c3d4e5f60718"
        assert result["fault_readback_verified"] == "cms-demo/fd-x"


class TestLandingReadbackGuard:
    """faultdrill-cr-channel task 2.1 (design D5): the GENERIC dispatch
    helper — programmatic executor pin. The guard runs in the
    post-execution block on the SAME iteration the landing result became
    visible, never on an LLM turn (a prompt-layer readback is observation,
    not a guard; safety rails are not delegable). These tests pin the
    dispatch contract: verdict pass → bookkeeping; verdict fail → the
    ``fail_state`` error triple (hard abort BEFORE any reconciliation
    entry); verdict None → untouched result."""

    @staticmethod
    def _fake_registry(monkeypatch, verdict):
        from chaos_agent.agent.providers import FaultProviderRegistry

        async def fake_verify(messages, state, *, kubeconfig=""):
            return verdict

        monkeypatch.setattr(
            FaultProviderRegistry, "verify_landing_readback", fake_verify,
        )
        return fake_verify

    @pytest.mark.asyncio
    async def test_pass_verdict_writes_bookkeeping(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _landing_readback_guard,
        )

        self._fake_registry(monkeypatch, {
            "ok": True, "reason": "", "handle": "cms-demo/fd-demo1", "detail": "",
        })
        result: dict = {}
        await _landing_readback_guard({}, result, [])
        assert result == {"fault_readback_verified": "cms-demo/fd-demo1"}

    @pytest.mark.asyncio
    async def test_fail_verdict_hard_aborts_with_error_triple(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _landing_readback_guard,
        )

        self._fake_registry(monkeypatch, {
            "ok": False,
            "reason": "stripped",
            "handle": "cms-demo/fd-demo1",
            "detail": (
                "recipe not found intact in the landed CR — empty/absent "
                "patches (an incompatible CRD pruned undeclared fields)"
            ),
        })
        result: dict = {}
        await _landing_readback_guard({}, result, [])
        # HARD ABORT: the fail_state triple, never a silent pass-through.
        assert result["error"]
        assert "readback guard aborted" in result["error"]
        assert result["failure_detail"]["category"] == "execution_failed"
        assert result["failure_detail"]["context"]
        # no bookkeeping on failure: the abort leaves the loop, and a
        # remembered failure would mask a genuinely-fixed re-apply
        assert "fault_readback_verified" not in result
        # replanable wording: the abort feeds the auto-replan channel
        # (fix the recipe, or route to the SOP recovery form)
        from chaos_agent.errors import should_auto_replan

        assert should_auto_replan(result["error"])

    @pytest.mark.asyncio
    async def test_none_verdict_touches_nothing(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _landing_readback_guard,
        )

        self._fake_registry(monkeypatch, None)
        result: dict = {}
        await _landing_readback_guard({}, result, [])
        assert result == {}


class TestArmSessionReconciler:
    """faultdrill-cr-channel task 2.2 (design D4): the arming dispatch
    contract. The handle comes from the readback guard's bookkeeping —
    written ONLY on a passing verdict — so a stripped landing (whose hard
    abort leaves the loop before the arm point) can never arm a
    reconciler. Arming is idempotent per handle, so re-arms on later
    iterations (state fallback) are no-ops absorbed by the registry."""

    @staticmethod
    def _fake_registry(monkeypatch):
        from chaos_agent.agent.providers import FaultProviderRegistry

        armed: list[tuple[str, str]] = []

        def fake_arm(handle_value, kubeconfig=""):
            armed.append((handle_value, kubeconfig))
            return True

        monkeypatch.setattr(
            FaultProviderRegistry, "arm_session_reconciler", fake_arm,
        )
        return armed

    def test_arms_with_the_verified_handle(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _arm_fault_reconciler,
        )

        armed = self._fake_registry(monkeypatch)
        _arm_fault_reconciler(
            {"fault_readback_verified": "cms-demo/fd-demo1"},
            {"fault_readback_verified": "cms-demo/fd-demo1"},
        )
        assert armed and armed[0][0] == "cms-demo/fd-demo1"

    def test_result_bookkeeping_arms_on_the_landing_iteration(self, monkeypatch):
        """The landing iteration arms from the RESULT dict (the guard's
        write is still local — state merge happens after the node)."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _arm_fault_reconciler,
        )

        armed = self._fake_registry(monkeypatch)
        _arm_fault_reconciler(
            {}, {"fault_readback_verified": "cms-demo/fd-demo1"},
        )
        assert armed and armed[0][0] == "cms-demo/fd-demo1"

    def test_state_fallback_re_arms_on_later_iterations(self, monkeypatch):
        """Later iterations carry the bookkeeping in STATE only — the
        fallback re-attempts the arm (idempotent at the registry), so a
        transient arming miss cannot strand an unverified-by-reconciler
        landing."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _arm_fault_reconciler,
        )

        armed = self._fake_registry(monkeypatch)
        _arm_fault_reconciler(
            {"fault_readback_verified": "cms-demo/fd-demo1"}, {},
        )
        assert armed and armed[0][0] == "cms-demo/fd-demo1"

    def test_no_verified_handle_never_dispatches(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _arm_fault_reconciler,
        )

        armed = self._fake_registry(monkeypatch)
        _arm_fault_reconciler({}, {})
        # A stripped landing hard-aborts BEFORE the arm point: no
        # bookkeeping was ever written, so no reconciler is spawned.
        assert armed == []


class TestEpochBoundedAttribution:
    """RESUME re-detection must read only the CURRENT attribution epoch.

    Regression for task-5193538b: after a replan seam the executor re-entered
    Phase 2, the history re-scan re-derived the PRE-replan attribution (stale
    diagnostic execs counted as the injection), and the node exited with a
    text-only conclusion without issuing anything.
    """

    @staticmethod
    def _mutating_exec_msg(pod: str) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "exec",
                        "v_args": f"{pod} -n chaosblade -- sh -c 'echo x >> /etc/hosts'",
                    },
                    "id": "call-1",
                }
            ],
        )

    def test_no_boundary_scans_full_history(self):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _epoch_bounded_messages,
        )
        from langchain_core.messages import HumanMessage

        msgs = [HumanMessage(content="hi"), self._mutating_exec_msg("tool-pod")]
        assert _epoch_bounded_messages(msgs, {}) == msgs
        assert _epoch_bounded_messages(msgs, {"attribution_epoch_index": None}) == msgs

    def test_boundary_excludes_pre_seam_attempts(self):
        """With the seam recorded AFTER the stale attempt, detection finds
        nothing — the executor cannot conclude 'already injected'."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_injection_method,
            _epoch_bounded_messages,
        )
        from langchain_core.messages import HumanMessage

        msgs = [
            self._mutating_exec_msg("otel-c-tool"),  # pre-seam (old contract)
            HumanMessage(content="replan approved"),  # the seam message
        ]
        state = {"attribution_epoch_index": 1}
        # Unbounded (pre-fix) scan attributes the stale attempt…
        assert _detect_injection_method(msgs, is_host=False) == "kubectl_native"
        # …the epoch-bounded scan does not.
        bounded = _epoch_bounded_messages(msgs, state)
        assert bounded == msgs[1:]
        assert _detect_injection_method(bounded, is_host=False) is None

    def test_current_epoch_attempt_still_attributed(self):
        """The boundary must never hide an injection issued AFTER the seam."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_injection_method,
            _epoch_bounded_messages,
        )
        from langchain_core.messages import HumanMessage

        msgs = [
            HumanMessage(content="replan approved"),  # the seam message
            self._mutating_exec_msg("target-pod"),  # current-epoch injection
        ]
        state = {"attribution_epoch_index": 1}
        bounded = _epoch_bounded_messages(msgs, state)
        assert _detect_injection_method(bounded, is_host=False) == "kubectl_native"

    def test_boundary_beyond_length_falls_back_to_full_scan(self):
        """Trimming may shift the list under a stored index; over-scan keeps
        pre-fix attribution instead of fabricating a window that hides the
        real attempt (never under-attribute)."""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _epoch_bounded_messages,
        )

        msgs = [self._mutating_exec_msg("tool-pod")]
        state = {"attribution_epoch_index": 99}
        assert _epoch_bounded_messages(msgs, state) == msgs

    def test_invalid_boundary_falls_back_to_full_scan(self):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _epoch_bounded_messages,
        )

        msgs = [self._mutating_exec_msg("tool-pod")]
        assert _epoch_bounded_messages(msgs, {"attribution_epoch_index": "x"}) == msgs


class TestParseBladeUidFromContent:
    """Tests for _parse_blade_uid_from_content helper."""

    def test_valid_success_json(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert (
            _parse_blade_uid_from_content(
                '{"code":200,"success":true,"result":"abc123def4567890"}'
            )
            == "abc123def4567890"
        )

    def test_failure_json(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert (
            _parse_blade_uid_from_content('{"code":500,"success":false,"error":"fail"}')
            is None
        )

    def test_non_string_result(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert (
            _parse_blade_uid_from_content(
                '{"code":200,"success":true,"result":{"uid":"abc"}}'
            )
            is None
        )

    def test_empty_result(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert (
            _parse_blade_uid_from_content('{"code":200,"success":true,"result":""}')
            is None
        )

    def test_non_json_content(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert _parse_blade_uid_from_content("not json") is None

    def test_non_string_input(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _parse_blade_uid_from_content,
        )

        assert _parse_blade_uid_from_content(None) is None


class TestParseBladeCreateFromVArgs:
    """Tests for _parse_blade_create_from_v_args helper."""

    def test_network_loss(self):
        from chaos_agent.agent.providers.chaosblade.provider import (
            _parse_blade_create_from_v_args,
        )

        v_args = (
            "otel-c-tool-xxx -n chaosblade -- blade create k8s pod-network loss "
            "--percent 100 --interface eth0 --namespace cms-demo "
            "--names mysql-79794985d4-7zl5p --kubeconfig /root/.kube/config"
        )
        result = _parse_blade_create_from_v_args(v_args)
        assert result == {
            "scope": "pod",
            "target": "network",
            "action": "loss",
            "flags": "--percent 100 --interface eth0 --namespace cms-demo "
            "--names mysql-79794985d4-7zl5p --kubeconfig /root/.kube/config",
        }

    def test_cpu_fullload(self):
        from chaos_agent.agent.providers.chaosblade.provider import (
            _parse_blade_create_from_v_args,
        )

        v_args = (
            "otel-c-tool-xxx -n chaosblade -- blade create k8s node-cpu fullload "
            "--cpu-percent 80 --names worker-1"
        )
        result = _parse_blade_create_from_v_args(v_args)
        assert result == {
            "scope": "node",
            "target": "cpu",
            "action": "fullload",
            "flags": "--cpu-percent 80 --names worker-1",
        }

    def test_no_blade_create(self):
        from chaos_agent.agent.providers.chaosblade.provider import (
            _parse_blade_create_from_v_args,
        )

        v_args = "otel-c-tool-xxx -n chaosblade -- blade destroy abc123def4567890"
        result = _parse_blade_create_from_v_args(v_args)
        assert result is None

    def test_non_blade_kubectl(self):
        from chaos_agent.agent.providers.chaosblade.provider import (
            _parse_blade_create_from_v_args,
        )

        v_args = "some-pod -n default -- cat /etc/hosts"
        result = _parse_blade_create_from_v_args(v_args)
        assert result is None


class TestExplicitReplan:
    def test_old_tool_error_does_not_preempt_current_action(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _handle_replan

        old_error = ToolMessage(
            content="Error: kubectl exec failed: ls not found",
            name="kubectl",
            tool_call_id="old-call",
            status="error",
        )
        current = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "exec",
                        "v_args": "debug-pod -- iptables -V",
                    },
                    "id": "new-call",
                }
            ],
        )
        result = {}
        _handle_replan(current, {"messages": [old_error]}, result)

        assert "replan_requested" not in result


class TestHostNativeDetection:
    """host_native is the 4th injection method (P1.1): a host-scope fault whose
    carrier is a raw shell command (no blade_uid, no kubectl)."""

    def _host_tool_msg(self, name="exec_host_command", content="filled /tmp/x"):
        return ToolMessage(content=content, name=name, tool_call_id="h1")

    def test_detect_host_native_only_when_is_host(self):
        msgs = [self._host_tool_msg()]
        # Without a resolved host channel, a bare shell carrier stays unknown.
        assert _detect_injection_method(msgs, is_host=False) is None
        # On a host channel it is classified host_native (recoverable).
        assert _detect_injection_method(msgs, is_host=True) == "host_native"

    def test_detect_blade_uid_wins_over_host_native(self):
        blade = ToolMessage(
            content='{"code":200,"success":true,"result":"a1b2c3d4e5f60718"}',
            name="blade_create",
            tool_call_id="b1",
        )
        # A real blade experiment must not be downgraded to host_native.
        assert _detect_injection_method([blade], is_host=True) == "host_blade"


class TestClassifyIssueTimeMethod:
    """Issue-time injection_method classification (direction B).

    Maps a single freshly-issued tool_call to its injection_method without
    scanning history, so the method is recorded when the injection is LAUNCHED.
    """

    def _classify(self, name, args, *, is_host=False):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            classify_issue_time_method,
        )

        return classify_issue_time_method(name, args, is_host=is_host)

    def test_blade_create_tool_is_host_blade(self):
        assert self._classify("blade_create", {}) == "host_blade"

    def test_kubectl_exec_blade_is_kubectl_exec(self):
        args = {
            "subcommand": "exec",
            "v_args": "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload",
        }
        assert self._classify("kubectl", args) == "kubectl_exec"

    def test_object_write_verbs_are_kubectl_native(self):
        for sub in (
            "scale",
            "patch",
            "delete",
            "cordon",
            "taint",
            "set",
            "drain",
            "label",
        ):
            args = {"subcommand": sub, "v_args": "deploy/foo --replicas=0"}
            assert self._classify("kubectl", args) == "kubectl_native", sub

    def test_exec_mutating_inner_is_kubectl_native(self):
        mutating = [
            "p -n ns -- sh -c 'while true; do :; done &'",
            "p -n ns -- dmsetup create errdev --table '0 100 error'",
            "p -n ns -- nc -l -p 80 -k",
            "p -n ns -- iptables -A OUTPUT -j DROP",
        ]
        for v in mutating:
            args = {"subcommand": "exec", "v_args": v}
            assert self._classify("kubectl", args) == "kubectl_native", v

    def test_exec_readonly_inner_is_none(self):
        for v in (
            "p -n ns -- cat /proc/net/dev",
            "p -n ns -- ps aux | grep x",
            "p -n ns -- tc qdisc show dev eth0",
        ):
            args = {"subcommand": "exec", "v_args": v}
            assert self._classify("kubectl", args) is None, v

    def test_readonly_subcommand_is_none(self):
        for sub in ("get", "describe", "logs"):
            assert (
                self._classify("kubectl", {"subcommand": sub, "v_args": "pods"}) is None
            )

    def test_host_inject_only_on_host_channel(self):
        assert self._classify("host_inject", {}, is_host=True) == "host_native"
        # A host carrier on a k8s channel is not attributed.
        assert self._classify("host_inject", {}, is_host=False) is None

    def test_unrelated_tool_is_none(self):
        assert self._classify("read_skill_resource", {"path": "x"}) is None


class TestIssueTimeRecording:
    """``_process_response_tool_calls`` records native methods at issue time."""

    class _StubTracker:
        """Minimal tracker so debug-mode ``post_invoke_debug`` stays a no-op."""

        def update(self, *args, **kwargs):
            return None

    def _run(self, tool_calls, state=None):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _process_response_tool_calls,
        )

        response = AIMessage(content="", tool_calls=tool_calls)
        state = state if state is not None else {}
        result: dict = {}
        _process_response_tool_calls(response, state, result, self._StubTracker(), 1)
        return result

    @staticmethod
    def _birth_pair(uid: str, tc_id: str = "b-birth") -> list:
        """The experiment's birth receipt (tool call + create response) —
        the owned evidence the round-28 live oracle reads. A live
        experiment's UID slot in real history ALWAYS rides with this
        receipt (or the durable ``owned_experiment_uids`` registry); a
        bare slot with no evidence is exactly the no-net shape the oracle
        refuses — the same line the r26 sweep already drew. The uid must
        carry the legislated HEX16 shape (round-19/21): the provenance
        scan shape-gates every capture anchor."""
        return [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": tc_id,
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id=tc_id,
            ),
        ]

    def test_records_kubectl_native_for_scale(self):
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(tcs)
        assert result.get("injection_method") == "kubectl_native"
        assert result.get("injection_start_time")

    def test_records_kubectl_native_for_mutating_exec(self):
        tcs = [
            {
                "name": "kubectl",
                "args": {
                    "subcommand": "exec",
                    "v_args": "p -n ns -- dmsetup create errdev --table '0 100 error'",
                },
                "id": "k1",
            }
        ]
        assert self._run(tcs).get("injection_method") == "kubectl_native"

    def test_no_record_for_readonly_exec(self):
        tcs = [
            {
                "name": "kubectl",
                "args": {
                    "subcommand": "exec",
                    "v_args": "p -n ns -- cat /proc/net/dev",
                },
                "id": "k1",
            }
        ]
        assert "injection_method" not in self._run(tcs)

    def test_blade_create_deferred_to_uid_path(self):
        # Experiment methods are NOT committed at issue time (they need the
        # blade_uid proof) so a failed blade + kubectl-native fallback is not
        # mis-recorded as host_blade.
        tcs = [{"name": "blade_create", "args": {}, "id": "b1"}]
        assert "injection_method" not in self._run(tcs)

    def test_does_not_override_existing_method(self):
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(tcs, state={"injection_method": "host_blade"})
        assert "injection_method" not in result

    def test_combo_marked_when_native_issued_after_experiment_method(self):
        """blade-first combo: the experiment method is already attributed AND
        its birth receipt attests the experiment live (round-28: the UID
        SLOT alone is no longer the proof — the liability oracle reads the
        owned evidence); a native mutating call issued alongside → durable
        combo marker for recovery routing (deterministic destroy would
        leak the native mutation)."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(
            tcs,
            state={
                "injection_method": "host_blade",
                "experiment_uid": "aabbccdd00000001",
                "messages": self._birth_pair("aabbccdd00000001", "b-host"),
            },
        )
        assert result.get("combo_native_issued") is True
        # Attribution itself stays monotonic.
        assert "injection_method" not in result

    def test_combo_not_marked_for_second_native_step(self):
        """Multi-step kubectl-native injections (same carrier, no experiment)
        are NOT combos — the marker only fires alongside an experiment UID."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(tcs, state={"injection_method": "kubectl_native"})
        assert result.get("combo_native_issued") is None

    def test_combo_marker_monotonic(self):
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(
            tcs, state={"injection_method": "host_blade", "combo_native_issued": True}
        )
        assert "combo_native_issued" not in result

    def test_combo_marked_when_native_issued_after_kubectl_exec_method(self):
        """kubectl_exec is a DELIVERY of the experiment (ChaosbladeProvider,
        has_experiment_uid=True) — native work issued alongside it is a combo
        exactly like host_blade. The judgment must be provider-driven, not a
        literal host_blade name check."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(
            tcs,
            state={
                "injection_method": "kubectl_exec",
                "experiment_uid": "aabbccdd00000002",
                "messages": self._birth_pair("aabbccdd00000002", "b-exec"),
            },
        )
        assert result.get("combo_native_issued") is True

    def test_no_combo_when_experiment_method_unfulfilled(self):
        """task-51193464 regression: an experiment-method attribution WITHOUT
        its UID proof is unfulfilled (in that task the recorded "uid" was a
        k8s debug-pod object uid mis-read as blade evidence). A native
        mutation issued on top is the ONLY real mutation → NOT a combo, so
        recovery keeps the native routing instead of the LLM path."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(tcs, state={"injection_method": "kubectl_exec"})
        assert result.get("combo_native_issued") is None
        # Attribution stays monotonic (no UID proof → no re-attribution here).
        assert "injection_method" not in result

    def test_combo_marked_when_native_issued_after_python_agent_method(self):
        """python_agent carries an experiment UID too — same combo semantics."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(
            tcs,
            state={
                "injection_method": "python_agent",
                "experiment_uid": "aabbccdd00000003",
                "messages": self._birth_pair("aabbccdd00000003", "b-py"),
            },
        )
        assert result.get("combo_native_issued") is True

    def test_combo_marked_when_native_issued_after_keep_uid_seam(self):
        """Execute-replan seam with keep_experiment_uid: the method is cleared for
        re-detection but the LIVE experiment's UID survives. Native work in
        the new epoch is still a combo — the epoch-bounded re-detect scan
        cannot see the pre-seam blade_create, so the issue-time live check
        is the only coverage. Round-28: the pre-seam birth receipt rides
        the (unbounded) message history, so the liability oracle still sees
        the ownership even though this epoch's re-detect cannot."""
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(
            tcs,
            state={
                "experiment_uid": "aabbccdd00000004",
                "messages": self._birth_pair("aabbccdd00000004", "b-seam"),
            },
        )
        assert result.get("injection_method") == "kubectl_native"
        assert result.get("combo_native_issued") is True

    def test_no_combo_when_blade_failed_before_native(self):
        """blade-first FAILURE then native fallback is NOT a combo: a failed
        blade_create left no UID, so only the native carrier mutated the
        target — recovery needs only the LLM-native path."""
        from langchain_core.messages import ToolMessage

        failed_blade = ToolMessage(
            content='{"code": 500, "success": false}',
            name="blade_create",
            tool_call_id="b1",
        )
        tcs = [
            {
                "name": "kubectl",
                "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
                "id": "k1",
            }
        ]
        result = self._run(tcs, state={"messages": [failed_blade]})
        assert result.get("injection_method") == "kubectl_native"
        assert result.get("combo_native_issued") is None


class TestTextOnlyStallGate:
    """Phase-2 text-only stalls nudge up to ``max_execute_text_stalls`` then
    fail; a productive tool-call turn resets the consecutive-stall counter."""

    class _StubTracker:
        def update(self, *args, **kwargs):
            return None

    def _detect(self, state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_terminal_conclusion,
        )

        # Text-only response: no tool_calls, no blade_uid/injection_method in
        # state, and no parseable replan marker → the stall branch.
        response = AIMessage(content="I think we should reconsider the plan.")
        result: dict = {}
        _detect_terminal_conclusion(response, state, result)
        return result

    def _nudged(self, result) -> bool:
        return any(
            "EXECUTION REQUIRED" in (getattr(m, "content", "") or "")
            for m in result.get("messages", [])
        )

    def test_first_stall_nudges_and_counts(self):
        result = self._detect({})
        assert result.get("_execute_text_stall_count") == 1
        assert not result.get("error")
        assert self._nudged(result)

    def test_second_stall_still_nudges_below_threshold(self):
        # Default threshold is 3 → the second consecutive stall still nudges.
        result = self._detect({"_execute_text_stall_count": 1})
        assert result.get("_execute_text_stall_count") == 2
        assert not result.get("error")
        assert self._nudged(result)

    def test_reaching_threshold_fails_without_further_nudge(self):
        # Third consecutive stall hits the default budget of 3 → fail fast.
        result = self._detect({"_execute_text_stall_count": 2})
        assert result.get("error")
        assert "concluded without tool use" in result["error"]
        # No new nudge appended and the counter is not bumped past the budget.
        assert not self._nudged(result)

    def test_threshold_is_configurable(self, monkeypatch):
        # Lowering the budget to 1 fails on the very first stall (no nudge).
        monkeypatch.setattr(settings, "max_execute_text_stalls", 1)
        result = self._detect({})
        assert result.get("error")
        assert "concluded without tool use" in result["error"]
        assert not self._nudged(result)

    def test_tool_call_turn_resets_stall_count(self):
        # A productive turn (issued a tool call) breaks the stall streak so a
        # later, unrelated stall starts from a fresh nudge budget.
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _process_response_tool_calls,
        )

        response = AIMessage(
            content="", tool_calls=[{"name": "blade_create", "args": {}, "id": "b1"}]
        )
        state = {"_execute_text_stall_count": 2}
        result: dict = {}
        _process_response_tool_calls(response, state, result, self._StubTracker(), 1)
        assert result.get("_execute_text_stall_count") == 0


class TestUnfulfilledAttributionFailFast:
    """task-51193464 regression: a text-only conclusion under an experiment-
    method attribution WITHOUT its UID proof must fail fast into the
    verifier — the unfulfilled promise means ``has_active_fault`` can never
    open the router's exit gate, so honouring the text exit would spin the
    loop until the budget dies (the model concluded "execution complete" for
    six minutes while the router kept returning "continue")."""

    def _detect(self, response, state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_terminal_conclusion,
        )

        result: dict = {}
        _detect_terminal_conclusion(response, state, result)
        return result

    def test_text_conclusion_with_unfulfilled_method_fails(self):
        response = AIMessage(content="Injection completed successfully.")
        result = self._detect(response, {"injection_method": "kubectl_exec"})
        assert result.get("error")
        assert "unfulfilled experiment attribution" in result["error"]
        assert "kubectl_exec" in result["error"]

    def test_fulfilled_method_exits_cleanly(self):
        # The UID proves the experiment live — the text-only exit is the
        # normal, correct terminal path.
        response = AIMessage(content="Injection completed successfully.")
        result = self._detect(
            response,
            {
                "injection_method": "kubectl_exec",
                "experiment_uid": "uid-live",
            },
        )
        assert not result.get("error")

    def test_native_method_without_uid_is_not_failed(self):
        # UID-less is the NATURAL state for a native method (the attempt is
        # its own proof) — only experiment-method attributions make the
        # unfulfilled promise. kubectl_native is multi-step so the one-shot
        # self-check path runs first and (no skill case) falls through clean.
        response = AIMessage(content="Injection completed successfully.")
        result = self._detect(
            response,
            {
                "injection_method": "kubectl_native",
                "_injection_selfcheck_nudged": True,
            },
        )
        assert not result.get("error")

    def test_tool_call_turn_not_failed(self):
        # The fail-fast guards TEXT-ONLY conclusions; a turn still issuing
        # tool calls has not concluded anything yet.
        response = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {"subcommand": "get", "v_args": "pods"},
                    "id": "k1",
                },
            ],
        )
        result = self._detect(response, {"injection_method": "kubectl_exec"})
        assert not result.get("error")


class TestShouldRedetectInjectionMethod:
    """Channel B re-scan gate: it runs only for RESUME + blade_uid UPGRADE,
    and is skipped in steady state so it does not re-derive the same answer
    every iteration (direction B: issue-time recording is the primary path).
    """

    def test_resume_when_no_method_yet(self):
        # Nothing recorded → scan history to (re)attribute (resume / first turn).
        assert _should_redetect_injection_method(None, None) is True
        assert _should_redetect_injection_method("", "b1c2d3e4f5a60789") is True

    def test_steady_kubectl_native_without_uid_skips(self):
        # The common multi-step case: method set, no blade_uid → no re-scan.
        assert _should_redetect_injection_method("kubectl_native", None) is False

    def test_upgrade_when_uid_appears_over_multi_step(self):
        # kubectl_native is multi-step; a fresh blade_uid must trigger the
        # upgrade check to promote it to the experiment backend.
        assert (
            _should_redetect_injection_method("kubectl_native", "b1c2d3e4f5a60789")
            is True
        )

    def test_no_rescan_once_experiment_backend_set(self):
        # host_blade / kubectl_exec are not multi-step: even with a UID present
        # there is nothing left to upgrade, so skip.
        assert (
            _should_redetect_injection_method("host_blade", "b1c2d3e4f5a60789") is False
        )
        assert (
            _should_redetect_injection_method("kubectl_exec", "b1c2d3e4f5a60789")
            is False
        )

    def test_host_native_redetected_on_uid(self):
        # host_native is now multi-step (opts into the injection step self-check),
        # so — like kubectl_native — a UID appearing triggers re-detect/upgrade
        # candidacy (the rare host+blade_uid hybrid). Pure host (no uid) still
        # short-circuits to False above.
        assert (
            _should_redetect_injection_method("host_native", "b1c2d3e4f5a60789") is True
        )

    def test_experiment_method_without_uid_arms_downgrade(self):
        """task-51193464 regression: an experiment-method attribution whose
        UID never materialised is UNFULFILLED — its promise (a live experiment)
        is outstanding, so the scan stays armed and the registry's RECENCY
        arbitration can correct the mis-attribution to a more-recent native
        backend. UID-less native methods are their own proof and keep
        skipping (see test_steady_kubectl_native_without_uid_skips)."""
        assert _should_redetect_injection_method("kubectl_exec", None) is True
        assert _should_redetect_injection_method("host_blade", None) is True
        assert _should_redetect_injection_method("python_agent", None) is True

    def test_fulfilled_experiment_method_closes_downgrade_arm(self):
        # Once the UID materialises the attribution is fulfilled — no more
        # downgrade candidacy, and non-multi-step methods skip the rescan.
        assert (
            _should_redetect_injection_method("kubectl_exec", "b1c2d3e4f5a60789")
            is False
        )
        assert (
            _should_redetect_injection_method("host_blade", "b1c2d3e4f5a60789") is False
        )


def _finalized_msg(summary: str = "inject mem load on pod-x") -> ToolMessage:
    return ToolMessage(
        content=f"Planning finalized. Summary: {summary}",
        name="finish_planning",
        tool_call_id="fp1",
    )


class TestPhase2Kickoff:
    """Phase 1 → Phase 2 seam kickoff: one explicit transition message per
    plan finalization (fresh ``Planning finalized`` ToolMessage with no
    kickoff / nudge / productive turn after it). Positional detection must
    re-arm after a replan produces a NEW finalization."""

    def _build(self, messages):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _maybe_build_phase2_kickoff,
        )

        return _maybe_build_phase2_kickoff(messages)

    def test_fires_on_fresh_finalization(self):
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "finish_planning", "args": {"summary": "x"}, "id": "fp1"},
                ],
            ),
            _finalized_msg(),
        ]
        kickoff = self._build(msgs)
        assert kickoff is not None
        assert "PHASE 2" in kickoff.content
        assert "approved" in kickoff.content

    def test_kickoff_carries_construction_time_id(self):
        # B78 belt-and-suspenders: the kickoff object is immediate-written to
        # the session store BEFORE add_messages merges it into state. An
        # id-less construction made the pre-merge and post-merge writes fall
        # under two different dedup keys — one logical message, two audit
        # records. Identity from birth keeps the deliberate double write
        # idempotent (the session store stamps uuids as the general guard;
        # this is the source-side layer).
        kickoff = self._build([_finalized_msg()])
        assert kickoff is not None
        assert kickoff.id is not None
        assert kickoff.id.startswith("phase2-kickoff:")

    def test_kickoff_ids_unique_per_finalization(self):
        # A replan produces a NEW finalization → a fresh kickoff that must
        # APPEND (its own audit entry), never collide with the previous one.
        first = self._build([_finalized_msg()])
        second = self._build([_finalized_msg()])
        assert first.id != second.id

    def test_no_fire_without_finalization(self):
        msgs = [
            AIMessage(
                content="still planning",
                tool_calls=[
                    {"name": "read_skill_resource", "args": {}, "id": "r1"},
                ],
            )
        ]
        assert self._build(msgs) is None

    def test_no_fire_on_rejected_planning(self):
        msgs = [
            ToolMessage(
                content="Planning rejected. Reason: unsafe target",
                name="finish_planning",
                tool_call_id="fp1",
            )
        ]
        assert self._build(msgs) is None

    def test_no_double_fire_when_kickoff_present(self):
        first = self._build([_finalized_msg()])
        msgs = [_finalized_msg(), first]
        assert self._build(msgs) is None

    def test_no_fire_after_productive_ai_turn(self):
        msgs = [
            _finalized_msg(),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "blade_create", "args": {}, "id": "b1"},
                ],
            ),
        ]
        assert self._build(msgs) is None

    def test_no_fire_after_stall_nudge(self):
        # The EXECUTION REQUIRED nudge already announced the transition —
        # a kickoff after it would be a duplicate signal.
        msgs = [
            _finalized_msg(),
            AIMessage(content="Plan complete. Summary: ..."),
            HumanMessage(content="**EXECUTION REQUIRED**: You output text ..."),
        ]
        assert self._build(msgs) is None

    def test_text_only_ai_turn_does_not_disarm(self):
        # The measured failure mode: the model answered the finalization with
        # prose. That text-only turn must NOT count as a handled transition.
        msgs = [_finalized_msg(), AIMessage(content="Plan complete. Summary: ...")]
        assert self._build(msgs) is not None

    def test_refires_after_replan_finalization(self):
        # Old kickoff + productive work belong to the PRE-replan plan; a NEW
        # finalization after them must re-arm the kickoff.
        first = self._build([_finalized_msg("plan v1")])
        msgs = [
            _finalized_msg("plan v1"),
            first,
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "blade_create", "args": {}, "id": "b1"},
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "finish_planning", "args": {"summary": "v2"}, "id": "fp2"},
                ],
            ),
            _finalized_msg("plan v2"),
        ]
        again = self._build(msgs)
        assert again is not None
        assert again is not first


class TestPhase2KickoffIntegration:
    """End-to-end wiring through make_execute_loop: the kickoff reaches
    the LLM call, is persisted into ``result["messages"]`` (LangGraph state)
    and is written directly to the session store (task JSON)."""

    class _FakeStore:
        def __init__(self):
            self.appended = []

        def append_messages(self, task_id, messages, node_name=""):
            for msg in messages:
                if node_name:
                    msg.additional_kwargs.setdefault("_node", node_name)
                self.appended.append(msg)

    class _FakeHook:
        def __init__(self):
            self.session_store = TestPhase2KickoffIntegration._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeLLM:
        def __init__(self):
            self.seen = None

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.seen = messages
            # Productive turn: carries tool_calls so the stall guard stays quiet.
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {"subcommand": "get", "v_args": "pods"},
                        "id": "c1",
                    },
                ],
            )

    @staticmethod
    def _kickoffs(messages):
        return [
            m
            for m in messages
            if isinstance(m, HumanMessage) and "PHASE 2" in (m.content or "")
        ]

    @pytest.mark.asyncio
    async def test_kickoff_reaches_llm_state_and_store(self, sample_agent_state):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop
        from chaos_agent.agent.node_names import EXECUTE_LOOP

        hook = self._FakeHook()
        llm = self._FakeLLM()
        node = make_execute_loop(
            hook=hook,
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [_finalized_msg()]

        result = await node(state)

        # Visible to the LLM on the seam turn.
        assert self._kickoffs(llm.seen)
        # Persisted into LangGraph state (ahead of this turn's response).
        result_msgs = result.get("messages", [])
        kickoffs = self._kickoffs(result_msgs)
        assert len(kickoffs) == 1
        assert result_msgs.index(kickoffs[0]) == 0
        # Written directly to the session store (task JSON), node-stamped.
        store_kickoffs = self._kickoffs(hook.session_store.appended)
        assert len(store_kickoffs) == 1
        assert store_kickoffs[0].additional_kwargs.get("_node") == EXECUTE_LOOP

    @pytest.mark.asyncio
    async def test_kickoff_not_repeated_next_iteration(self, sample_agent_state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            make_execute_loop,
            _maybe_build_phase2_kickoff,
        )

        hook = self._FakeHook()
        llm = self._FakeLLM()
        node = make_execute_loop(
            hook=hook,
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        kickoff = _maybe_build_phase2_kickoff([_finalized_msg()])
        state["messages"] = [
            _finalized_msg(),
            kickoff,
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "blade_create", "args": {}, "id": "b1"},
                ],
            ),
        ]

        result = await node(state)
        assert not self._kickoffs(result.get("messages", []))

    @pytest.mark.asyncio
    async def test_kickoff_refires_after_replan(self, sample_agent_state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            make_execute_loop,
            _maybe_build_phase2_kickoff,
        )

        hook = self._FakeHook()
        llm = self._FakeLLM()
        node = make_execute_loop(
            hook=hook,
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        kickoff = _maybe_build_phase2_kickoff([_finalized_msg("plan v1")])
        state["messages"] = [
            _finalized_msg("plan v1"),
            kickoff,
            AIMessage(
                content="",
                tool_calls=[
                    {"name": "blade_create", "args": {}, "id": "b1"},
                ],
            ),
            # Replan re-ran planning and produced a NEW finalization.
            _finalized_msg("plan v2"),
        ]

        result = await node(state)
        assert len(self._kickoffs(result.get("messages", []))) == 1


class TestLedgerTailMigration:
    """Unit A (context-cache-prefix-stability tasks 2.2/2.3/2.7): the execute
    progress ledger rides the message TAIL as an append-only system-reminder
    HumanMessage carrying a supersedes marker — NOT the system-prompt head.

    This is the node-level complement to the builder-level prefix guard
    (test_prefix_stability.py): that one proves the head is byte-stable; this
    one proves the ledger still reaches the model every round, at the tail.
    """

    class _FakeLLM:
        def __init__(self):
            self.seen = None

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            self.seen = messages
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {"subcommand": "get", "v_args": "pods"},
                        "id": "c1",
                    },
                ],
            )

    @staticmethod
    def _ledger(state):
        from chaos_agent.agent.progress_ledger import (
            freeze_anchor,
            merge_progress_ledger,
        )

        return merge_progress_ledger(
            freeze_anchor(state["fault_spec"], goal="inject mem load on pod-x"),
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

    @pytest.mark.asyncio
    async def test_ledger_rides_tail_not_system_head(self, sample_agent_state):
        from langchain_core.messages import SystemMessage
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        llm = self._FakeLLM()
        node = make_execute_loop(hook=None, llm=llm, tools=[], env_info={"context": "test"})
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [_finalized_msg()]
        state["progress_ledger"] = self._ledger(state)

        result = await node(state)

        # The system head must NOT carry the ledger anymore (that was the
        # per-round volatile byte that broke the cache prefix).
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
        # The anti-drift directive survives the move (still tells the model to
        # check the ledger before acting).
        assert "before acting" in content

        # Persisted into LangGraph state via the same _hints_for_state channel.
        assert len(self._ledger_tails(result.get("messages", []))) == 1

    @pytest.mark.asyncio
    async def test_no_ledger_tail_when_ledger_empty(self, sample_agent_state):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        llm = self._FakeLLM()
        node = make_execute_loop(hook=None, llm=llm, tools=[], env_info={"context": "test"})
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["messages"] = [_finalized_msg()]
        # No progress_ledger AND no fault_spec-derived anchor content: force an
        # empty render so the tail message is correctly suppressed.
        state["progress_ledger"] = None
        state["fault_spec"] = None

        result = await node(state)
        assert not self._ledger_tails(llm.seen)
        assert not self._ledger_tails(result.get("messages", []))


class TestDowngradeCommitIntegration:
    """task-51193464 regression, channel-B commit seam: an UNFULFILLED
    experiment-method attribution (no experiment UID anywhere) is corrected
    to the UID-less native backend once the registry's RECENCY arbitration
    recognises a more-recent native mutation in history — without the
    correction the fault-handle projection can never claim the fault and the
    router's has_active_fault gate stays closed forever."""

    class _FakeStore:
        def __init__(self):
            self.appended = []

        def append_messages(self, task_id, messages, node_name=""):
            self.appended.extend(messages)

    class _FakeHook:
        def __init__(self):
            self.session_store = TestDowngradeCommitIntegration._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            # Productive turn: carries tool_calls so the stall guard and the
            # fail-fast both stay quiet — the scan seam is what's under test.
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {"subcommand": "get", "v_args": "pods"},
                        "id": "c1",
                    },
                ],
            )

    def _node(self):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=self._FakeHook(),
            llm=self._FakeLLM(),
            tools=[],
            env_info={"context": "test"},
        )

    @pytest.mark.asyncio
    async def test_downgrade_commits_native_over_unfulfilled_experiment(
        self,
        sample_agent_state,
    ):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        # The poisoned shape from the task: experiment-method attribution
        # with its start time but NO uid promise ever fulfilled.
        state["injection_method"] = "kubectl_exec"
        state["injection_start_time"] = "2026-08-24T15:48:03+00:00"
        # A more-recent UID-less native mutation in history (RECENCY winner).
        state["messages"] = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "scale",
                            "v_args": "deploy/foo --replicas=0",
                        },
                        "id": "k1",
                    },
                ],
            ),
            ToolMessage(
                content="deployment.apps/foo scaled", name="kubectl", tool_call_id="k1"
            ),
        ]

        result = await node(state)

        assert result.get("injection_method") == "kubectl_native"
        # The native mutation is the ONLY real one — no combo marker.
        assert result.get("combo_native_issued") is None
        # The earlier (mis-)attribution time is preserved, not re-stamped.
        assert "injection_start_time" not in result

    @pytest.mark.asyncio
    async def test_unfulfilled_attribution_survives_without_native_evidence(
        self,
        sample_agent_state,
    ):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["injection_method"] = "kubectl_exec"
        # Only the false-evidence pair in history: the debug-pod-meta uid is
        # blocked as blade evidence (cross-check) and debug+sleep is a
        # read-only probe for the native scan — nothing to correct TO.
        state["messages"] = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "debug",
                            "v_args": "node/n1 --profile=sysadmin -- sleep 3600",
                        },
                        "id": "d1",
                    },
                ],
            ),
            ToolMessage(
                content='pod created [debug-pod-meta: {"name":"dbg-x",'
                '"uid":"3fbb468c-5ac2-4d05-b25c-17454df09ade"}]',
                name="kubectl",
                tool_call_id="d1",
            ),
        ]

        result = await node(state)

        # No commit: the attribution stays as-is (monotonic in state) — the
        # fail-fast on the eventual text conclusion is the exit, not a
        # silent re-attribution.
        assert "injection_method" not in result
        assert result.get("combo_native_issued") is None

    @pytest.mark.asyncio
    async def test_downgrade_stamps_start_time_when_absent(
        self,
        sample_agent_state,
    ):
        # Same downgrade shape but WITHOUT an existing injection_start_time
        # (e.g. the mis-attribution never got as far as stamping one): the
        # committed correction stamps the time so duration accounting starts
        # from the corrected attribution.
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "test-task"
        state["injection_method"] = "kubectl_exec"
        state["messages"] = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "scale",
                            "v_args": "deploy/foo --replicas=0",
                        },
                        "id": "k1",
                    },
                ],
            ),
            ToolMessage(
                content="deployment.apps/foo scaled", name="kubectl", tool_call_id="k1"
            ),
        ]

        result = await node(state)

        assert result.get("injection_method") == "kubectl_native"
        assert result.get("injection_start_time")


class TestExecuteAnchorSeeding:
    """tier1-speedup regression: the cross-graph bridge can deliver an
    intent-stage ledger (facts recorded during clarification, no anchor —
    ``update_progress`` never writes one). The freeze gate must seed the
    anchor onto that ledger while PRESERVING its state/log; anchoring only on
    an entirely absent ledger would let the truthy-but-anchorless ledger
    silently disable anchor freezing for the whole run."""

    class _FakeStore:
        def __init__(self):
            self.appended = []

        def append_messages(self, task_id, messages, node_name=""):
            self.appended.extend(messages)

    class _FakeHook:
        def __init__(self):
            self.session_store = TestExecuteAnchorSeeding._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {"subcommand": "get", "v_args": "pods"},
                        "id": "c1",
                    },
                ],
            )

    @staticmethod
    def _node():
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=TestExecuteAnchorSeeding._FakeHook(),
            llm=TestExecuteAnchorSeeding._FakeLLM(),
            tools=[],
            env_info={"context": "test"},
        )

    @pytest.mark.asyncio
    async def test_intent_stage_ledger_gets_anchor_and_keeps_facts(
        self, sample_agent_state
    ):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "anchor-seed-1"
        state["messages"] = [_finalized_msg()]
        state["input"] = "kill the nginx process"
        state["progress_ledger"] = {
            "state": {"established_facts": ["pod is Running on node-1"]},
            "log": [{"event": "probed target", "status": "observed"}],
        }

        result = await node(state)

        seeded = result.get("progress_ledger")
        assert seeded, "anchorless intent ledger must trigger seeding"
        assert seeded["anchor"]["fault_spec"]["names"] == ["my-pod"]
        assert seeded["anchor"]["goal"] == "kill the nginx process"
        # Intent-time evidence preserved verbatim — seeding must not reset it.
        assert seeded["state"]["established_facts"] == ["pod is Running on node-1"]
        assert seeded["log"] == [{"event": "probed target", "status": "observed"}]

    @pytest.mark.asyncio
    async def test_anchored_ledger_is_never_clobbered(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "anchor-seed-2"
        state["messages"] = [_finalized_msg()]
        state["progress_ledger"] = {
            "anchor": {"goal": "frozen"},
            "state": {"established_facts": ["fact"]},
            "log": [],
        }

        result = await node(state)

        assert "progress_ledger" not in result, "idempotent: anchored ledger untouched"

    @pytest.mark.asyncio
    async def test_absent_ledger_still_freezes_fresh(self, sample_agent_state):
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "anchor-seed-3"
        state["messages"] = [_finalized_msg()]
        state["progress_ledger"] = None

        result = await node(state)

        seeded = result.get("progress_ledger")
        assert seeded and seeded["anchor"]["fault_spec"]["names"] == ["my-pod"]
        assert seeded["state"] == {} and seeded["log"] == []

    @pytest.mark.asyncio
    async def test_drifted_anchor_spec_reconciles_on_reentry(
        self, sample_agent_state,
    ):
        """C2 plan-A: a spec changed through ANY seam (drift correction,
        replan lazy derivation — plan_change_confirm already refreshes) must
        realign the anchor on execute re-entry: the anchor's spec side is
        the CURRENTLY-APPROVED contract's snapshot, not the
        frozen-at-first-freeze one. Goal/state/log untouched."""
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "anchor-reconcile-1"
        state["messages"] = [_finalized_msg()]
        state["input"] = "kill the nginx process"
        # Anchor frozen on the PRE-correction spec (names drifted since),
        # with live model-recorded state/log that must ride through.
        _old_spec = {
            "scope": "pod", "fault_target": "process",
            "fault_action": "kill", "namespace": "default",
            "names": ["stale-pod"],
        }
        state["progress_ledger"] = {
            "anchor": {"goal": "kill the nginx process", "fault_spec": _old_spec},
            "state": {"established_facts": ["fact-1"]},
            "log": [{"event": "probed", "status": "observed"}],
        }

        result = await node(state)

        led = result.get("progress_ledger")
        assert led, "drifted anchor must emit the reconciled ledger"
        assert led["anchor"]["fault_spec"]["names"] == ["my-pod"], (
            "anchor carries the CURRENT contract, not the stale one"
        )
        assert led["anchor"]["goal"] == "kill the nginx process"
        assert led["state"]["established_facts"] == ["fact-1"]
        assert led["log"] == [{"event": "probed", "status": "observed"}]

    @pytest.mark.asyncio
    async def test_aligned_anchor_spec_emits_nothing(self, sample_agent_state):
        """The healthy steady state: anchor spec == current spec → no
        progress_ledger in the result (no needless override racing the
        model's update_progress)."""
        node = self._node()
        state = sample_agent_state
        state["execute_loop_count"] = 0
        state["task_id"] = "anchor-reconcile-2"
        state["messages"] = [_finalized_msg()]
        state["input"] = "kill the nginx process"
        _spec_now = state["fault_spec"]
        state["progress_ledger"] = {
            "anchor": {
                "goal": "kill the nginx process", "fault_spec": dict(_spec_now),
            },
            "state": {"established_facts": ["fact-1"]},
            "log": [],
        }

        result = await node(state)

        assert "progress_ledger" not in result


class TestComboLivePredicateGate:
    """Round-28 R2 — the combo mark gates on the LIVE predicate, both
    branches.

    The combo marker durably routes recovery to the LLM path (deterministic
    recovery can only destroy the blade experiment and would leak the
    native mutation), so marking a combo on a corpse is not cosmetic: a
    native-only task loses its deterministic recovery route forever. Both
    the method-cleared branch (the replan aftermath) and the
    method-attributed branch used presence/committed semantics before the
    round-28 swap."""

    class _StubTracker:
        def update(self, *args, **kwargs):
            return None

    _SCALE_CALLS = [{
        "name": "kubectl",
        "args": {"subcommand": "scale", "v_args": "deploy/foo --replicas=0"},
        "id": "k-combo",
    }]

    def _run(self, state):
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _process_response_tool_calls,
        )

        response = AIMessage(content="", tool_calls=self._SCALE_CALLS)
        result: dict = {}
        _process_response_tool_calls(response, state, result, self._StubTracker(), 1)
        return result

    @staticmethod
    def _create_pair(tc_id, uid):
        return [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": tc_id,
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id=tc_id,
            ),
        ]

    @staticmethod
    def _destroy_pair(tc_id, uid):
        return [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_destroy",
                    "args": {"uid": uid},
                    "id": tc_id,
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"success"}',
                name="blade_destroy",
                tool_call_id=tc_id,
            ),
        ]

    def test_method_cleared_aftermath_corpse_is_not_combo(self):
        # The replan-seam aftermath, DEAD flavour: UID kept, method cleared,
        # proven destroy. A native issue on top of it is a plain native
        # fallback — no combo mark, recovery keeps the deterministic route.
        uid = "deadbeef00000004"
        state = {
            "experiment_uid": uid,
            "retired_experiment_uids": [uid],
            "messages": (
                self._create_pair("tc-r28-cmb-c", uid)
                + self._destroy_pair("tc-r28-cmb-d", uid)
            ),
        }
        assert self._run(state).get("combo_native_issued") is None

    def test_method_cleared_aftermath_live_is_combo(self):
        # The PROTECTED shape: a live experiment survived the seam (UID
        # kept, method cleared for re-detection). Native work issued in
        # that epoch IS a combo — the experiment is alive.
        uid = "deadbeef00000005"
        state = {
            "experiment_uid": uid,
            "messages": self._create_pair("tc-r28-cmb-al", uid),
        }
        assert self._run(state).get("combo_native_issued") is True

    def test_method_attributed_corpse_slot_is_not_combo(self):
        # The R2 twin (round-28, confirmed during implementation): an
        # experiment method is attributed while the slot's UID is a corpse
        # (destroy clears no slot). UID PRESENCE used to license the combo;
        # the liability oracle judges the slot instead.
        uid = "deadbeef00000006"
        state = {
            "experiment_uid": uid,
            "injection_method": "kubectl_exec",
            "retired_experiment_uids": [uid],
            "messages": (
                self._create_pair("tc-r28-cmb-mc", uid)
                + self._destroy_pair("tc-r28-cmb-md", uid)
            ),
        }
        assert self._run(state).get("combo_native_issued") is None

    def test_method_attributed_live_experiment_is_combo(self):
        uid = "deadbeef00000007"
        state = {
            "experiment_uid": uid,
            "injection_method": "kubectl_exec",
            "messages": self._create_pair("tc-r28-cmb-ml", uid),
        }
        assert self._run(state).get("combo_native_issued") is True


class TestBirthComboDiscriminator:
    """Round-32b C2 — execute 出生 seam 持久化 combo 判别器。

    首次实验出生断言「只有实验」（False），与 owned 翼同批落库，
    让后续翼平衡能终审该行死亡（may_carry_live_fault A2，C2 幽灵
    修复的 DB 侧写点）；已标记的 combo（True）不得被出生断言降级
    ——那是恢复路由的既成事实。"""

    class _FakeStore:
        def append_messages(self, task_id, messages, node_name=""):
            return None

    class _FakeHook:
        def __init__(self):
            self.session_store = TestBirthComboDiscriminator._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            # Productive turn: carries tool_calls so the stall guard stays quiet.
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "get", "v_args": "pods"},
                    "id": "c1",
                }],
            )

    class _FakeNativeLLM:
        """Round-32b S1 门控锚用：签发一个 MUTATING kubectl 调用（patch）
        —— issue-time 分类器把它归为 kubectl_native，驱动迭代级 combo
        检测器的 native-family 分支（:940 的 continue 门放行的唯一形态）。"""

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {
                        "subcommand": "patch",
                        "v_args": "deployment demo -p '{\"spec\":{}}'",
                    },
                    "id": "c1",
                }],
            )

    @staticmethod
    def _birth_pair(uid, tc_id="tc-r32b-birth"):
        return [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": tc_id,
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id=tc_id,
            ),
        ]

    def _node(self, llm=None):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=self._FakeHook(),
            llm=llm or self._FakeLLM(),
            tools=[],
            env_info={"context": "test"},
        )

    @pytest.mark.asyncio
    async def test_first_birth_asserts_experiments_only(self, sample_agent_state):
        """实验首次出生 → 出生 seam 写 False（「无 native 伴生」断言），
        与 owned 翼同批 —— C2 幽灵修复的 DB 侧写点。"""
        state = sample_agent_state
        state["task_id"] = "test-task"  # not a real task- id → sync is a no-op
        state["execute_loop_count"] = 0
        state["messages"] = [_finalized_msg()] + self._birth_pair("beef000000320032")

        result = await self._node()(state)

        assert result.get("owned_experiment_uids") == ["beef000000320032"]
        assert result.get("combo_native_issued") is False

    @pytest.mark.asyncio
    async def test_birth_never_downgrades_a_marked_combo(self, sample_agent_state):
        """combo 已标记（True）→ 出生 seam 不得降级回 False（LLM 路由
        事实不可被出生断言覆写）。"""
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["combo_native_issued"] = True
        state["messages"] = [_finalized_msg()] + self._birth_pair("beef000000330033")

        result = await self._node()(state)

        assert result.get("owned_experiment_uids") == ["beef000000330033"]
        assert result.get("combo_native_issued") is not False

    @pytest.mark.asyncio
    async def test_native_issue_without_live_experiment_never_marks_combo(
        self, sample_agent_state,
    ):
        """S1 门控行为锚（round-32b 自检）：native 工具签发本身不构成
        combo —— 必须有 LIVE 实验伴生（检测器的 _experiment_live 门）。
        纯 native 行（无实验、无归因 method）：检测器照常记录 method，
        但绝不标 True。若此门失守，每个纯 native 行都被标 combo，
        A2 门控在真实流程中将永不触发——而直接构造行测谓词的锚测
        不出这个失守。"""
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["messages"] = [_finalized_msg()]

        result = await self._node(llm=self._FakeNativeLLM())(state)

        # 正面证据：检测器的 native-family 分支确实跑过（method 在
        # issue 时落记）——否则下面的否定断言会空洞地绿。
        assert result.get("injection_method") == "kubectl_native"
        # 门控本体：没有 live 实验伴生 → 不标 combo。
        assert result.get("combo_native_issued") is not True

    @pytest.mark.asyncio
    async def test_native_issue_with_live_experiment_marks_combo(
        self, sample_agent_state,
    ):
        """S1 门控的正面：carried-over live 实验 + native 签发 → True ——
        两辆车都动过（:969 的 has_live_fault 路径）。形态即 replan seam
        的真实携带面：experiment_uid 槽保留（has_live_fault 的 handle
        水合认领它）、归因 method 被 seam 清空（留给本轮 issue-time
        检测器记录）、owned 翼带该 UID 且无 destroy 证据（liability
        oracle 判 live）。"""
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["experiment_uid"] = "beef000000340034"
        state["owned_experiment_uids"] = ["beef000000340034"]
        state["retired_experiment_uids"] = []
        state["messages"] = [_finalized_msg()]

        result = await self._node(llm=self._FakeNativeLLM())(state)

        assert result.get("injection_method") == "kubectl_native"
        assert result.get("combo_native_issued") is True


class TestIssueTimeTeardownAttribution:
    """R6-1 — teardown delete 不是故障突变，issue-time 检测器不得归因。

    幽灵行链条（六环全代码实证）：replan seam 清空 method → 恢复不重记
    → §6 四连删清理 delete 被无条件归因为 kubectl_native → 行级
    _experiment_shaped_only 把「native 归因+实验」读作 combo 证据 →
    balanced 行不被判死 → _recovery_fully_cleared 只有 recover 流程
    写 → liability_live=1 幽灵。修复：registry 对账命中的 teardown
    delete 跳过 method commit 与 combo 评估（与 screener R5 门豁免
    同一判定，共享核心 execution_artifacts.is_vehicle_teardown_delete）。"""

    class _FakeStore:
        def append_messages(self, task_id, messages, node_name=""):
            return None

    class _FakeHook:
        def __init__(self):
            self.session_store = TestIssueTimeTeardownAttribution._FakeStore()

        async def __call__(self, state):
            return {}

    class _FakeDeleteLLM:
        """签发一个 kubectl delete（v_args 可调）——检测器对 delete
        无条件归因 kubectl_native，是本测试家族的被测行为。"""

        def __init__(self, v_args: str):
            self._v_args = v_args

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "delete", "v_args": self._v_args},
                    "id": "c1",
                }],
            )

    @staticmethod
    def _cleaned_carrier() -> dict:
        return {
            "artifact_id": "recovery_carrier:ns/drill-rc-x",
            "type": "recovery_carrier",
            "status": "cleaned",
            "task_id": "test-task",
            "name": "drill-rc-x",
            "namespace": "ns",
            "operation_family": "recovery_carrier",
            "rbac_family": [
                {"kind": "rolebinding", "name": "drill-rc-x", "namespace": "ns"},
                {"kind": "role", "name": "drill-rc-x", "namespace": "ns"},
                {"kind": "serviceaccount", "name": "drill-rc-x", "namespace": "ns"},
            ],
        }

    def _node(self, llm):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=self._FakeHook(),
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )

    def _state(self, sample_agent_state, **extra):
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["messages"] = [_finalized_msg()]
        state["execution_artifacts"] = [self._cleaned_carrier()]
        state.update(extra)
        return state

    @pytest.mark.asyncio
    async def test_teardown_delete_is_not_committed_as_injection(
        self, sample_agent_state,
    ):
        """主牙（幽灵链环 4）：seam 清空 method 后，§6 清理 delete
        （registered Role 的幂等重放）不得被 commit 为 kubectl_native
        ——修复前正是这条 commit 把已恢复行钉死在 fallback 上。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeDeleteLLM(
            "role drill-rc-x -n ns --ignore-not-found"
        ))(state)

        assert result.get("injection_method") is None
        assert "injection_start_time" not in result

    @pytest.mark.asyncio
    async def test_unregistered_delete_is_still_attributed(
        self, sample_agent_state,
    ):
        """豁免由注册表挣得不由动词挣得：删未注册对象（delete-pod-
        to-restart 故障形态）仍是真 native 突变，照常归因。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeDeleteLLM(
            "pod victim-pod -n ns --ignore-not-found"
        ))(state)

        assert result.get("injection_method") == "kubectl_native"

    @pytest.mark.asyncio
    async def test_teardown_with_live_experiment_marks_nothing(
        self, sample_agent_state,
    ):
        """teardown 也跳过 combo 评估：活实验伴生 + 清理 delete →
        method 不 commit 且 combo 不标（拆自家脚手架不是「两辆车
        都动过」）；对照组是 S1 牙 test_native_issue_with_live_experiment_
        marks_combo（真 patch 突变照标 True）。"""
        state = self._state(
            sample_agent_state,
            experiment_uid="beef000000350035",
            owned_experiment_uids=["beef000000350035"],
            retired_experiment_uids=[],
        )

        result = await self._node(self._FakeDeleteLLM(
            "role drill-rc-x -n ns --ignore-not-found"
        ))(state)

        assert result.get("injection_method") is None
        assert result.get("combo_native_issued") is not True

    # -- channel B (history re-scan) — R8-1 -------------------------------
    #
    # R6-1 taught the ISSUE-time detector to skip registered-vehicle
    # teardown deletes; channel A's skip leaves NO marker in the messages,
    # so the epoch-bounded RESUME re-scan (channel B) would re-derive the
    # SAME tool_call from history with no registry knowledge and commit
    # the kubectl_native attribution the ghost row feeds on. P3 threads
    # the ``is_teardown`` matcher into the re-scan (call-level skip at
    # the vocabulary layer, mixed batches included); these teeth pin the
    # matcher seam through the full node, with an LLM that issues
    # NOTHING — any attribution that lands must come from the history
    # re-scan.

    class _TextOnlyLLM:
        """Issues no tool_calls: channel A has nothing to do, so any
        attribution in the result must come from the channel B re-scan."""

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="nothing left to issue")

    def _history(self, subcommand: str, v_args: str) -> list:
        return [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": subcommand, "v_args": v_args},
                "id": "t1",
            }]),
            # a SUCCESS receipt: the disproven check ("most recent object
            # write returned Error:") must NOT veto anything here — a
            # teardown that deleted cleanly is exactly the case it would
            # wrongly confirm (a successful write "confirms" attribution).
            ToolMessage(
                content=f'{subcommand} ok: {v_args}',
                name="kubectl",
                tool_call_id="t1",
            ),
        ]

    @pytest.mark.asyncio
    async def test_rescan_teardown_in_history_not_attributed(
        self, sample_agent_state,
    ):
        """缺陷牙（R8-1，探针态1 转正）：method 空 + 窗口内已执行的注册
        载体 teardown delete → channel B 重扫不得 commit kubectl_native
        ——R6-1 幽灵链的第二扇门。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history("delete", "role drill-rc-x -n ns")

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") is None

    @pytest.mark.asyncio
    async def test_rescan_real_patch_in_history_still_attributed(
        self, sample_agent_state,
    ):
        """对照组（探针态2）：真对象写 patch 在窗口内 → 重扫描常 commit
        ——RESUME 语义（重启恢复归因）不受 teardown 过滤影响。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history(
            "patch", "deployment demo -n ns -p {}"
        )

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") == "kubectl_native"

    @pytest.mark.asyncio
    async def test_rescan_unregistered_delete_still_attributed(
        self, sample_agent_state,
    ):
        """对照组（探针态3）：未注册对象的 delete（delete-pod-to-restart
        故障形态）在窗口内 → 照常归因——豁免由注册表挣得，不由动词挣得。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history("delete", "pod victim -n default")

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") == "kubectl_native"

    @pytest.mark.asyncio
    async def test_rescan_mixed_batch_still_attributed(
        self, sample_agent_state,
    ):
        """混合批：同一 AIMessage 里 teardown delete + 真 patch → 整条
        保留、patch 归因成立——剔除只能是整消息级，真证据不因邻居
        是清理而丢失。"""
        state = self._state(sample_agent_state)
        state["messages"] = [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "delete",
                        "v_args": "role drill-rc-x -n ns",
                    },
                    "id": "t1",
                },
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "patch",
                        "v_args": "deployment demo -n ns -p {}",
                    },
                    "id": "t2",
                },
            ]),
        ]

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") == "kubectl_native"

    def test_rescan_matcher_skips_teardown_at_call_granularity(self):
        """单元牙（P3 matcher 形态）：seam 组合判定单元——_epoch_bounded_
        messages 切界 + make_teardown_matcher 在词汇层 call 级跳过。纯
        teardown AIMessage 不产出归因；混合批（teardown + 真 patch）的
        teardown 被跳过而 patch 照常归因；epoch 边界外不可见（先边界后
        豁免，seam 前的 teardown 本就出不了现在任何门）。"""
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _detect_injection_method,
            _epoch_bounded_messages,
        )

        state = {"execution_artifacts": [self._cleaned_carrier()]}
        matcher = make_teardown_matcher(state["execution_artifacts"])
        pure = AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
            "id": "t1",
        }])
        mixed = AIMessage(content="", tool_calls=[
            {
                "name": "kubectl",
                "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
                "id": "t1",
            },
            {
                "name": "kubectl",
                "args": {"subcommand": "patch", "v_args": "deployment demo -n ns -p {}"},
                "id": "t2",
            },
        ])
        text = AIMessage(content="thinking aloud")
        receipt = ToolMessage(content="ok", name="kubectl", tool_call_id="t1")
        history = [text, pure, receipt, mixed]

        # Call-level skip: the pure teardown contributes no attribution;
        # the mixed batch's teardown call is skipped while its patch call
        # still attributes (the whole message stays in the window).
        assert _detect_injection_method(
            _epoch_bounded_messages(history, state), is_teardown=matcher,
        ) == "kubectl_native"
        assert _detect_injection_method(
            [pure], is_teardown=matcher,
        ) is None

        # Epoch boundary applies first: messages before the seam are
        # invisible to the re-scan with or without the teardown matcher.
        state["attribution_epoch_index"] = 4
        assert _detect_injection_method(
            _epoch_bounded_messages(history, state), is_teardown=matcher,
        ) is None
        state["attribution_epoch_index"] = 1
        assert _detect_injection_method(
            _epoch_bounded_messages(history, state), is_teardown=matcher,
        ) == "kubectl_native"

    # -- O-2: teardown receipt must not CONFIRM attribution ----------------
    #
    # The confirmation guard (``scan_native_issue_disproven`` pre-pass)
    # reads any non-Error write receipt in the epoch as "a write landed —
    # attribution confirmed". A registered-vehicle teardown delete's
    # SUCCESS receipt used to ride that pre-pass: a genuinely failed
    # injection's revocation was masked by the cleanup that followed it.
    # The funnel (``_issue_disproven_in_epoch``) threads the ``is_teardown``
    # matcher into the guard (P3): the teardown call is skipped at CALL
    # granularity inside both the confirmation pre-pass and the judge
    # loop, so its receipt can never read as "a write landed".

    @pytest.mark.asyncio
    async def test_teardown_receipt_does_not_confirm_failed_injection(
        self, sample_agent_state,
    ):
        """O-2 主牙（撤销被掩蔽形态）：state 已 commit 的归因 + 同 epoch 内
        [失败的对象写 + 注册载体 teardown 删除成功] → 撤销不再被清理回执
        掩蔽——撤销 fire，method 显式清空。pre-fix：确认 pre-pass 把
        teardown 成功回执当「写已落地」→ 撤销不 fire → 本牙红。"""
        state = self._state(
            sample_agent_state, injection_method="kubectl_native",
        )
        state["messages"] = [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "patch",
                    "v_args": "deployment demo -n ns -p {}",
                },
                "id": "w1",
            }]),
            ToolMessage(
                content="Error: deployments.apps \"demo\" not found",
                name="kubectl",
                tool_call_id="w1",
            ),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "role drill-rc-x -n ns",
                },
                "id": "t1",
            }]),
            ToolMessage(
                content='delete ok: role "drill-rc-x" deleted',
                name="kubectl",
                tool_call_id="t1",
            ),
        ]

        result = await self._node(self._FakeDeleteLLM(
            "serviceaccount drill-rc-x -n ns --ignore-not-found"
        ))(state)

        # Channel A skips the teardown SA delete; the O-2 fix feeds the
        # confirmation guard the filtered window, so the teardown's success
        # receipt is an orphan the pre-pass cannot read: no "landed write" →
        # the failed patch's revocation fires (key present, explicit None).
        assert "injection_method" in result
        assert result["injection_method"] is None

    @pytest.mark.asyncio
    async def test_failed_injection_without_teardown_still_revoked(
        self, sample_agent_state,
    ):
        """对照组1（O-2 消防资格）：state 已 commit 的归因 + 失败对象写
        （无任何清理）→ 撤销照常开火——切窗不得改变撤销本体语义。断言用
        key 存在性：撤销 fire 时显式写 None，未 fire 时 result 无此键。"""
        state = self._state(
            sample_agent_state, injection_method="kubectl_native",
        )
        state["messages"] = [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "patch",
                    "v_args": "deployment demo -n ns -p {}",
                },
                "id": "w1",
            }]),
            ToolMessage(
                content="Error: deployments.apps \"demo\" not found",
                name="kubectl",
                tool_call_id="w1",
            ),
        ]

        result = await self._node(self._FakeDeleteLLM(
            "serviceaccount drill-rc-x -n ns --ignore-not-found"
        ))(state)

        # Revocation fired: the explicit None write (key present) is the
        # revocation seam's observable; a vacuous pass (key absent) fails.
        assert "injection_method" in result
        assert result["injection_method"] is None

    @pytest.mark.asyncio
    async def test_real_write_success_still_confirms(
        self, sample_agent_state,
    ):
        """对照组2（确认语义保留）：state 已 commit 的归因 + 同 epoch 真
        写成功（patch 落地）后跟失败写 → 确认守卫照常工作——成功落地的
        真写仍不可撤销（撤销不 fire，result 无 method 键）。"""
        state = self._state(
            sample_agent_state, injection_method="kubectl_native",
        )
        state["messages"] = [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "patch",
                    "v_args": "deployment demo -n ns -p {}",
                },
                "id": "w1",
            }]),
            ToolMessage(
                content='patch ok: deployment "demo" patched',
                name="kubectl",
                tool_call_id="w1",
            ),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "scale",
                    "v_args": "deployment demo -n ns --replicas=0",
                },
                "id": "w2",
            }]),
            ToolMessage(
                content="Error: cannot scale a terminating deployment",
                name="kubectl",
                tool_call_id="w2",
            ),
        ]

        result = await self._node(self._FakeDeleteLLM(
            "serviceaccount drill-rc-x -n ns --ignore-not-found"
        ))(state)

        # The landed patch is a REAL write: the confirmation guard keeps
        # the attribution — the revocation seam did NOT fire (no explicit
        # clear write; the attribution survives in state).
        assert "injection_method" not in result

    # -- R10-1: teardown ≠ replan-attempt evidence -----------------------
    #
    # The replan review rule refuses an infeasibility claim with no
    # attempt evidence under the current contract. Proof 2 of
    # ``_injection_attempted_this_contract`` scans epoch tool_calls with
    # the same issue-time classifier — a registered-vehicle teardown
    # delete used to classify as kubectl_native and masquerade as the
    # attempt that granted an otherwise-refused replan. Call-level skip
    # (mixed batches carry no genuine attempt either — message-level
    # filtering would miss that form).

    @staticmethod
    def _attempt_state(messages: list) -> dict:
        return {
            "task_id": "test-task",
            "messages": messages,
            "execution_artifacts": [TestIssueTimeTeardownAttribution._cleaned_carrier()],
            "injection_method": None,
            "experiment_uid": None,
        }

    def test_teardown_only_epoch_is_no_attempt(self):
        """缺陷牙（R10-1，探针态1 转正）：纯 teardown epoch 不构成尝试
        证据——清理不证明可行性，不得据此放行 replan。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        state = self._attempt_state([
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
                "id": "t1",
            }]),
        ])

        assert _injection_attempted_this_contract(state) is False

    def test_mixed_teardown_readonly_batch_is_no_attempt(self):
        """混合批牙（R10-1，探针态4 转正）：teardown delete + 只读 get 同批
        → 批内无任何真尝试，仍不得判尝试——call 级跳过覆盖消息级过滤
        救不了的形态（消息非纯 teardown，整条保留后 teardown call 会被
        分类为 kubectl_native）。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        state = self._attempt_state([
            AIMessage(content="", tool_calls=[
                {
                    "name": "kubectl",
                    "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
                    "id": "t1",
                },
                {
                    "name": "kubectl",
                    "args": {"subcommand": "get", "v_args": "pods -n ns"},
                    "id": "t2",
                },
            ]),
        ])

        assert _injection_attempted_this_contract(state) is False

    def test_empty_epoch_is_no_attempt(self):
        """对照组（探针态2）：无任何调用的 epoch 不构成尝试——规则本体
        语义不变。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        state = self._attempt_state([
            AIMessage(content="let me think about the plan"),
        ])

        assert _injection_attempted_this_contract(state) is False

    def test_unregistered_delete_is_still_an_attempt(self):
        """对照组（探针态3）：未注册对象的 delete（delete-pod-to-restart
        故障形态）仍是尝试——豁免由注册表挣得，不由动词挣得。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        state = self._attempt_state([
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "delete", "v_args": "pod victim -n default"},
                "id": "t1",
            }]),
        ])

        assert _injection_attempted_this_contract(state) is True

    def test_teardown_alongside_real_attempt_still_counts(self):
        """共存牙（用户质询形态「有 delete 不还是有注入吗」的代码化身）：
        teardown 与真注入同窗——正常干活形态（清障→注入）与失败尝试
        形态（注入失败→收尾清理）都必须 attempted=True（来自真注入
        call，teardown 不抢功也不添乱）。跳过只豁免 teardown 那一条
        call，绝不掩蔽同窗的真证据。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        teardown = {
            "name": "kubectl",
            "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
            "id": "t1",
        }
        real_write = {
            "name": "kubectl",
            "args": {"subcommand": "patch", "v_args": "deployment demo -n ns -p {}"},
            "id": "w1",
        }

        # 清障在前、注入在后（正常干活形态）。
        state = self._attempt_state([
            AIMessage(content="", tool_calls=[teardown]),
            AIMessage(content="", tool_calls=[real_write]),
        ])
        assert _injection_attempted_this_contract(state) is True

        # 注入在前、清理收尾在后（失败尝试形态——失败也是尝试，
        # 审查规则的核心语义；teardown 不能把它吃掉）。
        state = self._attempt_state([
            AIMessage(content="", tool_calls=[real_write]),
            AIMessage(content="", tool_calls=[teardown]),
        ])
        assert _injection_attempted_this_contract(state) is True

    def test_teardown_epoch_boundary_keeps_preseam_invisible(self):
        """epoch 边界牙：seam 前的 teardown 不出现在任何门；seam 前的真
        尝试同样不可见（epoch 语义不被 teardown 跳过改变）。"""
        from chaos_agent.agent.nodes.execute.execute_loop import (
            _injection_attempted_this_contract,
        )

        pre = AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": "delete", "v_args": "pod victim -n default"},
            "id": "p1",
        }])
        post = AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": "delete", "v_args": "role drill-rc-x -n ns"},
            "id": "t1",
        }])
        state = self._attempt_state([pre, post])
        state["attribution_epoch_index"] = 1

        assert _injection_attempted_this_contract(state) is False

    # -- O-3: teardown ≠ step-credit -------------------------------------
    #
    # The multi-step step self-check counts EXECUTED verbs off the message
    # history (ToolMessage receipts paired to issued kubectl calls). A
    # registered-vehicle teardown delete's SUCCESS receipt used to credit
    # the documented ``delete`` step — "asset removed" masquerading as
    # "fault injected against the target" — silencing the "step not yet
    # performed" soft reminder for a step the model never performed. P3
    # threads the ``is_teardown`` matcher into the executed-side scan
    # (call-level skip, mixed batches included); deliberately NO epoch
    # bound — the self-check is high-tolerance by design, so cross-epoch
    # credit for a GENUINE step verb stays.

    _STEP_SKILL_CASE = """# 案例文档

**演练步骤**
1. 使用 kubectl delete pod victim -n default 注入故障
2. 观察重建行为
"""

    @staticmethod
    def _receipt(tc_id: str) -> ToolMessage:
        return ToolMessage(
            name="kubectl", tool_call_id=tc_id, content="deleted"
        )

    def test_teardown_receipt_does_not_credit_delete_step(self):
        """主牙（P3 matcher 形态）：纯 teardown delete 的成功回执不得为文档
        化的 delete 步骤记功。同一消息列表两跑：无 matcher（缺陷形态活证，
        防牙退化）压制提醒返回 None；穿 matcher 后提醒恢复非 None——被测
        变量只有 matcher 穿线。"""
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher
        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )

        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "role drill-rc-x -n ns",
                },
                "id": "t1",
            }]),
            self._receipt("t1"),
        ]
        state = self._attempt_state(msgs)

        # 缺陷形态活证：无 matcher 时 teardown 回执记功 → 提醒被压制。
        assert (
            build_injection_step_selfcheck(
                self._STEP_SKILL_CASE, msgs, "kubectl_native"
            )
            is None
        )
        # 修复形态：matcher 穿线后 delete 步骤无执行证据 → 软提醒恢复。
        assert (
            build_injection_step_selfcheck(
                self._STEP_SKILL_CASE, msgs, "kubectl_native",
                is_teardown=make_teardown_matcher(
                    state["execution_artifacts"]
                ),
            )
            is not None
        )

    def test_unregistered_delete_still_credits_step(self):
        """对照牙：未注册对象的 delete（delete-pod-to-restart 故障形态）
        仍是真正的步骤执行——matcher 判非豁免、回执照常记功、无提醒。
        豁免由注册表挣得，不由动词挣得。"""
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher
        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )

        msgs = [
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {
                    "subcommand": "delete",
                    "v_args": "pod victim -n default",
                },
                "id": "d1",
            }]),
            self._receipt("d1"),
        ]
        state = self._attempt_state(msgs)
        matcher = make_teardown_matcher(state["execution_artifacts"])

        # Unregistered delete: the matcher refuses the exemption, so the
        # receipt keeps crediting the step — no reminder.
        assert matcher("kubectl", msgs[0].tool_calls[0]["args"]) is False
        assert (
            build_injection_step_selfcheck(
                self._STEP_SKILL_CASE, msgs, "kubectl_native",
                is_teardown=matcher,
            )
            is None
        )

    def test_mixed_batch_teardown_credit_matcher_skips_call(self):
        """混合批牙（P3 转正，原 xfail 钉住的残留）：[teardown delete + 只读
        get] 同批——批内无任何真 delete，但消息因 get 而必须保留。消息级
        过滤的结构性边界（整条保留后 teardown 的 delete 仍记功）由 call
        级 matcher 关闭：teardown 那一条被跳过，delete 步骤无执行证据，
        软提醒恢复非 None。"""
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher
        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )

        msgs = [
            AIMessage(content="", tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "delete",
                        "v_args": "role drill-rc-x -n ns",
                    },
                    "id": "t1",
                },
                {
                    "name": "kubectl_read",
                    "args": {
                        "subcommand": "get",
                        "v_args": "pod victim -n default",
                    },
                    "id": "g1",
                },
            ]),
            self._receipt("t1"),
            self._receipt("g1"),
        ]
        state = self._attempt_state(msgs)

        # RAW (no matcher) keeps the message whole — the teardown receipt
        # still credits (the pre-P3 defect shape, alive-proof).
        assert (
            build_injection_step_selfcheck(
                self._STEP_SKILL_CASE, msgs, "kubectl_native"
            )
            is None
        )
        # P3 call-level exemption: the teardown call is skipped inside the
        # executed-side scan; the delete step has no genuine evidence.
        assert (
            build_injection_step_selfcheck(
                self._STEP_SKILL_CASE, msgs, "kubectl_native",
                is_teardown=make_teardown_matcher(
                    state["execution_artifacts"]
                ),
            )
            is not None
        )


class TestZombieReplanLiveGate:
    """Round-28 R3 — the zombie-replan guard gates on the LIVE predicate.

    The committed twin is True for every task that ever injected, so on a
    destroyed experiment the guard NEVER fired and replan kept burning
    budget until MAX_EXECUTE_LOOP. "No further injection paths" is only
    worth terminating for when nothing live remains."""

    class _StubTracker:
        def update(self, *args, **kwargs):
            return None

        def fail(self, *args, **kwargs):
            return None

    async def _check(self, state, monkeypatch):
        import chaos_agent.agent.nodes.execute.execute_loop as loop_mod

        async def _no_sync(*args, **kwargs):
            return None

        monkeypatch.setattr(loop_mod, "sync_to_store", _no_sync)
        monkeypatch.setattr(settings, "max_replan_count", 2)
        return await loop_mod._check_execute_loop_limits(
            state, 3, "task-r28-zombie", self._StubTracker(),
        )

    @staticmethod
    def _lifecycle_pairs(uid, tc):
        return [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": tc + "-c",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id=tc + "-c",
            ),
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_destroy",
                    "args": {"uid": uid},
                    "id": tc + "-d",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"success"}',
                name="blade_destroy",
                tool_call_id=tc + "-d",
            ),
        ]

    def _stuck(self, **facts):
        return {"replan_requested": True, "replan_count": 2, **facts}

    @pytest.mark.asyncio
    async def test_destroyed_experiment_lets_guard_fire(self, monkeypatch):
        # The round-28 fix's own quadrant: nothing live remains (the
        # experiment is destroyed), so the stuck replan terminates — the
        # budget is saved instead of burning to MAX_EXECUTE_LOOP.
        uid = "deadbeef00000008"
        state = self._stuck(
            experiment_uid=uid,
            injection_method="kubectl_exec",
            retired_experiment_uids=[uid],
            messages=self._lifecycle_pairs(uid, "tc-r28-zd"),
        )
        result = await self._check(state, monkeypatch)
        assert result is not None
        assert result.get("replan_requested") is False
        assert result.get("error")

    @pytest.mark.asyncio
    async def test_never_injected_still_fires(self, monkeypatch):
        result = await self._check(self._stuck(), monkeypatch)
        assert result is not None

    @pytest.mark.asyncio
    async def test_live_experiment_keeps_guard_off(self, monkeypatch):
        # A live experiment still needs the loop to converge — the guard
        # must stay off (termination would strand the live fault).
        uid = "deadbeef00000009"
        state = self._stuck(
            experiment_uid=uid,
            injection_method="kubectl_exec",
            messages=self._lifecycle_pairs(uid, "tc-r28-zl")[:2],
        )
        assert await self._check(state, monkeypatch) is None

    @pytest.mark.asyncio
    async def test_native_attribution_keeps_guard_off(self, monkeypatch):
        # No native death oracle — conservative: the guard stays off.
        state = self._stuck(
            injection_method="kubectl_native",
            fault_handle={"kind": "native", "method": "kubectl_native"},
            messages=[],
        )
        assert await self._check(state, monkeypatch) is None


class TestIssueTimeChannelExecAttribution:
    """R22/G-6 — machinery exec（进注册恢复载体的 §3 验权 / §4 arm）
    不是故障注入，issue-time 检测器不得归因。

    通道族动词与拆除族（TestIssueTimeTeardownAttribution，R6-1）同属
    machinery≠mutation：删除面只答 delete，exec 面零立法——同一个谓词
    （issue_call_is_registered_teardown）升级为双面后此处全链收口：
    combo 误标链（活实验 + re-arm → combo_native_issued → 恢复永久
    改道 LLM 路径）、首注抢注链（verify exec 抢注 method + start_time，
    注入失败任务谎报已注入）、channel-B 重扫链（epoch 内 arm exec 被
    重扫 commit）。边界：豁免由 recovery_carrier 注册挣得（未注册 pod
    的 mutating exec 照常归因），fault binary 进载体不豁免（与漂移层
    同一个 withhold）。"""

    _ARM_V_ARGS = (
        "drill-rc-x -n ns -- sh -c '( sleep 300; "
        "C=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt; "
        "T=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token); "
        "U=https://kubernetes.default.svc/apis/apps/v1/namespaces/"
        "ns/deployments/web; "
        'for d in "{\\"spec\\":{\\"replicas\\":2}}"; do '
        "curl -s -X PATCH --cacert $C -H \"Authorization: Bearer $T\" "
        '-H "Content-Type: application/merge-patch+json" -d "$d" $U; done '
        ") >/tmp/restore.log 2>&1 & echo armed'"
    )
    _VERIFY_V_ARGS = (
        "drill-rc-x -n ns -- sh -c 'T=$(cat "
        "/var/run/secrets/kubernetes.io/serviceaccount/token); "
        "curl -s --cacert /var/run/secrets/kubernetes.io/serviceaccount/ca.crt "
        "-H \"Authorization: Bearer $T\" "
        "https://kubernetes.default.svc/api'"
    )

    class _FakeStore:
        def append_messages(self, task_id, messages, node_name=""):
            return None

    class _FakeHook:
        def __init__(self):
            self.session_store = (
                TestIssueTimeChannelExecAttribution._FakeStore()
            )

        async def __call__(self, state):
            return {}

    class _FakeExecLLM:
        """签发一个 kubectl exec（v_args 可调）——载荷在归因域内
        （词汇层判 mutating → kubectl_native），是被测行为。"""

        def __init__(self, v_args: str):
            self._v_args = v_args

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "exec", "v_args": self._v_args},
                    "id": "c1",
                }],
            )

    @staticmethod
    def _carrier() -> dict:
        return {
            "artifact_id": "recovery_carrier:ns/drill-rc-x",
            "type": "recovery_carrier",
            "status": "active",
            "task_id": "test-task",
            "name": "drill-rc-x",
            "namespace": "ns",
            "operation_family": "recovery_carrier",
            "rbac_family": [],
        }

    def _node(self, llm):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=self._FakeHook(),
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )

    def _state(self, sample_agent_state, **extra):
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["messages"] = [_finalized_msg()]
        state["execution_artifacts"] = [self._carrier()]
        state.update(extra)
        return state

    @pytest.mark.asyncio
    async def test_arm_exec_into_registered_carrier_not_committed(
        self, sample_agent_state,
    ):
        """主牙（H2a，§4 arm）：载体已注册 + arm exec → 不得 commit
        kubectl_native（timer 定时器是恢复机制的建立，不是注入），
        start_time 不得被劫持。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeExecLLM(
            self._ARM_V_ARGS
        ))(state)

        assert result.get("injection_method") is None
        assert "injection_start_time" not in result

    @pytest.mark.asyncio
    async def test_verify_exec_into_registered_carrier_not_committed(
        self, sample_agent_state,
    ):
        """主牙（H2a，§3 验权）：同一谓词面对验权载荷（curl GET 但
        $(cat) 使 readonly fail-closed 判 mutating）→ 同样不 commit。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeExecLLM(
            self._VERIFY_V_ARGS
        ))(state)

        assert result.get("injection_method") is None

    @pytest.mark.asyncio
    async def test_mutating_exec_into_unregistered_pod_still_attributed(
        self, sample_agent_state,
    ):
        """豁免由注册挣得不由动词挣得：mutating exec 进未注册 pod
        （exec 进程内改写 /etc/hosts 的原生注入形态）照常归因。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeExecLLM(
            "victim-pod -n ns -- sh -c 'echo x > /etc/hosts'"
        ))(state)

        assert result.get("injection_method") == "kubectl_native"

    @pytest.mark.asyncio
    async def test_fault_binary_exec_into_carrier_still_attributed(
        self, sample_agent_state,
    ):
        """withhold 钉住：stress-ng exec 进注册载体仍归因——与漂移层
        同一个 fault_binary_mutation 前置（静态分类器无法排除
        hostNetwork 形变），身份审查不因载体豁免。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeExecLLM(
            "drill-rc-x -n ns -- stress-ng --cpu 1"
        ))(state)

        assert result.get("injection_method") == "kubectl_native"

    @pytest.mark.asyncio
    async def test_arm_exec_with_live_experiment_marks_no_combo(
        self, sample_agent_state,
    ):
        """H1 主牙（combo 误标链）：活实验 + 载体 re-arm exec →
        combo_native_issued 不得标记——机制维护不是「两辆车都动过」，
        确定性恢复（blade destroy）不得被永久改道 LLM 路径。"""
        state = self._state(
            sample_agent_state,
            injection_method="host_blade",
            experiment_uid="beef000000350035",
            owned_experiment_uids=["beef000000350035"],
            retired_experiment_uids=[],
        )

        result = await self._node(self._FakeExecLLM(
            self._ARM_V_ARGS
        ))(state)

        assert result.get("combo_native_issued") is not True

    class _TextOnlyLLM:
        """Issues no tool_calls: channel A has nothing to do, so any
        attribution in the result must come from the channel B re-scan."""

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="nothing left to issue")

    def _history(self, v_args: str) -> list:
        return [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "kubectl",
                "args": {"subcommand": "exec", "v_args": v_args},
                "id": "t1",
            }]),
            ToolMessage(
                content="armed", name="kubectl", tool_call_id="t1",
            ),
        ]

    @pytest.mark.asyncio
    async def test_rescan_arm_exec_in_history_not_attributed(
        self, sample_agent_state,
    ):
        """channel-B 牙（H3 同源）：method 空 + 窗口内已执行的载体
        arm exec → epoch 重扫不得 commit kubectl_native（restored
        session 的幽灵接替）。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history(self._ARM_V_ARGS)

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") is None

    @pytest.mark.asyncio
    async def test_rescan_mutating_exec_into_unregistered_still_attributed(
        self, sample_agent_state,
    ):
        """对照组：真 mutating exec 在窗口内且目标未注册 → 重扫描常
        commit（RESUME 语义不受通道豁免影响）。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history(
            "victim-pod -n ns -- sh -c 'echo x > /etc/hosts'"
        )

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") == "kubectl_native"


class TestIssueTimeHostArmAttribution:
    """R23/G-7 — host 域 machinery（arm-first timer 登记）不是故障注入。

    host skill 降级方案的标准序列是「先武装定时恢复，再注入」：arm
    调用（systemd-run --on-active timer）先于注入命令发出，载荷在
    deadline 才执行。谓词只认 kubectl 通道时，arm 抢注 issue-time
    首个 slot（break 语义）→ 注入失败仍谎报已注入（H1'）、blade+timer
    对被误标 combo（H2'）、restored session 重扫把 arm 读成原生接替
    （H3'——scan_host_native_index 无 is_teardown 线程，P3 seam 在
    host 侧断线）。豁免锚是 timer 形态（ToolGuard 只放行该形态——
    单源 import，不二次推导）。"""

    _ARM = (
        "systemd-run --on-active=600s --unit=blade-cont-nginx "
        "sh -c 'kill -CONT $(pgrep -f nginx)'"
    )
    _INJECT = "kill -STOP 1234"

    class _FakeStore:
        def append_messages(self, task_id, messages, node_name=""):
            return None

    class _FakeHook:
        def __init__(self):
            self.session_store = (
                TestIssueTimeHostArmAttribution._FakeStore()
            )

        async def __call__(self, state):
            return {}

    class _FakeHostLLM:
        """签发 host_inject 调用（commands 可调，多调用同迭代发出，
        镜像 arm-first 标准序列）——载荷在归因域内。"""

        def __init__(self, commands: list[str]):
            self._commands = commands

        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": "host_inject",
                    "args": {"command": command},
                    "id": f"c{i}",
                } for i, command in enumerate(self._commands)],
            )

    class _TextOnlyLLM:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="nothing left to issue")

    def _node(self, llm):
        from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop

        return make_execute_loop(
            hook=self._FakeHook(),
            llm=llm,
            tools=[],
            env_info={"context": "test"},
        )

    def _state(self, sample_agent_state, **extra):
        state = sample_agent_state
        state["task_id"] = "test-task"
        state["execute_loop_count"] = 0
        state["messages"] = [_finalized_msg()]
        state["execution_artifacts"] = []
        # Host channel is a hard, known fact of the attribution scope:
        # host_native is only attributed on a resolved host channel.
        state["kube_connection_mode"] = "ssh"
        state.update(extra)
        return state

    @pytest.mark.asyncio
    async def test_arm_timer_not_committed(self, sample_agent_state):
        """主牙（H1' arm-only 谎报）：单 arm timer → 不得 commit
        host_native——timer 登记是恢复机制的建立，注入失败的会话
        不得凭 arm 证据谎报「已注入」。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeHostLLM([self._ARM]))(state)

        assert result.get("injection_method") is None
        assert "injection_start_time" not in result

    @pytest.mark.asyncio
    async def test_arm_first_order_does_not_preempt_injection(
        self, sample_agent_state,
    ):
        """H1' 抢注：同迭代 [arm, inject]（skill 标准序列）→
        host_native 由注入命令命中，slot 不被 arm 消耗。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeHostLLM(
            [self._ARM, self._INJECT]
        ))(state)

        assert result.get("injection_method") == "host_native"

    @pytest.mark.asyncio
    async def test_arm_timer_with_live_experiment_marks_no_combo(
        self, sample_agent_state,
    ):
        """H2'（combo 误标链）：活 blade 实验 + 防御性 arm timer →
        combo_native_issued 不得标记——机制维护不是「两辆车都动过」，
        确定性恢复（blade destroy）不得被永久改道 LLM 路径。"""
        state = self._state(
            sample_agent_state,
            injection_method="host_blade",
            experiment_uid="beef000000350035",
            owned_experiment_uids=["beef000000350035"],
            retired_experiment_uids=[],
        )

        result = await self._node(self._FakeHostLLM([self._ARM]))(state)

        assert result.get("combo_native_issued") is not True

    @pytest.mark.asyncio
    async def test_plain_injection_still_attributed(self, sample_agent_state):
        """钉住：无 arm 的纯注入（kill -STOP 即注入本体）照常归因
        host_native——豁免由 timer 形态挣得，不由工具名挣得。"""
        state = self._state(sample_agent_state)

        result = await self._node(self._FakeHostLLM([self._INJECT]))(state)

        assert result.get("injection_method") == "host_native"

    def _history(self, command: str) -> list:
        return [
            _finalized_msg(),
            AIMessage(content="", tool_calls=[{
                "name": "host_inject",
                "args": {"command": command},
                "id": "t1",
            }]),
            ToolMessage(
                content="Running timer as unit blade-cont-nginx.service",
                name="host_inject",
                tool_call_id="t1",
            ),
        ]

    @pytest.mark.asyncio
    async def test_rescan_arm_timer_in_history_not_attributed(
        self, sample_agent_state,
    ):
        """channel-B 牙（H3' 幽灵接替）：method 空 + 窗口内已成功的
        arm timer → 重扫不得 commit host_native（restored session 的
        message-scan 兜底读「timer 已武装」而非「原生已注入」）。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history(self._ARM)

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") is None

    @pytest.mark.asyncio
    async def test_rescan_plain_injection_still_attributed(
        self, sample_agent_state,
    ):
        """对照组：真注入在窗口内成功 → 重扫描常 commit（RESUME
        语义不受 host 面豁免影响）。"""
        state = self._state(sample_agent_state)
        state["messages"] = self._history(self._INJECT)

        result = await self._node(self._TextOnlyLLM())(state)

        assert result.get("injection_method") == "host_native"
