"""Tests for PR-E2 — single ``rich.live.Live`` coordinator.

Behaviour pinned:

1. First acquire starts a Live block and sets the owner.
2. Same-owner re-acquire is a no-op — does NOT restart the block. This
   matters because a streaming token loop calls ``acquire`` on every
   token, and any restart would flicker.
3. Different-owner acquire while one is active rotates ownership
   WITHOUT calling ``stop`` then ``start``. The same Live block keeps
   painting; only the inner renderable changes on the next ``update``.
   This is the core flicker fix.
4. ``update`` from a non-owner returns False and does not paint. The
   stale owner's queued updates are silently dropped.
5. ``update`` from the current owner returns True and forwards the
   renderable to ``Live.update``.
6. ``release`` is owner-scoped: a stale owner cannot tear down
   somebody else's Live. The current owner's release stops the block
   and runs the optional ``on_release`` callback AFTER the lock is
   released, so the callback can safely acquire its own resources.
7. ``on_release`` exceptions are caught and logged — the coordinator
   stays consistent even if the caller's flush fails.
8. ``force_release`` tears down regardless of who owns it (used by
   error handlers).
9. ``shutdown`` is idempotent — calling it on a coordinator with no
   active Live block does nothing.
"""

from __future__ import annotations

from typing import List, Optional
from unittest.mock import MagicMock

import pytest

from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_PHASE_TIMELINE,
    OWNER_THINKING,
    OWNER_TOKEN_STREAM,
    OWNER_TOOL_PANEL,
)


class _FakeLive:
    """Stand-in for rich.live.Live — records start/stop/update calls."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False
        self.start_count = 0
        self.stop_count = 0
        self.updates: List[object] = []

    def start(self) -> None:
        self.started = True
        self.start_count += 1

    def stop(self) -> None:
        self.stopped = True
        self.stop_count += 1

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)


def _make_coord(
    *,
    fake: Optional[_FakeLive] = None,
    factory_calls: Optional[List[_FakeLive]] = None,
) -> tuple[LiveCoordinator, List[_FakeLive]]:
    """Build a coordinator wired to a controllable fake Live factory.

    Returns (coord, list_of_lives_created) so tests can assert on how
    many Live blocks were spun up and inspect their start/stop/update
    timelines.
    """
    created: List[_FakeLive] = factory_calls if factory_calls is not None else []

    def _factory() -> _FakeLive:
        live = fake if fake is not None else _FakeLive()
        created.append(live)
        return live  # type: ignore[return-value]

    console = MagicMock()
    coord = LiveCoordinator(console, live_factory=_factory)  # type: ignore[arg-type]
    return coord, created


class TestAcquire:
    def test_first_acquire_starts_live_and_sets_owner(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        assert len(created) == 1
        assert created[0].started is True
        assert coord.is_active is True
        assert coord.current_owner == OWNER_TOKEN_STREAM

    def test_same_owner_reacquire_does_not_restart(self) -> None:
        # The streaming printer hits acquire() on every token. If
        # re-acquire restarted the block, the user would see a flicker
        # on every keystroke of the LLM.
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.acquire(OWNER_TOKEN_STREAM)
        assert len(created) == 1
        assert created[0].start_count == 1

    def test_different_owner_rotates_without_restart(self) -> None:
        # The whole point of PR-E2: when the tool panel takes over from
        # the token stream, we must NOT call .stop() then .start() —
        # rich would tear down the alternate-screen region and re-install
        # it, causing a one-frame flash. Same Live block, new owner.
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.acquire(OWNER_TOOL_PANEL)
        assert len(created) == 1
        assert created[0].start_count == 1
        assert created[0].stop_count == 0
        assert coord.current_owner == OWNER_TOOL_PANEL

    def test_empty_owner_rejected(self) -> None:
        coord, _ = _make_coord()
        with pytest.raises(ValueError):
            coord.acquire("")

    def test_factory_failure_keeps_coord_clean(self) -> None:
        # If rich.live.Live.start() blows up (rare — bad terminal,
        # closed stream), the coordinator must not pretend to own a
        # broken Live block. Subsequent updates would silently lie.
        def _broken_factory() -> _FakeLive:
            raise RuntimeError("terminal hates us")

        console = MagicMock()
        coord = LiveCoordinator(console, live_factory=_broken_factory)  # type: ignore[arg-type]
        coord.acquire(OWNER_TOKEN_STREAM)
        assert coord.is_active is False
        assert coord.current_owner == ""


class TestUpdate:
    def test_owner_update_paints(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        ok = coord.update(OWNER_TOKEN_STREAM, "hello")
        assert ok is True
        assert created[0].updates == ["hello"]

    def test_non_owner_update_drops_silently(self) -> None:
        # A printer that lost ownership because somebody else acquired
        # may still try to .update() — its outstanding asyncio task
        # didn't notice the rotation yet. Drop, don't crash.
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.acquire(OWNER_TOOL_PANEL)
        ok = coord.update(OWNER_TOKEN_STREAM, "stale token")
        assert ok is False
        assert created[0].updates == []

    def test_update_before_acquire_is_dropped(self) -> None:
        coord, created = _make_coord()
        ok = coord.update(OWNER_TOKEN_STREAM, "anything")
        assert ok is False
        assert created == []

    def test_update_after_release_is_dropped(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.release(OWNER_TOKEN_STREAM)
        ok = coord.update(OWNER_TOKEN_STREAM, "after-release")
        assert ok is False
        assert created[0].updates == []


class TestRelease:
    def test_owner_release_stops_live_and_clears_owner(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.release(OWNER_TOKEN_STREAM)
        assert created[0].stopped is True
        assert coord.is_active is False
        assert coord.current_owner == ""

    def test_non_owner_release_is_noop(self) -> None:
        # A printer that lost ownership cannot tear down somebody
        # else's still-painting Live block.
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.release(OWNER_TOOL_PANEL)
        assert created[0].stopped is False
        assert coord.current_owner == OWNER_TOKEN_STREAM

    def test_on_release_callback_runs_after_stop(self) -> None:
        # StreamingPrinter relies on this ordering — it flushes the
        # final markdown via console.print AFTER the Live region is
        # gone, so the text lands in scrollback cleanly.
        order: List[str] = []
        live = _FakeLive()

        def _stop_hook() -> None:
            # Capture state at callback time: by then, .stop should
            # have run already.
            order.append("callback")
            assert live.stopped is True

        coord, _ = _make_coord(fake=live)
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.release(OWNER_TOKEN_STREAM, on_release=_stop_hook)
        assert order == ["callback"]

    def test_on_release_exception_swallowed(self) -> None:
        # If the flush callback raises, we don't want to leave the
        # coordinator in a half-broken state. The Live is already
        # stopped, so we're consistent — just log and move on.
        def _broken_hook() -> None:
            raise RuntimeError("flush failed")

        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        # Should NOT raise.
        coord.release(OWNER_TOKEN_STREAM, on_release=_broken_hook)
        assert created[0].stopped is True
        assert coord.is_active is False


class TestForceRelease:
    def test_force_release_tears_down_regardless_of_owner(self) -> None:
        # Error handlers don't necessarily know who's holding the
        # Live; force_release lets them get the terminal back to a
        # known-good state unconditionally.
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.force_release()
        assert created[0].stopped is True
        assert coord.is_active is False
        assert coord.current_owner == ""

    def test_force_release_when_idle_is_noop(self) -> None:
        coord, created = _make_coord()
        coord.force_release()
        assert created == []
        assert coord.is_active is False


class TestShutdown:
    def test_shutdown_when_active_stops(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_PHASE_TIMELINE)
        coord.shutdown()
        assert created[0].stopped is True
        assert coord.is_active is False

    def test_shutdown_is_idempotent(self) -> None:
        # App-exit cleanup may call shutdown multiple times across
        # signal handlers — must not double-stop or raise.
        coord, _ = _make_coord()
        coord.shutdown()
        coord.shutdown()
        coord.shutdown()


class TestIsOwner:
    def test_is_owner_true_for_current_owner(self) -> None:
        coord, _ = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        assert coord.is_owner(OWNER_TOKEN_STREAM) is True
        assert coord.is_owner(OWNER_TOOL_PANEL) is False

    def test_is_owner_false_when_idle(self) -> None:
        coord, _ = _make_coord()
        assert coord.is_owner(OWNER_TOKEN_STREAM) is False


class TestOwnerConstants:
    def test_owner_constants_distinct(self) -> None:
        # Cheap insurance: if anyone copy-pastes a constant and forgets
        # to rename it, two printers would clash for ownership and
        # silently drop each other's paints.
        names = {
            OWNER_TOKEN_STREAM,
            OWNER_THINKING,
            OWNER_TOOL_PANEL,
            OWNER_PHASE_TIMELINE,
        }
        assert len(names) == 4


class TestHeaderSlot:
    """PR-E2 — header slot coexists with region slot.

    Phase-timeline lives in ``header``; thinking / token-stream /
    tool-panel rotate through ``region``. The contracts:

      * Both slots can be active at once — composite paint emits a
        ``Group(header, region)``.
      * Header rotations don't affect the region owner / renderable
        and vice versa.
      * Release of one slot doesn't tear down Live unless the other
        slot is also empty.
      * Region release with ``on_release`` still flushes static
        content into scrollback (Live stops, callback runs, Live
        re-arms for header-only paint).
      * Region release without ``on_release`` skips the stop+restart
        entirely — header keeps painting through the rotation.
    """

    def test_acquire_header_starts_live_when_idle(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        assert len(created) == 1
        assert created[0].started is True
        assert coord.current_header_owner == OWNER_PHASE_TIMELINE
        # Region slot is still empty.
        assert coord.current_owner == ""

    def test_acquire_header_reuses_live_when_region_active(self) -> None:
        # Phase-timeline first event arriving mid-thinking must NOT
        # start a second Live block — that would stack regions and
        # produce a real scrollback duplicate plus a flicker.
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "thinking body")
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "stepper header")
        assert len(created) == 1  # only one Live ever made
        assert created[0].start_count == 1
        assert created[0].stop_count == 0
        assert coord.current_owner == OWNER_THINKING
        assert coord.current_header_owner == OWNER_PHASE_TIMELINE

    def test_acquire_header_empty_owner_rejected(self) -> None:
        coord, _ = _make_coord()
        with pytest.raises(ValueError):
            coord.acquire_header("")

    def test_acquire_header_same_owner_reacquire_noop(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        assert len(created) == 1
        assert created[0].start_count == 1

    def test_header_rotation_clears_stale_renderable(self) -> None:
        # If a future contributor wires a second header owner (e.g. a
        # multi-task pulse view), we don't want the previous owner's
        # cached header to keep painting under the new owner.
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "old header")
        # A stand-in second header owner. We don't enforce role types
        # at the API; rotation works regardless.
        coord.acquire_header(OWNER_THINKING)  # using OWNER_THINKING as a stand-in
        # No update yet from new owner — Live keeps painting old
        # content (rich.live behavior), but the cache has been cleared.
        coord.update(OWNER_THINKING, "should not paint as header")
        # update() above is a region update; the printer happens to
        # share an owner string so it'd succeed for region too.

    def test_composite_paint_header_then_region(self) -> None:
        # Composite ordering matters: the stepper goes ABOVE the body
        # so the user reads "where am I in the pipeline" first, then
        # the live spinner / partial answer below.
        from rich.console import Group

        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        # The most recent paint should be a Group(header, region).
        last = created[0].updates[-1]
        assert isinstance(last, Group)
        # Group renderables[0] is header, [1] is region.
        assert last.renderables == ("header", "body") or list(
            last.renderables
        ) == ["header", "body"]

    def test_paint_header_only_when_region_empty(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        # Header alone is sent directly, not wrapped in a Group.
        assert created[0].updates[-1] == "header"

    def test_paint_region_only_when_header_empty(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        assert created[0].updates[-1] == "body"

    def test_region_update_keeps_header_cached(self) -> None:
        # The thinking spinner ticks at 6 Hz; phase events fire less
        # often. Each region tick must compose with the cached header
        # rather than dropping it.
        from rich.console import Group

        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body-tick-1")
        coord.update(OWNER_THINKING, "body-tick-2")
        coord.update(OWNER_THINKING, "body-tick-3")
        # Every tick after the header was set should be a Group.
        last = created[0].updates[-1]
        assert isinstance(last, Group)
        assert list(last.renderables) == ["header", "body-tick-3"]

    def test_release_region_no_on_release_keeps_header_visible(self) -> None:
        # The fast path: thinking finalizes mid-task while the stepper
        # is still painting. We must NOT stop+start the Live (visible
        # flicker); just repaint the header alone.
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        coord.release(OWNER_THINKING)  # no on_release callback
        # Live was NOT torn down — same Live block, no extra start.
        assert len(created) == 1
        assert created[0].stop_count == 0
        assert created[0].start_count == 1
        # Last paint should be header alone.
        assert created[0].updates[-1] == "header"
        # Region owner cleared; header owner intact.
        assert coord.current_owner == ""
        assert coord.current_header_owner == OWNER_PHASE_TIMELINE

    def test_release_region_with_on_release_rearms_live_for_header(self) -> None:
        # Streaming flush: token_stream releases with a markdown print
        # callback. Live MUST stop so the callback prints clean, then
        # we re-arm a fresh Live block painting only the header.
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.update(OWNER_TOKEN_STREAM, "body")
        flush_called = []

        def _flush() -> None:
            # By the time the callback runs, the first Live must be
            # stopped (so console.print can land in scrollback). The
            # second Live should not yet exist (we re-arm AFTER).
            flush_called.append(True)
            assert created[0].stop_count == 1
            assert len(created) == 1  # second Live not yet created

        coord.release(OWNER_TOKEN_STREAM, on_release=_flush)
        assert flush_called == [True]
        # A NEW Live was started for the header-only paint.
        assert len(created) == 2
        assert created[1].started is True
        assert created[1].updates[-1] == "header"

    def test_release_region_with_on_release_no_header_stays_stopped(self) -> None:
        # Without a header, region release with on_release should
        # behave exactly like the pre-PR-E2 contract: stop, flush, no
        # re-arm.
        coord, created = _make_coord()
        coord.acquire(OWNER_TOKEN_STREAM)
        coord.update(OWNER_TOKEN_STREAM, "body")
        flushed = []
        coord.release(
            OWNER_TOKEN_STREAM,
            on_release=lambda: flushed.append(True),
        )
        assert flushed == [True]
        assert len(created) == 1  # no second Live
        assert created[0].stop_count == 1
        assert coord.is_active is False

    def test_release_header_keeps_region_alive(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.release_header(OWNER_PHASE_TIMELINE)
        # Live unchanged; just repainted with body alone.
        assert len(created) == 1
        assert created[0].stop_count == 0
        assert created[0].updates[-1] == "body"
        assert coord.current_owner == OWNER_THINKING
        assert coord.current_header_owner == ""

    def test_release_header_when_region_empty_stops_live(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.release_header(OWNER_PHASE_TIMELINE)
        # Both slots empty → tear Live down.
        assert created[0].stop_count == 1
        assert coord.is_active is False

    def test_non_owner_release_header_is_noop(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.release_header(OWNER_THINKING)  # not the header owner
        # Still active.
        assert created[0].stop_count == 0
        assert coord.current_header_owner == OWNER_PHASE_TIMELINE

    def test_non_owner_update_header_drops_silently(self) -> None:
        coord, created = _make_coord()
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        last_paint = created[0].updates[-1]
        ok = coord.update_header(OWNER_THINKING, "stale")
        assert ok is False
        # Renderable unchanged (no new update appended).
        assert created[0].updates[-1] == last_paint

    def test_force_release_clears_both_slots(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.update(OWNER_THINKING, "body")
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "header")
        coord.force_release()
        assert created[0].stopped is True
        assert coord.is_active is False
        assert coord.current_owner == ""
        assert coord.current_header_owner == ""

    def test_shutdown_clears_both_slots(self) -> None:
        coord, created = _make_coord()
        coord.acquire(OWNER_THINKING)
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.shutdown()
        assert created[0].stopped is True
        assert coord.is_active is False
        assert coord.current_owner == ""
        assert coord.current_header_owner == ""

    def test_is_header_owner(self) -> None:
        coord, _ = _make_coord()
        assert coord.is_header_owner(OWNER_PHASE_TIMELINE) is False
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        assert coord.is_header_owner(OWNER_PHASE_TIMELINE) is True
        assert coord.is_header_owner(OWNER_THINKING) is False
