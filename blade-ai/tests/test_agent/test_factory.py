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
        from chaos_agent.tools import (
            blade_create, blade_destroy, blade_status,
            kubectl, read_knowledge_resource,
        )

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

    def test_phase2_tools_exclude_destroy_and_skill_resource(self, mock_registry):
        """Phase 2 must NOT bind blade_destroy / activate_skill / read_skill_resource."""
        from chaos_agent.agent.factory import _build_skill_tools
        from chaos_agent.tools import (
            blade_create, blade_status, blade_query_k8s,
            kubectl, read_knowledge_resource,
        )

        skill_tools = _build_skill_tools(mock_registry)
        by_name = {t.name: t for t in skill_tools}
        phase2 = [
            blade_create, blade_status, blade_query_k8s,
            kubectl, by_name["execute_skill_script"], read_knowledge_resource,
        ]
        names = {t.name for t in phase2}

        # Removed
        assert "blade_destroy" not in names
        assert "activate_skill" not in names
        assert "read_skill_resource" not in names

        # Required for execution
        for required in ("blade_create", "blade_status", "kubectl", "execute_skill_script"):
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
            "Use `blade_destroy`",
            "Use blade_destroy",
            "call `blade_destroy`",
            "Use `activate_skill`",
            "Use activate_skill",
            "call `activate_skill`",
        ]
        for phrase in forbidden_promotions:
            assert phrase not in prompt, (
                f"Phase 2 prompt actively promotes a tool that is not bound: {phrase!r}"
            )
