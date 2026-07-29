"""Tests for intent state preservation across multi-turn dialogue.

Verifies that the "unset" confirmed_intent semantics, fault_intent
carry-forward, and dynamic section injection (completeness signal +
confirmed parameters) work correctly after the converse_stream state
reset strategy was changed from aggressive reset (None) to selective
carry-forward ("unset").
"""

from langchain_core.messages import AIMessage

from chaos_agent.agent.prompts.sections.intent import (
    get_intent_completeness_section,
)
from chaos_agent.agent.router import (
    route_after_intent_clarification,
    should_continue_intent_clarification,
)


class TestUnsetDoesNotShortCircuit:
    """confirmed_intent="unset" must NOT trigger the short-circuit
    return {} path in intent_clarification. It should fall through
    to the LLM dialogue path."""

    def test_unset_is_not_in_short_circuit_set(self):
        """"unset" is not one of ("inject", "chat", "recover")."""
        assert "unset" not in ("inject", "chat", "recover")

    def test_unset_routes_to_continue_or_end_not_inject(self):
        """route_after_intent_clarification with "unset" should not
        route to agent_loop/save_memory/recover_handler — it should
        fall through to intent_clarification (continue dialogue)."""

        state = {
            "confirmed_intent": "unset",
            "messages": [AIMessage(content="好的，让我帮你确认参数。")],
        }
        result = route_after_intent_clarification(state)
        assert result == "intent_clarification"

    def test_unset_with_tool_calls_routes_to_continue(self):
        """unset + tool_calls → "continue" (ReAct loop within
        should_continue_intent_clarification)."""
        ai_msg = AIMessage(
            content="",
            tool_calls=[{"name": "kubectl", "args": {"cmd": "get nodes"}, "id": "tc1"}],
        )
        state = {
            "confirmed_intent": "unset",
            "messages": [ai_msg],
        }
        # tool_calls routing is in should_continue_intent_clarification
        result = should_continue_intent_clarification(state)
        assert result == "continue"


class TestRouterUnsetFallThrough:
    """Router correctly handles confirmed_intent="unset" by treating
    it like None — no confirmed intent, fall through to tool/END."""

    def test_inject_routes_to_agent_loop(self):
        state = {"confirmed_intent": "inject", "messages": []}
        assert route_after_intent_clarification(state) == "agent_loop"

    def test_recover_routes_to_recover_handler(self):
        state = {"confirmed_intent": "recover", "messages": []}
        assert route_after_intent_clarification(state) == "recover_handler"

    def test_chat_routes_to_save_memory(self):
        state = {"confirmed_intent": "chat", "messages": []}
        assert route_after_intent_clarification(state) == "save_memory"

    def test_unset_routes_to_intent_clarification(self):
        """unset → continue dialogue (intent_clarification)."""
        state = {"confirmed_intent": "unset", "messages": []}
        result = route_after_intent_clarification(state)
        assert result == "intent_clarification"

    def test_none_also_routes_to_intent_clarification(self):
        """None (original behavior) also routes to continue dialogue."""
        state = {"confirmed_intent": None, "messages": []}
        result = route_after_intent_clarification(state)
        assert result == "intent_clarification"


class TestReviewedFaultSpec:
    """Verify the one FaultSpec contract is injected without a snapshot."""

    def test_empty_fault_spec_describes_the_contract(self):
        section = get_intent_completeness_section(None)
        assert "Reviewed FaultSpec" in section
        assert "No FaultSpec has been collected yet" in section

    def test_partial_fault_spec_remains_visible_during_collection(self):
        section = get_intent_completeness_section({
            "scope": "node", "target": "cpu", "namespace": "default",
        })
        assert '"scope": "node"' in section

    def test_complete_fault_spec_is_rendered_as_json(self):
        section = get_intent_completeness_section({
            "revision": 1,
            "objective": "节点 CPU 满载",
            "scope": "node",
            "blade_target": "cpu",
            "blade_action": "fullload",
            "namespace": "",
            "names": ["node-a"],
            "labels": {},
            "params": {"percent": "80"},
            "boundaries": [],
            "constraints": [],
            "assumptions": [],
        })
        assert "Reviewed FaultSpec" in section
        assert '"scope": "node"' in section
        assert '"objective": "节点 CPU 满载"' in section
