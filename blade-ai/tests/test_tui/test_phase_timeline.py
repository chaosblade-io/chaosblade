"""Tests for ``PhaseTimelineRenderer``.

Two sets of contracts:

  * **Legacy path** (no coordinator): the renderer owns its own
    ``rich.live.Live`` block. This is what existed before PR-E2 and
    what unit tests / standalone construction paths still hit.
  * **Coordinator path** (PR-E2): the renderer paints into the
    ``LiveCoordinator``'s **header slot** (``OWNER_PHASE_TIMELINE``),
    which coexists with whichever region owner is active. Region owners
    keep their slot; the stepper sits in the header so the user always
    sees pipeline progress on top of the live body.
"""

from __future__ import annotations

import os
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_PHASE_TIMELINE,
    OWNER_THINKING,
)
from chaos_agent.tui.renderers.phase_timeline import (
    PhaseTimelineRenderer,
    phase_for_node,
)


def _term_size(cols: int, rows: int = 24) -> os.terminal_size:
    """Build a real ``os.terminal_size`` so the production code's
    ``.columns`` attribute access works under ``patch``.

    Plain tuples don't have ``.columns`` — using one of those caused the
    first round of failures when migrating these tests over from inline
    construction. Centralising it here makes the patches one-liners and
    keeps the helper out of every test body.
    """
    return os.terminal_size((cols, rows))


class _FakeLive:
    """Stand-in for rich.live.Live. Records lifecycle + updates."""

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.updates: List[object] = []

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)


def _make_renderer_with_coord() -> tuple[
    PhaseTimelineRenderer, LiveCoordinator, List[_FakeLive]
]:
    """Build a renderer wired through a coordinator with a fake Live factory."""
    created: List[_FakeLive] = []

    def _factory() -> _FakeLive:
        live = _FakeLive()
        created.append(live)
        return live  # type: ignore[return-value]

    coord = LiveCoordinator(MagicMock(), live_factory=_factory)  # type: ignore[arg-type]
    cc = MagicMock()
    renderer = PhaseTimelineRenderer(cc, coordinator=coord)
    return renderer, coord, created


# ---------------------------------------------------------------------------
# phase_for_node mapping (independent of renderer)
# ---------------------------------------------------------------------------


class TestPhaseForNode:
    def test_known_node_returns_phase(self) -> None:
        assert phase_for_node("safety_check") == "safety"
        assert phase_for_node("agent_loop") == "inject"
        assert phase_for_node("verifier_loop") == "verify"
        assert phase_for_node("recover_verifier_loop") == "recovery"

    def test_unknown_node_returns_empty(self) -> None:
        # intent_clarification is intentionally absent — chat-only
        # turns must not paint the 5-stage stepper.
        assert phase_for_node("intent_clarification") == ""
        assert phase_for_node("nonexistent_node") == ""
        assert phase_for_node("") == ""


# ---------------------------------------------------------------------------
# Legacy path — pre-PR-E2 behavior must be preserved when no coord is passed
# ---------------------------------------------------------------------------


class TestLegacyPath:
    """No coordinator → renderer owns its own Live block."""

    def test_idle_active_is_false(self) -> None:
        renderer = PhaseTimelineRenderer(MagicMock())
        assert renderer.active is False

    def test_start_in_wide_terminal_creates_live(self) -> None:
        renderer = PhaseTimelineRenderer(MagicMock())
        # Patch terminal size to wide (≥80 cols).
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
            try:
                assert renderer.active is True
                assert renderer._live is not None
            finally:
                renderer.stop()
        assert renderer.active is False

    def test_start_in_narrow_terminal_degrades(self) -> None:
        # Narrow terminal: silently no-op so the 5-stage stepper
        # doesn't wrap into a multi-line mess.
        renderer = PhaseTimelineRenderer(MagicMock())
        with patch("shutil.get_terminal_size", return_value=_term_size(60)):
            renderer.start()
        assert renderer.active is False

    def test_start_is_idempotent(self) -> None:
        # Calling start twice should NOT leak two Live blocks.
        renderer = PhaseTimelineRenderer(MagicMock())
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
            first_live = renderer._live
            renderer.start()
            second_live = renderer._live
            try:
                assert first_live is not second_live  # rebuilt cleanly
                assert renderer.active is True
            finally:
                renderer.stop()

    def test_stop_when_idle_is_noop(self) -> None:
        renderer = PhaseTimelineRenderer(MagicMock())
        renderer.stop()  # should not raise


# ---------------------------------------------------------------------------
# Coordinator path — header slot integration (PR-E2)
# ---------------------------------------------------------------------------


class TestCoordinatorPath:
    """When wired through a coordinator, the stepper lives in the header slot."""

    def test_idle_active_is_false(self) -> None:
        renderer, _, _ = _make_renderer_with_coord()
        assert renderer.active is False

    def test_start_acquires_header_slot(self) -> None:
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        try:
            # Header owner set; region untouched.
            assert coord.current_header_owner == OWNER_PHASE_TIMELINE
            assert coord.current_owner == ""
            # Single Live block created and started.
            assert len(created) == 1
            assert created[0].start_count == 1
            # The first paint went through update_header.
            assert created[0].updates  # non-empty
            # No local Live was used.
            assert renderer._live is None
            # Local flag mirrors ownership.
            assert renderer.active is True
        finally:
            renderer.stop()

    def test_start_in_narrow_terminal_does_not_acquire(self) -> None:
        # Same degradation as legacy path — stepper would wrap badly,
        # so we don't even acquire the header.
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(60)):
            renderer.start()
        assert renderer.active is False
        assert coord.current_header_owner == ""
        assert created == []  # No Live ever created

    def test_stop_releases_header_slot(self) -> None:
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        renderer.stop()
        assert renderer.active is False
        assert coord.current_header_owner == ""
        # When neither slot has content, Live tears down.
        assert created[0].stop_count == 1
        assert coord.is_active is False

    def test_phase_event_updates_header(self) -> None:
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        try:
            # First paint happened on start. Now fire a phase event.
            paints_before = len(created[0].updates)
            renderer.on_phase_event("safety_check", is_start=True)
            paints_after = len(created[0].updates)
            assert paints_after == paints_before + 1
        finally:
            renderer.stop()

    def test_unknown_node_does_not_paint(self) -> None:
        # An intent_clarification event must not advance the stepper.
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        try:
            paints_before = len(created[0].updates)
            renderer.on_phase_event("intent_clarification", is_start=True)
            assert len(created[0].updates) == paints_before
        finally:
            renderer.stop()

    def test_mark_failed_repaints(self) -> None:
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        try:
            paints_before = len(created[0].updates)
            renderer.mark_failed("inject")
            assert len(created[0].updates) == paints_before + 1
        finally:
            renderer.stop()

    def test_coexists_with_region_owner(self) -> None:
        # The whole point of the header slot: region owners keep
        # painting their body while the stepper stays on top.
        renderer, coord, created = _make_renderer_with_coord()
        # A region owner takes the body slot first.
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "thinking body")
        # Then phase_timeline starts on top.
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        try:
            # Single Live block — no second one was created.
            assert len(created) == 1
            assert created[0].start_count == 1
            assert created[0].stop_count == 0
            # Both slots active simultaneously.
            assert coord.current_owner == OWNER_THINKING
            assert coord.current_header_owner == OWNER_PHASE_TIMELINE
            # Latest paint should be a Group(header, region).
            from rich.console import Group

            last = created[0].updates[-1]
            assert isinstance(last, Group)
            # body half is the same string we updated with.
            assert "thinking body" in [r for r in last.renderables]
        finally:
            renderer.stop()
            coord.release(OWNER_THINKING)

    def test_release_header_keeps_region_alive(self) -> None:
        # If the stepper finishes (stop) but the body is still
        # painting, the Live must stay up.
        renderer, coord, created = _make_renderer_with_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        # Stepper finishes first.
        renderer.stop()
        # Live still alive; region still owned.
        assert coord.is_active is True
        assert coord.current_owner == OWNER_THINKING
        assert coord.current_header_owner == ""
        coord.release(OWNER_THINKING)

    def test_stop_is_idempotent(self) -> None:
        renderer, coord, _ = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
        renderer.stop()
        renderer.stop()  # second call should not raise
        renderer.stop()
        assert renderer.active is False

    def test_start_after_stop_reuses_coord(self) -> None:
        # A new task starts after a previous one finished — the
        # coordinator instance is the same; the stepper just acquires
        # the header slot again.
        renderer, coord, created = _make_renderer_with_coord()
        with patch("shutil.get_terminal_size", return_value=_term_size(120)):
            renderer.start()
            renderer.stop()
            renderer.start()
        try:
            # Two Live blocks created in total (one per start), both
            # cleanly paired with their stops.
            assert len(created) == 2
            assert created[0].start_count == 1
            assert created[0].stop_count == 1
            assert created[1].start_count == 1
            assert renderer.active is True
        finally:
            renderer.stop()
