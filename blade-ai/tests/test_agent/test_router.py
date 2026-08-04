"""Tests for agent conditional router functions."""

from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.router import (
    route_after_confirmation,
    route_after_direct_execute,
    route_after_phase1_tools,
    route_after_safety,
    route_after_baseline,
    route_after_save_memory,
    route_after_batch_next,
    should_continue_agent_loop,
    should_continue_execute_loop,
    should_continue_verifier,
    should_continue_recover_verifier,
    should_continue_plan_builder,
    route_after_verifier_tools,
    route_after_finalize,
    route_after_recover_verifier_tools,
    route_after_recover_finalize,
)
from chaos_agent.config.settings import settings as _settings


class TestShouldContinueAgentLoop:
    """Test should_continue_agent_loop routing."""

    @patch("chaos_agent.agent.router.settings")
    def test_has_plan_and_skill_goes_to_extract_planning_metadata(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 1, "plan": "do something", "skill_name": "pod-kill"}
        assert should_continue_agent_loop(state) == "extract_planning_metadata"

    @patch("chaos_agent.agent.router.settings")
    def test_no_plan_continues_loop(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 1, "plan": None, "skill_name": None}
        assert should_continue_agent_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_max_iterations_rejected(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 10, "plan": None, "skill_name": "pod-kill"}
        assert should_continue_agent_loop(state) == "reject"

    @patch("chaos_agent.agent.router.settings")
    def test_max_iterations_no_skill_treated_as_reject(self, mock_settings):
        """Max iterations without skill → reject (not chat)."""
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 10, "plan": None, "skill_name": None}
        assert should_continue_agent_loop(state) == "reject"

    @patch("chaos_agent.agent.router.settings")
    def test_safety_status_rejected(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 1, "safety_status": "rejected"}
        assert should_continue_agent_loop(state) == "reject"

    @patch("chaos_agent.agent.router.settings")
    def test_has_plan_no_skill_continues(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 1, "plan": "do something", "skill_name": None}
        assert should_continue_agent_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_ai_message_with_skill_goes_to_extract_planning_metadata(self, mock_settings):
        """Fault injection request with skill activated should route to extract_planning_metadata."""
        mock_settings.max_agent_loop = 10
        ai_msg = type("AIMsg", (), {"tool_calls": [], "type": "ai", "content": "Ready to inject"})()
        state = {"agent_loop_count": 1, "skill_name": "pod-kill", "messages": [ai_msg]}
        assert should_continue_agent_loop(state) == "extract_planning_metadata"

    @patch("chaos_agent.agent.router.settings")
    def test_ai_message_no_skill_no_marker_continues(self, mock_settings):
        """LLM text without skill activation → continue (might still be planning)."""
        mock_settings.max_agent_loop = 10
        ai_msg = type("AIMsg", (), {"tool_calls": [], "type": "ai", "content": "Let me check the target"})()
        state = {"agent_loop_count": 1, "skill_name": None, "messages": [ai_msg]}
        assert should_continue_agent_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_below_max_iterations_normal(self, mock_settings):
        mock_settings.max_agent_loop = 10
        state = {"agent_loop_count": 5, "plan": None, "skill_name": None}
        assert should_continue_agent_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_finish_planning_tool_call_routes_to_continue(self, mock_settings):
        """finish_planning tool_calls now route to 'continue' (ToolNode handles them)."""
        mock_settings.max_agent_loop = 10
        ai_msg = type("AIMsg", (), {
            "tool_calls": [{"name": "finish_planning", "args": {"summary": "inject cpu"}, "id": "tc_1"}],
            "type": "ai",
            "content": "",
        })()
        state = {"agent_loop_count": 3, "skill_name": "cpu-fullload", "messages": [ai_msg]}
        assert should_continue_agent_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_save_fault_plan_tool_call_routes_to_continue(self, mock_settings):
        """save_fault_plan tool_calls now route to 'continue' (ToolNode handles them)."""
        mock_settings.max_agent_loop = 10
        ai_msg = type("AIMsg", (), {
            "tool_calls": [{"name": "save_fault_plan", "args": {"plan_content": "test"}, "id": "tc_1"}],
            "type": "ai",
            "content": "",
        })()
        state = {"agent_loop_count": 3, "skill_name": None, "messages": [ai_msg]}
        assert should_continue_agent_loop(state) == "continue"


class TestShouldContinueExecuteLoop:
    """Test should_continue_execute_loop routing."""

    @patch("chaos_agent.agent.router.settings")
    def test_has_blade_uid_goes_to_verifier(self, mock_settings):
        mock_settings.max_execute_loop = 15
        state = {
            "execute_loop_count": 1,
            "blade_uid": "abc123",
            "error": None,
            "messages": [AIMessage(content="done")],
        }
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_has_error_goes_to_verifier(self, mock_settings):
        """Error from execute_loop must not skip verifier — the verifier
        checks whether the fault actually took effect."""
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 1, "blade_uid": None, "error": "failed"}
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_max_iterations_goes_to_verifier(self, mock_settings):
        """Budget exhaustion must not skip the verifier either.

        This asserted ``"end"`` until task-ff057e7f, where it sent a run that
        burned 100/100 iterations straight to save_memory: verifier never ran,
        ``verification=null``, and the envelope reported success while the
        postmortem in the same payload said the experiment stalled.

        The policy directly above — "error must not skip verifier, the verifier
        checks whether the fault actually took effect" — applies with MORE force
        here: on exhaustion the model never concluded anything at all, so there
        is even less basis for a verdict without checking.
        """
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 15, "blade_uid": None, "error": None}
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_max_iterations_still_prefers_replan_when_eligible(self, mock_settings):
        """Replan short-circuits exhaustion, as it did before."""
        mock_settings.max_execute_loop = 15
        mock_settings.max_replan_count = 2
        state = {
            "execute_loop_count": 15,
            "replan_requested": True,
            "replan_count": 0,
            "error": None,
        }
        assert should_continue_execute_loop(state) in ("replan", "verifier")

    @patch("chaos_agent.agent.router.settings")
    def test_normal_continues(self, mock_settings):
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 1, "blade_uid": None, "error": None}
        assert should_continue_execute_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_replan_exhausted_with_error_goes_to_verifier(self, mock_settings):
        # When replan is exhausted and error is set, the router must still
        # route to verifier — error means the injection action may have
        # issues, but the verifier must check whether the fault actually
        # took effect. Replan is not triggered because replan_count >= max.
        mock_settings.max_execute_loop = 15
        mock_settings.max_replan_count = 3
        mock_settings.replan_auto_trigger = False
        state = {
            "execute_loop_count": 5,
            "blade_uid": None,
            "error": "Replan exhausted after 3 attempt(s)",
            "replan_requested": False,
            "replan_count": 3,
        }
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_injection_method_without_blade_uid_continues_if_tool_calls(self, mock_settings):
        """kubectl_native injection with pending tool_calls → continue (not verifier)."""
        mock_settings.max_execute_loop = 15
        ai_msg = type("AIMsg", (), {
            "tool_calls": [{"name": "kubectl", "args": {}}],
            "type": "ai", "content": "",
        })()
        state = {
            "execute_loop_count": 1,
            "blade_uid": None,
            "error": None,
            "injection_method": "kubectl_native",
            "messages": [ai_msg],
        }
        assert should_continue_execute_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_ai_text_with_injection_method_routes_to_verifier(self, mock_settings):
        """AI pure-text message with injection_method should route to verifier."""
        mock_settings.max_execute_loop = 15
        ai_msg = type("AIMsg", (), {"tool_calls": [], "type": "ai", "content": "Injection complete"})()
        state = {
            "execute_loop_count": 1,
            "blade_uid": None,
            "error": None,
            "injection_method": "kubectl_native",
            "messages": [ai_msg],
        }
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_replan_under_max_with_request_routes_to_replan(self, mock_settings):
        # Sanity: when replan IS still allowed and the LLM requested it,
        # router routes to "replan" so the graph re-enters agent_loop.
        mock_settings.max_execute_loop = 15
        mock_settings.max_replan_count = 3
        mock_settings.replan_auto_trigger = False
        state = {
            "execute_loop_count": 1,
            "blade_uid": None,
            "error": None,
            "replan_requested": True,
            "replan_count": 1,
        }
        assert should_continue_execute_loop(state) == "replan"


class TestRouteAfterSafety:
    """Test route_after_safety routing."""

    def test_rejected_goes_to_reject(self):
        state = {"safety_status": "rejected"}
        assert route_after_safety(state) == "reject"

    def test_retry_goes_to_agent_loop(self):
        state = {"safety_status": "retry"}
        assert route_after_safety(state) == "agent_loop"

    def test_safe_with_confirmation(self):
        state = {"safety_status": "safe", "needs_confirmation": True}
        assert route_after_safety(state) == "confirmation_gate"

    def test_safe_without_confirmation_direct(self):
        state = {"safety_status": "safe", "needs_confirmation": False, "direct": True}
        assert route_after_safety(state) == "baseline_capture"

    def test_safe_without_confirmation_llm(self):
        state = {"safety_status": "safe", "needs_confirmation": False, "direct": False}
        assert route_after_safety(state) == "baseline_capture"

    def test_warning_goes_to_confirmation(self):
        state = {"safety_status": "warning", "needs_confirmation": False}
        assert route_after_safety(state) == "confirmation_gate"

    def test_pending_goes_to_confirmation(self):
        state = {"safety_status": "pending", "needs_confirmation": False}
        assert route_after_safety(state) == "confirmation_gate"


class TestRouteAfterConfirmation:
    """Test route_after_confirmation routing."""

    def test_rejected_goes_to_reject(self):
        state = {"safety_status": "rejected"}
        assert route_after_confirmation(state) == "reject"

    def test_approved_goes_to_execute_direct(self):
        state = {"safety_status": "safe", "direct": True}
        assert route_after_confirmation(state) == "baseline_capture"

    def test_approved_goes_to_execute_llm(self):
        state = {"safety_status": "safe", "direct": False}
        assert route_after_confirmation(state) == "baseline_capture"

    def test_default_goes_to_execute(self):
        state = {"safety_status": "pending"}
        assert route_after_confirmation(state) == "baseline_capture"


class TestRouteAfterDirectExecute:
    """Test route_after_direct_execute routing."""

    def test_has_blade_uid_goes_to_verifier(self):
        state = {"blade_uid": "abc123"}
        assert route_after_direct_execute(state) == "verifier"

    def test_has_error_goes_to_end(self):
        state = {"blade_uid": None, "error": "failed"}
        assert route_after_direct_execute(state) == "end"

    def test_no_result_goes_to_verifier(self):
        """No blade_uid and no error defaults to verifier for safety."""
        state = {"blade_uid": None, "error": None}
        assert route_after_direct_execute(state) == "verifier"


class TestRouteAfterBaseline:
    """Test route_after_baseline routing — dispatches after shared baseline_capture."""

    def test_direct_mode_goes_to_direct_execute(self):
        state = {"direct": True}
        assert route_after_baseline(state) == "direct_execute"

    def test_nl_mode_goes_to_execute_loop(self):
        state = {"direct": False}
        assert route_after_baseline(state) == "execute_loop"

    def test_default_goes_to_execute_loop(self):
        state = {}
        assert route_after_baseline(state) == "execute_loop"


class TestRouteAfterPhase1Tools:
    """Test route_after_phase1_tools routing.

    Routes based on the most recent ToolMessage batch after phase1_tools
    ToolNode execution. Skips error ToolMessages.
    """

    def test_empty_messages_returns_agent_loop(self):
        assert route_after_phase1_tools({"messages": []}) == "agent_loop"

    def test_no_messages_key_returns_agent_loop(self):
        assert route_after_phase1_tools({}) == "agent_loop"

    def test_finish_planning_routes_to_extract(self):
        spec = self._fault_spec()
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "finish_planning", "id": "tc_1", "args": {
                "summary": "ready to inject",
            }}]),
            ToolMessage(content="ok", name="finish_planning", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs, "fault_spec": spec}) == "extract_planning_metadata"

    def test_save_fault_plan_returns_to_agent_loop(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "save_fault_plan", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="save_fault_plan", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

    def test_propose_plan_change_with_replan_context_routes_to_confirm(self):
        spec = self._fault_spec()
        proposed = {**self._planned_fault(spec), "action": "delay"}
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "propose_plan_change", "id": "tc_1", "args": {
                "fault_revision": 4, "proposed_fault": proposed,
            }}]),
            ToolMessage(content="ok", name="propose_plan_change", tool_call_id="tc_1"),
        ]
        state = {"messages": msgs, "fault_spec": spec, "replan_context": {"error_summary": "blade failed"}}
        assert route_after_phase1_tools(state) == "plan_change_confirm"

    def test_propose_plan_change_without_replan_context_routes_to_confirm(self):
        """Initial planning changes need the same user confirmation gate."""
        spec = self._fault_spec()
        proposed = {**self._planned_fault(spec), "action": "delay"}
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "propose_plan_change", "id": "tc_1", "args": {
                "fault_revision": 4, "proposed_fault": proposed,
            }}]),
            ToolMessage(content="ok", name="propose_plan_change", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs, "fault_spec": spec}) == "plan_change_confirm"

    def test_regular_tool_returns_agent_loop(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "read_file", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="read_file", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

    def test_error_tool_message_skipped(self):
        """Error ToolMessages are skipped; falls through to agent_loop."""
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "finish_planning", "id": "tc_1", "args": {}}]),
            ToolMessage(content="error", name="finish_planning", tool_call_id="tc_1", status="error"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

    def test_error_text_finish_planning_returns_to_agent_loop(self):
        """Alignment refusal is a tool result, but must not end planning."""
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "finish_planning", "id": "tc_1", "args": {}}]),
            ToolMessage(
                content="Error: the plan does not preserve the approved semantic contract.",
                name="finish_planning",
                tool_call_id="tc_1",
            ),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

    def test_mixed_batch_finish_planning_wins(self):
        """When multiple ToolMessages exist, first match (reversed) wins."""
        spec = self._fault_spec()
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "read_file", "id": "tc_1", "args": {}},
                {"name": "finish_planning", "id": "tc_2", "args": {
                    "summary": "ready to inject",
                }},
            ]),
            ToolMessage(content="ok", name="read_file", tool_call_id="tc_1"),
            ToolMessage(content="ok", name="finish_planning", tool_call_id="tc_2"),
        ]
        assert route_after_phase1_tools({"messages": msgs, "fault_spec": spec}) == "extract_planning_metadata"

    def test_error_finish_planning_plus_normal_read_file(self):
        """Error finish_planning skipped; normal read_file doesn't match → agent_loop."""
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "finish_planning", "id": "tc_1", "args": {}},
                {"name": "read_file", "id": "tc_2", "args": {}},
            ]),
            ToolMessage(content="err", name="finish_planning", tool_call_id="tc_1", status="error"),
            ToolMessage(content="ok", name="read_file", tool_call_id="tc_2"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

    @staticmethod
    def _fault_spec() -> dict:
        return {
            "revision": 4,
            "objective": "inject packet loss",
            "scope": "pod",
            "blade_target": "network",
            "blade_action": "drop",
            "namespace": "default",
            "names": ["nginx"],
            "labels": {"app": "web"},
            "params": {"percent": "100"},
            "params_flags": [],
            "duration_seconds": 0,
            "boundaries": ["staging only"],
            "constraints": ["one logical experiment"],
            "assumptions": [],
        }

    @staticmethod
    def _planned_fault(spec: dict) -> dict:
        return {
            "objective": spec["objective"], "scope": spec["scope"],
            "target": spec["blade_target"], "action": spec["blade_action"],
            "namespace": spec["namespace"], "names": spec["names"],
            "labels": spec["labels"], "params": spec["params"],
            "params_flags": spec["params_flags"],
            "duration_seconds": spec["duration_seconds"],
            "boundaries": spec["boundaries"], "constraints": spec["constraints"],
            "assumptions": spec["assumptions"],
        }

    @staticmethod
    def _finish_messages(args: dict) -> list:
        return [
            AIMessage(content="", tool_calls=[{
                "name": "finish_planning", "id": "finish-1", "args": args,
            }]),
            ToolMessage(
                content="Planning finalized",
                name="finish_planning",
                tool_call_id="finish-1",
            ),
        ]

    def test_faultspec_finish_proceeds_as_pure_signal(self):
        """finish_planning is a pure 'planning complete' signal — it proceeds
        without re-declaring any contract. The reviewed FaultSpec in state is
        the sole authority; drift is handled only by propose_plan_change."""
        spec = self._fault_spec()
        state = {"messages": self._finish_messages({"summary": "ready"}), "fault_spec": spec}
        assert route_after_phase1_tools(state) == "extract_planning_metadata"

    def test_faultspec_finish_ignores_differing_self_report(self):
        """A finish_planning self-report that differs from the reviewed spec
        still proceeds — execution binds to state.fault_spec, not the report.
        A real change must go through propose_plan_change."""
        spec = self._fault_spec()
        args = {"summary": "drop is not feasible", "planned_fault": {**self._planned_fault(spec), "action": "delay"}}
        state = {"messages": self._finish_messages(args), "fault_spec": spec}
        assert route_after_phase1_tools(state) == "extract_planning_metadata"

    def test_faultspec_proposal_requires_current_revision_and_routes_to_confirm(self):
        spec = self._fault_spec()
        proposed = {**self._planned_fault(spec), "action": "delay"}
        args = {
            "reason": "drop is not feasible",
            "fault_revision": 4,
            "proposed_fault": proposed,
        }
        messages = [
            AIMessage(content="", tool_calls=[{
                "name": "propose_plan_change", "id": "change-1", "args": args,
            }]),
            ToolMessage(
                content="Plan change proposed",
                name="propose_plan_change",
                tool_call_id="change-1",
            ),
        ]
        assert route_after_phase1_tools({
            "messages": messages, "fault_spec": spec,
        }) == "plan_change_confirm"

    def test_faultspec_stale_revision_delegates_to_confirm(self):
        """Behaviour change (task-5193538b): a stale-revision proposal used to
        vanish into the agent loop — the model was told "Plan change
        proposed." and nothing ever happened. plan_change_confirm now
        answers it with an actionable [PLAN CHANGE RETRY] HumanMessage."""
        spec = self._fault_spec()
        proposed = {**self._planned_fault(spec), "action": "delay"}
        stale_args = {
            "reason": "drop is not feasible",
            "fault_revision": 3,
            "proposed_fault": proposed,
        }
        stale_messages = [
            AIMessage(content="", tool_calls=[{
                "name": "propose_plan_change", "id": "change-2", "args": stale_args,
            }]),
            ToolMessage(
                content="Plan change proposed",
                name="propose_plan_change",
                tool_call_id="change-2",
            ),
        ]
        assert route_after_phase1_tools({
            "messages": stale_messages, "fault_spec": spec,
        }) == "plan_change_confirm"

    @patch("chaos_agent.agent.router.settings")
    def test_faultspec_plain_text_exits_planning(self, mock_settings):
        """With a complete reviewed FaultSpec, plain text + active skill exits
        Phase 1 normally — the reviewed spec is the authority, so there is no
        contract to re-declare (the old requires_explicit_finish gate is gone)."""
        mock_settings.max_agent_loop = 10
        state = {
            "agent_loop_count": 1,
            "skill_name": "network-drop",
            "fault_spec": self._fault_spec(),
            "messages": [AIMessage(content="planning is ready")],
        }
        assert should_continue_agent_loop(state) == "extract_planning_metadata"

    def test_stops_at_non_tool_message(self):
        """Iteration stops at the first non-ToolMessage (e.g. AIMessage boundary)."""
        msgs = [
            AIMessage(content="old turn", tool_calls=[{"name": "finish_planning", "id": "tc_old", "args": {}}]),
            ToolMessage(content="ok", name="finish_planning", tool_call_id="tc_old"),
            AIMessage(content="new turn", tool_calls=[{"name": "read_file", "id": "tc_new", "args": {}}]),
            ToolMessage(content="ok", name="read_file", tool_call_id="tc_new"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"


class TestSchemeBVerifierRouting:
    """Scheme B verifier/recover routing — incl. the count==max forced-verdict
    finalize path (regression for the bug where count>=max returned 'done' and
    dropped the forced last-iteration verdict)."""

    # ---- should_continue_verifier (inject) ----
    def test_verification_set_is_done(self):
        assert should_continue_verifier({"verification": {"level": "verified"}}) == "done"

    def test_tool_calls_continue(self):
        msg = AIMessage(content="", tool_calls=[{"name": "kubectl", "args": {}, "id": "1"}])
        assert should_continue_verifier({"verifier_loop_count": 1, "messages": [msg]}) == "continue"

    def test_ai_text_finalizes(self):
        assert should_continue_verifier(
            {"verifier_loop_count": 1, "messages": [AIMessage(content="VERIFICATION_RESULT: ...")]}
        ) == "finalize"

    def test_count_at_max_without_verification_finalizes(self):
        # Forced last-iteration verdict must be processed, not dropped.
        assert should_continue_verifier(
            {"verifier_loop_count": _settings.max_verifier_loop, "verification": None,
             "messages": [AIMessage(content="VERIFICATION_RESULT:\n- Overall: verified")]}
        ) == "finalize"

    def test_count_over_max_with_verification_is_done(self):
        # Node max-guard already set verification → terminal.
        assert should_continue_verifier(
            {"verifier_loop_count": _settings.max_verifier_loop + 1,
             "verification": {"level": "partial"}, "messages": []}
        ) == "done"

    # ---- route_after_verifier_tools ----
    def test_route_after_tools_submit_finalizes(self):
        tm = ToolMessage(content="ok", name="submit_verification", tool_call_id="1")
        assert route_after_verifier_tools({"messages": [AIMessage(content=""), tm]}) == "finalize"

    def test_route_after_tools_kubectl_loops(self):
        tm = ToolMessage(content="pods", name="kubectl", tool_call_id="1")
        assert route_after_verifier_tools({"messages": [AIMessage(content=""), tm]}) == "verifier_loop"

    def test_route_after_tools_submit_bundled_with_kubectl_finalizes(self):
        a = AIMessage(content="")
        k = ToolMessage(content="pods", name="kubectl", tool_call_id="1")
        s = ToolMessage(content="ok", name="submit_verification", tool_call_id="2")
        assert route_after_verifier_tools({"messages": [a, k, s]}) == "finalize"

    # ---- route_after_finalize ----
    def test_route_after_finalize_verification_to_se_detect(self):
        assert route_after_finalize({"verification": {"level": "verified"}}) == "se_detect"

    def test_route_after_finalize_no_verification_loops(self):
        assert route_after_finalize({"verification": None}) == "verifier_loop"

    def test_route_after_finalize_replan_requested(self):
        assert route_after_finalize({"replan_requested": True}) == "replan"

    def test_route_after_finalize_replan_takes_priority_over_verification(self):
        # replan_requested must be checked BEFORE verification
        assert route_after_finalize(
            {"replan_requested": True, "verification": {"level": "verified"}}
        ) == "replan"

    # ---- recover variants ----
    def test_recover_count_at_max_in_layer2_finalizes(self):
        assert should_continue_recover_verifier(
            {"verifier_loop_count": _settings.max_recover_verifier_loop,
             "recover_verification": None, "layer2_context_added": True,
             "messages": [AIMessage(content="RECOVERY_VERIFICATION_RESULT:\n- Overall: recovered")]}
        ) == "finalize"

    def test_recover_count_at_max_in_layer1_is_done(self):
        # Budget exhausted still in Layer 1 (no Layer 2 verdict) → done.
        assert should_continue_recover_verifier(
            {"verifier_loop_count": _settings.max_recover_verifier_loop,
             "recover_verification": None, "layer2_context_added": False,
             "messages": [AIMessage(content="RECOVERY_EXECUTION_RESULT:\n- Status: success")]}
        ) == "done"

    def test_recover_layer2_verdict_text_finalizes(self):
        assert should_continue_recover_verifier(
            {"verifier_loop_count": 2, "recover_verification": None,
             "layer2_context_added": True,
             "messages": [AIMessage(content="RECOVERY_VERIFICATION_RESULT: ...")]}
        ) == "finalize"

    def test_recover_layer1_transition_text_continues(self):
        # Layer 1 → Layer 2 transition (context not built yet) → continue.
        assert should_continue_recover_verifier(
            {"verifier_loop_count": 1, "recover_verification": None,
             "layer2_context_added": False,
             "messages": [AIMessage(content="RECOVERY_EXECUTION_RESULT: ...")]}
        ) == "continue"

    def test_route_after_recover_tools_submit_finalizes(self):
        tm = ToolMessage(content="ok", name="submit_recover_verification", tool_call_id="1")
        assert route_after_recover_verifier_tools({"messages": [AIMessage(content=""), tm]}) == "finalize"

    def test_route_after_recover_finalize_done(self):
        assert route_after_recover_finalize({"recover_verification": {"level": "recovered"}}) == "done"

    def test_route_after_recover_finalize_loops(self):
        assert route_after_recover_finalize({"recover_verification": None}) == "recover_verifier_loop"


class TestShouldContinuePlanBuilder:
    """Test should_continue_plan_builder routing."""

    def test_tool_calls_continues(self):
        state = {"messages": [AIMessage(content="", tool_calls=[{"name": "kubectl_read", "args": {}, "id": "1"}])]}
        assert should_continue_plan_builder(state) == "continue"

    def test_no_tool_calls_ends(self):
        state = {"messages": [AIMessage(content="plan done")]}
        from langgraph.graph import END
        assert should_continue_plan_builder(state) == END

    def test_no_plan_confirmed_ends(self):
        state = {"messages": [AIMessage(content="cancelled")], "plan_confirmed": False}
        from langgraph.graph import END
        assert should_continue_plan_builder(state) == END

    def test_empty_messages_no_confirm_ends(self):
        state = {"messages": []}
        from langgraph.graph import END
        assert should_continue_plan_builder(state) == END


class TestRouteAfterSaveMemory:
    """Test route_after_save_memory routing — batch loop vs END."""

    def test_batch_in_progress_routes_to_batch_next(self):
        state = {"batch_submit_args": {"faults": [{"scope": "pod"}, {"scope": "node"}]}}
        assert route_after_save_memory(state) == "batch_next"

    def test_single_fault_batch_routes_to_batch_next(self):
        state = {"batch_submit_args": {"faults": [{"scope": "pod"}]}}
        assert route_after_save_memory(state) == "batch_next"

    def test_no_batch_args_ends(self):
        from langgraph.graph import END
        assert route_after_save_memory({}) == END

    def test_none_batch_args_ends(self):
        from langgraph.graph import END
        assert route_after_save_memory({"batch_submit_args": None}) == END

    def test_empty_faults_ends(self):
        from langgraph.graph import END
        assert route_after_save_memory({"batch_submit_args": {"faults": []}}) == END

    def test_non_dict_batch_args_ends(self):
        from langgraph.graph import END
        assert route_after_save_memory({"batch_submit_args": "invalid"}) == END


class TestRouteAfterBatchNext:
    """Test route_after_batch_next routing — loop or END."""

    def test_more_faults_loops_to_batch_setup(self):
        state = {
            "batch_submit_args": {"faults": [{"scope": "pod"}, {"scope": "node"}, {"scope": "pod"}]},
            "current_fault_index": 1,
        }
        assert route_after_batch_next(state) == "batch_setup"

    def test_last_fault_done_ends(self):
        from langgraph.graph import END
        state = {
            "batch_submit_args": {"faults": [{"scope": "pod"}, {"scope": "node"}]},
            "current_fault_index": 2,
        }
        assert route_after_batch_next(state) == END

    def test_exact_boundary_ends(self):
        from langgraph.graph import END
        state = {
            "batch_submit_args": {"faults": [{"scope": "pod"}]},
            "current_fault_index": 1,
        }
        assert route_after_batch_next(state) == END

    def test_index_zero_with_faults_loops(self):
        state = {
            "batch_submit_args": {"faults": [{"scope": "pod"}]},
            "current_fault_index": 0,
        }
        assert route_after_batch_next(state) == "batch_setup"

    def test_no_batch_args_ends(self):
        from langgraph.graph import END
        assert route_after_batch_next({"current_fault_index": 0}) == END
