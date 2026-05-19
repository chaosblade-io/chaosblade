"""Shared layout helpers — small renderable factories used by multiple cards.

PR-C2 (§17.5 i18n / CJK alignment): ``ljust(N)`` aligns by Python character
count, but in a CJK locale a Han character occupies 2 terminal columns. The
old framed bodies in ``confirm.py`` / ``intent_confirm.py`` / ``result.py``
all hand-built ``┌─…─┐`` boxes with ``ljust``-padded rows, which drift by
half a column for every Chinese glyph rendered. Switching to a
``rich.table.Table`` lets Rich's own width logic (``cell_len``) do the
padding — Han glyphs get 2 cells, ASCII gets 1, and the box edges line up
on every locale.

These helpers are intentionally tiny: each card composes them with its own
preamble/footer text (warnings, action hints, divider) into a Group. We
export factories rather than rendered objects so the caller can adjust
``min_width`` / ``border_style`` per situation.
"""

from __future__ import annotations

from typing import Iterable, Tuple

from rich import box
from rich.console import RenderableType
from rich.padding import Padding
from rich.table import Table

from chaos_agent.tui.theme import Colors


def make_field_table(
    fields: Iterable[Tuple[str, object]],
    *,
    label_min_width: int = 12,
    value_min_width: int = 38,
    label_style: str = "bold",
    value_style: str | None = None,
    boxed: bool = True,
    indent: int = 2,
) -> RenderableType:
    """Build a 2-column key/value Table, CJK-safe.

    ``fields`` is an iterable of ``(label, value)`` pairs. The label gets
    a trailing colon automatically (``"Fault Type"`` → ``"Fault Type:"``).

    ``boxed=True`` draws a single-line frame matching the old hand-drawn
    ``┌─…─┐`` look but uses Rich's box module so the frame width adapts
    to the content width Rich measures. ``boxed=False`` drops the frame
    and is used by ``result.py`` where the surrounding Panel already
    provides the visual container.

    ``indent`` is applied via ``rich.padding.Padding`` so the table edge
    isn't drawn flush at column 0 — this matches the leading spaces the
    caller's surrounding Text was using.
    """
    table = Table(
        show_header=False,
        show_edge=boxed,
        box=box.SQUARE if boxed else None,
        padding=(0, 1),
        border_style=Colors.DIM,
    )
    table.add_column(
        "label",
        style=label_style,
        min_width=label_min_width,
        no_wrap=True,
    )
    table.add_column(
        "value",
        style=value_style or "",
        min_width=value_min_width,
        overflow="fold",
    )
    for label, value in fields:
        table.add_row(f"{label}:", str(value))
    if indent > 0:
        return Padding(table, (0, 0, 0, indent))
    return table
