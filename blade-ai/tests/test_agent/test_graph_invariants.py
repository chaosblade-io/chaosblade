"""Graph-structure invariant pinning for the three execution graphs.

Implements the ``graph-structure-invariants`` spec: the node sets, safety
guardrail branch keys, terminal funnel and conditional-registration forms
of ``build_recover_graph`` / ``build_intent_graph`` / ``build_pipeline_graph``
are pinned by test-side assertions against LangGraph introspection — the
graph stays hand-built (NodeRegistry table-isation was rejected, see the
change's design.md D2), and its hard-won invariants get a regression net.

Blood-tear anchors mirrored here (see ``src/chaos_agent/agent/graph.py``):
- execute_loop's conditional edges deliberately omit an "end" key, so a
  stray return of "end" raises instead of silently bypassing the verifier.
- finalize_verification must keep a "replan" branch (task-edfed134: the
  missing key surfaced at runtime as a KeyError).
- reject funnels into terminal_reports, the single terminal path
  (task-349ccf5d).
"""

from chaos_agent.agent.graph import (
    build_intent_graph,
    build_pipeline_graph,
    build_recover_graph,
)
from chaos_agent.tools.progress import update_progress

# ---------------------------------------------------------------------------
# Introspection helpers — the ONLY code touching LangGraph internals
# (design D1). Branch-KEY assertions must read ``StateGraph.branches``:
# its BranchSpec carries the COMPLETE ends_map, whereas
# ``get_graph().edges`` merges the ``data`` field when several keys share
# a target and is not a reliable key source. Node-set and topology
# assertions read ``compile().get_graph()``.
# ---------------------------------------------------------------------------


def _nodes_of(graph) -> set[str]:
    """Business node set of a compiled graph (drops ``__start__``/``__end__``)."""
    return {n for n in graph.compile().get_graph().nodes if not n.startswith("__")}


def _direct_edges_of(graph) -> set[tuple[str, str]]:
    """All non-conditional ``(source, target)`` pairs of a compiled graph."""
    return {
        (e.source, e.target)
        for e in graph.compile().get_graph().edges
        if not e.conditional
    }


def _branches_of(graph) -> dict[str, tuple[str, list[str]]]:
    """Map each routed node to ``(route_fn_name, sorted end-map keys)``.

    ``BranchSpec`` is indexed positionally (``[0]`` route fn, ``[1]``
    ends_map); inline lambdas surface as ``"<lambda>"`` and are pinned by
    their key sets, never by name (design D1).
    """
    out: dict[str, tuple[str, list[str]]] = {}
    for node, branch_map in graph.branches.items():
        for _key, spec in branch_map.items():
            fn, ends_map = spec[0], spec[1]
            name = getattr(getattr(fn, "func", fn), "__name__", repr(fn))
            out[node] = (name, sorted(ends_map.keys()))
    return out


# Two-form fixtures use a real @tool singleton (design D4): graph building
# only consumes tool names and the runnable interface, so no mocks needed.
_TOOLS = [update_progress]


def _pipeline_full():
    return build_pipeline_graph(
        phase1_tools=_TOOLS,
        phase2_tools=_TOOLS,
        verifier_tools=_TOOLS,
        clarification_tools=_TOOLS,
    )


# ---------------------------------------------------------------------------
# Baselines (spike-measured 2026-08-21; update deliberately on structural
# change and re-check the blood-tear comments in graph.py stay in sync).
# ---------------------------------------------------------------------------

_PIPELINE_NODES = frozenset(
    {
        "agent_loop",
        "baseline_capture",
        "batch_next",
        "batch_setup",
        "confirmation_gate",
        "execute_loop",
        "extract_planning_metadata",
        "finalize_verification",
        "phase1_screener",
        "phase1_tools",
        "phase2_tools",
        "pipeline_init",
        "plan_builder",
        "plan_builder_screener",
        "plan_builder_tools",
        "plan_change_confirm",
        "planning_handoff",
        "preplan_probe",
        "reject",
        "safety_check",
        "save_memory",
        "se_detect",
        "se_snapshot",
        "terminal_reports",
        "tool_screener",
        "verifier_loop",
        "verifier_screener",
        "verifier_tools",
    }
)

_RECOVER_NODES_NO_TOOLS = frozenset({"finalize_recover_verification", "recover_verifier_loop"})
_RECOVER_NODES_WITH_TOOLS = _RECOVER_NODES_NO_TOOLS | {
    "recover_verifier_screener",
    "recover_verifier_tools",
}

_INTENT_NODES_NO_TOOLS = frozenset(
    {
        "intent_clarification",
        "intent_confirm",
        "load_memory",
        "recover_handler",
        "save_dialogue",
    }
)
_INTENT_NODES_WITH_TOOLS = _INTENT_NODES_NO_TOOLS | {"intent_screener", "clarification_tools"}

# Full key-set baseline of all 17 routed pipeline nodes (design D5: the
# blanket layer — every routing change, blood-tear or not, becomes an
# explicit baseline update).
_PIPELINE_ROUTE_KEYS = {
    "preplan_probe": ["agent_loop", "batch_setup", "plan_builder"],
    "plan_builder": ["__end__", "continue"],
    "plan_builder_screener": ["pass", "retry"],
    "agent_loop": ["continue", "extract_planning_metadata", "reject"],
    "phase1_screener": ["pass", "retry"],
    "phase1_tools": ["agent_loop", "extract_planning_metadata", "plan_change_confirm"],
    "extract_planning_metadata": ["agent_loop", "planning_handoff", "reject"],
    "safety_check": ["agent_loop", "baseline_capture", "confirmation_gate", "reject"],
    # Human veto is a LEGAL "end" exit (design D3): the no-"end" invariant
    # is per-node (execute_loop), never global.
    "confirmation_gate": ["baseline_capture", "end", "reject"],
    "execute_loop": ["continue", "replan", "verifier"],
    # "reject": the W-56-5 hard-termination route (SCREENER_ROUTE_FAIL) —
    # a hard stop ends at the terminal node instead of looping as retry.
    # (keys come back sorted; order below is the sorted form.)
    "tool_screener": ["pass", "reject", "replan", "retry"],
    "verifier_loop": ["continue", "done", "finalize"],
    "verifier_screener": ["pass", "retry"],
    "verifier_tools": ["finalize", "verifier_loop"],
    "finalize_verification": ["replan", "se_detect", "verifier_loop"],
    "save_memory": ["__end__", "batch_next"],
    "batch_next": ["__end__", "batch_setup"],
}


class TestNodeSetBaseline:
    """Requirement: 三图节点全集基线钉扎."""

    def test_pipeline_full_tools_form_node_set(self):
        graph = _pipeline_full()
        assert _nodes_of(graph) == _PIPELINE_NODES

    def test_recover_graph_two_forms(self):
        bare = _nodes_of(build_recover_graph())
        full = _nodes_of(build_recover_graph(verifier_tools=_TOOLS))
        assert bare == _RECOVER_NODES_NO_TOOLS
        assert full == _RECOVER_NODES_WITH_TOOLS
        # The delta is exactly the conditional registration.
        assert full - bare == {"recover_verifier_screener", "recover_verifier_tools"}

    def test_intent_graph_two_forms(self):
        bare = _nodes_of(build_intent_graph())
        full = _nodes_of(build_intent_graph(clarification_tools=_TOOLS))
        assert bare == _INTENT_NODES_NO_TOOLS
        assert full == _INTENT_NODES_WITH_TOOLS
        assert full - bare == {"intent_screener", "clarification_tools"}


class TestSafetyGuardrailBranchKeys:
    """Requirement: 安全护栏分支键集不变量 — the blood-tear subset, pinned with
    semantic assertions layered over the blanket baseline (design D5)."""

    def test_execute_loop_has_no_end_branch(self):
        # graph.py (execute_loop add_conditional_edges): omitting the "end"
        # key makes a stray return "end" raise KeyError rather than silently
        # bypass the verifier — every exit of the ReAct execution loop must
        # funnel into verification.
        _, keys = _branches_of(_pipeline_full())["execute_loop"]
        assert "end" not in keys
        assert keys == ["continue", "replan", "verifier"]

    def test_finalize_verification_keeps_replan_branch(self):
        # task-edfed134: the replan back-edge once went missing and surfaced
        # at runtime as a KeyError — pin the key's presence explicitly.
        _, keys = _branches_of(_pipeline_full())["finalize_verification"]
        assert "replan" in keys
        assert keys == ["replan", "se_detect", "verifier_loop"]


class TestTerminalFunnelInvariants:
    """Requirement: 终端漏斗不变量 — every terminal path funnels into
    terminal_reports (task-349ccf5d)."""

    def test_reject_funnels_into_terminal_reports(self):
        assert ("reject", "terminal_reports") in _direct_edges_of(_pipeline_full())

    def test_se_detect_only_exit_is_terminal_reports(self):
        graph = _pipeline_full()
        targets = {t for (s, t) in _direct_edges_of(graph) if s == "se_detect"}
        assert targets == {"terminal_reports"}


class TestConditionalRegistrationForms:
    """Requirement: 条件注册形态不变量 — tool-truth changes node sets AND can
    swap a route function wholesale; that shape-shifting itself is pinned."""

    def test_intent_clarification_route_fn_swaps_with_form(self):
        bare_fn, _ = _branches_of(build_intent_graph())["intent_clarification"]
        full_fn, _ = _branches_of(build_intent_graph(clarification_tools=_TOOLS))[
            "intent_clarification"
        ]
        assert bare_fn == "route_after_intent_clarification"
        assert full_fn == "should_continue_intent_clarification"

    def test_recover_conditional_nodes_registered_with_tools(self):
        graph = build_recover_graph(verifier_tools=_TOOLS)
        nodes = _nodes_of(graph)
        assert {"recover_verifier_screener", "recover_verifier_tools"} <= nodes
        _, keys = _branches_of(graph)["recover_verifier_loop"]
        assert keys == ["continue", "done", "finalize"]


class TestPlanningHandoffTopology:
    """Requirement: planning handoff 接线不变量 — the context strip sits on
    the single edge every finalized plan crosses (phase-handoff-context-
    stripping). Its position is load-bearing: AFTER extract_planning_metadata
    (which reverse-scans the AIMessage tool_calls the strip removes) and
    BEFORE safety_check (deterministic gates that don't consume the
    stripped context)."""

    def test_planning_handoff_only_exit_is_safety_check(self):
        graph = _pipeline_full()
        targets = {t for (s, t) in _direct_edges_of(graph) if s == "planning_handoff"}
        assert targets == {"safety_check"}

    def test_finalize_route_no_longer_reaches_safety_check_directly(self):
        # The safety_check target moved behind planning_handoff: a finalized
        # plan must cross the strip edge. The route key swap is pinned in the
        # blanket baseline; this asserts the OLD direct wiring is gone.
        _, keys = _branches_of(_pipeline_full())["extract_planning_metadata"]
        assert "safety_check" not in keys
        assert "planning_handoff" in keys


class TestPipelineRouteKeyBaseline:
    """Requirement: 全量路由键集基线 — blanket layer over all 17 routed nodes."""

    def test_all_route_nodes_match_key_baseline(self):
        branches = _branches_of(_pipeline_full())
        assert set(branches.keys()) == set(_PIPELINE_ROUTE_KEYS.keys())
        for node, (_, keys) in branches.items():
            assert keys == _PIPELINE_ROUTE_KEYS[node], node

    def test_lambda_route_node_pinned_by_keys_not_name(self):
        # extract_planning_metadata routes via an inline lambda: the fn name
        # surfaces as "<lambda>" and is NOT an assertion target — the key set
        # carries the guardrail semantics (design D1).
        name, keys = _branches_of(_pipeline_full())["extract_planning_metadata"]
        assert name == "<lambda>"
        assert keys == ["agent_loop", "planning_handoff", "reject"]
