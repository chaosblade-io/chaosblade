"""Tests for TUI event dataclasses."""

from chaos_agent.tui.events import (
    InterruptRequired,
    TaskResult,
    TokenReceived,
    TUIEvent,
)


class TestTokenReceived:
    def test_creation(self):
        event = TokenReceived(content="hello", node="agent_loop")
        assert event.content == "hello"
        assert event.node == "agent_loop"

    def test_default_node(self):
        event = TokenReceived(content="test")
        assert event.node == ""

    def test_subclass_of_base(self):
        assert issubclass(TokenReceived, TUIEvent)


class TestInterruptRequired:
    def test_confirmation_interrupt(self):
        info = {"type": "confirmation", "plan_summary": "inject CPU fault"}
        event = InterruptRequired(interrupt_info=info, task_id="task-123")
        assert event.interrupt_info["type"] == "confirmation"
        assert event.task_id == "task-123"

    def test_question_interrupt(self):
        info = {"type": "question", "content": "What namespace?"}
        event = InterruptRequired(interrupt_info=info)
        assert event.interrupt_info["type"] == "question"

    def test_default_interrupt_info_is_dict(self):
        event = InterruptRequired()
        assert event.interrupt_info == {}


class TestTaskResult:
    def test_creation(self):
        data = {"task_id": "task-1", "result": "injected"}
        event = TaskResult(data=data, task_id="task-1")
        assert event.data == data
        assert event.task_id == "task-1"

    def test_default_data_is_dict(self):
        event = TaskResult()
        assert event.data == {}
