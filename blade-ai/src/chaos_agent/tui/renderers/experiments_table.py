"""Experiments table renderer — `/experiments`.

Sourced from ``AgentRunner.list_skills()``. The envelope contains a
``categories`` list, each with a ``faults`` array; we render one Table
per category so users can see the full catalog at a glance.
"""

from __future__ import annotations

from rich.table import Table
from rich.text import Text

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Colors


def _short(s: str, n: int) -> str:
    if not s:
        return "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def render_experiments(console: ChaosConsole, envelope: dict) -> None:
    if (envelope or {}).get("code") != 0:
        msg = (envelope or {}).get("message") or "查询故障实验失败"
        console.print_text(f"  {msg}", style=Colors.ERROR)
        return

    data = (envelope or {}).get("data") or {}
    categories = data.get("categories") or []
    total = data.get("total") or sum(len(c.get("faults") or []) for c in categories)

    if not categories:
        console.print_text("  暂无可用故障实验", style=Colors.MUTED)
        return

    console.print_text(
        f"故障实验目录  •  共 {total} 项  •  {len(categories)} 类",
        style=f"bold {Colors.BRAND}",
    )

    for cat in categories:
        name = cat.get("category") or "其他"
        desc = cat.get("description") or ""
        faults = cat.get("faults") or []
        if not faults:
            continue

        table = Table(
            title=Text(f"[{name}] {desc}", style=f"bold {Colors.ACCENT}"),
            title_justify="left",
            header_style="bold",
            border_style=Colors.MUTED,
            expand=False,
            pad_edge=False,
        )
        table.add_column("场景", overflow="fold")
        table.add_column("症状", overflow="fold")
        table.add_column("示例命令", overflow="fold")

        for f in faults:
            use_case = f.get("use_case_name") or f.get("name") or "—"
            symptom = f.get("fault_symptom") or f.get("description") or "—"
            example = f.get("example_cmd") or "—"
            table.add_row(
                _short(use_case, 32),
                _short(symptom, 60),
                _short(example, 80),
            )
        console.print(table)
