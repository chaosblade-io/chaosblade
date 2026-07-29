"""Tests for agent_loop node."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.nodes.execute.agent_loop import (
    agent_loop,
    make_agent_loop,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_repeated_tool_calls,
    _compare_tool_outputs,
)
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings


class TestAgentLoop:
    """Tests for the agent_loop node function."""

    @pytest.mark.asyncio
    async def test_increments_counter(self, sample_agent_state):
        state = sample_agent_state
        state["agent_loop_count"] = 0

        result = await agent_loop(state)
        assert result["agent_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_increments_from_nonzero(self, sample_agent_state):
        state = sample_agent_state
        state["agent_loop_count"] = 5

        result = await agent_loop(state)
        assert result["agent_loop_count"] == 6

    @pytest.mark.asyncio
    async def test_exceeds_max_iterations(self, sample_agent_state, monkeypatch):
        """When agent_loop_count exceeds MAX_AGENT_LOOP, should return error."""
        monkeypatch.setattr(settings, "max_agent_loop", 3)
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", 3)

        state = sample_agent_state
        state["agent_loop_count"] = 3

        result = await agent_loop(state)
        assert "error" in result
        assert "planning_timeout" in result["error"]
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_at_max_iterations_still_ok(self, sample_agent_state, monkeypatch):
        """At exactly MAX_AGENT_LOOP (not exceeding), loop continues normally."""
        monkeypatch.setattr(settings, "max_agent_loop", 5)
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", 5)

        state = sample_agent_state
        state["agent_loop_count"] = 4  # 4 + 1 = 5, which equals MAX, not exceeds

        result = await agent_loop(state)
        assert result["agent_loop_count"] == 5
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_exceeds_max_by_one(self, sample_agent_state, monkeypatch):
        """Exceeding max by 1 should trigger rejection."""
        monkeypatch.setattr(settings, "max_agent_loop", 2)
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", 2)

        state = sample_agent_state
        state["agent_loop_count"] = 2  # 2 + 1 = 3 > 2

        result = await agent_loop(state)
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_default_count_missing(self):
        """When agent_loop_count is missing from state, defaults to 0+1=1."""
        result = await agent_loop({})
        assert result["agent_loop_count"] == 1

    @pytest.mark.asyncio
    async def test_returns_only_relevant_fields(self, sample_agent_state):
        """Normal loop iteration should only return agent_loop_count."""
        state = sample_agent_state
        state["agent_loop_count"] = 0

        result = await agent_loop(state)
        assert set(result.keys()) == {"agent_loop_count"}

    @pytest.mark.asyncio
    async def test_rejects_transport_incompatible_semantic_intent_before_llm(self, monkeypatch):
        """Intent may recognize host, but planning must fail closed on K8s transport."""
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod

        llm = MagicMock()
        node = make_agent_loop(llm=llm, tools=[])
        monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
        monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())

        result = await node({
            "task_id": "task-profile-conflict",
            "operation": "inject",
            "agent_loop_count": 0,
            "messages": [],
            "fault_spec": {
                "scope": "host",
                "blade_target": "cpu",
                "blade_action": "fullload",
                "names": ["host-1"],
            },
            "kube_connection_mode": "kubeconfig",
        })

        assert result["safety_status"] == "rejected"
        assert result["failure_detail"]["category"] == FailureCategory.PLANNING_REJECTED.value
        assert "configured execution transport" in result["error"]
        llm.bind_tools.assert_not_called()

    @pytest.mark.asyncio
    async def test_gate_emptying_the_tool_set_binds_nothing(self, monkeypatch):
        """A gate that removes every tool must NOT fall back to an unbound LLM.

        ``bind_tools`` is not an enforcement boundary, but an UNBOUND llm is
        strictly worse: the model still emits calls from what the prompt showed
        it, and a static ToolNode would run them. Reachable with a supported
        profile: here the only tool offered belongs to a provider that cannot
        operate on a k8s channel, so the visible set empties while the
        environment itself stays supported.
        """
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        from chaos_agent.agent.providers import FaultProviderRegistry
        from chaos_agent.tools import host_read

        FaultProviderRegistry.register_builtins()
        response = AIMessage(content="No usable tool here; reporting.")
        bound = MagicMock()
        bound.ainvoke = AsyncMock(return_value=response)
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=bound)
        node = make_agent_loop(llm=llm, tools=[host_read])
        monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
        monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())

        await node({
            "task_id": "task-gate-empty",
            "operation": "inject",
            "agent_loop_count": 0,
            "messages": [],
            "fault_spec": {
                "scope": "node", "blade_target": "cpu",
                "blade_action": "fullload", "names": ["node-1"],
            },
            "kube_connection_mode": "kubeconfig",
        })

        llm.bind_tools.assert_called_once_with([])


class TestAgentLoopReplanHandoff:
    """Replan context is an internal handoff, not a repeated user turn."""

    @pytest.mark.asyncio
    async def test_replan_context_is_persisted_once_per_attempt(self, monkeypatch):
        response = AIMessage(
            content="Analyzed once; checking the target.",
            tool_calls=[{
                "name": "kubectl_read",
                "args": {"subcommand": "get", "v_args": "node n1"},
                "id": "tc-replan-1",
            }],
        )
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=response)
        llm = MagicMock()
        llm.bind_tools = MagicMock(return_value=bound_llm)
        mock_tool = MagicMock()
        mock_tool.name = "kubectl_read"
        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        state = {
            "task_id": "task-replan-once",
            "operation": "inject",
            "agent_loop_count": 4,
            "skill_name": "k8s-chaos-skills",
            "messages": [],
            "replan_context": {
                "trigger": "execute_loop",
                "error_summary": "blade failed",
                "failed_tool_calls": [{"name": "blade_create", "error": "failed"}],
                "existing_blade_uids": [],
            },
            "replan_history": [],
            "replan_count": 1,
            "verify_replan_count": 0,
            "replan_context_injected_attempt": None,
        }

        first = await node(state)
        handoffs = [
            msg for msg in first["messages"]
            if isinstance(msg, SystemMessage)
            and msg.additional_kwargs.get("kind") == "replan_context"
        ]
        assert len(handoffs) == 1
        assert first["replan_context_injected_attempt"] == 1
        assert not any(isinstance(msg, HumanMessage) for msg in handoffs)

        second_state = {
            **state,
            "agent_loop_count": first["agent_loop_count"],
            "_replan_loop_reset": first["_replan_loop_reset"],
            "replan_context_injected_attempt": 1,
            "messages": first["messages"] + [
                ToolMessage(
                    content="node/n1 Ready",
                    name="kubectl_read",
                    tool_call_id="tc-replan-1",
                )
            ],
        }
        second = await node(second_state)

        # The second delta contains only the new AI response. The original
        # handoff remains in state history and was not fabricated again.
        assert not any(
            isinstance(msg, SystemMessage)
            and msg.additional_kwargs.get("kind") == "replan_context"
            for msg in second["messages"]
        )
        second_invoke_messages = bound_llm.ainvoke.call_args_list[1].args[0]
        persisted_handoffs = [
            msg for msg in second_invoke_messages
            if isinstance(msg, SystemMessage)
            and msg.additional_kwargs.get("kind") == "replan_context"
        ]
        assert len(persisted_handoffs) == 1


class TestAgentLoopConvergence:
    """Tests for convergence hints in the LLM-enabled agent_loop (make_agent_loop)."""

    MAX = 10  # Small max for testability

    def _make_mock_llm(self):
        """Create a mock LLM with bind_tools and ainvoke."""
        mock_response = MagicMock()
        mock_response.content = "Planning summary: ready to execute."
        mock_response.tool_calls = []
        mock_response.additional_kwargs = {}

        llm = MagicMock()
        llm.ainvoke = AsyncMock(return_value=mock_response)

        # bind_tools returns a new object with its own ainvoke
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=mock_response)
        llm.bind_tools = MagicMock(return_value=bound_llm)

        return llm, bound_llm

    def _make_state(self, agent_loop_count):
        """Build a minimal state for convergence hint tests."""
        return {
            "task_id": "task-convergence-test",
            "operation": "inject",
            "agent_loop_count": agent_loop_count,
            "skill_name": "k8s-chaos-skills",
            "messages": [],
            "replan_context": None,
            "replan_history": None,
            "replan_count": 0,
            "target": {"namespace": "test-ns"},
        }

    def _get_human_messages_from_invoke(self, llm_ainvoke_mock):
        """Extract HumanMessage texts from the messages passed to llm.ainvoke."""
        call_args = llm_ainvoke_mock.call_args
        if call_args is None:
            return []
        messages = call_args[0][0]  # First positional arg
        return [
            msg.content for msg in messages
            if isinstance(msg, HumanMessage)
        ]

    @pytest.mark.asyncio
    async def test_no_hint_below_threshold(self, monkeypatch):
        """No convergence hint injected when well below the iteration limit."""
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        # Patch async dependencies
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        state = self._make_state(agent_loop_count=3)  # count=4, well below MAX-5=5
        await node(state)

        # Tools should be bound (not at final iteration)
        llm.bind_tools.assert_called_once()

        # No convergence hint should be injected
        human_texts = self._get_human_messages_from_invoke(bound_llm.ainvoke)
        for text in human_texts:
            assert "Iteration Progress" not in text
            assert "CRITICAL WARNING" not in text
            assert "FINAL ITERATION" not in text

    @pytest.mark.asyncio
    async def test_tier1_soft_warning(self, monkeypatch):
        """Tier 1 soft warning injected when iterations are running low."""
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        # count=6, which is in range [MAX-5=5, MAX-1=9)
        state = self._make_state(agent_loop_count=5)
        await node(state)

        # Tools should still be bound
        llm.bind_tools.assert_called_once()

        # Tier 1 hint should be present
        human_texts = self._get_human_messages_from_invoke(bound_llm.ainvoke)
        assert any("Iteration Progress" in t for t in human_texts)

    @pytest.mark.asyncio
    async def test_tier2_urgent_warning(self, monkeypatch):
        """Tier 2 urgent warning injected on the second-to-last iteration."""
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        # count=9 = MAX-1
        state = self._make_state(agent_loop_count=8)
        await node(state)

        # Tools should still be bound
        llm.bind_tools.assert_called_once()

        # Tier 2 hint should be present
        human_texts = self._get_human_messages_from_invoke(bound_llm.ainvoke)
        assert any("CRITICAL WARNING" in t for t in human_texts)

    @pytest.mark.asyncio
    async def test_tier3_final_unbinds_tools(self, monkeypatch):
        """Tier 3 final iteration: hint injected and tools unbound."""
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        # count=10 = MAX
        state = self._make_state(agent_loop_count=9)
        await node(state)

        # Tools should NOT be bound (unbound at final iteration)
        llm.bind_tools.assert_not_called()

        # LLM should be called directly (not the bound version)
        llm.ainvoke.assert_called_once()
        bound_llm.ainvoke.assert_not_called()

        # Tier 3 hint should be present
        human_texts = self._get_human_messages_from_invoke(llm.ainvoke)
        assert any("FINAL ITERATION" in t for t in human_texts)



# ---------------------------------------------------------------------------
# Tests for _compare_tool_outputs — output-aware loop detection
# ---------------------------------------------------------------------------

class TestCompareToolOutputs:
    """Tests for _compare_tool_outputs()."""

    def test_all_outputs_identical(self):
        """All outputs are the same → (True, True)."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1", "id2", "id3"]}
        id_to_output = {
            "id1": "CPU: 3m\nMEM: 10Mi",
            "id2": "CPU: 3m\nMEM: 10Mi",
            "id3": "CPU: 3m\nMEM: 10Mi",
        }
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is True
        assert have_outputs is True

    def test_outputs_differ_progressing(self):
        """Outputs differ (CPU ramping up) → (False, True) — suppress loop."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1", "id2", "id3"]}
        id_to_output = {
            "id1": "CPU: 3m\nMEM: 10Mi",
            "id2": "CPU: 97m\nMEM: 10Mi",
            "id3": "CPU: 161m\nMEM: 10Mi",
        }
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is False
        assert have_outputs is True

    def test_no_matching_tool_call_ids(self):
        """No tool call IDs match the fingerprint → (False, False) — no evidence."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {}  # No entries
        id_to_output = {}
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is False
        assert have_outputs is False

    def test_single_output_cannot_determine(self):
        """A single output proves nothing → (False, False) — caller stays silent.

        Previously this returned (True, True), letting the caller fire a LOOP
        DETECTED hint on one sample. Warning without evidence risks telling a
        correct model it is looping.
        """
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1"]}
        id_to_output = {"id1": "CPU: 3m"}
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is False
        assert have_outputs is False

    def test_outputs_trimmed_and_truncated(self):
        """Whitespace is stripped and outputs truncated to 500 chars for comparison."""
        fp = "kubectl(subcommand=get, v_args=pods)"
        fp_to_ids = {fp: ["id1", "id2"]}
        id_to_output = {
            "id1": "  pod-1   Running  \n",
            "id2": "pod-1   Running",
        }
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is True
        assert have_outputs is True

    def test_tool_call_id_not_in_output_map(self):
        """IDs exist in the fingerprint map but no outputs → (False, False)."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1", "id2"]}
        id_to_output = {}  # No outputs at all
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is False
        assert have_outputs is False


# ---------------------------------------------------------------------------
# Tests for detect_repeated_tool_calls — full loop detection logic
# ---------------------------------------------------------------------------

class TestDetectRepeatedToolCalls:
    """Tests for detect_repeated_tool_calls() with output-aware suppression."""

    def _build_kubectl_top_aimessage(self, name="kubectl", args=None, tc_id="tc1"):
        """Build an AIMessage with a single kubectl tool call."""
        if args is None:
            args = {"subcommand": "top", "v_args": "pods"}
        tc = {"name": name, "args": args, "id": tc_id}
        return AIMessage(content="Checking pods", tool_calls=[tc])

    def _build_tool_message(self, tc_id, content):
        """Build a ToolMessage for a given tool_call_id."""
        return ToolMessage(content=content, tool_call_id=tc_id)

    def test_below_threshold_returns_none(self, monkeypatch):
        """Less than threshold identical calls → no loop detected."""
        monkeypatch.setattr(settings, "loop_detection_window", 10)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        messages = [
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_tool_message("tc1", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_tool_message("tc2", "CPU: 3m"),
        ]
        result = detect_repeated_tool_calls(messages)
        assert result is None

    def test_identical_outputs_triggers_loop(self, monkeypatch):
        """3 identical calls with identical outputs → LOOP DETECTED."""
        monkeypatch.setattr(settings, "loop_detection_window", 12)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        messages = [
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_tool_message("tc1", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_tool_message("tc2", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc3"),
            self._build_tool_message("tc3", "CPU: 3m"),
        ]
        result = detect_repeated_tool_calls(messages)
        assert result is not None
        assert "LOOP DETECTED" in result
        assert "REFLECT" in result

    def test_differing_outputs_suppresses_loop(self, monkeypatch):
        """3 calls but outputs differ (CPU 3m→97m→161m) → suppressed, returns None."""
        monkeypatch.setattr(settings, "loop_detection_window", 12)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        messages = [
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_tool_message("tc1", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_tool_message("tc2", "CPU: 97m"),
            self._build_kubectl_top_aimessage(tc_id="tc3"),
            self._build_tool_message("tc3", "CPU: 161m"),
        ]
        result = detect_repeated_tool_calls(messages)
        assert result is None

    def test_no_outputs_stays_silent(self, monkeypatch):
        """3 calls but no ToolMessages → silent (contract inverted deliberately).

        This used to assert LOOP DETECTED. Firing without any output evidence
        means guessing: the model may be reasoning correctly, and being told it
        is looping would push it to discard a valid conclusion. Missing/unpaired
        tool results now yield no warning.
        """
        monkeypatch.setattr(settings, "loop_detection_window", 12)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        messages = [
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_kubectl_top_aimessage(tc_id="tc3"),
        ]
        assert detect_repeated_tool_calls(messages) is None

    def test_non_kubectl_repeated_tool(self, monkeypatch):
        """Repeated non-kubectl tool (e.g., read_skill_resource) with identical outputs triggers loop."""
        monkeypatch.setattr(settings, "loop_detection_window", 12)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        args = {"resource_name": "Pod_cpu使用率过高"}
        messages = [
            AIMessage(content="", tool_calls=[{"name": "read_skill_resource", "args": args, "id": "tc1"}]),
            ToolMessage(content="skill content here", tool_call_id="tc1"),
            AIMessage(content="", tool_calls=[{"name": "read_skill_resource", "args": args, "id": "tc2"}]),
            ToolMessage(content="skill content here", tool_call_id="tc2"),
            AIMessage(content="", tool_calls=[{"name": "read_skill_resource", "args": args, "id": "tc3"}]),
            ToolMessage(content="skill content here", tool_call_id="tc3"),
        ]
        result = detect_repeated_tool_calls(messages)
        assert result is not None
        assert "LOOP DETECTED" in result
        assert "REFLECT" in result

    def test_window_limits_scope(self, monkeypatch):
        """Only calls within the window are counted."""
        monkeypatch.setattr(settings, "loop_detection_window", 4)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        # First 2 calls are outside the window (only 4 messages counted from the end)
        # Inside window: 2 calls = below threshold
        messages = [
            self._build_kubectl_top_aimessage(tc_id="old1"),
            self._build_tool_message("old1", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_tool_message("tc1", "CPU: 3m"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_tool_message("tc2", "CPU: 3m"),
        ]
        result = detect_repeated_tool_calls(messages)
        # Window=4 means only last 4 messages: tc1 AIMessage, tc1 ToolMessage, tc2 AIMessage, tc2 ToolMessage
        # That's only 2 calls → below threshold 3
        assert result is None


class TestFinishPlanningRejection:
    """Tests for finish_planning with rejected=True."""

    MAX = 10

    def _make_state(self):
        return {
            "task_id": "task-fp-reject",
            "operation": "inject",
            "agent_loop_count": 3,
            "skill_name": "k8s-chaos-skills",
            "messages": [],
            "replan_context": None,
            "replan_history": None,
            "replan_count": 0,
            "target": {"namespace": "test-ns"},
        }

    @pytest.mark.asyncio
    async def test_finish_planning_passes_through_to_toolnode(self, monkeypatch):
        """finish_planning tool_calls pass through agent_loop without inline processing.

        After refactor, agent_loop no longer handles finish_planning/save_fault_plan
        inline — they go through ToolNode and are processed by extract_planning_metadata.
        agent_loop should simply return the AIMessage containing the tool_call.
        """
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        mock_response = MagicMock()
        mock_response.content = "Cannot inject into kube-system."
        mock_response.tool_calls = [
            {
                "name": "finish_planning",
                "args": {
                    "summary": "kube-system is protected",
                    "rejected": True,
                    "rejection_reason": "Safety red line: kube-system namespace",
                },
                "id": "tc_fp_reject",
            }
        ]
        mock_response.additional_kwargs = {}

        llm = MagicMock()
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=mock_response)
        llm.bind_tools = MagicMock(return_value=bound_llm)

        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.execute.agent_loop.sync_to_store",
            AsyncMock(),
        )

        state = self._make_state()
        result = await node(state)

        # agent_loop no longer sets error/plan for finish_planning —
        # it passes through to ToolNode → extract_planning_metadata
        assert "error" not in result
        assert "failure_detail" not in result
        assert "messages" in result


class TestAgentLoopTextOnlyStall:
    """Phase-1 text-only stalls (no tool call, no skill) nudge up to
    ``max_plan_text_stalls`` then fail; a productive turn resets the streak."""

    MAX = 10

    def _text_only_llm(self, content="Let me think about how to approach this."):
        mock_response = MagicMock()
        mock_response.content = content
        mock_response.tool_calls = []
        mock_response.additional_kwargs = {}
        llm = MagicMock()
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=mock_response)
        llm.bind_tools = MagicMock(return_value=bound_llm)
        llm.ainvoke = AsyncMock(return_value=mock_response)
        return llm

    def _tool_call_llm(self):
        mock_response = MagicMock()
        mock_response.content = ""
        mock_response.tool_calls = [
            {"name": "read_skill_resource", "args": {}, "id": "t1"}
        ]
        mock_response.additional_kwargs = {}
        llm = MagicMock()
        bound_llm = MagicMock()
        bound_llm.ainvoke = AsyncMock(return_value=mock_response)
        llm.bind_tools = MagicMock(return_value=bound_llm)
        llm.ainvoke = AsyncMock(return_value=mock_response)
        return llm

    def _state(self, *, stall_count=0, agent_loop_count=1, skill_name=None):
        state = {
            "task_id": "task-plan-stall",
            "operation": "inject",
            "agent_loop_count": agent_loop_count,
            "messages": [],
            "target": {"namespace": "test-ns"},
            "_plan_text_stall_count": stall_count,
        }
        if skill_name:
            state["skill_name"] = skill_name
        return state

    def _patch(self, monkeypatch, *, max_stalls=3):
        import chaos_agent.agent.nodes.execute.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)
        monkeypatch.setattr(settings, "max_plan_text_stalls", max_stalls)
        monkeypatch.setattr(loop_mod, "compute_env_info", AsyncMock(return_value=""))
        monkeypatch.setattr(loop_mod, "sync_to_store", AsyncMock())

    @pytest.mark.asyncio
    async def test_first_stall_nudges_not_fail(self, monkeypatch):
        self._patch(monkeypatch)
        node = make_agent_loop(llm=self._text_only_llm(), tools=[], skill_catalog="x")
        result = await node(self._state(stall_count=0))
        assert result.get("_plan_text_stall_count") == 1
        assert not result.get("error")

    @pytest.mark.asyncio
    async def test_reaching_threshold_fails(self, monkeypatch):
        # Third consecutive stall hits the default budget of 3 → fail.
        self._patch(monkeypatch)
        node = make_agent_loop(llm=self._text_only_llm(), tools=[], skill_catalog="x")
        result = await node(self._state(stall_count=2))
        assert result.get("error")
        assert "without tool use or skill activation" in result["error"]

    @pytest.mark.asyncio
    async def test_final_iteration_fails_directly(self, monkeypatch):
        # At the loop cap tools are unbound; text is the expected handoff →
        # terminate immediately regardless of the (fresh) stall counter.
        self._patch(monkeypatch)
        node = make_agent_loop(llm=self._text_only_llm(), tools=[], skill_catalog="x")
        result = await node(self._state(stall_count=0, agent_loop_count=self.MAX - 1))
        assert result.get("error")
        assert "without tool use or skill activation" in result["error"]

    @pytest.mark.asyncio
    async def test_skill_active_resets_stall(self, monkeypatch):
        # A turn with a skill active is productive (normal planning-complete
        # path) → reset the streak, never fail.
        self._patch(monkeypatch)
        node = make_agent_loop(llm=self._text_only_llm(), tools=[], skill_catalog="x")
        result = await node(self._state(stall_count=2, skill_name="k8s-chaos-skills"))
        assert result.get("_plan_text_stall_count") == 0
        assert not result.get("error")

    @pytest.mark.asyncio
    async def test_tool_call_turn_resets_stall(self, monkeypatch):
        # A tool-call turn breaks the stall streak.
        self._patch(monkeypatch)
        node = make_agent_loop(llm=self._tool_call_llm(), tools=[], skill_catalog="x")
        result = await node(self._state(stall_count=2))
        assert result.get("_plan_text_stall_count") == 0
        assert not result.get("error")
