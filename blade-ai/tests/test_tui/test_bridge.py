"""Tests for EventBridge — StreamEvent → TUIEvent conversion."""

import pytest

from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.tui.bridge import (
    EventBridge,
    _parse_interrupt_content,
    _safe_json_parse,
)


class FakeRenderer:
    def __init__(self):
        self.dispatched: list = []

    async def dispatch(self, event):
        self.dispatched.append(event)


class TestParseInterruptContent:
    def test_empty_content_returns_confirmation(self):
        result = _parse_interrupt_content("")
        assert result["type"] == "confirmation"

    def test_none_content_returns_confirmation(self):
        result = _parse_interrupt_content(None)
        assert result["type"] == "confirmation"

    def test_json_question(self):
        content = '{"type": "question", "content": "What namespace?"}'
        result = _parse_interrupt_content(content)
        assert result["type"] == "question"
        assert result["content"] == "What namespace?"

    def test_json_confirmation(self):
        content = '{"type": "confirmation", "plan_summary": "inject CPU"}'
        result = _parse_interrupt_content(content)
        assert result["type"] == "confirmation"

    def test_plain_text_fallback(self):
        result = _parse_interrupt_content("inject CPU fault")
        assert result["type"] == "confirmation"
        assert result["plan_summary"] == "inject CPU fault"


class TestSafeJsonParse:
    def test_valid_json(self):
        result = _safe_json_parse('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json(self):
        result = _safe_json_parse("not json")
        assert result == "not json"

    def test_empty_string(self):
        result = _safe_json_parse("")
        assert result == ""


class TestEventBridgeConversion:
    """Test _convert_stream_event mapping."""

    def setup_method(self):
        self.renderer = FakeRenderer()
        self.bridge = EventBridge(renderer=self.renderer)

    def test_token_event(self):
        event = StreamEvent(type="token", content="hello", node="agent_loop")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import TokenReceived
        assert isinstance(tui_event, TokenReceived)
        assert tui_event.content == "hello"

    def test_thinking_event(self):
        event = StreamEvent(type="thinking", content="reasoning...", node="agent_loop")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import ThinkingReceived
        assert isinstance(tui_event, ThinkingReceived)

    def test_tool_start_event(self):
        event = StreamEvent(type="tool_start", tool_name="blade_create", node="execute_loop")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import ToolStarted
        assert isinstance(tui_event, ToolStarted)
        assert tui_event.tool_name == "blade_create"

    def test_tool_end_event(self):
        event = StreamEvent(type="tool_end", tool_name="blade_create", content="uid-123")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import ToolCompleted
        assert isinstance(tui_event, ToolCompleted)

    def test_confirm_event(self):
        event = StreamEvent(type="confirm", content='{"type": "confirmation"}', task_id="task-1")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import InterruptRequired
        assert isinstance(tui_event, InterruptRequired)

    def test_result_event(self):
        event = StreamEvent(type="result", content='{"task_id": "t1"}', task_id="t1")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import TaskResult
        assert isinstance(tui_event, TaskResult)

    def test_error_event(self):
        event = StreamEvent(type="error", content="something failed", task_id="t1")
        tui_event = self.bridge._convert_stream_event(event)
        from chaos_agent.tui.events import TaskError
        assert isinstance(tui_event, TaskError)

    def test_unknown_event_returns_none(self):
        event = StreamEvent(type="unknown_type")
        tui_event = self.bridge._convert_stream_event(event)
        assert tui_event is None


class TestProcessStreamEvent:
    @pytest.mark.asyncio
    async def test_process_dispatches_to_renderer(self):
        renderer = FakeRenderer()
        bridge = EventBridge(renderer=renderer)
        await bridge.process_stream_event(StreamEvent(type="token", content="x"))
        assert len(renderer.dispatched) == 1

    @pytest.mark.asyncio
    async def test_unknown_event_does_not_dispatch(self):
        renderer = FakeRenderer()
        bridge = EventBridge(renderer=renderer)
        await bridge.process_stream_event(StreamEvent(type="unknown"))
        assert renderer.dispatched == []
