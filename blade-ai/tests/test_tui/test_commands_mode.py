"""Tests for ``/mode`` slash dispatch (PR-D1 §17.1).

The dispatcher pulls in conversation/config_store/renderer/runner as
collaborators, but ``/mode`` only ever touches ``state`` and ``renderer``.
Rather than wire the full app stack we stub the unused collaborators with
``SimpleNamespace`` and a tiny renderer that captures every system line —
that keeps the test fast and the failure messages readable.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from chaos_agent.tui.controllers.commands import CommandDispatcher
from chaos_agent.tui.state import DisplayMode, SessionState


class _CapturingRenderer:
    """Implements only ``system`` — the rest is poked via SimpleNamespace.

    The CommandDispatcher's ``/mode`` handlers only ever call
    ``renderer.system(...)``; we don't need any of the other surfaces.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def system(self, msg: str) -> None:
        self.messages.append(msg)


def _make_dispatcher(state: SessionState) -> tuple[CommandDispatcher, _CapturingRenderer]:
    renderer = _CapturingRenderer()
    conversation = SimpleNamespace(
        in_conversation=False,
        is_streaming=False,
    )
    config_store = SimpleNamespace()
    dispatcher = CommandDispatcher(
        state=state,
        conversation=conversation,
        config_store=config_store,
        renderer=renderer,
    )
    return dispatcher, renderer


class TestModeCycle:
    def test_bare_mode_cycles_from_working_to_dense(self):
        state = SessionState()
        assert state.display_mode == DisplayMode.WORKING
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/mode"))
        assert state.display_mode == DisplayMode.DENSE
        # Announcement names the new mode and includes the human label.
        assert any("dense" in msg and "全开" in msg for msg in renderer.messages)

    def test_bare_mode_cycles_through_three_modes(self):
        state = SessionState()
        dispatcher, _ = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/mode"))  # working → dense
        asyncio.run(dispatcher.dispatch("/mode"))  # dense → calm
        asyncio.run(dispatcher.dispatch("/mode"))  # calm → working
        assert state.display_mode == DisplayMode.WORKING


class TestModeNamedSubcommands:
    @pytest.mark.parametrize(
        "command,expected",
        [
            ("/mode calm", DisplayMode.CALM),
            ("/mode working", DisplayMode.WORKING),
            ("/mode dense", DisplayMode.DENSE),
        ],
    )
    def test_named_subcommand_sets_mode(self, command: str, expected: DisplayMode):
        state = SessionState()
        # Start somewhere else so each test moves the needle.
        state.display_mode = (
            DisplayMode.DENSE if expected != DisplayMode.DENSE else DisplayMode.CALM
        )
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch(command))
        assert state.display_mode == expected
        assert any(expected.value in msg for msg in renderer.messages)


class TestModeInvalidArg:
    def test_unknown_mode_does_not_change_state(self):
        state = SessionState()
        before = state.display_mode
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/mode bogus"))
        assert state.display_mode == before
        # Usage line surfaces the valid set so the user can recover.
        joined = "\n".join(renderer.messages)
        assert "calm" in joined
        assert "working" in joined
        assert "dense" in joined


class TestModeStreamingSafe:
    """``/mode`` must work mid-stream — dropping density on a long-running
    task is exactly when a user reaches for it."""

    def test_mode_dispatch_during_streaming(self):
        state = SessionState()
        state.is_streaming = True
        dispatcher, renderer = _make_dispatcher(state)
        asyncio.run(dispatcher.dispatch("/mode calm"))
        assert state.display_mode == DisplayMode.CALM
        # No "请等待" block message.
        assert not any("请等待" in msg for msg in renderer.messages)
