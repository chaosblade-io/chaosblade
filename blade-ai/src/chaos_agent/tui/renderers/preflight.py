"""Preflight renderer — run checks and print the results.

Returns the action chosen by the user when the ChaosBlade Operator is
missing: "install_helm" / "install_kubectl" / "skip".
"""

from __future__ import annotations

from typing import Optional

from prompt_toolkit import PromptSession
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.preflight import CheckResult, needs_operator_install, run_tui_checks
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Theme

# Route through Theme so the Okabe-Ito colorblind-safe palette
# applies here as well; defining a separate hex would silently drift
# from the rest of the TUI on a future a11y tweak.
_ACCENT = Theme.gradient_bright
_OK = Theme.state_ok       # Okabe-Ito bluish-green (was Material #66bb6a)
_WARN = Theme.state_warn
_FAIL = Theme.state_err    # Okabe-Ito vermilion (was Material #ef5350)


def _status_color(results: list[CheckResult]) -> str:
    if any(not r.passed and r.severity == "blocking" for r in results):
        return _FAIL
    if any(not r.passed for r in results):
        return _WARN
    return _OK


def _sort_key(r: CheckResult) -> int:
    """Sort blocking failures first, then warnings, then passes."""
    if not r.passed and r.severity == "blocking":
        return 0
    if not r.passed:
        return 1
    return 2


def _build_body(results: list[CheckResult]):
    table = Table(
        show_header=False,
        box=None,
        pad_edge=False,
        padding=(0, 1),
    )
    table.add_column(justify="center", no_wrap=True, width=2)
    table.add_column(justify="left", no_wrap=True, min_width=22)
    table.add_column(justify="left", overflow="fold")

    from chaos_agent.tui.theme import Theme

    sorted_results = sorted(results, key=_sort_key)
    for r in sorted_results:
        if r.passed:
            icon = Text("✓", style=f"bold {_OK}")
            name = Text(r.name, style=f"bold {Theme.gradient_mid}")
            status = Text("通过", style=_OK)
        elif r.severity == "blocking":
            icon = Text("✗", style=f"bold {_FAIL}")
            name = Text(r.name, style=f"bold {_FAIL}")
            status = Text(r.message or "blocking", style=_FAIL)
        else:
            icon = Text("⚠", style=f"bold {_WARN}")
            name = Text(r.name, style=f"bold {Theme.gradient_mid}")
            status = Text(r.message or "warning", style=Theme.text_secondary)
        table.add_row(icon, name, status)

    fixes = [r for r in sorted_results if not r.passed and r.fix]
    if not fixes:
        return table

    hint = Text()
    hint.append("\n")
    hint.append("  建议修复\n", style="bold dim")
    for r in fixes:
        for line in r.fix.split("\n"):
            hint.append(f"    {line}\n", style="dim")
    # trim trailing newline
    if hint.plain.endswith("\n"):
        hint = hint[: len(hint.plain) - 1]

    return Group(table, hint)


def _render_results(console: ChaosConsole, results: list[CheckResult]) -> None:
    passed = sum(1 for r in results if r.passed)
    total = len(results)
    blocking_fail = sum(1 for r in results if not r.passed and r.severity == "blocking")
    warning_fail = sum(1 for r in results if not r.passed and r.severity != "blocking")

    title = Text()
    title.append("✻ 环境自检", style=f"bold {_ACCENT}")
    title.append("   ", style="dim")
    title.append(f"{passed}/{total} 通过", style=f"bold {_status_color(results)}")
    if blocking_fail:
        title.append(" · ", style="dim")
        title.append(f"{blocking_fail} 阻塞", style=f"bold {_FAIL}")
    if warning_fail:
        title.append(" · ", style="dim")
        title.append(f"{warning_fail} 警告", style=f"bold {_WARN}")

    panel = Panel(
        _build_body(results),
        title=title,
        title_align="left",
        border_style=_ACCENT,
        padding=(0, 1),
    )
    console.print(panel)


async def run_and_render(
    console: ChaosConsole,
    session: Optional[PromptSession] = None,
) -> tuple[list[CheckResult], str]:
    """Run preflight checks, print results, and (optionally) prompt for action.

    Returns (results, action) where action is "" if no install prompt was shown.
    """
    results = await run_tui_checks()
    _render_results(console, results)

    action = ""
    has_blocking = any(not r.passed and r.severity == "blocking" for r in results)
    if has_blocking:
        return results, action
    if needs_operator_install(results):
        console.print("")
        console.print_text(
            "选择 ChaosBlade Operator 安装方式: [h] Helm  [k] kubectl apply  [s] 跳过",
            style="bold",
        )
        sess = session or PromptSession()
        choice = await sess.prompt_async("Choice [s]: ")
        choice = (choice or "").strip().lower()
        if choice == "h":
            action = "install_helm"
        elif choice == "k":
            action = "install_kubectl"
        else:
            action = "skip"

    return results, action
