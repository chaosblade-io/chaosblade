"""ToolPanelRenderer — animated spinner + inline result line for tool calls.

Uses rich.live to animate a single line. On completion, generic tools render
**inline two-line** (PR-C1):

    ⏺ kubectl(get ns default)
      ⎿  Active  (0.4s)

Three categories still get a Rich Panel because the payload is genuinely
multi-line and structured:

  - ``TodoWrite`` (a checklist with per-item status icons)
  - ``Agent`` / ``Explore`` (multi-paragraph sub-agent reports)
  - error completions (``complete_error``: red border with full traceback)

Anything else collapses to two lines — same shape Claude Code uses, removes
~80% of the box-drawing the old generic panel emitted on every kubectl call.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_TOOL_PANEL,
)
from chaos_agent.tui.theme import BREATHING_DOTS, Borders, Colors, Icons, Spacing, Theme

logger = logging.getLogger(__name__)

# Spinner frames — sourced from theme so the running glyph (·→✻→✽) shares
# the filled-circle/star family with the static success marker (⏺/✓),
# preventing a visual jump when a tool transitions from running to done.
_FRAMES = BREATHING_DOTS

# Tools that receive structured JSON output and get custom rendering.
_TODO_TOOLS = ("TodoWrite", "todo_write")
_AGENT_TOOLS = (
    "Agent",
    "agent",
    "Explore",
    "explore",
    "general-purpose",
)


class ToolPanelRenderer:
    """Animated tool-call panel.

    PR-E2 — when a ``LiveCoordinator`` is injected, the spinner runs
    in the **region slot** under ``OWNER_TOOL_PANEL``. The completion
    paths (``complete`` / ``complete_error``) still call
    ``_stop_live`` first and then ``console.print(Panel|inline)``;
    under coord that ``console.print`` lands above the live region in
    scrollback if a header (phase-timeline) is still painting, or in
    the now-empty terminal if not. Either way the static result
    captures cleanly.

    Backward-compatible: when no coordinator is passed, the renderer
    runs its own embedded ``rich.live.Live`` block — the pre-PR-E2
    behavior. Test fixtures that construct the renderer standalone hit
    this path.
    """

    def __init__(
        self,
        console: ChaosConsole,
        state=None,
        *,
        coordinator: Optional[LiveCoordinator] = None,
    ) -> None:
        self._console = console
        self._live: Optional[Live] = None  # used only when no coordinator
        self._tool_name: str = ""
        self._start_time: float = 0.0
        self._tick_thread: Optional[threading.Thread] = None
        self._stop_event: Optional[threading.Event] = None
        # Store full outputs for /expand command
        self._full_outputs: dict[str, str] = {}
        self._tool_index: int = 0
        # PR-D4 — when ``state`` is provided we allocate a [T#] locator
        # per completed tool call and stash the output in
        # ``state.locators`` so /show / /copy can resolve it later.
        # Optional so the test fixtures that construct the renderer
        # standalone keep working.
        self._state = state
        # PR-E2 — coord routing. ``_coord_active`` mirrors region
        # ownership locally so we can tear down internal state (tick
        # thread, tool name) even when ownership has already been
        # rotated to another region owner.
        self._coord: Optional[LiveCoordinator] = coordinator
        self._coord_active: bool = False

    def start(self, tool_name: str) -> None:
        self.cancel()
        self._tool_name = tool_name
        self._start_time = time.monotonic()

        if self._coord is not None:
            self._coord.acquire(OWNER_TOOL_PANEL)
            self._coord_active = True
            self._coord.update(OWNER_TOOL_PANEL, self._frame(0))
        else:
            self._live = Live(
                self._frame(0),
                console=self._console.console,
                refresh_per_second=10,
                transient=True,
            )
            self._live.start()

        self._stop_event = threading.Event()
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True)
        self._tick_thread.start()

    def complete(self, tool_name: str, output: str = "") -> None:
        # Idempotent: the legacy guard was ``if self._live is not None``
        # but with the coord path ``_live`` is always None — use the
        # broader ``_running()`` check that covers both routes.
        if self._running():
            self._stop_live()
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        effective_name = tool_name or self._tool_name

        # Store full output for /expand
        self._tool_index += 1
        if output:
            self._full_outputs[str(self._tool_index)] = output

        # PR-D4 — allocate a [T#] locator and snapshot the call so /show
        # T# can re-render the inline result later. The label string is
        # passed into the dispatched render method; whether it actually
        # paints depends on display_mode.
        locator = ""
        if self._state is not None and getattr(self._state, "locators", None) is not None:
            locator = self._state.locators.allocate_tool({
                "tool_name": effective_name,
                "output": output,
                "elapsed": elapsed,
            })

        # Vertical breath: 1 blank line before each tool result,
        # separating it from preceding agent text / previous tool output.
        self._console.print("")

        # Dispatch to specialised renderers based on tool name.
        if effective_name in _TODO_TOOLS:
            self._render_todo_panel(effective_name, output, elapsed, locator)
        elif any(
            effective_name.startswith(prefix) or effective_name == prefix
            for prefix in _AGENT_TOOLS
        ):
            self._render_agent_panel(effective_name, output, elapsed, locator)
        else:
            self._render_generic_inline(effective_name, output, elapsed, locator)

        self._tool_name = ""
        self._start_time = 0.0

    def _locator_suffix(self, locator: str) -> str:
        """Format ``[T#]`` suffix honouring display_mode (calm hides it)."""
        if not locator or self._state is None:
            return ""
        from chaos_agent.tui.state import DisplayMode
        mode = getattr(self._state, "display_mode", DisplayMode.WORKING)
        if mode == DisplayMode.CALM:
            return ""
        return f"  [{locator}]"

    def complete_error(self, tool_name: str, error: str = "", elapsed: float = 0) -> None:
        """Render a tool completion with error status (red border)."""
        if self._running():
            self._stop_live()
        if not elapsed:
            elapsed = time.monotonic() - self._start_time if self._start_time else 0
        effective_name = tool_name or self._tool_name

        # Failed tool calls are exactly the things users want to /show or
        # /copy after the fact — allocate a [T#] just like complete() does.
        locator = ""
        if self._state is not None and getattr(self._state, "locators", None) is not None:
            locator = self._state.locators.allocate_tool({
                "tool_name": effective_name,
                "output": error,
                "elapsed": elapsed,
                "status": "error",
            })

        title = Text()
        title.append(f" {Icons.FAIL} ", style=f"bold {Colors.ERROR}")
        title.append(effective_name)
        suffix = self._locator_suffix(locator)
        if suffix:
            title.append(suffix, style=Colors.DIM)

        body = Text()
        if error:
            lines = error.strip().splitlines()[:Spacing.TOOL_PREVIEW_LINES]
            for line in lines:
                body.append(f"{line}\n", style=Colors.ERROR)

        self._console.print(
            Panel(
                body,
                title=title,
                subtitle=f"{elapsed:.1f}s",
                border_style=Borders.TOOL_ERROR,
                padding=(0, 1),
            )
        )
        self._tool_name = ""
        self._start_time = 0.0

    def get_full_output(self, index: str) -> str | None:
        """Retrieve full output for /expand command."""
        return self._full_outputs.get(index)

    # -- Specialised panel renderers -------------------------------------------

    def _render_todo_panel(
        self, tool_name: str, output: str, elapsed: float, locator: str = ""
    ) -> None:
        """TodoWrite - Panel with progress indicators."""
        title = Text()
        title.append(f" {Icons.SUCCESS} ", style=f"bold {Colors.SUCCESS}")
        title.append(f"{tool_name} Update todos")
        suffix = self._locator_suffix(locator)
        if suffix:
            title.append(suffix, style=Colors.DIM)

        body = Text()
        todos = _parse_todos(output)
        if todos:
            for todo in todos:
                status = todo.get("status", "pending")
                content = todo.get("content", "")
                if status == "completed":
                    icon, icon_style = "\u25cf", Colors.SUCCESS
                elif status == "in_progress":
                    icon, icon_style = "\u25d0", Colors.ACTIVE
                else:
                    icon, icon_style = "\u25cb", Colors.DIM
                body.append(f"  {icon}  ", style=icon_style)
                body.append(f"{content}\n")
        else:
            preview = output[:200] + "\u2026" if len(output) > 200 else output
            body.append(preview, style=Colors.DIM)

        self._console.print(
            Panel(
                body,
                title=title,
                subtitle=f"{elapsed:.1f}s",
                border_style=Borders.TOOL_SUCCESS,
                padding=(0, 1),
            )
        )

    def _render_agent_panel(
        self, tool_name: str, output: str, elapsed: float, locator: str = ""
    ) -> None:
        """Agent/Explore - Panel with summary stats."""
        title = Text()
        title.append(f" {Icons.SUCCESS} ", style=f"bold {Colors.SUCCESS}")
        title.append(tool_name)
        suffix = self._locator_suffix(locator)
        if suffix:
            title.append(suffix, style=Colors.DIM)

        body = Text()
        summary = _extract_agent_summary(output)
        if summary:
            body.append(summary, style=Colors.DIM)

        footer_parts = [f"{elapsed:.0f}s"]
        token_info = _extract_token_info(output)
        if token_info:
            footer_parts.append(token_info)

        self._console.print(
            Panel(
                body or Text(""),
                title=title,
                subtitle="  \u00b7  ".join(footer_parts),
                border_style=Borders.TOOL_SUCCESS,
                padding=(0, 1),
            )
        )

    def _render_generic_inline(
        self, tool_name: str, output: str, elapsed: float, locator: str = ""
    ) -> None:
        """Generic tool — inline two-line result (PR-C1).

        Line 1 is the tool tag (``⏺ tool_name``); line 2 is one indented
        ``⎿`` summary line with a 1-line preview and the elapsed time.
        Multi-line outputs are summarised — the full text is still cached
        in ``_full_outputs`` so ``/expand <index>`` can recall it.
        """
        # Line 1 — agent marker glyph + tool name (+ optional [T#])
        line1 = Text()
        line1.append(f" {Icons.MARKER} ", style=f"bold {Colors.SUCCESS}")
        line1.append(tool_name)
        suffix = self._locator_suffix(locator)
        if suffix:
            line1.append(suffix, style=Colors.DIM)
        self._console.print(line1)

        # Line 2 — └─ preview · elapsed (use ⎿ indent glyph for tree feel)
        preview = _summarize_output(output)
        line_count = _line_count(output)
        line2 = Text()
        line2.append(f"  {Icons.TREE_BRANCH}  ", style=Colors.DIM)
        if preview:
            line2.append(preview)
        else:
            line2.append("(no output)", style=Colors.DIM)
        line2.append(f"  ({elapsed:.1f}s)", style=Colors.DIM)
        if line_count > 1:
            line2.append(
                f"  · /expand T{self._tool_index} \u67e5\u770b\u5168\u90e8 ({line_count} \u884c)",
                style=Colors.DIM,
            )
        self._console.print(line2)

    # -- Spinner lifecycle -----------------------------------------------------

    def cancel(self) -> None:
        """Abort the running spinner without printing a result."""
        if self._running():
            self._stop_live()
        self._tool_name = ""
        self._start_time = 0.0

    def _running(self) -> bool:
        """True when the spinner is currently painting (coord or legacy)."""
        if self._coord is not None:
            return self._coord_active
        return self._live is not None

    def _stop_live(self) -> None:
        """Tear down the live spinner so the next ``console.print``
        (the static panel / inline result) lands cleanly.

        Coord path: release the region owner without an
        ``on_release`` callback. If a header (phase-timeline) is
        still active the coord skips the stop+restart entirely and
        keeps the Live painting header alone — the subsequent
        ``console.print(Panel)`` then lands above the header live
        region in scrollback (rich's behaviour for transient Live).
        """
        if self._stop_event is not None:
            self._stop_event.set()
        if self._tick_thread is not None and self._tick_thread.is_alive():
            try:
                self._tick_thread.join(timeout=0.5)
            except Exception:
                pass
        self._tick_thread = None
        self._stop_event = None

        if self._coord is not None:
            if self._coord_active:
                self._coord_active = False
                self._coord.release(OWNER_TOOL_PANEL)
            return

        if self._live is not None:
            try:
                self._live.stop()
            except Exception:
                pass
            self._live = None

    def _tick_loop(self) -> None:
        """Background driver. Coord path: routes paints through the
        coordinator under ``OWNER_TOOL_PANEL``. If ownership has been
        rotated away (another region owner took over), updates are
        silently dropped — the loop keeps running until ``stop_event``
        is set in ``_stop_live``.
        """
        idx = 0
        while self._stop_event is not None and not self._stop_event.is_set():
            try:
                if self._coord is not None:
                    self._coord.update(OWNER_TOOL_PANEL, self._frame(idx))
                elif self._live is not None:
                    self._live.update(self._frame(idx))
            except Exception:
                pass
            idx += 1
            if self._stop_event.wait(0.1):
                return

    def _frame(self, idx: int) -> Text:
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        text = Text()
        text.append(f"  {_FRAMES[idx % len(_FRAMES)]} ", style=Theme.gradient_bright)
        text.append(self._tool_name, style=Theme.gradient_mid)
        text.append(f"  {elapsed:.1f}s", style=f"italic {Theme.gradient_dim}")
        return text


# -- Output parsing helpers ----------------------------------------------------


_INLINE_PREVIEW_MAX = 70  # one-line preview cap; keeps line2 ≤ ~80 cols total


def _summarize_output(output: str) -> str:
    """Reduce arbitrary tool output to a single inline preview line.

    Strategy:
      1. Try to parse as JSON envelope ``{status, code, message, data}``;
         if matched, render ``status · message`` (Active · ok-to-go) since
         that's how every internal tool reports.
      2. Otherwise pick the first non-empty line of the raw text.
      3. Truncate to ``_INLINE_PREVIEW_MAX`` chars with an ellipsis.
    """
    if not output:
        return ""
    text = output.strip()
    parsed = _safe_json(text)
    if parsed is not None:
        summary = _summarize_envelope(parsed)
        if summary:
            return _truncate(summary, _INLINE_PREVIEW_MAX)
    for line in text.splitlines():
        line = line.strip()
        if line:
            return _truncate(line, _INLINE_PREVIEW_MAX)
    return ""


def _line_count(output: str) -> int:
    """Return the number of non-blank lines in the output (≥ 0)."""
    if not output:
        return 0
    return sum(1 for line in output.strip().splitlines() if line.strip())


def _safe_json(text: str):
    if not text or text[0] not in "{[":
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return None


def _summarize_envelope(parsed) -> str:
    """Render a JSON envelope as ``status · message`` if it matches."""
    if not isinstance(parsed, dict):
        return ""
    status = parsed.get("status")
    message = parsed.get("message")
    if status and message:
        return f"{status} \u00b7 {message}"
    if status:
        return str(status)
    if message:
        return str(message)
    return ""


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "\u2026"


def _parse_todos(output: str) -> list[dict]:
    """Extract todo items from TodoWrite tool output."""
    if not output:
        return []
    try:
        data = json.loads(output)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(data, dict):
        if "todos" in data and isinstance(data["todos"], list):
            return data["todos"]
    if isinstance(data, list):
        return data
    return []


def _extract_agent_summary(output: str) -> str:
    """Extract a one-line summary from an Agent/Explore tool output."""
    if not output:
        return ""
    for line in output.strip().splitlines():
        line = line.strip()
        if line:
            return line[:80] + ("\u2026" if len(line) > 80 else "")
    return ""


def _extract_token_info(output: str) -> str:
    """Try to extract token usage info from agent output for the footer."""
    if not output:
        return ""
    import re

    m = re.search(r"(\d+(?:\.\d+)?k?)\s*tokens", output, re.IGNORECASE)
    if m:
        return f"{m.group(1)} tokens"
    m = re.search(r"tokens?[_ :]?\s*(\d+)", output, re.IGNORECASE)
    if m:
        val = int(m.group(1))
        if val >= 1000:
            return f"{val // 1000}k tokens"
        return f"{val} tokens"
    return ""
