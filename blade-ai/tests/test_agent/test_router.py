"""Tests for agent conditional router functions."""

from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.router import (
    route_after_confirmation,
    route_after_direct_execute,
    route_after_load_memory,
    route_after_phase1_tools,
    route_after_safety,
    route_after_baseline,
    should_continue_agent_loop,
    should_continue_execute_loop,
    should_continue_verifier,
    should_continue_recover_verifier,
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
        state = {"execute_loop_count": 1, "blade_uid": "abc123", "error": None}
        assert should_continue_execute_loop(state) == "verifier"

    @patch("chaos_agent.agent.router.settings")
    def test_has_error_goes_to_end(self, mock_settings):
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 1, "blade_uid": None, "error": "failed"}
        assert should_continue_execute_loop(state) == "end"

    @patch("chaos_agent.agent.router.settings")
    def test_max_iterations_goes_to_end(self, mock_settings):
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 15, "blade_uid": None, "error": None}
        assert should_continue_execute_loop(state) == "end"

    @patch("chaos_agent.agent.router.settings")
    def test_normal_continues(self, mock_settings):
        mock_settings.max_execute_loop = 15
        state = {"execute_loop_count": 1, "blade_uid": None, "error": None}
        assert should_continue_execute_loop(state) == "continue"

    @patch("chaos_agent.agent.router.settings")
    def test_replan_exhausted_with_terminal_error_goes_to_end(self, mock_settings):
        # Regression: when ``execute_loop`` converts a post-max [REPLAN]
        # request into a terminal failure (sets ``error`` but keeps
        # ``replan_requested=False``), the router MUST take the "end"
        # branch via the ``state.error`` check. Before the fix the
        # router would fall through to "continue" because the side-
        # effect block in execute_loop cleared ``error`` whenever
        # [REPLAN] fired, regardless of ``replan_count`` — letting the
        # LLM keep emitting [REPLAN] indefinitely.
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
        assert should_continue_execute_loop(state) == "end"

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


class TestRouteAfterLoadMemory:
    """Test route_after_load_memory routing."""

    def test_direct_goes_to_direct_setup(self):
        state = {"direct": True}
        assert route_after_load_memory(state) == "direct_setup"

    def test_llm_goes_to_agent_loop(self):
        state = {"direct": False}
        assert route_after_load_memory(state) == "agent_loop"

    def test_default_goes_to_agent_loop(self):
        state = {}
        assert route_after_load_memory(state) == "agent_loop"


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
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "finish_planning", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="finish_planning", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "extract_planning_metadata"

    def test_save_fault_plan_routes_to_extract(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "save_fault_plan", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="save_fault_plan", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "extract_planning_metadata"

    def test_propose_plan_change_with_replan_context_routes_to_confirm(self):
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "propose_plan_change", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="propose_plan_change", tool_call_id="tc_1"),
        ]
        state = {"messages": msgs, "replan_context": {"error_summary": "blade failed"}}
        assert route_after_phase1_tools(state) == "plan_change_confirm"

    def test_propose_plan_change_without_replan_context_returns_agent_loop(self):
        """Initial planning: propose_plan_change should NOT route to confirm."""
        msgs = [
            AIMessage(content="", tool_calls=[{"name": "propose_plan_change", "id": "tc_1", "args": {}}]),
            ToolMessage(content="ok", name="propose_plan_change", tool_call_id="tc_1"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "agent_loop"

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

    def test_mixed_batch_finish_planning_wins(self):
        """When multiple ToolMessages exist, first match (reversed) wins."""
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "read_file", "id": "tc_1", "args": {}},
                {"name": "finish_planning", "id": "tc_2", "args": {}},
            ]),
            ToolMessage(content="ok", name="read_file", tool_call_id="tc_1"),
            ToolMessage(content="ok", name="finish_planning", tool_call_id="tc_2"),
        ]
        assert route_after_phase1_tools({"messages": msgs}) == "extract_planning_metadata"

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
