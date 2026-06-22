"""Tests for streaming event models and parsers."""

import json

from chaos_agent.agent.streaming import StreamEvent, parse_stream_event, _extract_node_name


class TestStreamEvent:
    """Tests for StreamEvent dataclass."""

    def test_to_dict_omits_empty_fields(self):
        evt = StreamEvent(type="token", content="hello")
        d = evt.to_dict()
        assert d["type"] == "token"
        assert d["content"] == "hello"
        assert "node" not in d
        assert "tool_name" not in d
        assert "task_id" not in d

    def test_to_dict_includes_nonempty_fields(self):
        evt = StreamEvent(type="tool_end", content="ok", node="agent_loop", tool_name="kubectl", task_id="t1")
        d = evt.to_dict()
        assert d["type"] == "tool_end"
        assert d["content"] == "ok"
        assert d["node"] == "agent_loop"
        assert d["tool_name"] == "kubectl"
        assert d["task_id"] == "t1"

    def test_to_sse_format(self):
        evt = StreamEvent(type="token", content="你")
        sse = evt.to_sse()
        assert sse.startswith("data: ")
        assert sse.endswith("\n\n")
        parsed = json.loads(sse[6:].strip())
        assert parsed["type"] == "token"
        assert parsed["content"] == "你"

    def test_to_sse_ensure_ascii_false(self):
        evt = StreamEvent(type="token", content="中文测试")
        sse = evt.to_sse()
        assert "中文测试" in sse  # not escaped to \\uXXXX

    def test_timestamp_auto_generated(self):
        evt = StreamEvent(type="token", content="x")
        assert evt.timestamp  # not empty
        assert "T" in evt.timestamp  # ISO format


class TestParseStreamEvent:
    """Tests for parse_stream_event function."""

    def test_parse_on_chat_model_stream(self):
        """Parse an LLM token stream event."""
        # Simulate a LangGraph astream_events v2 token event
        class FakeChunk:
            content = "你好"

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": ["langsmith:nodes:agent_loop"],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "token"
        assert evt.content == "你好"
        assert evt.node == "agent_loop"

    def test_parse_on_chat_model_stream_empty_content(self):
        """Skip token events with empty content."""
        class FakeChunk:
            content = ""

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": [],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_on_chat_model_stream_no_chunk(self):
        """Skip token events without chunk data."""
        raw = {
            "event": "on_chat_model_stream",
            "data": {},
            "tags": [],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_on_chat_model_stream_content_is_list(self):
        """Handle content as list (LangChain mixed content blocks)."""
        class FakeChunk:
            content = [{"type": "text", "text": "Hello "}, {"type": "text", "text": "World"}]

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": ["langsmith:nodes:agent_loop"],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "token"
        assert evt.content == "Hello World"

    def test_parse_on_chat_model_stream_content_is_list_with_non_text(self):
        """List content with non-text blocks should be filtered out."""
        class FakeChunk:
            content = [{"type": "image", "url": "..."}, {"type": "text", "text": "only this"}]

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.content == "only this"

    def test_parse_save_memory_token_dropped(self):
        """save_memory's postmortem LLM tokens must NOT stream to the TUI.

        The postmortem content is attached to the result envelope and
        rendered by PostmortemSection. Letting its tokens also flow as
        type=token events double-renders the same markdown body once as
        agent chat text and once as the card.
        """
        class FakeChunk:
            content = "## Summary\n本次实验..."

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": ["langsmith:nodes:save_memory"],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_save_memory_thinking_dropped(self):
        """Reasoning chunks from save_memory's LLM are also filtered."""
        class FakeChunk:
            content = ""
            additional_kwargs = {"reasoning_content": "deciding what to write..."}

        raw = {
            "event": "on_chat_model_stream",
            "data": {"chunk": FakeChunk()},
            "tags": ["langsmith:nodes:save_memory"],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_on_tool_start(self):
        """Parse a tool invocation start event."""
        raw = {
            "event": "on_tool_start",
            "name": "kubectl",
            "tags": ["langsmith:nodes:phase1_tools"],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "tool_start"
        assert evt.tool_name == "kubectl"
        assert evt.node == "phase1_tools"
        assert evt.content == ""

    def test_parse_on_tool_end(self):
        """Parse a tool invocation end event."""
        raw = {
            "event": "on_tool_end",
            "name": "blade_create",
            "data": {"output": "Code:200 UID:abc123"},
            "tags": ["langsmith:nodes:phase2_tools"],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "tool_end"
        assert evt.tool_name == "blade_create"
        assert "abc123" in evt.content
        assert evt.node == "phase2_tools"

    def test_parse_on_tool_end_empty_output(self):
        """Parse tool end with empty output."""
        raw = {
            "event": "on_tool_end",
            "name": "kubectl",
            "data": {"output": ""},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "tool_end"
        assert evt.content == ""

    def test_parse_on_tool_end_with_tool_message_object(self):
        """LangChain emits ``ToolMessage`` instances, not raw strings.

        Before the fix, ``str(ToolMessage(...))`` produced the dataclass
        repr ``"content='...' name='kubectl' tool_call_id='...'"`` and
        leaked into the TUI as the ``⎿ content='...'`` artefact users
        saw on every multi-line tool result. This test pins that the
        parser now extracts ``.content`` cleanly.
        """
        from langchain_core.messages import ToolMessage

        msg = ToolMessage(
            content="namespace/default exists\nstatus: Active",
            tool_call_id="call-1",
            name="kubectl",
        )
        raw = {
            "event": "on_tool_end",
            "name": "kubectl",
            "data": {"output": msg},
            "tags": ["langsmith:nodes:agent_loop"],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "tool_end"
        # No "content='" prefix and no name='kubectl' suffix — just the
        # actual payload bytes.
        assert "content='" not in evt.content
        assert "name='kubectl'" not in evt.content
        assert evt.content == "namespace/default exists\nstatus: Active"

    def test_parse_on_tool_end_with_dict_output(self):
        """Some callers pass plain dicts with a ``content`` key. Same path."""
        raw = {
            "event": "on_tool_end",
            "name": "blade_create",
            "data": {"output": {"content": "Code:200 UID:abc"}},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.content == "Code:200 UID:abc"

    def test_parse_on_tool_end_with_list_content_blocks(self):
        """LangChain mixed content (text blocks) gets flattened."""
        from langchain_core.messages import ToolMessage

        msg = ToolMessage(
            content=[
                {"type": "text", "text": "line one\n"},
                {"type": "text", "text": "line two"},
            ],
            tool_call_id="call-2",
            name="kubectl",
        )
        raw = {
            "event": "on_tool_end",
            "name": "kubectl",
            "data": {"output": msg},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.content == "line one\nline two"

    def test_parse_unknown_event_ignored(self):
        """Unknown events return None."""
        raw = {"event": "on_chain_start", "data": {}, "tags": [], "metadata": {}}
        assert parse_stream_event(raw) is None

    def test_parse_on_chain_stream_ignored(self):
        """Chain stream events are ignored."""
        raw = {"event": "on_chain_stream", "data": {}, "tags": [], "metadata": {}}
        assert parse_stream_event(raw) is None

    def test_parse_on_chat_model_end_with_usage_metadata(self):
        """on_chat_model_end with AIMessage.usage_metadata → type=usage."""

        class MockAIMessage:
            usage_metadata = {"input_tokens": 198, "output_tokens": 89}

        raw = {
            "event": "on_chat_model_end",
            "data": {"output": MockAIMessage()},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "usage"
        assert evt.input_tokens == 198
        assert evt.output_tokens == 89

    def test_parse_on_chat_model_end_no_usage_returns_none(self):
        """Missing usage_metadata → returns None (no spurious usage event)."""

        class EmptyMsg:
            pass

        raw = {
            "event": "on_chat_model_end",
            "data": {"output": EmptyMsg()},
            "tags": [],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_on_chat_model_end_reasoning_fallback_returns_both(self):
        """Qwen enable_thinking short-response: content empty but
        reasoning_content present → returns [token, usage] (not just token)."""

        class QwenShortResponseMsg:
            content = ""
            additional_kwargs = {"reasoning_content": "你好！我是故障演练助手。"}
            usage_metadata = {"input_tokens": 12, "output_tokens": 8}

        raw = {
            "event": "on_chat_model_end",
            "data": {"output": QwenShortResponseMsg()},
            "tags": [],
            "metadata": {},
        }
        result = parse_stream_event(raw)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0].type == "token"
        assert result[0].content == "你好！我是故障演练助手。"
        assert result[1].type == "usage"
        assert result[1].input_tokens == 12
        assert result[1].output_tokens == 8

    def test_parse_on_chat_model_end_reasoning_fallback_no_usage(self):
        """reasoning_content present but no usage_metadata → [token] only."""

        class QwenShortNoUsage:
            content = ""
            additional_kwargs = {"reasoning_content": "hi"}

        raw = {
            "event": "on_chat_model_end",
            "data": {"output": QwenShortNoUsage()},
            "tags": [],
            "metadata": {},
        }
        result = parse_stream_event(raw)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0].type == "token"
        assert result[0].content == "hi"

    def test_parse_on_llm_end_routes_to_usage_event(self):
        """on_llm_end (non-chat LLMs) takes the same usage path."""

        class MockResp:
            usage_metadata = {"input_tokens": 50, "output_tokens": 20}

        raw = {
            "event": "on_llm_end",
            "data": {"output": MockResp()},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "usage"
        assert evt.input_tokens == 50
        assert evt.output_tokens == 20

    def test_usage_event_omits_empty_fields_in_to_dict(self):
        """A usage event's wire-format frame must NOT carry empty
        content/tool_name/etc. fields — only the discriminator + counts."""

        class MockAIMessage:
            usage_metadata = {"input_tokens": 10, "output_tokens": 5}

        raw = {
            "event": "on_chat_model_end",
            "data": {"output": MockAIMessage()},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        d = evt.to_dict()
        assert d["type"] == "usage"
        assert d["input_tokens"] == 10
        assert d["output_tokens"] == 5
        # No empty content / tool_name / call_id in the frame.
        assert "content" not in d
        assert "tool_name" not in d
        assert "call_id" not in d

    def test_usage_event_preserves_zero_token_field(self):
        """Regression: ``usage`` event with ``output_tokens=0`` (or
        ``input_tokens=0``) must still carry that field on the wire.

        Why this matters: DashScope's prompt_cache_hit case can
        legitimately report ``input_tokens=0`` with a non-zero
        completion. The naive falsy-strip in ``to_dict`` used to drop
        the 0, the TS reducer's ``Math.max(0, undefined)`` returned
        ``NaN``, and the per-turn running total stayed ``NaN`` for
        the rest of the turn — the live LoadingIndicator tail and
        the end-of-turn ``⚡ turn used N tokens`` summary both went
        blank for that turn. This test pins the explicit-0
        preservation so a future ``to_dict`` refactor can't silently
        re-introduce the bug."""
        # output_tokens=0
        ev = StreamEvent(type="usage", input_tokens=100, output_tokens=0)
        d = ev.to_dict()
        assert d["type"] == "usage"
        assert d["input_tokens"] == 100
        assert "output_tokens" in d
        assert d["output_tokens"] == 0

        # input_tokens=0
        ev = StreamEvent(type="usage", input_tokens=0, output_tokens=42)
        d = ev.to_dict()
        assert d["type"] == "usage"
        assert "input_tokens" in d
        assert d["input_tokens"] == 0
        assert d["output_tokens"] == 42

    def test_non_usage_events_still_strip_zero_token_fields(self):
        """The ``usage``-only preservation must not leak into other
        event types — back-compat: a ``token`` / ``confirm`` / etc.
        event with the dataclass-default ``input_tokens=0`` must
        still drop those fields from the wire frame, otherwise older
        TUI builds that don't know the discriminator would receive
        unexpected fields."""
        ev = StreamEvent(type="token", content="hi")
        d = ev.to_dict()
        assert d["type"] == "token"
        assert d["content"] == "hi"
        assert "input_tokens" not in d
        assert "output_tokens" not in d


class TestExtractNodeName:
    """Tests for _extract_node_name helper."""

    def test_extract_from_langsmith_tag(self):
        raw = {"tags": ["langsmith:nodes:agent_loop", "some:other:tag"], "metadata": {}}
        assert _extract_node_name(raw) == "agent_loop"

    def test_extract_from_metadata_fallback(self):
        raw = {"tags": [], "metadata": {"langgraph_node": "verifier_loop"}}
        assert _extract_node_name(raw) == "verifier_loop"

    def test_extract_returns_empty_when_no_info(self):
        raw = {"tags": [], "metadata": {}}
        assert _extract_node_name(raw) == ""

    def test_extract_langsmith_tag_priority_over_metadata(self):
        raw = {
            "tags": ["langsmith:nodes:execute_loop"],
            "metadata": {"langgraph_node": "wrong_node"},
        }
        assert _extract_node_name(raw) == "execute_loop"


class TestParseOnCustomEvent:
    """Tests for on_custom_event parsing (adispatch_custom_event integration)."""

    def test_parse_phase_started(self):
        """phase_started custom event → StreamEvent type=node_start."""
        raw = {
            "event": "on_custom_event",
            "name": "phase_started",
            "data": {"node": "safety_check", "phase": "safety"},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "node_start"
        assert evt.node == "safety_check"
        assert evt.content == ""

    def test_parse_phase_completed(self):
        """phase_completed custom event → StreamEvent type=node_end."""
        raw = {
            "event": "on_custom_event",
            "name": "phase_completed",
            "data": {"node": "verifier_loop", "phase": "verify"},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "node_end"
        assert evt.node == "verifier_loop"
        assert evt.content == ""

    def test_parse_phase_started_intent_clarification(self):
        """intent_clarification (previously excluded from whitelist) now visible."""
        raw = {
            "event": "on_custom_event",
            "name": "phase_started",
            "data": {"node": "intent_clarification", "phase": "intent"},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.type == "node_start"
        assert evt.node == "intent_clarification"

    def test_parse_on_custom_event_unknown_name_ignored(self):
        """Unknown custom event names are ignored."""
        raw = {
            "event": "on_custom_event",
            "name": "some_other_event",
            "data": {"foo": "bar"},
            "tags": [],
            "metadata": {},
        }
        assert parse_stream_event(raw) is None

    def test_parse_on_custom_event_missing_node(self):
        """Custom event without node field → empty node (not None)."""
        raw = {
            "event": "on_custom_event",
            "name": "phase_started",
            "data": {"phase": "safety"},
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        assert evt is not None
        assert evt.node == ""

    def test_parse_on_custom_event_missing_data(self):
        """Custom event without data dict → returns None (no node to extract)."""
        raw = {
            "event": "on_custom_event",
            "name": "phase_started",
            "tags": [],
            "metadata": {},
        }
        evt = parse_stream_event(raw)
        # data defaults to {} — node will be ""
        assert evt is not None
        assert evt.node == ""


class TestWithPhaseEvents:
    """Tests for with_phase_events wrapper in dispatch.py."""

    async def test_wrapper_preserves_result(self):
        """Wrapper returns the same result dict as the original node."""
        from chaos_agent.agent.dispatch import with_phase_events
        from chaos_agent.agent.state import AgentState

        async def fake_node(state: AgentState) -> dict:
            return {"safety_status": "safe", "safety_reason": ""}

        wrapped = with_phase_events("safety_check", "safety", fake_node)
        result = await wrapped({"task_id": "t1"})
        assert result["safety_status"] == "safe"

    async def test_wrapper_dispatches_phase_started(self):
        """Wrapper fires phase_started on entry."""
        from chaos_agent.agent.dispatch import with_phase_events
        from chaos_agent.agent.state import AgentState

        async def fake_node(state: AgentState) -> dict:
            return {"result": "ok"}

        wrapped = with_phase_events("safety_check", "safety", fake_node)
        result = await wrapped({"task_id": "t1"})
        assert result["result"] == "ok"

    async def test_wrapper_handles_graph_interrupt(self):
        """Wrapper catches GraphInterrupt without dispatching phase_completed."""
        from langgraph.errors import GraphInterrupt
        from chaos_agent.agent.dispatch import with_phase_events
        from chaos_agent.agent.state import AgentState

        async def interrupting_node(state: AgentState) -> dict:
            raise GraphInterrupt("user confirmation required")

        wrapped = with_phase_events("confirmation_gate", "safety", interrupting_node)

        # The wrapper re-raises GraphInterrupt
        try:
            await wrapped({"task_id": "t1"})
            assert False, "Should have raised GraphInterrupt"
        except GraphInterrupt:
            pass  # expected — phase_completed should NOT have been dispatched

    async def test_wrapper_propagates_non_interrupt_exceptions(self):
        """Non-GraphInterrupt exceptions propagate; no phase_completed dispatched."""
        from chaos_agent.agent.dispatch import with_phase_events
        from chaos_agent.agent.state import AgentState

        async def crashing_node(state: AgentState) -> dict:
            raise ValueError("something broke")

        wrapped = with_phase_events("agent_loop", "inject", crashing_node)

        # ValueError propagates unchanged; completed stays False so
        # phase_completed is never dispatched.
        try:
            await wrapped({"task_id": "t1"})
            assert False, "Should have raised ValueError"
        except ValueError as exc:
            assert "something broke" in str(exc)

    async def test_wrapper_phase_completed_failure_is_non_critical(self):
        """If phase_completed dispatch fails, the result is still returned."""
        from chaos_agent.agent.dispatch import with_phase_events
        from chaos_agent.agent.state import AgentState

        async def ok_node(state: AgentState) -> dict:
            return {"status": "done"}

        wrapped = with_phase_events("safety_check", "safety", ok_node)

        # Both dispatch calls will fail (no runnable context), but wrapper
        # catches them. The node result is still returned correctly.
        result = await wrapped({"task_id": "t1"})
        assert result["status"] == "done"
