"""PR-E2 — single ``rich.live.Live`` coordinator with region + header slots.

Why this exists. The original streaming pipeline had four owners that
each spun up their own ``Live`` block:

  * ``StreamingPrinter``       — token stream
  * ``ThinkingPrinter``        — chain-of-thought spinner
  * ``ToolPanelRenderer``      — running tool panel
  * ``PhaseTimelineRenderer``  — 5-stage stepper

Each Live owner repaints at 20 Hz. When event flow forces a handoff
(token arrives while the tool spinner is alive) the previous owner
calls ``.stop()`` and the next owner calls ``.start()``; rich tears
down and re-installs the alternate-screen region between those two
calls and the terminal flickers as the redraw region resizes.

The coordinator collapses the four owners onto **one** ``Live`` block
split into two slots:

  * **region** — the body. Rotates between thinking / token-stream /
    tool-panel as the event flow demands. Only one owner at a time.
  * **header** — the always-on slot. Used by phase-timeline so the
    5-stage stepper stays visible regardless of which body is painting.
    Coexists with the region; doesn't compete.

Each slot has its own owner identity. A new region owner takes over
without ``stop`` / ``start`` cycling — same Live block keeps painting,
only the inner renderable changes (composite ``Group(header, region)``).
That's a single contiguous redraw, no terminal-region churn, no
flicker.

The release path for the region preserves the *flush* contract used by
``StreamingPrinter`` and ``ToolPanelRenderer.complete``: the static
text those callers want to land in scrollback can't be printed under a
live region (the alternate-screen redraw competes with scrollback
writes), so the region's release stops the Live block, runs the
``on_release`` callback to print the static content, and — if the
header is still active — restarts a fresh Live painting only the
header. The few-millisecond gap is invisible in practice and is the
cost of keeping streaming output landing in scrollback as the user
scrolls back through the conversation.

When ``on_release`` is **not** provided (the common thinking /
tool-cancel case), region release skips the stop+restart entirely and
simply repaints the Live with header alone — no flicker for those
cases at all.

Owner conventions (suggested, not enforced at the API level):

  * ``OWNER_TOKEN_STREAM`` / ``OWNER_THINKING`` / ``OWNER_TOOL_PANEL``
    are region owners — they claim the body slot via ``acquire``.
  * ``OWNER_PHASE_TIMELINE`` is the header owner — it claims the
    header slot via ``acquire_header``.

Mixing slots (e.g. calling ``acquire(OWNER_PHASE_TIMELINE)``) is
allowed but logically incorrect; phase-timeline using ``acquire``
would steal the body and the user's thinking content would vanish.
We rely on docstring discipline rather than runtime checks because
adding type-tagged owner enums would crowd the API for the small
benefit of catching one wrong call site.

Threading. The TUI runs the asyncio loop on the main thread, but
``rich.live`` itself starts a daemon thread to drive its refresh
clock. Updates from ``Renderer.dispatch`` happen on the loop thread;
the coordinator wraps every state mutation in a lock so two near-
simultaneous events (a token + a phase change) can't end up with
mismatched owner / Live state.

Contract for ``_coord_active`` flags. Each printer that integrates
with the coordinator carries a local ``_coord_active: bool`` that
records "I think I currently hold the slot." The flag becomes stale
after a rotation (someone else acquires; we silently drop ownership).
That's tolerated because the coordinator's ``release`` is owner-scoped
no-op-safe — a stale-flag printer that calls ``release`` does not
disturb the current owner's slot. The contract printers must follow:
**unconditionally clear local state in finalize / cancel before
attempting release.** Otherwise a stale local ``_live`` reference or
buffer would leak across turns. Same applies to ``force_release`` and
``shutdown`` — printers must run their own teardown first (the
``Renderer.shutdown`` in ``renderers/__init__.py`` already does this
in order; preserve it when adding new printers).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from rich.console import Group, RenderableType
from rich.live import Live
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole

logger = logging.getLogger(__name__)


# 20 Hz matches what every existing printer uses; centralising it here
# means a future "low-spec terminal" knob (10 Hz) only has to change
# one constant.
DEFAULT_REFRESH_HZ: int = 20


class LiveCoordinator:
    """Single owner of the active rich.live.Live block, split into two slots.

    Public API:

        Region (body) slot — rotates per event:
            coord.acquire(owner)
            coord.update(owner, body) -> bool
            coord.release(owner, *, on_release=None)

        Header slot — coexists with region:
            coord.acquire_header(owner)
            coord.update_header(owner, header) -> bool
            coord.release_header(owner)

        Inspection:
            coord.is_active
            coord.current_owner          # region owner (back-compat name)
            coord.current_header_owner
            coord.is_owner(owner)
            coord.is_header_owner(owner)

        Lifecycle:
            coord.force_release()
            coord.shutdown()

    Region acquisition rules:

      * If neither slot active → start a new Live block.
      * If region empty but header active → reuse the same Live block;
        just set the region owner.
      * Same region owner re-acquires → no-op.
      * Different region owner acquires while one is active → the new
        owner takes over WITHOUT stop/start. Region renderable is
        cleared (stale; the new owner will update next). Header slot
        is untouched.

    Header acquisition rules:

      * Symmetric to region — Live keeps painting through rotations.
      * Different header owner → header renderable cleared; new owner
        will update.

    Region release rules:

      * Only the current region owner can release; others are no-ops.
      * **Without** ``on_release``: just clear the region. If header is
        still active, repaint Live with header alone (no flicker).
        Otherwise stop Live entirely.
      * **With** ``on_release``: stop Live so the callback can print
        static content into scrollback. After the callback runs, if
        header is still active, restart Live painting only the header.
        The few-ms gap is the cost of preserving the flush contract.

    Header release rules:

      * Only the current header owner can release.
      * Doesn't stop Live unless region is also empty.
      * If region still active → Live keeps painting, repainted with
        header cleared.

    Composite paint. Every state-changing call ends with a repaint
    that emits to the Live block:
      * ``Group(header, region)``  — both slots populated
      * ``header``                 — only header populated
      * ``region``                 — only region populated
      * ``Text("")``               — neither (idle Live; should be rare)

    Locking. Every state mutation runs under ``self._lock`` (RLock).
    ``on_release`` callbacks run *outside* the lock so a callback that
    writes to the console can't deadlock against rich's internal
    refresh thread.
    """

    def __init__(
        self,
        console: ChaosConsole,
        *,
        refresh_per_second: int = DEFAULT_REFRESH_HZ,
        transient: bool = True,
        live_factory: Optional[Callable[..., Live]] = None,
    ) -> None:
        self._console = console
        self._refresh_per_second = refresh_per_second
        self._transient = transient
        self._live: Optional[Live] = None
        self._owner: str = ""
        self._header_owner: str = ""
        # Cached renderables for composite repaint. Cleared on owner
        # rotation so a new owner's slot doesn't paint with stale content
        # before it has a chance to call update.
        self._region_renderable: Optional[RenderableType] = None
        self._header_renderable: Optional[RenderableType] = None
        self._lock = threading.RLock()
        # Tests inject a fake Live factory that doesn't write to a real
        # terminal. Production passes None and we use rich.live.Live.
        self._live_factory = live_factory or self._default_factory

    # ------------------------------------------------------------------
    # Region (body) slot
    # ------------------------------------------------------------------

    def acquire(self, owner: str) -> None:
        """Take ownership of the region (body) slot.

        Starts the Live block on first acquire across either slot;
        on a same-owner re-acquire it's a no-op; on a different-owner
        acquire it rotates ownership without restarting the block (no
        flicker). The region renderable cache is cleared on rotation
        so a stale paint from the previous owner doesn't bleed into
        the next composite repaint.
        """
        if not owner:
            raise ValueError("LiveCoordinator.acquire requires a non-empty owner")
        with self._lock:
            if self._owner == owner and self._live is not None:
                return
            if self._live is None:
                if not self._start_live_locked():
                    # factory failed; do not pretend to own a broken Live
                    self._owner = ""
                    return
            if self._owner != owner:
                # Rotation — drop the prior owner's cached body so the
                # next composite repaint doesn't mix old body with new
                # header / new owner's content.
                self._region_renderable = None
            self._owner = owner

    def update(self, owner: str, renderable: RenderableType) -> bool:
        """Repaint the region slot iff ``owner`` is the region owner.

        Returns True on a successful update, False otherwise. Callers
        whose updates were silently dropped (e.g. a delayed update
        from a stale owner) can use the return value to decide whether
        to fall back to a console.print.
        """
        with self._lock:
            if self._owner != owner or self._live is None:
                return False
            prev = self._region_renderable
            self._region_renderable = renderable
            try:
                self._repaint_locked()
                return True
            except Exception:
                logger.exception("LiveCoordinator: update failed")
                # Revert the cache so a transient paint failure
                # doesn't leave a broken renderable behind.
                self._region_renderable = prev
                return False

    def release(
        self,
        owner: str,
        *,
        on_release: Optional[Callable[[], None]] = None,
    ) -> None:
        """Release the region slot iff ``owner`` is the region owner.

        ``on_release`` runs AFTER the Live block has been torn down so
        callers (e.g. ``StreamingPrinter._flush_final``) can print
        static text into scrollback without competing with the live
        region. When ``on_release`` is None we skip the stop+restart
        entirely and just repaint with header alone — that's the
        zero-flicker fast path.
        """
        cb_to_run: Optional[Callable[[], None]] = None
        with self._lock:
            if self._owner != owner:
                return
            self._owner = ""
            self._region_renderable = None

            if on_release is None:
                # Fast path: no flush to land in scrollback.
                if self._header_owner and self._header_renderable is not None:
                    # Header still active → repaint header alone, keep Live alive.
                    self._repaint_locked()
                else:
                    # Both slots empty → tear Live down entirely.
                    self._stop_live_locked()
                return

            # Slow path: on_release provided. We must stop Live so the
            # callback's console.print lands cleanly in scrollback.
            self._stop_live_locked()
            cb_to_run = on_release

        # Run on_release outside the lock — the callback may print to
        # the console, and we don't want a print to deadlock against a
        # rich internal that re-acquires our lock.
        if cb_to_run is not None:
            try:
                cb_to_run()
            except Exception:
                logger.exception("LiveCoordinator: on_release callback failed")

        # Re-arm Live for the header if it was active. The lock
        # round-trip is the cost of letting on_release print between
        # stops.
        with self._lock:
            if (
                self._live is None
                and self._header_owner
                and self._header_renderable is not None
            ):
                if self._start_live_locked():
                    self._repaint_locked()

    # ------------------------------------------------------------------
    # Header slot — always-on, coexists with region
    # ------------------------------------------------------------------

    def acquire_header(self, owner: str) -> None:
        """Take ownership of the header slot.

        Symmetric to ``acquire`` for the region: starts the Live block
        on first acquire if neither slot was active; otherwise rotates
        the header owner without restart. Header renderable cache is
        cleared on rotation so the next composite repaint doesn't mix
        a stale header from the previous owner with the new region.
        """
        if not owner:
            raise ValueError(
                "LiveCoordinator.acquire_header requires a non-empty owner"
            )
        with self._lock:
            if self._header_owner == owner and self._live is not None:
                return
            if self._live is None:
                if not self._start_live_locked():
                    self._header_owner = ""
                    return
            if self._header_owner != owner:
                self._header_renderable = None
            self._header_owner = owner

    def update_header(self, owner: str, renderable: RenderableType) -> bool:
        """Repaint the header slot iff ``owner`` is the header owner."""
        with self._lock:
            if self._header_owner != owner or self._live is None:
                return False
            prev = self._header_renderable
            self._header_renderable = renderable
            try:
                self._repaint_locked()
                return True
            except Exception:
                logger.exception("LiveCoordinator: update_header failed")
                self._header_renderable = prev
                return False

    def release_header(self, owner: str) -> None:
        """Release the header slot iff ``owner`` is the header owner.

        Doesn't stop the Live block unless the region is also empty.
        When the region is still active, the next composite repaint
        will paint the body alone (header rendered as empty).
        """
        with self._lock:
            if self._header_owner != owner:
                return
            self._header_owner = ""
            self._header_renderable = None
            if self._owner == "" and self._region_renderable is None:
                # Neither slot has content → tear down the Live entirely.
                self._stop_live_locked()
            else:
                # Region still alive — repaint without header.
                self._repaint_locked()

    # ------------------------------------------------------------------
    # Lifecycle (covers both slots)
    # ------------------------------------------------------------------

    def force_release(self) -> None:
        """Tear down regardless of who owns either slot.

        Used by error handlers when the slot owners may be in an
        unknown state. **Printers that integrate with the coordinator
        must clear their local ``_coord_active`` flags before this
        path runs** — typically via their own ``finalize`` / ``cancel``
        — so a force-release doesn't leave them thinking they still
        own the slot.
        """
        with self._lock:
            self._owner = ""
            self._header_owner = ""
            self._region_renderable = None
            self._header_renderable = None
            self._stop_live_locked()

    def shutdown(self) -> None:
        """Permanently tear down. Idempotent. Used at app exit.

        Same caveat as ``force_release``: printers must run their
        finalize / cancel before the renderer calls ``coord.shutdown()``.
        ``Renderer.shutdown`` in ``renderers/__init__.py`` already does
        this in order; preserve it when adding new printers.
        """
        with self._lock:
            self._owner = ""
            self._header_owner = ""
            self._region_renderable = None
            self._header_renderable = None
            self._stop_live_locked()

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        with self._lock:
            return self._live is not None

    @property
    def current_owner(self) -> str:
        """Region (body) owner. Name kept for backward compatibility."""
        with self._lock:
            return self._owner

    @property
    def current_header_owner(self) -> str:
        with self._lock:
            return self._header_owner

    def is_owner(self, owner: str) -> bool:
        """True iff ``owner`` is the current region owner."""
        with self._lock:
            return self._owner == owner

    def is_header_owner(self, owner: str) -> bool:
        with self._lock:
            return self._header_owner == owner

    # ------------------------------------------------------------------
    # Internal — must be called with self._lock held
    # ------------------------------------------------------------------

    def _start_live_locked(self) -> bool:
        """Start a new Live block. Returns True on success.

        On failure (factory raised) ``self._live`` is left None so the
        caller can decide how to recover (usually: clear ownership and
        return).
        """
        try:
            self._live = self._live_factory()
            self._live.start()
            return True
        except Exception:
            logger.exception("LiveCoordinator: failed to start Live block")
            self._live = None
            return False

    def _stop_live_locked(self) -> None:
        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                logger.exception("LiveCoordinator: stop failed")
            self._live = None

    def _repaint_locked(self) -> None:
        """Compose header + region into the Live's current renderable.

        Falls back gracefully when only one slot has content:
        ``Group`` is only used when both are populated, otherwise the
        single populated renderable is sent directly so we don't pay
        the (small) cost of a Group wrapper for nothing.
        """
        if self._live is None:
            return
        header = self._header_renderable
        region = self._region_renderable
        if header is not None and region is not None:
            rend: RenderableType = Group(header, region)
        elif header is not None:
            rend = header
        elif region is not None:
            rend = region
        else:
            rend = Text("")
        try:
            self._live.update(rend)
        except Exception:
            logger.exception("LiveCoordinator: live.update failed")

    def _default_factory(self) -> Live:
        return Live(
            Text(""),
            console=self._console.console,
            refresh_per_second=self._refresh_per_second,
            transient=self._transient,
        )


# ---------------------------------------------------------------------------
# Owner identifiers — kept here so all four printers reference the same names
# ---------------------------------------------------------------------------

OWNER_TOKEN_STREAM: str = "token-stream"
OWNER_THINKING: str = "thinking"
OWNER_TOOL_PANEL: str = "tool-panel"
OWNER_PHASE_TIMELINE: str = "phase-timeline"
