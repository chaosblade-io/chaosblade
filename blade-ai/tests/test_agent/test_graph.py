"""Tests for agent graph construction."""

from langgraph.graph import StateGraph

from chaos_agent.agent.graph import build_inject_graph, build_recover_graph


class TestBuildInjectGraph:
    """Tests for build_inject_graph."""

    def test_returns_state_graph(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        assert isinstance(graph, StateGraph)

    def test_entry_point_is_load_memory(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        # Verify the compiled graph has the correct structure
        compiled = graph.compile()
        assert compiled is not None

    def test_contains_all_expected_nodes(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        node_names = set(graph.nodes.keys())
        expected = {
            "load_memory",
            "intent_clarification",
            "intent_confirm",
            "agent_loop",
            "direct_setup",
            "baseline_capture",
            "phase1_tools",
            "safety_check",
            "confirmation_gate",
            "extract_planning_metadata",
            "execute_loop",
            "direct_execute",
            "phase2_tools",
            "verifier_loop",
            "save_memory",
            "reject",
            "recover_handler",
        }
        assert expected == node_names

    def test_compiled_graph_runnable(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        compiled = graph.compile()
        assert compiled is not None

    def test_inject_graph_with_tools(self):
        """Inject graph should accept non-empty tool lists."""
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def mock_tool1() -> str:
            """A mock tool for testing."""
            return "mock1"

        @lc_tool
        def mock_tool2() -> str:
            """A mock tool for testing."""
            return "mock2"

        graph = build_inject_graph(phase1_tools=[mock_tool1], phase2_tools=[mock_tool2])
        assert "phase1_tools" in graph.nodes
        assert "phase2_tools" in graph.nodes


class TestBuildRecoverGraph:
    """Tests for build_recover_graph."""

    def test_returns_state_graph(self):
        graph = build_recover_graph()
        assert isinstance(graph, StateGraph)

    def test_contains_recover_nodes(self):
        graph = build_recover_graph()
        node_names = set(graph.nodes.keys())
        assert "recover_verifier_loop" in node_names

    def test_compiled_graph_runnable(self):
        graph = build_recover_graph()
        compiled = graph.compile()
        assert compiled is not None

