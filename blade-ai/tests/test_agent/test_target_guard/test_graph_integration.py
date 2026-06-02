"""Graph-level integration tests for the target_guard screener.

Unit tests in test_screener.py exercise the node in isolation; these
tests verify it is correctly wired into the inject graph and that the
edges go where the spec says they should.

Scope:
  - tool_screener appears between execute_loop and phase2_tools
  - the screener has three outbound edges: pass / replan / retry
  - the graph compiles cleanly with the new node + edges
"""

from __future__ import annotations

from chaos_agent.agent.graph import build_inject_graph


class TestScreenerWiring:
    def test_tool_screener_node_present(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        assert "tool_screener" in graph.nodes

    def test_graph_compiles(self):
        # Catches mismatches between conditional-edge targets and
        # declared nodes — LangGraph rejects on compile when an edge
        # references an unknown node.
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        compiled = graph.compile()
        assert compiled is not None

    def test_execute_loop_routes_continue_to_screener_not_tools(self):
        # The "continue" branch from execute_loop must route via
        # tool_screener now, not straight to phase2_tools. If anyone
        # restores the old direct edge by accident, the screener
        # becomes dead code and the drift bug returns.
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        # Inspect the graph's adjacency. LangGraph exposes branches
        # via graph.branches and edges via graph.edges; conditional
        # edges land in branches keyed by source node.
        branches = graph.branches.get("execute_loop") or {}
        assert branches, "execute_loop must have conditional edges"
        # All conditional-edge target maps for execute_loop must map
        # "continue" to "tool_screener".
        for branch in branches.values():
            ends = getattr(branch, "ends", None) or {}
            if "continue" in ends:
                assert ends["continue"] == "tool_screener", (
                    f"execute_loop['continue'] must route to tool_screener, "
                    f"got {ends['continue']}"
                )

    def test_screener_has_three_outbound_routes(self):
        graph = build_inject_graph(phase1_tools=[], phase2_tools=[])
        branches = graph.branches.get("tool_screener") or {}
        assert branches, "tool_screener must have conditional edges"
        for branch in branches.values():
            ends = getattr(branch, "ends", None) or {}
            assert set(ends.keys()) == {"pass", "replan", "retry"}, (
                f"screener must route exactly pass/replan/retry, got {set(ends.keys())}"
            )
            # And those routes must point to known successor nodes.
            assert ends["pass"] == "phase2_tools"
            assert ends["replan"] == "agent_loop"
            assert ends["retry"] == "execute_loop"
