"""Interrupted-tasks renderer — panel with ROUNDED border, matching preflight style."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Theme

_ACCENT = Theme.gradient_bright
_WARN = Theme.gradient_mid
_QUESTION = Theme.role_agent    # purple — matches agent role


def _interrupt_icon(task: dict) -> Text:
    info = task.get("interrupt_info") or {}
    kind = info.get("type")
    if kind == "confirmation":
        return Text("⚠", style=f"bold {_WARN}")
    if kind == "question":
        return Text("ℹ", style=f"bold {_QUESTION}")
    return Text("⏸", style="dim")


def _interrupt_label(task: dict) -> str:
    info = task.get("interrupt_info") or {}
    kind = info.get("type")
    if kind == "confirmation":
        return strings.INTERRUPTED_CONFIRMATION
    if kind == "question":
        return strings.INTERRUPTED_QUESTION
    return strings.INTERRUPTED_GENERIC


def _build_body(tasks: list[dict]) -> Group:
    table = Table(
        show_header=False,
        box=None,
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column(justify="center", no_wrap=True, width=2)
    table.add_column(justify="left", no_wrap=True, min_width=22)
    table.add_column(justify="left", overflow="fold")

    for t in tasks:
        task_id = t.get("task_id", "unknown")
        next_nodes = t.get("next_nodes") or []

        icon = _interrupt_icon(t)
        name = Text(task_id, style="bold")

        label = _interrupt_label(t)
        detail = Text()
        detail.append(label)
        if next_nodes:
            detail.append(f" ({', '.join(next_nodes)})", style="dim")
        detail.append("\n")
        detail.append(f"/recover {task_id}", style=_ACCENT)

        table.add_row(icon, name, detail)

    hint = Text()
    hint.append("\n")
    hint.append(f"  {strings.INTERRUPTED_TASKS_HINT}", style="dim")

    return Group(table, hint)


def render_interrupted_tasks(console: ChaosConsole, tasks: list[dict]) -> None:
    """Render the interrupted-tasks notification.

    Always shows a panel — even when empty (displays
    "没有未执行完的任务"). When non-empty, lists each
    task with its interrupt type and /recover hint.
    """
    if not tasks:
        title = Text()
        title.append(f"✻ {strings.INTERRUPTED_TASKS_TITLE}", style=f"bold {_ACCENT}")

        panel = Panel(
            Text(f"  {strings.INTERRUPTED_TASKS_NONE}", style=Theme.text_muted),
            title=title,
            title_align="left",
            border_style=_ACCENT,
            padding=(0, 1),
        )
        console.print(panel)
        return

    count = len(tasks)

    title = Text()
    title.append(f"✻ {strings.INTERRUPTED_TASKS_TITLE}", style=f"bold {_ACCENT}")
    title.append("   ", style="dim")
    title.append(f"{count} {strings.INTERRUPTED_TASKS_COUNT}", style=f"bold {_WARN}")

    panel = Panel(
        _build_body(tasks),
        title=title,
        title_align="left",
        border_style=_ACCENT,
        padding=(0, 1),
    )
    console.print(panel)
