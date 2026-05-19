"""Tests for agent conditional router functions."""

from unittest.mock import patch

from chaos_agent.agent.router import (
    route_after_confirmation,
    route_after_direct_execute,
    route_after_load_memory,
    route_after_safety,
    route_after_baseline,
    should_continue_agent_loop,
    should_continue_execute_loop,
)


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
