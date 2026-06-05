"""Tests for agent graph construction."""

from langgraph.graph import StateGraph

from chaos_agent.agent.graph import (
    build_inject_graph,
    build_intent_graph,
    build_pipeline_graph,
    build_recover_graph,
)


class TestBuildInjectGraph:
    """Tests for build_inject_graph (legacy, kept for compatibility)."""

    def test_returns_state_graph(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        assert isinstance(graph, StateGraph)

    def test_compiled_graph_runnable(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        compiled = graph.compile()
        assert compiled is not None


class TestBuildIntentGraph:
    """Tests for build_intent_graph."""

    def test_returns_state_graph(self):
        graph = build_intent_graph()
        assert isinstance(graph, StateGraph)

    def test_contains_expected_nodes(self):
        graph = build_intent_graph()
        node_names = set(graph.nodes.keys())
        expected = {
            "load_memory",
            "intent_clarification",
            "intent_confirm",
            "recover_handler",
            "save_dialogue",
        }
        assert expected == node_names

    def test_compiled_graph_runnable(self):
        graph = build_intent_graph()
        compiled = graph.compile()
        assert compiled is not None


class TestBuildPipelineGraph:
    """Tests for build_pipeline_graph."""

    def test_returns_state_graph(self):
        graph = build_pipeline_graph(phase1_tools=[], phase2_tools=[])
        assert isinstance(graph, StateGraph)

    def test_contains_all_expected_nodes(self):
        graph = build_pipeline_graph(phase1_tools=[], phase2_tools=[])
        node_names = set(graph.nodes.keys())
        expected = {
            "pipeline_init",
            "plan_builder",
            "batch_setup",
            "batch_next",
            "agent_loop",
            "direct_setup",
            "baseline_capture",
            "se_snapshot",
            "phase1_tools",
            "phase1_screener",
            "safety_check",
            "confirmation_gate",
            "extract_planning_metadata",
            "execute_loop",
            "direct_execute",
            "tool_screener",
            "phase2_tools",
            "verifier_loop",
            "finalize_verification",
            "se_detect",
            "save_memory",
            "reject",
            "plan_change_confirm",
        }
        assert expected == node_names

    def test_compiled_graph_runnable(self):
        graph = build_pipeline_graph(phase1_tools=[], phase2_tools=[])
        compiled = graph.compile()
        assert compiled is not None

    def test_pipeline_graph_with_tools(self):
        """Pipeline graph should accept non-empty tool lists."""
        from langchain_core.tools import tool as lc_tool

        @lc_tool
        def mock_tool1() -> str:
            """A mock tool for testing."""
            return "mock1"

        @lc_tool
        def mock_tool2() -> str:
            """A mock tool for testing."""
            return "mock2"

        graph = build_pipeline_graph(phase1_tools=[mock_tool1], phase2_tools=[mock_tool2])
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


class TestPhase1ToolErrorHandler:
    """Tests for the custom phase1 ToolNode error handler (Layer D)."""

    def test_recognizes_unknown_tool_error(self):
        from chaos_agent.agent.graph import _phase1_handle_tool_error
        err = ValueError(
            "Error: blade_create is not a valid tool, "
            "try one of [activate_skill, kubectl, ...]."
        )
        msg = _phase1_handle_tool_error(err)
        assert "blade_create" in msg
        assert "Phase 1" in msg
        assert "final summary text" in msg.lower()
        assert "try one of" not in msg.lower()
        assert "[activate_skill" not in msg

    def test_recognizes_invocation_error(self):
        from chaos_agent.agent.graph import _phase1_handle_tool_error
        err = ValueError(
            "Error invoking tool 'kubectl_ro' with kwargs "
            "{'subcommand': 'exec'} with error:\n "
            "1 validation error for kubectl_ro\nsubcommand\n  "
            "Input should be 'get', 'describe', ..."
        )
        msg = _phase1_handle_tool_error(err)
        assert "kubectl_ro" in msg
        assert "Phase 1" in msg

    def test_recognizes_execution_error(self):
        from chaos_agent.agent.graph import _phase1_handle_tool_error
        err = ValueError(
            "Error executing tool 'some_tool' with kwargs {} with "
            "error:\n RuntimeError: oops"
        )
        msg = _phase1_handle_tool_error(err)
        assert "some_tool" in msg

    def test_unrecognized_error_falls_back_safely(self):
        from chaos_agent.agent.graph import _phase1_handle_tool_error
        err = RuntimeError("totally unrelated error format")
        msg = _phase1_handle_tool_error(err)
        assert "<unknown>" in msg
        assert "Phase 1" in msg
        assert "try one of" not in msg.lower()
