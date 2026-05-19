"""Tests for TaskTracker controller."""

import pytest

from chaos_agent.tui.controllers.task_tracker import TaskTracker
from chaos_agent.tui.state import SessionState


class FakeRenderer:
    def __init__(self):
        self.system_messages: list[str] = []
        self.interrupted_tasks_calls: list[list[dict]] = []

    def system(self, message: str) -> None:
        self.system_messages.append(message)

    def interrupted_tasks(self, tasks: list[dict]) -> None:
        self.interrupted_tasks_calls.append(tasks)


class FakeRunner:
    def __init__(self, interrupted=None):
        self._interrupted = interrupted or []

    async def list_interrupted_tasks(self):
        return self._interrupted


class TestTaskTracker:
    def setup_method(self):
        self.state = SessionState()
        self.renderer = FakeRenderer()
        self.tracker = TaskTracker(self.state, FakeRunner(), self.renderer)

    def test_initially_not_active(self):
        assert self.tracker.injection_active is False

    def test_mark_active(self):
        self.tracker.mark_injection_active()
        assert self.tracker.injection_active is True
        assert self.state.active_task_count == 1

    def test_mark_done(self):
        self.tracker.mark_injection_active()
        self.tracker.mark_injection_done()
        assert self.tracker.injection_active is False
        assert self.state.active_task_count == 0

    def test_state_notification_on_active(self):
        notifications = []
        self.state.add_listener(lambda s, f: notifications.append(f))
        self.tracker.mark_injection_active()
        assert "active_task_count" in notifications


class TestRecoverInterruptedTasks:
    @pytest.mark.asyncio
    async def test_no_interrupted_tasks_still_renders_panel(self):
        state = SessionState()
        renderer = FakeRenderer()
        tracker = TaskTracker(state, FakeRunner(interrupted=[]), renderer)
        await tracker.recover_interrupted_tasks()
        # Even with no interrupted tasks, the panel is rendered with empty list
        assert len(renderer.interrupted_tasks_calls) == 1
        assert renderer.interrupted_tasks_calls[0] == []

    @pytest.mark.asyncio
    async def test_confirmation_interrupt_rendered(self):
        state = SessionState()
        renderer = FakeRenderer()
        runner = FakeRunner(
            interrupted=[
                {
                    "task_id": "t-1",
                    "interrupt_info": {"type": "confirmation"},
                    "next_nodes": ["confirmation_gate"],
                }
            ]
        )
        tracker = TaskTracker(state, runner, renderer)
        await tracker.recover_interrupted_tasks()
        assert len(renderer.interrupted_tasks_calls) == 1
        tasks = renderer.interrupted_tasks_calls[0]
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "t-1"
        assert tasks[0]["interrupt_info"]["type"] == "confirmation"

    @pytest.mark.asyncio
    async def test_multiple_interrupted_tasks_single_call(self):
        state = SessionState()
        renderer = FakeRenderer()
        runner = FakeRunner(
            interrupted=[
                {
                    "task_id": "t-1",
                    "interrupt_info": {"type": "confirmation"},
                    "next_nodes": ["confirmation_gate"],
                },
                {
                    "task_id": "t-2",
                    "interrupt_info": {"type": "question"},
                    "next_nodes": [],
                },
                {
                    "task_id": "t-3",
                    "interrupt_info": {},
                    "next_nodes": ["inject_node"],
                },
            ]
        )
        tracker = TaskTracker(state, runner, renderer)
        await tracker.recover_interrupted_tasks()
        assert len(renderer.interrupted_tasks_calls) == 1
        tasks = renderer.interrupted_tasks_calls[0]
        assert len(tasks) == 3
        assert tasks[0]["task_id"] == "t-1"
        assert tasks[1]["task_id"] == "t-2"
        assert tasks[2]["task_id"] == "t-3"
