"""CJK alignment tests for ``_layout.make_field_table`` (PR-C2 / §17.5).

These lock the contract that the field table renders with terminal-cell
alignment, not Python-character alignment. Before PR-C2, the equivalent
hand-built ``┌─…─┐`` blocks padded values via ``ljust(N)`` which counts
Han characters as 1 — but every modern terminal renders them as 2 cells
wide. The visual symptom: the closing ``│`` walked left for every
Chinese glyph in the value column, breaking the box on every CJK row.

The Rich Table path uses ``cell_len`` internally, so the right edge is
fixed regardless of glyph width. We verify that here.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from chaos_agent.tui.renderers._layout import make_field_table

pytestmark = pytest.mark.usefixtures("require_unicode_locale")


def _render(table) -> list[str]:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=80).print(table)
    return buf.getvalue().splitlines()


class TestFieldTableCjkAlignment:
    """The box must keep its right edge aligned across rows with mixed
    ASCII and CJK widths. If a future change reverts to ``ljust``, the
    closing ``│`` will drift on the Chinese rows and the column-stop
    assertion below fails."""

    def test_box_right_edge_constant_across_mixed_width_rows(self):
        # Mix wide CJK values with narrow ASCII to guarantee any
        # cell-counting bug actually shifts something.
        from rich.cells import cell_len

        fields = [
            ("Namespace", "cms-demo"),                     # all ASCII, narrow
            ("故障类型", "CPU 满载"),                       # all CJK, wide
            ("Target", "cpu"),                              # all ASCII
            ("描述", "在 staging 集群对 web-2 注入"),         # mixed CJK+ASCII
        ]
        table = make_field_table(
            fields, label_min_width=12, value_min_width=38, indent=0
        )
        lines = [line.rstrip() for line in _render(table) if line.strip()]
        # Right edge alignment is in *terminal cells*, not Python chars —
        # CJK glyphs are 1 char but 2 cells. ``cell_len`` is what every
        # modern terminal uses for column accounting. If ljust(N) creeps
        # back in, the cell-len of CJK rows will be 1 cell short per
        # Han glyph.
        widths = {cell_len(line) for line in lines if line.endswith("\u2502")}
        assert (
            len(widths) == 1
        ), f"right edge drifted in cell-cols: {widths} | lines: {lines!r}"

    def test_pure_cjk_row_does_not_break_box(self):
        # A single all-CJK row used to be the worst offender — `ljust`
        # would compute padding based on character count, not cell count,
        # so the row was visibly half-width.
        table = make_field_table(
            [("命名空间", "生产环境")], label_min_width=12, value_min_width=20, indent=0
        )
        lines = [line for line in _render(table) if "\u2502" in line]
        # The data row's right edge must be at the same column as the
        # box top (which has no CJK).
        top = next(
            line for line in _render(table) if line.startswith("\u250c")
        )
        data_row = next(line for line in lines if "命名空间" in line)
        assert top.rstrip().endswith("\u2510")
        assert data_row.rstrip().endswith("\u2502")
        # Trim trailing spaces — both ends should match in cell width.
        from rich.cells import cell_len

        assert cell_len(top.rstrip()) == cell_len(data_row.rstrip())

    def test_unboxed_table_has_no_edge_glyphs(self):
        """``boxed=False`` (used by result.py) drops the frame entirely
        because the surrounding Panel already provides a container."""
        table = make_field_table(
            [("Task ID", "t-1")],
            label_min_width=10,
            value_min_width=20,
            boxed=False,
            indent=0,
        )
        out = "\n".join(_render(table))
        for glyph in ("\u250c", "\u2510", "\u2514", "\u2518", "\u2502"):
            assert glyph not in out, f"unboxed table should not contain {glyph!r}"

    def test_field_table_uses_padding_not_ljust(self):
        """If a future contributor reintroduces ``ljust`` in this helper,
        the file source itself will fail this guardrail. We're not
        asserting on output here — we're asserting that the function
        body doesn't reach for the broken-on-CJK API."""
        import inspect

        from chaos_agent.tui.renderers import _layout

        src = inspect.getsource(_layout)
        assert ".ljust(" not in src, ".ljust use re-introduced — fails CJK alignment"
        assert ".rjust(" not in src, ".rjust use re-introduced — fails CJK alignment"
