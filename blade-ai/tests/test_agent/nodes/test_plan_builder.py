"""Tests for plan_builder node — interrupt-based selection card flow."""

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes.plan_builder import (
    MAX_PLAN_BUILDER_ROUNDS,
    PRESENT_OPTIONS_TOOL,
    SUBMIT_PLAN_TOOL,
    _extract_present_options,
    _extract_submit_plan,
    _filter_internal_from_response,
    _get_tool_call_id,
    make_plan_builder,
)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_ai_response(content="", tool_calls=None):
    """Build a mock AIMessage with optional tool_calls."""
    return AIMessage(content=content, tool_calls=tool_calls or [])


def _present_options_tc(question="选择 namespace", options=None, call_id="call_opt1"):
    """Build a present_options tool_call dict."""
    if options is None:
        options = [
            {"key": "A", "label": "cms-demo", "description": "3 Deployments", "recommended": True},
            {"key": "free_input", "label": "自由输入"},
        ]
    return {
        "name": "present_options",
        "id": call_id,
        "args": {"question": question, "options": options},
    }


def _submit_plan_tc(faults=None, call_id="call_sub1"):
    """Build a submit_plan tool_call dict."""
    if faults is None:
        faults = [{
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": ["payment-7b4f8c-x1z"],
            "params": {"cpu-percent": "80"},
        }]
    return {
        "name": "submit_plan",
        "id": call_id,
        "args": {"faults": faults},
    }


def _kubectl_ro_tc(call_id="call_kube1"):
    """Build a kubectl_ro tool_call dict."""
    return {
        "name": "kubectl_ro",
        "id": call_id,
        "args": {"command": "get namespaces"},
    }


def _make_state(**overrides):
    """Build minimal plan_builder state."""
    state = {
        "messages": [HumanMessage(content="CPU压测 payment")],
        "plan_builder_round": 0,
        "task_id": "",
        "tui_session_id": "",
        "fault_spec": None,
        "plan_confirmed": False,
    }
    state.update(overrides)
    return state


def _make_mock_llm(*responses):
    """Create a mock LLM that returns responses in sequence."""
    llm = AsyncMock()
    bound = AsyncMock()
    bound.ainvoke = AsyncMock(side_effect=list(responses))
    llm.bind_tools = lambda *a, **kw: bound
    return llm


# ── Extraction helpers ───────────────────────────────────────────────────────


class TestExtractionHelpers:
    """Unit tests for tool_call extraction functions."""

    def test_extract_submit_plan_found(self):
        tcs = [_submit_plan_tc()]
        result = _extract_submit_plan(tcs)
        assert result is not None
        assert "faults" in result

    def test_extract_submit_plan_not_found(self):
        tcs = [_kubectl_ro_tc()]
        assert _extract_submit_plan(tcs) is None

    def test_extract_present_options_found(self):
        tcs = [_present_options_tc()]
        result = _extract_present_options(tcs)
        assert result is not None
        assert result["question"] == "选择 namespace"
        assert len(result["options"]) == 2

    def test_extract_present_options_not_found(self):
        tcs = [_kubectl_ro_tc()]
        assert _extract_present_options(tcs) is None

    def test_get_tool_call_id_found(self):
        tcs = [_present_options_tc(call_id="abc123")]
        assert _get_tool_call_id(tcs, "present_options") == "abc123"

    def test_get_tool_call_id_fallback(self):
        tcs = [_kubectl_ro_tc()]
        result = _get_tool_call_id(tcs, "present_options")
        assert result.startswith("call_")

    def test_filter_internal_strips_present_options(self):
        response = AIMessage(
            content="some text",
            tool_calls=[_present_options_tc(), _kubectl_ro_tc()],
        )
        filtered = _filter_internal_from_response(response)
        assert len(filtered.tool_calls) == 1
        assert filtered.tool_calls[0]["name"] == "kubectl_ro"
        assert filtered.content == "some text"

    def test_filter_internal_strips_submit_plan(self):
        response = AIMessage(
            content="",
            tool_calls=[_submit_plan_tc(), _kubectl_ro_tc()],
        )
        filtered = _filter_internal_from_response(response)
        names = [tc["name"] for tc in filtered.tool_calls]
        assert "submit_plan" not in names
        assert "kubectl_ro" in names


# ── Node behavior ────────────────────────────────────────────────────────────


class TestPlanBuilderNode:
    """Integration tests for the plan_builder node function."""

    @pytest.mark.asyncio
    async def test_llm_none_returns_error(self):
        """No LLM available → returns error message."""
        node = make_plan_builder(llm=None)
        result = await node(_make_state())
        msgs = result["messages"]
        assert len(msgs) == 1
        assert "LLM 不可用" in msgs[0].content

    @pytest.mark.asyncio
    async def test_submit_plan_finalizes(self):
        """Priority 1: submit_plan → sets plan_confirmed and fault_spec."""
        response = _make_ai_response(tool_calls=[_submit_plan_tc()])
        llm = _make_mock_llm(response)
        node = make_plan_builder(llm=llm)

        with patch("chaos_agent.agent.nodes.plan_builder.interrupt"):
            result = await node(_make_state())

        assert result["plan_confirmed"] is True
        assert result["fault_spec"] is not None
        assert result["fault_spec"]["namespace"] == "cms-demo"

    @pytest.mark.asyncio
    async def test_present_options_calls_interrupt(self):
        """Priority 2: present_options → calls interrupt with correct payload."""
        options_response = _make_ai_response(tool_calls=[_present_options_tc()])
        # After interrupt resume, LLM calls submit_plan to end the loop
        submit_response = _make_ai_response(tool_calls=[_submit_plan_tc()])
        llm = _make_mock_llm(options_response, submit_response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="A",
        ) as mock_interrupt:
            result = await node(_make_state())

        # interrupt called with plan_selection payload
        call_args = mock_interrupt.call_args[0][0]
        assert call_args["type"] == "plan_selection"
        assert call_args["question"] == "选择 namespace"
        assert len(call_args["options"]) == 2
        # Loop continued → submit_plan finalized
        assert result["plan_confirmed"] is True

    @pytest.mark.asyncio
    async def test_present_options_cancel_exits(self):
        """Priority 2: user cancels (Esc) → exits cleanly."""
        response = _make_ai_response(tool_calls=[_present_options_tc()])
        llm = _make_mock_llm(response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="rejected",
        ):
            result = await node(_make_state())

        msgs = result["messages"]
        assert any("已取消" in m.content for m in msgs)
        assert result.get("plan_confirmed") is not True

    @pytest.mark.asyncio
    async def test_real_tools_return_to_toolnode(self):
        """Priority 3: kubectl_ro → return for ToolNode execution."""
        response = _make_ai_response(
            content="Let me check namespaces",
            tool_calls=[_kubectl_ro_tc()],
        )
        llm = _make_mock_llm(response)
        node = make_plan_builder(llm=llm)

        with patch("chaos_agent.agent.nodes.plan_builder.interrupt"):
            result = await node(_make_state())

        msgs = result["messages"]
        # Should contain the filtered AI response with kubectl_ro tool_call
        last_msg = msgs[-1]
        assert isinstance(last_msg, AIMessage)
        assert any(tc["name"] == "kubectl_ro" for tc in last_msg.tool_calls)
        # plan_confirmed NOT set (still in progress)
        assert result.get("plan_confirmed") is not True

    @pytest.mark.asyncio
    async def test_text_fallback_interrupts(self):
        """Priority 4: pure text → interrupt with empty options."""
        text_response = _make_ai_response(content="请告诉我更多信息")
        submit_response = _make_ai_response(tool_calls=[_submit_plan_tc()])
        llm = _make_mock_llm(text_response, submit_response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="some user text",
        ) as mock_interrupt:
            result = await node(_make_state())

        call_args = mock_interrupt.call_args[0][0]
        assert call_args["type"] == "plan_selection"
        assert call_args["question"] == "请告诉我更多信息"
        assert call_args["options"] == []
        assert result["plan_confirmed"] is True

    @pytest.mark.asyncio
    async def test_text_fallback_cancel_exits(self):
        """Priority 4: text fallback + user cancels → exits."""
        response = _make_ai_response(content="请问...")
        llm = _make_mock_llm(response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="rejected",
        ):
            result = await node(_make_state())

        msgs = result["messages"]
        assert any("已取消" in m.content for m in msgs)

    @pytest.mark.asyncio
    async def test_mixed_tools_priority2_strips_real(self):
        """present_options + kubectl_ro in same response → only present_options kept."""
        mixed_response = _make_ai_response(
            content="checking",
            tool_calls=[_present_options_tc(call_id="opt1"), _kubectl_ro_tc(call_id="kube1")],
        )
        submit_response = _make_ai_response(tool_calls=[_submit_plan_tc()])
        llm = _make_mock_llm(mixed_response, submit_response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="A",
        ):
            result = await node(_make_state())

        # Verify accumulated messages don't have orphan kubectl_ro tool_call
        msgs = result["messages"]
        for msg in msgs:
            if isinstance(msg, AIMessage) and msg.tool_calls:
                names = [tc["name"] for tc in msg.tool_calls]
                assert "kubectl_ro" not in names

    @pytest.mark.asyncio
    async def test_max_rounds_exceeded(self):
        """Exceeding MAX_PLAN_BUILDER_ROUNDS → exits with limit message."""
        response = _make_ai_response(content="another question")
        # Return the same text response forever (will hit max rounds)
        llm = _make_mock_llm(*([response] * (MAX_PLAN_BUILDER_ROUNDS + 1)))
        node = make_plan_builder(llm=llm)
        state = _make_state(plan_builder_round=MAX_PLAN_BUILDER_ROUNDS - 1)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="keep going",
        ):
            result = await node(state)

        msgs = result["messages"]
        assert any("轮数已达上限" in m.content for m in msgs if isinstance(m, AIMessage))

    @pytest.mark.asyncio
    async def test_llm_exception_returns_error(self):
        """LLM raises → returns error message gracefully."""
        llm = AsyncMock()
        bound = AsyncMock()
        bound.ainvoke = AsyncMock(side_effect=RuntimeError("API timeout"))
        llm.bind_tools = lambda *a, **kw: bound
        node = make_plan_builder(llm=llm)

        with patch("chaos_agent.agent.nodes.plan_builder.interrupt"):
            result = await node(_make_state())

        msgs = result["messages"]
        assert any("遇到了一些问题" in m.content for m in msgs)

    @pytest.mark.asyncio
    async def test_activate_skill_extracts_skill_name(self):
        """activate_skill in tool_calls → skill_name set in state update."""
        response = _make_ai_response(tool_calls=[{
            "name": "activate_skill",
            "id": "call_skill",
            "args": {"skill_name": "pod-cpu-fullload"},
        }])
        llm = _make_mock_llm(response)
        node = make_plan_builder(llm=llm)

        with patch("chaos_agent.agent.nodes.plan_builder.interrupt"):
            result = await node(_make_state())

        assert result["skill_name"] == "pod-cpu-fullload"

    @pytest.mark.asyncio
    async def test_accumulated_messages_on_resume(self):
        """After interrupt resume, accumulated includes AI + ToolMessage pair."""
        options_response = _make_ai_response(
            content="context text",
            tool_calls=[_present_options_tc(call_id="opt99")],
        )
        submit_response = _make_ai_response(tool_calls=[_submit_plan_tc()])
        llm = _make_mock_llm(options_response, submit_response)
        node = make_plan_builder(llm=llm)

        with patch(
            "chaos_agent.agent.nodes.plan_builder.interrupt",
            return_value="B",
        ):
            result = await node(_make_state())

        msgs = result["messages"]
        # Find the ToolMessage responding to present_options
        tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
        assert any("用户选择: B" in m.content for m in tool_msgs)
        # The AI message before it should have only present_options call
        ai_msgs = [m for m in msgs if isinstance(m, AIMessage) and m.tool_calls]
        for ai in ai_msgs:
            for tc in ai.tool_calls:
                assert tc["name"] != "kubectl_ro"


# ── Schema validation ────────────────────────────────────────────────────────


class TestToolSchemas:
    """Verify tool schemas are well-formed for LLM binding."""

    def test_present_options_schema_structure(self):
        schema = PRESENT_OPTIONS_TOOL
        assert schema["name"] == "present_options"
        props = schema["parameters"]["properties"]
        assert "question" in props
        assert "options" in props
        assert props["options"]["type"] == "array"
        items_props = props["options"]["items"]["properties"]
        assert "key" in items_props
        assert "label" in items_props
        assert "description" in items_props
        assert "recommended" in items_props

    def test_submit_plan_schema_structure(self):
        schema = SUBMIT_PLAN_TOOL
        assert schema["name"] == "submit_plan"
        props = schema["parameters"]["properties"]
        assert "faults" in props
        assert props["faults"]["type"] == "array"
        fault_props = props["faults"]["items"]["properties"]
        assert "scope" in fault_props
        assert "target" in fault_props
        assert "action" in fault_props
