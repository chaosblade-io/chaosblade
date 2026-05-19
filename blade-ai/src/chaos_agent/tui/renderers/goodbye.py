"""Goodbye renderer — farewell panel with session stats at exit."""

from __future__ import annotations

import time

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.state import SessionState
from chaos_agent.tui.theme import Theme

# Route through Theme to keep the Okabe-Ito colorblind-safe palette
# consistent across the TUI.
_ACCENT = Theme.gradient_bright
_OK = Theme.state_ok
_FAIL = Theme.state_err


def _fmt_duration(seconds: float) -> str:
    total = int(seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def _injection_value(state: SessionState) -> Text:
    t = Text()
    t.append(f"{state.injection_count} 次", style="bold")
    if state.injection_count > 0:
        t.append("  (")
        t.append(f"✓ {state.injection_success}", style=_OK)
        if state.injection_fail > 0:
            t.append(" · ", style="dim")
            t.append(f"✗ {state.injection_fail}", style=_FAIL)
        t.append(")")
    return t


def _kv_table(rows: list[tuple[str, object]]) -> Table:
    table = Table(show_header=False, box=None, pad_edge=False, padding=(0, 0))
    table.add_column(width=5)              # indent
    table.add_column(min_width=18)         # key (CJK-aware)
    table.add_column(justify="left")       # value
    for key, value in rows:
        key_text = Text(key, style="dim")
        val_text = value if isinstance(value, Text) else Text(str(value))
        table.add_row("", key_text, val_text)
    return table


def _section_header(label: str) -> Text:
    return Text(f"   {label}", style=f"bold {_ACCENT}")


def print_card(console: ChaosConsole, state: SessionState) -> None:
    duration = _fmt_duration(time.time() - state.session_start_ts)
    cluster_ns = f"{state.cluster_name or '(auto)'} / {state.namespace}"

    body = Group(
        Text(""),
        Text("   感谢使用 blade-ai，期待下次再见", style="bold"),
        Text(""),
        _section_header("会话概览"),
        _kv_table([
            ("会话 ID", Text(state.tui_session_id, style="dim")),
            ("持续时间", duration),
            ("集群 / 命名空间", cluster_ns),
        ]),
        Text(""),
        _section_header("活动统计"),
        _kv_table([
            ("消息交互", f"{state.message_count} 次"),
            ("故障注入", _injection_value(state)),
            ("故障恢复", f"{state.recovery_count} 次"),
        ]),
        Text(""),
    )

    title = Text()
    title.append("✻ 再见", style=f"bold {_ACCENT}")

    panel = Panel(
        body,
        title=title,
        title_align="left",
        border_style=_ACCENT,
        padding=(0, 1),
    )
    console.print(panel)
