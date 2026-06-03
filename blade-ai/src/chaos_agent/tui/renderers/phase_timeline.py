"""PhaseTimelineRenderer — single-line phase stepper.

Renders ``○ 意图识别 → ◉ 安全检查 → ○ 故障注入 → ○ 注入验证 → ○ 恢复就绪``
using rich.live so it updates in place during a task. Stops when the
task ends.

PR-E2 — when a ``LiveCoordinator`` is injected, the stepper runs in
the **header slot** (``OWNER_PHASE_TIMELINE``), which coexists with
whatever region owner is painting (thinking / token-stream / tool).
That preserves today's visible behavior — the stepper line stays on
top, the live body paints below — while collapsing onto a single
shared Live block so we don't flicker on every region rotation.

Backward-compatible: when no coordinator is passed, the renderer runs
its own embedded ``rich.live.Live`` block — the pre-PR-E2 behavior.
That path is what existing test fixtures hit, and what production
falls back to if the renderer is constructed standalone.
"""

from __future__ import annotations

import shutil
from typing import Optional

from rich.live import Live
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_PHASE_TIMELINE,
)

PHASE_ORDER = ["intent", "safety", "inject", "verify", "recovery"]

# Map of pipeline graph node → stepper phase. `intent_clarification` is
# intentionally absent: chat-only turns finish inside that node and must
# not paint the 5-stage stepper.
_NODE_PHASE_MAP = {
    # ``intent_confirm`` is the first user-facing gate — fold it into
    # "safety" so the stepper paints a ``◉ 安全检查`` indicator while
    # the user reads the confirm card. Without this, the gap between
    # "开始" and the confirm panel rendering felt like a stuck terminal.
    "intent_confirm": "safety",
    "safety_check": "safety",
    "confirmation_gate": "safety",
    "baseline_capture": "inject",
    "agent_loop": "inject",
    "execute_loop": "inject",
    "direct_execute": "inject",
    "verifier_loop": "verify",
    "recover_verifier_loop": "recovery",
}


def phase_for_node(node_name: str) -> str:
    """Return the stepper phase for a graph node, or '' if not tracked."""
    return _NODE_PHASE_MAP.get(node_name, "")


# Same narrow-terminal threshold as before — narrower than this and
# the 5-stage stepper wraps badly, so we silently degrade to "don't
# paint anything." Pinned via tests so a future minimum width tweak
# is a deliberate decision.
_MIN_TERMINAL_WIDTH = 80


class PhaseTimelineRenderer:
    """Single-line phase stepper, optionally wired through ``LiveCoordinator``.

    Usage:
        renderer.start()        # idempotent; respects narrow-terminal degradation
        renderer.on_phase_event(node_name, is_start=True)  # mark a phase running
        renderer.on_phase_event(node_name, is_start=False) # mark it complete
        renderer.mark_failed(phase="")  # paint current phase as failed
        renderer.stop()         # tear down

    When ``coordinator`` is provided, the stepper paints into the
    coord's **header slot** so the region (thinking / token / tool)
    keeps working below it. When ``coordinator`` is None, the
    renderer manages its own embedded ``rich.live.Live`` — the
    pre-PR-E2 behavior, preserved for unit tests and any standalone
    construction path.
    """

    def __init__(
        self,
        console: ChaosConsole,
        *,
        coordinator: Optional[LiveCoordinator] = None,
    ) -> None:
        self._console = console
        self._live: Optional[Live] = None  # used only when no coordinator
        self._coord: Optional[LiveCoordinator] = coordinator
        # Local "do I think I currently hold the header slot?" flag.
        # See LiveCoordinator's docstring for why this is necessary
        # (rotation can silently drop ownership; we still need to
        # know to clean up on our side).
        self._coord_active: bool = False
        self._current: str = ""
        self._failed: str = ""
        self._completed: set[str] = set()

    @property
    def active(self) -> bool:
        """True when the stepper is currently painting.

        For the coord path this reflects local state — if a future
        contributor wires another header owner that rotates us out,
        the flag would lag until ``stop`` clears it. That's fine for
        the renderer-internal use case (``Renderer.dispatch`` checks
        ``active`` before deciding to (re)start the stepper).
        """
        if self._coord is not None:
            return self._coord_active
        return self._live is not None

    def start(self) -> None:
        # Idempotent — stop any prior block before starting a new one.
        if self.active:
            self.stop()
        if shutil.get_terminal_size((80, 24)).columns < _MIN_TERMINAL_WIDTH:
            return
        self._current = ""
        self._failed = ""
        self._completed.clear()

        if self._coord is not None:
            self._coord.acquire_header(OWNER_PHASE_TIMELINE)
            self._coord_active = True
            self._coord.update_header(OWNER_PHASE_TIMELINE, self._render())
            return

        # Legacy path — own Live block.
        self._live = Live(
            self._render(),
            console=self._console.console,
            refresh_per_second=8,
            transient=True,
        )
        self._live.start()

    def stop(self) -> None:
        if self._coord is not None:
            if self._coord_active:
                # Always clear the local flag first — even if release
                # is a no-op (rotation already stole us), we're done.
                self._coord_active = False
                self._coord.release_header(OWNER_PHASE_TIMELINE)
            return

        if self._live is None:
            return
        try:
            self._live.stop()
        except Exception:
            pass
        self._live = None

    def on_phase_event(self, node_name: str, is_start: bool) -> None:
        phase = _NODE_PHASE_MAP.get(node_name)
        if not phase:
            return
        if is_start:
            if self._current and self._current != phase:
                self._completed.add(self._current)
            self._current = phase
        else:
            self._completed.add(phase)
        self._refresh()

    def mark_failed(self, phase: str = "") -> None:
        self._failed = phase or self._current
        self._refresh()

    def _refresh(self) -> None:
        if self._coord is not None:
            if not self._coord_active:
                return
            self._coord.update_header(OWNER_PHASE_TIMELINE, self._render())
            return

        if self._live is None:
            return
        try:
            self._live.update(self._render())
        except Exception:
            pass

    def _render(self) -> Text:
        # Designer pass: only the **current** phase gets accent color.
        # Completed phases use a soft ✓ in muted gray (still legible
        # as "done" via the glyph alone); pending stays dim ○. This
        # collapses the previous 3-color rainbow (orange + green +
        # gray) into a single attention focus, which is the cardinal
        # rule for status indicators — one thing should "pop", not
        # everything at once.
        from chaos_agent.tui.theme import Theme
        accent = Theme.gradient_bright
        out = Text()
        for i, phase in enumerate(PHASE_ORDER):
            label = strings.PHASE_NAMES.get(phase, phase)
            if phase == self._failed:
                out.append("✗ ", style=f"bold {Theme.state_err}")
                out.append(label, style=f"bold {Theme.state_err}")
            elif phase == self._current:
                # Current phase: bright accent (brand blue) — the
                # single focus point that "pops"
                out.append("◉ ", style=f"bold {Theme.gradient_bright}")
                out.append(label, style=f"bold {Theme.gradient_bright}")
            elif phase in self._completed:
                # Completed: transition color (gradient_mid) — still
                # visible but clearly past, not competing with current
                out.append("✓ ", style=Theme.gradient_mid)
                out.append(label, style=Theme.gradient_mid)
            else:
                # Pending: muted (gradient_dim) — awaiting, recedes
                out.append("○ ", style=Theme.gradient_dim)
                out.append(label, style=Theme.gradient_dim)
            if i < len(PHASE_ORDER) - 1:
                out.append(" → ", style=Theme.gradient_dim)
        return out
