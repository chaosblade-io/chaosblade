"""Tests for agent_loop node."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.agent_loop import (
    agent_loop,
    make_agent_loop,
)
from chaos_agent.agent.nodes.react_helpers import (
    detect_repeated_tool_calls,
    _compare_tool_outputs,
)
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", 3)

        state = sample_agent_state
        state["agent_loop_count"] = 3

        result = await agent_loop(state)
        assert "error" in result
        assert "max iterations" in result["error"].lower()
        assert result["safety_status"] == "rejected"

    @pytest.mark.asyncio
    async def test_at_max_iterations_still_ok(self, sample_agent_state, monkeypatch):
        """At exactly MAX_AGENT_LOOP (not exceeding), loop continues normally."""
        monkeypatch.setattr(settings, "max_agent_loop", 5)
        import chaos_agent.agent.nodes.agent_loop as loop_mod
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        # Patch async dependencies
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.sync_to_store",
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.sync_to_store",
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.sync_to_store",
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
        import chaos_agent.agent.nodes.agent_loop as loop_mod
        monkeypatch.setattr(loop_mod, "MAX_AGENT_LOOP", self.MAX)
        monkeypatch.setattr(settings, "max_agent_loop", self.MAX)

        llm, bound_llm = self._make_mock_llm()
        mock_tool = MagicMock()
        mock_tool.name = "test_tool"

        node = make_agent_loop(llm=llm, tools=[mock_tool], skill_catalog="test")

        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.compute_env_info",
            AsyncMock(return_value=""),
        )
        monkeypatch.setattr(
            "chaos_agent.agent.nodes.agent_loop.sync_to_store",
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


class TestExtractTargetFromKubectlGet:
    """Tests for _extract_target_from_kubectl_get write-once + blacklist guard."""

    DEFAULT_BLACKLIST = ["kube-system", "kube-public"]

    def test_namespace_set_from_first_query(self):
        """Namespace is extracted when target is empty."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n cms-demo -l app=payment",
            existing_target=None,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["namespace"] == "cms-demo"
        assert result["labels"] == "app=payment"
        assert result["resource_type"] == "pod"

    def test_blacklisted_namespace_does_not_overwrite(self):
        """A kubectl get to kube-system must NOT overwrite an existing namespace."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n kube-system -l app=chaosblade -o name",
            existing_target={"namespace": "cms-demo"},
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["namespace"] == "cms-demo"

    def test_blacklisted_namespace_skipped_when_empty(self):
        """Even when target namespace is empty, blacklisted ns is not set."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n kube-system -l app=chaosblade",
            existing_target=None,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert "namespace" not in result

    def test_namespace_write_once(self):
        """After namespace is set, a second call with a different ns does not overwrite."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        # First call sets namespace
        result = _extract_target_from_kubectl_get(
            v_args="svc payment -n cms-demo",
            existing_target=None,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["namespace"] == "cms-demo"

        # Second call with different namespace: write-once prevents overwrite
        result2 = _extract_target_from_kubectl_get(
            v_args="pods -n monitoring -l app=prometheus",
            existing_target=result,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result2["namespace"] == "cms-demo"

    def test_labels_write_once(self):
        """After labels is set, a second call with a different selector does not overwrite."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n cms-demo -l app=payment",
            existing_target=None,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["labels"] == "app=payment"

        result2 = _extract_target_from_kubectl_get(
            v_args="pods -n cms-demo -l app=chaosblade",
            existing_target=result,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result2["labels"] == "app=payment"

    def test_resource_type_write_once(self):
        """After resource_type is set, a second call does not overwrite."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n cms-demo -l app=payment",
            existing_target=None,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["resource_type"] == "pod"

        result2 = _extract_target_from_kubectl_get(
            v_args="nodes -n cms-demo",
            existing_target=result,
            state_target=None,
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result2["resource_type"] == "pod"

    def test_namespace_preserved_from_state_for_cluster_scoped_query(self):
        """Cluster-scoped query (no -n flag) preserves namespace from state_target."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="nodes",
            existing_target=None,
            state_target={"namespace": "cms-demo"},
            blacklist=self.DEFAULT_BLACKLIST,
        )
        assert result["namespace"] == "cms-demo"

    def test_empty_blacklist_still_write_once(self):
        """With empty blacklist, write-once still prevents overwrite."""
        from chaos_agent.agent.nodes.agent_loop import _extract_target_from_kubectl_get

        result = _extract_target_from_kubectl_get(
            v_args="pods -n cms-demo",
            existing_target=None,
            state_target=None,
            blacklist=[],
        )
        assert result["namespace"] == "cms-demo"

        result2 = _extract_target_from_kubectl_get(
            v_args="pods -n other-ns",
            existing_target=result,
            state_target=None,
            blacklist=[],
        )
        assert result2["namespace"] == "cms-demo"


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
        """No tool call IDs match the fingerprint → (True, False)."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {}  # No entries
        id_to_output = {}
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is True
        assert have_outputs is False

    def test_single_output_cannot_determine(self):
        """Single output (less than 2) → (True, True) — cannot determine progression."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1"]}
        id_to_output = {"id1": "CPU: 3m"}
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is True
        assert have_outputs is True

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
        """Tool call ID exists in fingerprint map but not in output map → treated as no output."""
        fp = "kubectl(subcommand=top, v_args=pods)"
        fp_to_ids = {fp: ["id1", "id2"]}
        id_to_output = {}  # No outputs at all
        all_identical, have_outputs = _compare_tool_outputs(fp, fp_to_ids, id_to_output)
        assert all_identical is True
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
        """3 identical calls with identical outputs → LOOP DETECTED with '基本一致'."""
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
        assert "基本一致" in result

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

    def test_no_outputs_triggers_loop_with_fallback_message(self, monkeypatch):
        """3 calls with no ToolMessages → LOOP DETECTED with '无法获取'."""
        monkeypatch.setattr(settings, "loop_detection_window", 12)
        monkeypatch.setattr(settings, "loop_detection_threshold", 3)

        messages = [
            self._build_kubectl_top_aimessage(tc_id="tc1"),
            self._build_kubectl_top_aimessage(tc_id="tc2"),
            self._build_kubectl_top_aimessage(tc_id="tc3"),
        ]
        result = detect_repeated_tool_calls(messages)
        assert result is not None
        assert "LOOP DETECTED" in result
        assert "无法获取" in result

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
        # No UnboundLocalError for subcmd
        assert "基本一致" in result

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
