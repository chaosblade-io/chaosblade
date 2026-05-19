"""PR-E4 — baseline / post-recovery comparison panel.

Renders a ``BaselineSnapshot`` (see ``tui/baseline.py``) as a panel
of side-by-side bars: pre-injection on the left, post-recovery on
the right, with the percent-change delta stamped under each row.
Recovered samples (post within tolerance of pre) get a green check;
unrecovered ones get an amber warning. Incomplete samples (one side
missing) render a dim placeholder so the user can see what data
*didn't* arrive without conflating that with a real regression.

Intended call sites:

* ``result.render_result`` — when the task wraps up, call
  ``render_baseline_compare`` with the snapshot pulled from state
  for that task_id. The panel sits beneath the verification section.
* ``/show E#`` — same panel, re-rendered from the snapshot stored
  on the locator's payload.

Calm mode hides the panel entirely; working / dense show it.
"""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui.baseline import (
    BaselineSample,
    BaselineSnapshot,
    PHASE_POST,
    PHASE_PRE,
)
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.state import DisplayMode
from chaos_agent.tui.theme import Colors, Icons

# Width of the inner bar — kept short enough to render on 80-col
# terminals after the label / value columns. The bar is ASCII-block
# so it survives PR-B4's ASCII fallback unchanged.
_BAR_WIDTH = 16


def _bar(value: float, scale: float, width: int = _BAR_WIDTH) -> str:
    """Render a horizontal bar proportional to ``value / scale``.

    ``scale`` is the larger of pre/post, so both bars share the same
    axis and the user can eyeball the ratio without computing it.
    Clamps to [0, width]; negative values render empty.
    """
    if scale <= 0 or value <= 0:
        return "\u2591" * width
    filled = int(round((value / scale) * width))
    filled = max(0, min(width, filled))
    return "\u2588" * filled + "\u2591" * (width - filled)


def _format_value(value: float, unit: str) -> str:
    """Round to 2 decimals and append the unit."""
    if value == int(value):
        body = str(int(value))
    else:
        body = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{body}{unit}".strip()


def _render_sample(sample: BaselineSample) -> Text:
    """One sample → 3-line block: label, pre row, post row, delta."""
    pre = sample.get(PHASE_PRE)
    post = sample.get(PHASE_POST)
    scale = max(pre or 0.0, post or 0.0, 1.0)

    body = Text()
    body.append(f"{sample.display_label()}\n", style="bold")

    body.append("  注入前 ", style=Colors.DIM)
    if pre is None:
        body.append("(缺失)\n", style=Colors.DIM)
    else:
        body.append(_bar(pre, scale), style=Colors.DIM)
        body.append(f"  {_format_value(pre, sample.unit)}\n")

    body.append("  恢复后 ", style=Colors.DIM)
    if post is None:
        body.append("(等待中)\n", style=Colors.DIM)
    else:
        delta_pct = sample.delta_percent()
        recovered = sample.recovered()
        bar_style = Colors.SUCCESS if recovered else Colors.WARNING
        body.append(_bar(post, scale), style=bar_style)
        body.append(f"  {_format_value(post, sample.unit)}")
        if delta_pct is not None:
            sign = "+" if delta_pct >= 0 else ""
            tag_style = Colors.SUCCESS if recovered else Colors.WARNING
            tag = (
                f"  \u0394 {sign}{delta_pct:.1f}%"
                + (f" {Icons.SUCCESS}" if recovered else f" {Icons.WARNING}")
            )
            body.append(tag, style=tag_style)
        body.append("\n")
    return body


def build_panel(
    snapshot: BaselineSnapshot,
    *,
    display_mode: DisplayMode = DisplayMode.WORKING,
) -> Panel | None:
    """Assemble the comparison panel; returns None when nothing to show.

    Returns None in three cases:
      1. ``display_mode`` is calm — user opted out of decoration.
      2. The snapshot is empty (no samples recorded).
      3. The snapshot has samples but none have *any* values yet
         (e.g. baseline_capture fired but didn't produce numbers).
    """
    if display_mode == DisplayMode.CALM:
        return None
    if not snapshot.has_any():
        return None

    pieces: list[Text] = []
    samples = list(snapshot.samples.values())
    for i, sample in enumerate(samples):
        if i > 0:
            pieces.append(Text(""))  # blank line between samples
        pieces.append(_render_sample(sample))

    title = Text()
    title.append(" 基线对比 ", style=f"bold {Colors.ACCENT}")

    border_style = Colors.SUCCESS if snapshot.is_complete() else Colors.DIM
    return Panel(
        Group(*pieces),
        title=title,
        border_style=border_style,
        padding=(0, 1),
    )


def render(
    console: ChaosConsole,
    snapshot: BaselineSnapshot,
    *,
    display_mode: DisplayMode = DisplayMode.WORKING,
) -> None:
    """Print the comparison panel; no-op when ``build_panel`` returns None."""
    panel = build_panel(snapshot, display_mode=display_mode)
    if panel is None:
        return
    console.print(panel)
