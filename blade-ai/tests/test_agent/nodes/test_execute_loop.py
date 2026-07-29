"""Tests for execute_loop node."""

import pytest
from langchain_core.messages import ToolMessage, AIMessage

from chaos_agent.agent.nodes.execute.execute_loop import (
    execute_loop,
    _extract_blade_uid_from_messages,
    _detect_injection_method,
    _should_redetect_injection_method,
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

    def test_destroyed_uid_not_returned(self):
        """Root-cause guard: a UID sent to blade_destroy is cleaned-up/residual
        and must NOT be returned as the active injection, even if blade_create
        originally reported it. (Contrast with test_blade_create_tool_message,
        where the same create result IS returned when no destroy happened.)"""
        ai_destroy = AIMessage(content="", tool_calls=[
            {"name": "blade_destroy", "args": {"uid": "dead-uid"}, "id": "bd1"},
        ])
        create_ok = ToolMessage(
            content='{"code":200,"success":true,"result":"dead-uid"}',
            tool_call_id="bc1", name="blade_create",
        )
        destroy_ok = ToolMessage(
            content='{"code":200,"success":true,"result":"dead-uid"}',
            tool_call_id="bd1", name="blade_destroy",
        )
        messages = [create_ok, ai_destroy, destroy_ok]
        assert _extract_blade_uid_from_messages(messages) is None


class TestExtractBladeUidKubectlExec:
    """Tests for _extract_blade_uid_from_messages with kubectl exec blade output."""

    def test_kubectl_exec_blade_success(self):
        """kubectl exec blade ToolMessage with ChaosBlade success JSON → extract uid."""
        from langchain_core.messages import AIMessage
        ai_msg = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "tc1",
            "args": {"subcommand": "exec", "v_args": "pod1 -n chaosblade -- blade create k8s pod-cpu fullload"},
        }])
        tool_msg = ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            tool_call_id="tc1",
            name="kubectl",
        )
        assert _extract_blade_uid_from_messages([ai_msg, tool_msg]) == "a0f2357a939a9bb8"

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
        from langchain_core.messages import AIMessage
        ai_msg = AIMessage(content="", tool_calls=[{
            "name": "kubectl", "id": "tc2",
            "args": {"subcommand": "exec", "v_args": "pod1 -n chaosblade -- blade create k8s pod-cpu fullload"},
        }])
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
        messages = [ai_msg, msg1, msg2]
        # msg1 is not valid JSON, msg2 provides the fallback uid
        assert _extract_blade_uid_from_messages(messages) == "a0f2357a939a9bb8"

    def test_multiple_kubectl_results_uses_latest(self):
        """Multiple kubectl exec blade results → returns the latest one."""
        from langchain_core.messages import AIMessage
        ai_msg = AIMessage(content="", tool_calls=[
            {"name": "kubectl", "id": "tc1", "args": {"subcommand": "exec", "v_args": "pod1 -- blade create k8s pod-cpu fullload"}},
            {"name": "kubectl", "id": "tc2", "args": {"subcommand": "exec", "v_args": "pod1 -- blade create k8s pod-cpu fullload"}},
        ])
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
        messages = [ai_msg, msg1, msg2]
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
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":"abc123"}') == "abc123"

    def test_failure_json(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":500,"success":false,"error":"fail"}') is None

    def test_non_string_result(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":{"uid":"abc"}}') is None

    def test_empty_result(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content('{"code":200,"success":true,"result":""}') is None

    def test_non_json_content(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content("not json") is None

    def test_non_string_input(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_uid_from_content
        assert _parse_blade_uid_from_content(None) is None


class TestParseBladeCreateFromVArgs:
    """Tests for _parse_blade_create_from_v_args helper."""

    def test_network_loss(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_create_from_v_args
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
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_create_from_v_args
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
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_create_from_v_args
        v_args = "otel-c-tool-xxx -n chaosblade -- blade destroy abc123"
        result = _parse_blade_create_from_v_args(v_args)
        assert result is None

    def test_non_blade_kubectl(self):
        from chaos_agent.agent.nodes.execute.execute_loop import _parse_blade_create_from_v_args
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
        current = AIMessage(content="", tool_calls=[{
            "name": "kubectl",
            "args": {"subcommand": "exec", "v_args": "debug-pod -- iptables -V"},
            "id": "new-call",
        }])
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
        assert _detect_injection_method(msgs, None, is_host=False) is None
        # On a host channel it is classified host_native (recoverable).
        assert _detect_injection_method(msgs, None, is_host=True) == "host_native"

    def test_detect_blade_uid_wins_over_host_native(self):
        blade = ToolMessage(
            content='{"code":200,"success":true,"result":"uid-123"}',
            name="blade_create",
            tool_call_id="b1",
        )
        # A real blade experiment must not be downgraded to host_native.
        assert _detect_injection_method([blade], "uid-123", is_host=True) == "host_blade"


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
        args = {"subcommand": "exec", "v_args": "tool-pod -n chaosblade -- blade create k8s pod-cpu fullload"}
        assert self._classify("kubectl", args) == "kubectl_exec"

    def test_object_write_verbs_are_kubectl_native(self):
        for sub in ("scale", "patch", "delete", "cordon", "taint", "set", "drain", "label"):
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
        for v in ("p -n ns -- cat /proc/net/dev", "p -n ns -- ps aux | grep x",
                  "p -n ns -- tc qdisc show dev eth0"):
            args = {"subcommand": "exec", "v_args": v}
            assert self._classify("kubectl", args) is None, v

    def test_readonly_subcommand_is_none(self):
        for sub in ("get", "describe", "logs"):
            assert self._classify("kubectl", {"subcommand": sub, "v_args": "pods"}) is None

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

    def test_records_kubectl_native_for_scale(self):
        tcs = [{"name": "kubectl", "args": {"subcommand": "scale",
                "v_args": "deploy/foo --replicas=0"}, "id": "k1"}]
        result = self._run(tcs)
        assert result.get("injection_method") == "kubectl_native"
        assert result.get("injection_start_time")

    def test_records_kubectl_native_for_mutating_exec(self):
        tcs = [{"name": "kubectl", "args": {"subcommand": "exec",
                "v_args": "p -n ns -- dmsetup create errdev --table '0 100 error'"},
                "id": "k1"}]
        assert self._run(tcs).get("injection_method") == "kubectl_native"

    def test_no_record_for_readonly_exec(self):
        tcs = [{"name": "kubectl", "args": {"subcommand": "exec",
                "v_args": "p -n ns -- cat /proc/net/dev"}, "id": "k1"}]
        assert "injection_method" not in self._run(tcs)

    def test_blade_create_deferred_to_uid_path(self):
        # Experiment methods are NOT committed at issue time (they need the
        # blade_uid proof) so a failed blade + kubectl-native fallback is not
        # mis-recorded as host_blade.
        tcs = [{"name": "blade_create", "args": {}, "id": "b1"}]
        assert "injection_method" not in self._run(tcs)

    def test_does_not_override_existing_method(self):
        tcs = [{"name": "kubectl", "args": {"subcommand": "scale",
                "v_args": "deploy/foo --replicas=0"}, "id": "k1"}]
        result = self._run(tcs, state={"injection_method": "host_blade"})
        assert "injection_method" not in result


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
        _process_response_tool_calls(
            response, state, result, self._StubTracker(), 1
        )
        assert result.get("_execute_text_stall_count") == 0


class TestShouldRedetectInjectionMethod:
    """Channel B re-scan gate: it runs only for RESUME + blade_uid UPGRADE,
    and is skipped in steady state so it does not re-derive the same answer
    every iteration (direction B: issue-time recording is the primary path).
    """

    def test_resume_when_no_method_yet(self):
        # Nothing recorded → scan history to (re)attribute (resume / first turn).
        assert _should_redetect_injection_method(None, None) is True
        assert _should_redetect_injection_method("", "uid-1") is True

    def test_steady_kubectl_native_without_uid_skips(self):
        # The common multi-step case: method set, no blade_uid → no re-scan.
        assert _should_redetect_injection_method("kubectl_native", None) is False

    def test_upgrade_when_uid_appears_over_multi_step(self):
        # kubectl_native is multi-step; a fresh blade_uid must trigger the
        # upgrade check to promote it to the experiment backend.
        assert _should_redetect_injection_method("kubectl_native", "uid-1") is True

    def test_no_rescan_once_experiment_backend_set(self):
        # host_blade / kubectl_exec are not multi-step: even with a UID present
        # there is nothing left to upgrade, so skip.
        assert _should_redetect_injection_method("host_blade", "uid-1") is False
        assert _should_redetect_injection_method("kubectl_exec", "uid-1") is False

    def test_host_native_redetected_on_uid(self):
        # host_native is now multi-step (opts into the injection step self-check),
        # so — like kubectl_native — a UID appearing triggers re-detect/upgrade
        # candidacy (the rare host+blade_uid hybrid). Pure host (no uid) still
        # short-circuits to False above.
        assert _should_redetect_injection_method("host_native", "uid-1") is True
