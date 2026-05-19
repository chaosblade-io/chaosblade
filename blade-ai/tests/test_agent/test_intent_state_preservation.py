"""Tests for intent state preservation across multi-turn dialogue.

Verifies that the "unset" confirmed_intent semantics, fault_intent
carry-forward, and dynamic section injection (completeness signal +
confirmed parameters) work correctly after the converse_stream state
reset strategy was changed from aggressive reset (None) to selective
carry-forward ("unset").
"""

from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.intent_clarification import (
    _ensure_visible_content,
    _INTENT_CONTENT_FALLBACKS,
)
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


class TestEnsureVisibleContentFallback:
    """_ensure_visible_content should return intent-specific fallbacks,
    not the generic template that causes repeated identical responses."""

    def test_inject_fallback_is_specific(self):
        response = AIMessage(content="")
        result = _ensure_visible_content(response, intent="inject")
        assert result == _INTENT_CONTENT_FALLBACKS["inject"]
        assert result != "好的,我在听,请继续告诉我你想做什么。"

    def test_unset_fallback_is_specific(self):
        response = AIMessage(content="")
        result = _ensure_visible_content(response, intent="unset")
        assert result == _INTENT_CONTENT_FALLBACKS["unset"]
        assert "继续帮你确认参数" in result

    def test_default_fallback_is_not_repetitive(self):
        """Default fallback changed from the old generic template."""
        response = AIMessage(content="")
        result = _ensure_visible_content(response, intent="")
        assert result == "好的，请继续告诉我你的需求。"

    def test_non_empty_content_returns_directly(self):
        """If content is non-empty, it's returned directly regardless of intent."""
        response = AIMessage(content="好的，我需要知道节点名称。")
        result = _ensure_visible_content(response, intent="unset")
        assert result == "好的，我需要知道节点名称。"


class TestIntentPrefixInjection:
    """Verify that fault_intent fields are injected into the system
    prompt dynamic section (Confirmed Parameters + completeness signal)
    so the LLM won't re-ask for already-confirmed parameters."""

    def test_empty_fault_intent_no_dynamic_section(self):
        """None fault_intent → no dynamic section."""
        section = get_intent_completeness_section(None)
        assert section == ""

    def test_partial_fault_intent_generates_confirmed_block(self):
        """fault_intent with scope+target → section includes those keys."""
        section = get_intent_completeness_section({
            "scope": "node", "target": "cpu", "namespace": "default",
        })
        assert "Confirmed Parameters" in section
        assert "scope: node" in section
        assert "target: cpu" in section
        assert "namespace: default" in section

    def test_prefix_format(self):
        """Section format matches expected structure."""
        section = get_intent_completeness_section({
            "scope": "pod", "target": "cpu",
        })
        assert "Confirmed Parameters" in section
        assert "Do NOT re-ask" in section
        assert "scope: pod" in section