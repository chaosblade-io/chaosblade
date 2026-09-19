"""Tests for agent factory: create_agent and _build_skill_tools."""

import pytest

from chaos_agent.agent.factory import _build_skill_tools, create_agent


class TestBuildSkillTools:
    """Tests for _build_skill_tools."""

    def test_returns_eight_tools(self, mock_registry):
        tools = _build_skill_tools(mock_registry)
        assert len(tools) == 8

    def test_tool_names(self, mock_registry):
        tools = _build_skill_tools(mock_registry)
        tool_names = [t.name for t in tools]
        assert "activate_skill" in tool_names
        assert "read_skill_resource" in tool_names
        assert "read_file" in tool_names
        assert "write_file" in tool_names
        assert "save_fault_plan" in tool_names
        assert "finish_planning" in tool_names
        assert "execute_skill_script" in tool_names
        # search_files was removed in P2-1: it was registered but never bound
        # to any phase, so it served no purpose.
        assert "search_files" not in tool_names

    def test_activate_skill_calls_registry(self, mock_registry):
        tools = _build_skill_tools(mock_registry)
        activate_tool = next(t for t in tools if t.name == "activate_skill")

        result = activate_tool.invoke({"skill_name": "test-skill"})
        assert result is not None

    def test_read_skill_resource_calls_registry(self, mock_registry):
        tools = _build_skill_tools(mock_registry)
        read_tool = next(t for t in tools if t.name == "read_skill_resource")

        # First activate the skill so resources are available
        activate_tool = next(t for t in tools if t.name == "activate_skill")
        activate_tool.invoke({"skill_name": "test-skill"})

        result = read_tool.invoke({
            "skill_name": "test-skill",
            "resource_path": "scripts/verify.py",
        })
        assert result is not None

    def test_activate_skill_has_description(self, mock_registry):
        """activate_skill tool should have a docstring/description."""
        tools = _build_skill_tools(mock_registry)
        activate_tool = next(t for t in tools if t.name == "activate_skill")
        assert activate_tool.description is not None
        assert len(activate_tool.description) > 0

    def test_activate_skill_catalog_placeholder_resolved(self, mock_registry):
        """activate_skill description must not contain literal placeholders.

        Skill names are no longer frozen into the description — the LLM
        reads them from the dynamic Skill Index in the system prompt.
        """
        tools = _build_skill_tools(mock_registry)
        activate_tool = next(t for t in tools if t.name == "activate_skill")
        assert "{skill_names_str}" not in activate_tool.description
        assert "{catalog}" not in activate_tool.description
        assert "available" in activate_tool.description.lower()

    def test_execute_skill_script_no_frozen_placeholder(self, mock_registry):
        """execute_skill_script description must not contain frozen placeholders."""
        tools = _build_skill_tools(mock_registry)
        execute_tool = next(t for t in tools if t.name == "execute_skill_script")
        assert "{_scripts_catalog}" not in execute_tool.description

    def test_save_fault_plan_docstring_matches_router_semantics(self, mock_registry):
        """save_fault_plan docstring must teach the router's actual exit rule.

        route_after_phase1_tools treats finish_planning (or
        propose_plan_change) as the ONLY Phase 1 exit signals — a saved
        plan alone keeps the loop going. The legacy "your next message
        should be final summary text WITHOUT tool_calls — the system
        advances to Phase 2" wording described a transition that never
        happens on save and steered the model into a text-only stall the
        loop hint then has to bounce.
        """
        tools = _build_skill_tools(mock_registry)
        save_tool = next(t for t in tools if t.name == "save_fault_plan")
        assert "persists the draft" in save_tool.description
        assert "finish_planning" in save_tool.description
        assert "advances to Phase 2" not in save_tool.description

    def test_finish_planning_docstring_claims_no_floor_raise(self, mock_registry):
        """finish_planning duration teaching must not claim floor-raising.

        inject-aac02265 (#35 round audit): the docstring taught "values
        below the safety floor (600s) are raised to the floor" — false on
        both counts (ensure_min_duration honours declared values
        verbatim; the floor is 300s and applies only to the unspecified
        default path). The purge directive: no LLM-facing surface may
        teach raise-the-floor semantics.
        """
        tools = _build_skill_tools(mock_registry)
        finish_tool = next(t for t in tools if t.name == "finish_planning")
        assert "raised to the floor" not in finish_tool.description
        assert "safety floor" not in finish_tool.description
        assert "Default 0 = not declared" in finish_tool.description

    def test_finish_planning_declares_fault_identity_triple(self, mock_registry):
        """B83/B84 (#49 post-mortem): finish_planning carries the fault-identity
        declaration surface.

        The planner is the only actor that knows which mechanism the plan
        chose; the declaration (fault_scope / fault_target / fault_action)
        hands that knowledge to the spec-resolution node, which never
        rewrites a reviewed identity from it — a conflict is routed back as
        a split with propose_plan_change as the revision exit.
        """
        tools = _build_skill_tools(mock_registry)
        finish_tool = next(t for t in tools if t.name == "finish_planning")
        assert {"fault_scope", "fault_target", "fault_action"} <= set(finish_tool.args)
        assert "MAIN injection mechanism" in finish_tool.description
        assert "propose_plan_change" in finish_tool.description


class TestCreateAgent:
    """Tests for create_agent.

    Uses checkpointer=False to avoid needing a real AsyncSqliteSaver,
    since LangGraph validates checkpointer type strictly.
    """

    @pytest.mark.asyncio
    async def test_returns_dict_with_graphs(self, mock_registry):
        result = await create_agent(
            registry=mock_registry,
            checkpointer=False,
        )

        assert "pipeline" in result
        assert "recover" in result
        assert "checkpointer" in result

    @pytest.mark.asyncio
    async def test_inject_graph_compiled(self, mock_registry):
        result = await create_agent(
            registry=mock_registry,
            checkpointer=False,
        )

        inject = result["pipeline"]
        assert hasattr(inject, "ainvoke")

    @pytest.mark.asyncio
    async def test_recover_graph_compiled(self, mock_registry):
        result = await create_agent(
            registry=mock_registry,
            checkpointer=False,
        )

        recover = result["recover"]
        assert hasattr(recover, "ainvoke")

    @pytest.mark.asyncio
    async def test_checkpointer_is_false_when_no_checkpointer(self, mock_registry):
        result = await create_agent(
            registry=mock_registry,
            checkpointer=False,
        )

        assert result["checkpointer"] is False

    @pytest.mark.asyncio
    async def test_none_checkpointer_graceful(self, mock_registry):
        """When checkpointer is None, should handle gracefully (fallback)."""
        result = await create_agent(
            registry=mock_registry,
            checkpointer=None,
        )
        # Should return a dict with both graphs regardless
        assert "pipeline" in result
        assert "recover" in result

        # Close aiosqlite connection to prevent ResourceWarning
        conn = result.get("checkpointer_conn")
        if conn is not None:
            await conn.close()

    @pytest.mark.asyncio
    async def test_skill_tools_built(self, mock_registry):
        result = await create_agent(
            registry=mock_registry,
            checkpointer=False,
        )

        assert result["pipeline"] is not None
        assert result["recover"] is not None


class TestPhaseToolSurface:
    """P1-1: phase tool sets must be tightly scoped.

    Phase 1 (planning) and Phase 2 (execution) should bind only the tools
    appropriate to their role. The bound LLM exposes its tool schema via
    `kwargs.tools`; we read that schema (rather than the source-level list)
    to verify what the model will actually see at inference time.
    """

    def _bound_tool_names(self, llm_or_tools) -> set[str]:
        if hasattr(llm_or_tools, "kwargs"):
            return {t["function"]["name"] for t in llm_or_tools.kwargs.get("tools", [])}
        return {getattr(t, "name", "") for t in llm_or_tools}

    @pytest.mark.asyncio
    async def test_phase1_tools_exclude_write_search_runscript(self, mock_registry):
        """Phase 1 must NOT bind write_file / search_files / execute_skill_script."""
        # Build the same lists factory would, but inspect them directly.
        from chaos_agent.agent.factory import _build_skill_tools
        from chaos_agent.agent.providers.chaosblade.cli import (
            blade_create, blade_destroy, blade_status,
        )
        from chaos_agent.tools import kubectl, read_knowledge_resource

        skill_tools = _build_skill_tools(mock_registry)
        by_name = {t.name: t for t in skill_tools}
        phase1 = [
            by_name["activate_skill"],
            by_name["read_skill_resource"],
            by_name["read_file"],
            by_name["save_fault_plan"],
            blade_create, blade_status, blade_destroy,
            kubectl, read_knowledge_resource,
        ]
        names = {t.name for t in phase1}

        # Removed
        assert "write_file" not in names
        assert "search_files" not in names
        assert "execute_skill_script" not in names

        # Required for planning
        for required in ("activate_skill", "read_skill_resource", "blade_create",
                         "blade_status", "blade_destroy", "kubectl", "save_fault_plan"):
            assert required in names, f"phase1 missing {required}"

    def test_phase2_tools_include_guarded_destroy_and_exclude_skill_resource(self, mock_registry):
        """Phase 2 exposes guarded cleanup but not planning-only tools."""
        from chaos_agent.agent.factory import _build_skill_tools
        from chaos_agent.agent.providers.chaosblade.cli import (
            blade_create, blade_destroy, blade_status, blade_query_k8s,
        )
        from chaos_agent.tools import kubectl, read_knowledge_resource

        skill_tools = _build_skill_tools(mock_registry)
        by_name = {t.name: t for t in skill_tools}
        phase2 = [
            blade_create, blade_destroy, blade_status, blade_query_k8s,
            kubectl, by_name["execute_skill_script"], read_knowledge_resource,
        ]
        names = {t.name for t in phase2}

        # Removed
        assert "activate_skill" not in names
        assert "read_skill_resource" not in names

        # Required for execution
        for required in ("blade_create", "blade_destroy", "blade_status", "kubectl", "execute_skill_script"):
            assert required in names, f"phase2 missing {required}"

    def test_phase1_prompt_omits_disallowed_tools(self):
        """Phase 1 system prompt must not advertise removed tools AS USABLE.

        ``write_file`` and ``execute_skill_script`` are intentionally listed
        in the workflow's "NOT available in Phase 1" section so the LLM
        knows which tools it'll find in Phase 2 (the listing reduces test-
        and-fail tool calls — see Layer C of the phase 1 readonly plan).
        We assert the listings appear ONLY in the not-available context,
        not as positive recommendations.
        """
        from chaos_agent.agent.prompts import build_inject_system_prompt

        prompt = build_inject_system_prompt(skill_catalog="(none)")

        # write_file: only allowed mention is the "Do NOT write files"
        # advisory or the "NOT available" section listing
        assert (
            "write_file" not in prompt
            or "Do NOT write files" in prompt
            or "NOT available in Phase 1" in prompt
        )
        # search_files is fully dead (no binding anywhere), should be
        # completely absent
        assert "search_files" not in prompt
        # execute_skill_script: must not appear as an available tool.
        # It may appear in the NOT-available listing or not at all —
        # both are safe since runtime rejects it regardless.
        assert "execute_skill_script" not in prompt or "NOT available" in prompt or "not available" in prompt.lower()

    @pytest.mark.asyncio
    async def test_no_phase_binds_dead_tools(self, mock_registry):
        """Dead tools (web_search, search_files) must not be bound to any phase.

        Both modules still exist on disk (their tests still import them) but
        the agent factory must not surface them via any LLM binding —
        otherwise the LLM may try to call a tool the runtime rejects.
        """
        from chaos_agent.agent.factory import create_agent

        result = await create_agent(registry=mock_registry, checkpointer=False)

        dead_names = {"web_search", "search_files"}
        for graph_key in ("pipeline", "recover"):
            graph = result[graph_key]
            assert graph is not None
            # Walk the compiled graph's nodes and inspect any LLM with bound
            # tool schemas. If a node doesn't expose `kwargs.tools`, skip it.
            for node in getattr(graph, "nodes", {}).values():
                runnable = getattr(node, "runnable", node)
                kwargs = getattr(runnable, "kwargs", None)
                if not isinstance(kwargs, dict):
                    continue
                bound = {
                    t.get("function", {}).get("name", "")
                    for t in kwargs.get("tools", [])
                }
                leaked = bound & dead_names
                assert not leaked, (
                    f"graph={graph_key!r} node leaks dead tool(s): {leaked}"
                )

    def test_phase2_prompt_omits_disallowed_tools(self):
        """Phase 2 system prompt must not actively promote tools that aren't bound.

        The prompt uses a general tool constraint ("Only call tools that are
        bound to you"). We only forbid PROMOTION patterns ("Use blade_destroy ...").
        """
        from chaos_agent.agent.prompts import build_execute_system_prompt

        prompt = build_execute_system_prompt(skill_catalog="(none)")

        # No active recommendation to call these tools.
        forbidden_promotions = [
            "Use `read_skill_resource`",
            "Use read_skill_resource",
            "call `read_skill_resource`",
            "Use `activate_skill`",
            "Use activate_skill",
            "call `activate_skill`",
        ]
        for phrase in forbidden_promotions:
            assert phrase not in prompt, (
                f"Phase 2 prompt actively promotes a tool that is not bound: {phrase!r}"
            )

        # Phase 2 must still carry partial-failure cleanup guidance
        # (wording evolved away from naming blade_create/UID explicitly).
        assert "residue before switching methods" in prompt


class TestAppendProviderTools:
    """The build-time provider-tool union seam (_append_provider_tools).

    This is the mechanism that lets a new execution backend surface its LLM
    tools by registering a FaultProvider instead of editing the factory's five
    static tool lists.
    """

    class _Named:
        def __init__(self, name):
            self.name = name

    class _FakeToolProvider:
        carrier = "fake_carrier"
        injection_methods = ("fake_method",)

        def __init__(self, phase, tools):
            self._phase = phase
            self._tools = tools

        def tools(self, phase):
            return list(self._tools) if phase == self._phase else []

    def test_builtin_host_provider_surfaces_host_tools_on_execute(self):
        # The built-in HostShellProvider contributes host_inject to the EXECUTE
        # phase via the union seam (no factory edit). host_inject is the
        # superset of host_read, so no separate read tool is bound.
        from chaos_agent.agent.factory import _append_provider_tools
        from chaos_agent.agent.providers import EXECUTE

        base = [self._Named("blade_create"), self._Named("kubectl")]
        out = _append_provider_tools(base, EXECUTE)
        names = [t.name for t in out]
        assert names[:2] == ["blade_create", "kubectl"]
        assert "host_inject" in names
        assert "host_read" not in names

    def test_builtin_host_provider_contributes_only_diagnostics_on_plan(self):
        # Host planning discovers the current environment through host_read,
        # but must never surface the mutating host_inject tool.
        from chaos_agent.agent.factory import _append_provider_tools
        from chaos_agent.agent.providers import PLAN

        base = [self._Named("blade_create"), self._Named("kubectl")]
        out = _append_provider_tools(base, PLAN)
        names = [t.name for t in out]
        # Base preserved verbatim at the front.
        assert names[:2] == ["blade_create", "kubectl"]
        assert "host_inject" not in names
        assert "host_read" in names
        # No full-kubectl / blade_create surfaces beyond the caller-supplied
        # base (planning stays read-only).
        assert names.count("kubectl") == 1
        assert names.count("blade_create") == 1

    def test_unions_provider_tools_and_dedups_by_name(self):
        from chaos_agent.agent.factory import _append_provider_tools
        from chaos_agent.agent.providers import EXECUTE, PLAN, FaultProviderRegistry

        prov = self._FakeToolProvider(
            EXECUTE, [self._Named("host_inject"), self._Named("kubectl")]
        )
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register(prov)
        try:
            base = [self._Named("kubectl")]
            out = _append_provider_tools(base, EXECUTE)
            # kubectl is NOT double-bound; host_inject is appended.
            assert [t.name for t in out] == ["kubectl", "host_inject"]
            # A provider contributes only to its declared phase.
            assert [t.name for t in _append_provider_tools(base, PLAN)] == ["kubectl"]
        finally:
            # Phase-8 registry-state hygiene: teardown RESTORES the builtins
            # (clear-only leaked an empty registry into later tests —
            # registry-dispatched consumers like the step self-check would
            # silently resolve nothing).
            FaultProviderRegistry.clear()
            FaultProviderRegistry.register_builtins()


class TestPhaseToolUnionGuard:
    """Behaviour-preservation guard for the factory → provider tool migration.

    The backend execution tools (blade_* / kubectl / kubectl_read)
    moved out of factory.py's four static lists and into each
    FaultProvider's ``tools(phase)``. This guard reconstructs each phase's tool
    list exactly as ``create_agent`` now does (post-migration static base +
    provider union) and asserts the resulting tool *set* equals the documented
    pre-migration set — so a dropped or double-bound tool fails loudly.
    """

    # Pre-migration bound-tool name sets, per phase (source of truth: the
    # factory static lists + host union, as they stood before this change).
    _PLAN = {
        "activate_skill", "read_skill_resource", "read_file", "save_fault_plan",
        "finish_planning", "propose_plan_change",
        "read_knowledge_resource",
        "blade_help", "blade_status", "kubectl_read", "host_read",
    }
    _EXECUTE = {
        "execute_skill_script", "read_knowledge_resource", "time_wait",
        "blade_create", "blade_destroy", "blade_help", "blade_status",
        "blade_query_k8s", "kubectl", "host_inject",
        # chaosblade_python provider (EXECUTE phase)
        "blade_python_create", "blade_python_prepare", "blade_python_revoke",
    }
    _VERIFY = {
        "read_skill_resource", "execute_skill_script", "read_knowledge_resource",
        "submit_verification", "time_wait",
        "kubectl_read", "host_read",
    }
    _RECOVER_VERIFY = {
        "read_skill_resource", "execute_skill_script", "read_knowledge_resource",
        "submit_recover_verification", "time_wait",
        "kubectl", "host_inject",
    }

    def _build_phase_sets(self, mock_registry):
        """Reconstruct the four phase tool lists the way the factory does."""
        from chaos_agent.agent.factory import _append_provider_tools, _build_skill_tools
        from chaos_agent.agent.providers import (
            EXECUTE, PLAN, RECOVER_VERIFY, VERIFY, FaultProviderRegistry,
        )
        from chaos_agent.agent.nodes.verify._verifier_submit import (
            submit_recover_verification, submit_verification,
        )
        from chaos_agent.tools import read_knowledge_resource
        from chaos_agent.tools.wait import time_wait

        FaultProviderRegistry.clear()  # force self-bootstrap of built-ins
        try:
            skill = {t.name: t for t in _build_skill_tools(mock_registry)}

            phase1 = [
                skill["activate_skill"], skill["read_skill_resource"],
                skill["read_file"], skill["save_fault_plan"],
                skill["finish_planning"], skill["propose_plan_change"],
                read_knowledge_resource,
            ]
            phase1 = _append_provider_tools(phase1, PLAN)

            phase2 = [
                skill["execute_skill_script"], read_knowledge_resource, time_wait,
            ]
            phase2 = _append_provider_tools(phase2, EXECUTE)

            verifier = [
                skill["read_skill_resource"], skill["execute_skill_script"],
                read_knowledge_resource, submit_verification, time_wait,
            ]
            verifier = _append_provider_tools(verifier, VERIFY)

            recover_verifier = [
                skill["read_skill_resource"], skill["execute_skill_script"],
                read_knowledge_resource, submit_recover_verification, time_wait,
            ]
            recover_verifier = _append_provider_tools(recover_verifier, RECOVER_VERIFY)

            return {
                "plan": [t.name for t in phase1],
                "execute": [t.name for t in phase2],
                "verify": [t.name for t in verifier],
                "recover_verify": [t.name for t in recover_verifier],
            }
        finally:
            # Phase-8 registry-state hygiene: teardown RESTORES the builtins
            # (clear-only leaked an empty registry into later tests).
            FaultProviderRegistry.clear()
            FaultProviderRegistry.register_builtins()

    def test_each_phase_binds_expected_tool_set(self, mock_registry):
        sets = self._build_phase_sets(mock_registry)
        assert set(sets["plan"]) == self._PLAN
        assert set(sets["execute"]) == self._EXECUTE
        assert set(sets["verify"]) == self._VERIFY
        assert set(sets["recover_verify"]) == self._RECOVER_VERIFY

    def test_no_tool_double_bound_within_a_phase(self, mock_registry):
        sets = self._build_phase_sets(mock_registry)
        for phase, names in sets.items():
            assert len(names) == len(set(names)), (
                f"phase {phase!r} double-binds a tool: {names}"
            )


class TestPhaseSpecMatrix:
    """Declarative phase → tool-surface matrix (PhaseSpec consolidation).

    The five per-phase tool lists previously assembled inline in
    ``create_agent`` were consolidated into the ``_phase_specs``
    declaration table + ``_assemble_phase_tools`` loop. These tests pin
    the consolidated surface to the pre-consolidation baseline (captured
    with mcp_manager=None) and pin the cross-file safety invariants the
    table now makes assertable.
    """

    # Baseline captured from the pre-consolidation per-phase blocks
    # (mcp_manager=None): member names in exact order — static base
    # first, then the provider union in registry order.
    _BASELINE = {
        "clarification": [
            "activate_skill", "read_skill_resource", "read_knowledge_resource",
            "submit_fault_intent",
            "submit_batch_intent", "query_active_experiments", "recover_task",
            # Intent-time fact recording (tier1-speedup): update_progress lets
            # the model log probe-established facts during clarification; the
            # ledger re-injection chain carries them to the planner.
            "update_progress",
            "blade_help", "blade_status", "kubectl_read", "host_read",
        ],
        "phase1": [
            "activate_skill", "read_skill_resource", "read_file",
            "save_fault_plan", "finish_planning", "propose_plan_change",
            "read_knowledge_resource", "update_progress",
            "blade_help", "blade_status", "kubectl_read", "host_read",
        ],
        "phase2": [
            "execute_skill_script", "read_knowledge_resource", "time_wait",
            "request_replan", "update_progress",
            # #39 tail-tension root fix: the clean terminal exit for a
            # finished execution (see test_finish_execution.py).
            "finish_execution",
            "blade_create", "blade_destroy", "blade_help", "blade_status",
            "blade_query_k8s", "kubectl", "host_inject",
            "blade_python_create", "blade_python_prepare", "blade_python_revoke",
        ],
        "verifier": [
            "read_skill_resource", "execute_skill_script",
            "read_knowledge_resource", "submit_verification", "time_wait",
            "update_progress", "kubectl_read", "host_read",
        ],
        "recover_verifier": [
            "read_skill_resource", "execute_skill_script",
            "read_knowledge_resource", "submit_recover_verification",
            "time_wait", "update_progress", "kubectl", "host_inject",
        ],
    }

    class _Named:
        def __init__(self, name):
            self.name = name

    class _FakeToolProvider:
        carrier = "fake_carrier"
        injection_methods = ("fake_method",)

        def __init__(self, phase, tools):
            self._phase = phase
            self._tools = tools

        def tools(self, phase):
            return list(self._tools) if phase == self._phase else []

    class _FakeMcpManager:
        def __init__(self, mapping):
            self._mapping = mapping

        def tools_for_phase(self, phase):
            return list(self._mapping.get(phase, []))

    @pytest.fixture()
    def assembled(self, mock_registry):
        from chaos_agent.agent.factory import _assemble_phase_tools, _build_skill_tools

        skill_tools = _build_skill_tools(mock_registry)
        return _assemble_phase_tools(skill_tools)

    @staticmethod
    def _names(tools):
        return [getattr(t, "name", None) for t in tools]

    # ── 3.1 member + order baseline ─────────────────────────────────

    def test_assembled_phases_match_baseline(self, assembled):
        """Member names AND order per phase equal the pre-consolidation baseline."""
        assert set(assembled) == set(self._BASELINE)
        for phase, expected in self._BASELINE.items():
            assert self._names(assembled[phase]) == expected, (
                f"phase {phase!r} diverged from the pre-consolidation "
                f"baseline:\n  got      {self._names(assembled[phase])}\n"
                f"  expected {expected}"
            )

    # ── 3.2 vocabulary mapping (table = bridge across the three
    #    vocabularies: phase name ↔ MCP attach_to string ↔ provider
    #    phase constant) ──────────────────────────────────────────

    def test_phase_specs_vocabulary_mapping(self, mock_registry):
        from chaos_agent.agent.factory import _build_skill_tools, _phase_specs
        from chaos_agent.agent.providers import (
            EXECUTE, PLAN, RECOVER_VERIFY, VERIFY,
        )

        skill_tools = _build_skill_tools(mock_registry)
        specs = {s.name: s for s in _phase_specs(skill_tools)}
        assert set(specs) == {
            "clarification", "phase1", "phase2", "verifier", "recover_verifier",
        }
        assert specs["clarification"].provider_phase == PLAN
        assert specs["phase1"].provider_phase == PLAN
        assert specs["phase2"].provider_phase == EXECUTE
        assert specs["verifier"].provider_phase == VERIFY
        assert specs["recover_verifier"].provider_phase == RECOVER_VERIFY
        # MCP attach_to vocabulary: each phase attaches under its own name,
        # except recover_verifier, which shares the inject verifier's
        # "verifier" attach point.
        for name in ("clarification", "phase1", "phase2", "verifier"):
            assert specs[name].mcp_attach == name
        assert specs["recover_verifier"].mcp_attach == "verifier"

    # ── 3.3 safety invariants (provider side + assembled side) ──────

    def test_builtin_plan_contribution_excludes_injection_tools(self):
        """Built-in providers contribute no mutating tool on the PLAN/VERIFY side."""
        from chaos_agent.agent.providers import (
            PLAN, VERIFY, FaultProviderRegistry,
        )

        FaultProviderRegistry.clear()
        try:
            if not FaultProviderRegistry.all_providers():
                FaultProviderRegistry.register_builtins()
            for provider in FaultProviderRegistry.all_providers():
                carrier = getattr(provider, "carrier", provider)
                for phase in (PLAN, VERIFY):
                    contributed = [
                        getattr(t, "name", None) for t in provider.tools(phase)
                    ]
                    for tool in (
                        "blade_create", "blade_destroy", "kubectl", "host_inject",
                    ):
                        assert tool not in contributed, (
                            f"provider {carrier!r} leaks {tool!r} into the "
                            f"{phase!r} provider phase"
                        )
        finally:
            # Registry-state hygiene: teardown RESTORES the builtins.
            FaultProviderRegistry.clear()
            FaultProviderRegistry.register_builtins()

    def test_injection_tools_absent_from_readonly_phases(self, assembled):
        """Read-only phases must not bind any mutating/exec-class tool."""
        for phase in ("clarification", "phase1", "verifier"):
            names = set(self._names(assembled[phase]))
            for tool in (
                "blade_create", "blade_destroy", "kubectl", "host_inject",
            ):
                assert tool not in names, (
                    f"{tool!r} surfaced in read-only phase {phase!r}"
                )

    def test_full_kubectl_and_host_inject_only_in_execute_phases(self, assembled):
        """Full kubectl / host_inject surface on EXECUTE/RECOVER_VERIFY only."""
        for phase, tools in assembled.items():
            names = set(self._names(tools))
            for tool in ("kubectl", "host_inject"):
                if tool in names:
                    assert phase in ("phase2", "recover_verifier"), (
                        f"{tool!r} surfaced in phase {phase!r} (only the "
                        "EXECUTE/RECOVER_VERIFY phases may bind it)"
                    )
        # Positive side: the execute-side phases DO bind the reverse-op
        # carriers (recovery must be able to run the reverse op).
        for phase in ("phase2", "recover_verifier"):
            names = set(self._names(assembled[phase]))
            assert "kubectl" in names, f"phase {phase!r} lost full kubectl"
            assert "host_inject" in names, f"phase {phase!r} lost host_inject"

    # ── 3.4 dedup behaviour through the consolidated loop ───────────

    def test_provider_duplicate_of_static_base_not_double_bound(self, mock_registry):
        """A provider re-declaring a static-base tool name binds it once."""
        from chaos_agent.agent.factory import _assemble_phase_tools, _build_skill_tools
        from chaos_agent.agent.providers import EXECUTE, FaultProviderRegistry

        dup = self._FakeToolProvider(
            EXECUTE,
            [self._Named("execute_skill_script"), self._Named("brand_new_tool")],
        )
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register(dup)
        try:
            skill_tools = _build_skill_tools(mock_registry)
            out = _assemble_phase_tools(skill_tools)
            phase2 = self._names(out["phase2"])
            assert phase2.count("execute_skill_script") == 1, (
                f"phase2 double-binds execute_skill_script: {phase2}"
            )
            # The provider's novel tool still lands on its declared phase...
            assert "brand_new_tool" in phase2
            # ...and only there.
            assert "brand_new_tool" not in self._names(out["phase1"])
        finally:
            # Registry-state hygiene: teardown RESTORES the builtins.
            FaultProviderRegistry.clear()
            FaultProviderRegistry.register_builtins()

    def test_mcp_attach_appends_and_recover_verifier_shares_verifier(self, mock_registry):
        """MCP tools land on their attach_to phase; recover_verifier shares "verifier"."""
        from chaos_agent.agent.factory import _assemble_phase_tools, _build_skill_tools

        mapping = {
            "clarification": [self._Named("mcp_clarify")],
            "phase1": [self._Named("mcp_plan")],
            "phase2": [self._Named("mcp_exec")],
            "verifier": [self._Named("mcp_verify")],
        }
        skill_tools = _build_skill_tools(mock_registry)
        out = _assemble_phase_tools(skill_tools, self._FakeMcpManager(mapping))
        names = {ph: self._names(tools) for ph, tools in out.items()}
        assert "mcp_clarify" in names["clarification"]
        assert "mcp_plan" in names["phase1"]
        assert "mcp_exec" in names["phase2"]
        # recover_verifier shares the inject verifier's MCP attach_to.
        assert "mcp_verify" in names["verifier"]
        assert "mcp_verify" in names["recover_verifier"]
        # No cross-leak: each MCP tool lands only on its attach_to phases.
        assert "mcp_clarify" not in names["phase1"]
        assert "mcp_plan" not in names["phase2"]

    def test_mcp_duplicate_of_provider_tool_not_double_bound(self, mock_registry):
        """Unified order (static → MCP → provider): MCP name collisions dedup.

        The pre-consolidation clarification phase appended MCP tools
        verbatim AFTER the provider union and could double-bind a
        colliding name; the consolidated loop runs the MCP attach before
        the provider union, so ``_append_provider_tools`` dedups the
        provider's copy against the MCP-contributed name.
        """
        from chaos_agent.agent.factory import _assemble_phase_tools, _build_skill_tools

        # host_read is contributed by the PLAN provider union AND by the
        # (fake) MCP manager on the clarification attach point.
        manager = self._FakeMcpManager(
            {"clarification": [self._Named("host_read")]}
        )
        skill_tools = _build_skill_tools(mock_registry)
        out = _assemble_phase_tools(skill_tools, manager)
        clarification = self._names(out["clarification"])
        assert clarification.count("host_read") == 1, (
            f"clarification double-binds host_read: {clarification}"
        )
