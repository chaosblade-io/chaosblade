"""Tests for the goodbye-panel injection counter logic.

The counter must reflect *intent* — not raw input count. Chat / Q&A turns
end inside intent_clarification (graph emits ``conversation_turn``), so the
controller's ``last_turn_was_injection`` stays False. Inject turns run the
full pipeline (no ``conversation_turn``), flipping the flag to True.

These tests verify the controller-side flag rather than spinning up the
full TUI REPL — ``tui/app.py`` reads the property in its ``finally`` block.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.tui.controllers.conversation import ConversationController
from chaos_agent.tui.state import SessionState


class FakeRenderer:
    def error(self, message: str, task_id: str = "") -> None:
        pass

    def system(self, message: str) -> None:
        pass


class FakeBridge:
    """Swallow events; we only care about the controller's flag bookkeeping."""

    def __init__(self):
        self.processed: list[StreamEvent] = []

    async def process_stream_event(self, event: StreamEvent) -> None:
        self.processed.append(event)


class FakeRunner:
    """Yields a pre-canned event sequence from ``inject_stream``."""

    _tui_session_store = None

    def __init__(self, events: list[StreamEvent]):
        self._events = events

    def inject_stream(self, **_kwargs):
        async def _gen():
            for e in self._events:
                yield e
        return _gen()

    async def _wrap_stream_with_sidewrite(self, stream, session_id, source="pipeline"):
        async for evt in stream:
            yield evt

    async def cleanup(self) -> None:
        pass


def _make_controller(events: list[StreamEvent]) -> ConversationController:
    controller = ConversationController(
        SessionState(), FakeRunner(events), FakeRenderer()
    )
    # Replace EventBridge to keep the test focused on flag bookkeeping.
    controller._bridge = FakeBridge()
    return controller


class TestLastTurnWasInjection:
    """Mirrors the gate ``tui/app.py`` uses to bump injection_count."""

    @pytest.mark.asyncio
    async def test_chat_turn_does_not_count(self):
        """Graph ended in intent_clarification → conversation_turn event →
        flag stays False so app.py won't bump injection_count."""
        events = [
            StreamEvent(type="token", content="你好！", node="intent_clarification",
                        task_id="t-chat"),
            StreamEvent(type="conversation_turn", task_id="t-chat"),
        ]
        controller = _make_controller(events)

        await controller.handle_input("你好")

        assert controller.last_turn_was_injection is False
        assert controller.last_turn_failed is False
        assert controller.in_conversation is True

    @pytest.mark.asyncio
    async def test_inject_turn_counts(self):
        """Full pipeline ran (no conversation_turn, ends with result) →
        flag flips True, app.py bumps injection_count + injection_success."""
        events = [
            StreamEvent(type="node_start", node="agent_loop", task_id="t-inj"),
            StreamEvent(type="result", content="ok", task_id="t-inj"),
        ]
        controller = _make_controller(events)

        await controller.handle_input("给 default 注入 cpu 满载")

        assert controller.last_turn_was_injection is True
        assert controller.last_turn_failed is False
        assert controller.in_conversation is False

    @pytest.mark.asyncio
    async def test_inject_pipeline_error_with_conversation_turn_counts(self):
        """Regression: when inject fails inside the pipeline (e.g. baseline_capture
        error or safety rejection), the runner emits BOTH ``error`` and
        ``conversation_turn`` so the TUI stays interactive. The controller
        must still classify the turn as an injection attempt.
        """
        events = [
            StreamEvent(type="error", content="baseline failed", task_id="t-fail"),
            StreamEvent(type="conversation_turn", task_id="t-fail"),
        ]
        controller = _make_controller(events)

        await controller.handle_input("注入 cpu 满载到不存在的命名空间")

        # conversation mode is entered (matches runner's intent), but the
        # turn is still counted as an injection attempt so the goodbye
        # panel reflects reality, and as failed since runner emitted error.
        assert controller.in_conversation is True
        assert controller.last_turn_was_injection is True
        assert controller.last_turn_failed is True

    @pytest.mark.asyncio
    async def test_failed_injection_simulates_app_finally_path(self):
        """When the stream raises, app.py sets injection_failed=True and bumps
        injection_count + injection_fail in its finally block. We replicate
        that bookkeeping here to lock the contract."""

        class BoomRunner:
            _tui_session_store = None

            def inject_stream(self, **_kwargs):
                async def _gen():
                    raise RuntimeError("boom")
                    yield  # pragma: no cover
                return _gen()

            async def _wrap_stream_with_sidewrite(self, stream, session_id, source="pipeline"):
                async for evt in stream:
                    yield evt

            async def cleanup(self) -> None:
                pass

        controller = ConversationController(
            SessionState(), BoomRunner(), FakeRenderer()
        )
        controller._bridge = FakeBridge()

        # _start_stream catches the exception internally and renders an error;
        # what matters is the flag after handle_input returns.
        await controller.handle_input("inject something that explodes")

        # The pipeline did NOT produce conversation_turn before failing,
        # so app.py's finally block (injection_failed OR last_turn_was_injection)
        # would still bump the counter because injection_failed=True.
        # Here we assert the controller flag specifically: a stream error
        # before conversation_turn falls into the "full pipeline" branch.
        assert controller.last_turn_was_injection is True
