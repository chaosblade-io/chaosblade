"""Help panel renderer — pretty `/help` output.

Layout:
    ┌── ⌘ Slash Commands ───────────────────────────────────────┐
    │                                                            │
    │  通用                                                      │
    │  /help               Show help              ...            │
    │  ...                                                       │
    │                                                            │
    │  业务                                                      │
    │  /run    <NL>        Inject a fault         ...            │
    │  ...                                                       │
    │                                                            │
    │  键位                                                      │
    │  Enter        Submit / apply selected slash command        │
    │  ...                                                       │
    │                                                            │
    └────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.tui.commands import SlashCommandRegistry
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Theme

_KEY_BINDINGS: tuple[tuple[str, str], ...] = (
    ("Enter", "提交输入；在 / 菜单中应用所选命令"),
    ("Shift+Enter / Alt+Enter / Ctrl+J", "插入换行（多行输入）"),
    ("↑ / ↓", "在 / 菜单中切换候选"),
    ("Tab", "在 / 菜单中应用所选命令"),
    ("Esc", "在 / 菜单中清空输入并关闭菜单"),
    ("Shift+Tab", "切换权限模式（confirm ↔ auto）"),
    ("Ctrl+C", "中断当前任务并触发自动恢复"),
)


def _build_command_table(registry: SlashCommandRegistry) -> Table:
    """Render commands grouped by category, with subcommands indented."""
    table = Table(
        show_header=True,
        header_style=f"bold {Theme.gradient_bright}",
        show_edge=False,
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 3),
        expand=False,
    )
    table.add_column("Command", style=f"bold {Theme.gradient_mid}", no_wrap=True)
    table.add_column("Usage", style="dim", no_wrap=True)
    table.add_column("Description", style="default", overflow="fold")

    grouped = registry.list_by_group()
    first = True
    last_label: str | None = None
    for group in registry.group_order():
        cmds = grouped.get(group) or []
        if not cmds:
            continue
        label = registry.group_label(group)
        if label != last_label:
            if not first:
                table.add_row("", "", "")
            table.add_row(
                Text(f"[{label}]", style=f"bold {Theme.role_agent}"),
                "",
                "",
            )
            last_label = label
        first = False
        for cmd in cmds:
            table.add_row(cmd.name, cmd.usage or "", cmd.description)
            for sub in sorted(cmd.subcommands.values(), key=lambda c: c.name):
                table.add_row(
                    Text(f"  └ {sub.name}", style=Theme.gradient_mid),
                    sub.usage or "",
                    sub.description,
                )
    return table


def _build_key_table() -> Table:
    """Sub-table listing key bindings."""
    table = Table(
        show_header=True,
        header_style=f"bold {Theme.gradient_bright}",
        show_edge=False,
        box=box.SIMPLE_HEAD,
        pad_edge=False,
        padding=(0, 3),
        expand=False,
    )
    # Route key style through Theme so the Okabe-Ito colorblind-safe
    # palette propagates here too — `style=` accepts any Rich-style
    # color spec, hex included.
    table.add_column("Key", style=f"bold {Theme.state_ok}", no_wrap=True)
    table.add_column("Description", style="default", overflow="fold")
    table.add_row(Text("[键位]", style=f"bold {Theme.role_agent}"), "")
    for key, desc in _KEY_BINDINGS:
        table.add_row(key, desc)
    return table


def print_panel(console: ChaosConsole, registry: SlashCommandRegistry) -> None:
    """Render the slash-command help as a bordered panel with grouped tables."""
    cmd_table = _build_command_table(registry)
    key_table = _build_key_table()

    hint = Text()
    hint.append("提示: ", style="dim")
    hint.append("输入 ", style="dim")
    hint.append("/", style=f"bold {Theme.gradient_mid}")
    hint.append(" 后用 ", style="dim")
    hint.append("↑↓", style="bold")
    hint.append(" 选择，", style="dim")
    hint.append("Enter/Tab", style="bold")
    hint.append(" 选中，", style="dim")
    hint.append("Esc", style="bold")
    hint.append(" 取消。", style="dim")

    panel = Panel(
        Group(cmd_table, Text(""), key_table),
        title="⌘ Slash Commands",
        title_align="left",
        subtitle=hint,
        subtitle_align="left",
        border_style=Theme.gradient_bright,
        padding=(1, 2),
    )
    console.print(panel)
