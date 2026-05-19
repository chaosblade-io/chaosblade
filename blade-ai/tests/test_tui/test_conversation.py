"""Tests for ConversationController — cancel/recover wiring."""

import pytest

from chaos_agent.tui.controllers.conversation import ConversationController
from chaos_agent.tui.state import SessionState


class FakeRenderer:
    def __init__(self):
        self.errors: list[str] = []
        self.system_messages: list[str] = []

    def error(self, message: str, task_id: str = "") -> None:
        self.errors.append(message)

    def system(self, message: str) -> None:
        self.system_messages.append(message)


class FakeRunner:
    def __init__(self):
        self.recover_calls: list[str] = []

    async def recover(self, task_id: str = "") -> None:
        self.recover_calls.append(task_id)

    async def cleanup(self) -> None:
        pass


class TestCancelRecover:
    @pytest.mark.asyncio
    async def test_cancel_with_no_prior_task_is_noop(self):
        runner = FakeRunner()
        controller = ConversationController(SessionState(), runner, FakeRenderer())
        await controller.cancel()
        assert runner.recover_calls == []

    @pytest.mark.asyncio
    async def test_cancel_uses_last_task_id(self):
        runner = FakeRunner()
        controller = ConversationController(SessionState(), runner, FakeRenderer())
        controller._last_task_id = "t-42"
        await controller.cancel()
        assert runner.recover_calls == ["t-42"]

    @pytest.mark.asyncio
    async def test_cancel_skips_pending_sentinel(self):
        runner = FakeRunner()
        controller = ConversationController(SessionState(), runner, FakeRenderer())
        controller._last_task_id = "pending"
        await controller.cancel()
        assert runner.recover_calls == []

    def test_last_task_id_property(self):
        controller = ConversationController(SessionState(), FakeRunner(), FakeRenderer())
        assert controller.last_task_id == ""
        controller._last_task_id = "t-9"
        assert controller.last_task_id == "t-9"
