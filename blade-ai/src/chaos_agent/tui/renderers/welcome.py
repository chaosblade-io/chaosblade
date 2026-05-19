"""Welcome renderer — big branded welcome panel at startup."""

from __future__ import annotations

import os
from pathlib import Path

from rich import box
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.state import SessionState
from chaos_agent.tui.theme import Theme

# Compact logo — uniform width keeps centering crisp.
_LOGO_LINES = [
    "█▄▄ █   ▄▀█ █▀▄ █▀▀  ▄▀█ █",
    "█▄█ █▄▄ █▀█ █▄▀ ██▄  █▀█ █",
]


def _pretty_path(path: str, max_len: int = 32) -> str:
    """Compact a filesystem path for display: ``$HOME`` → ``~`` and
    over-long middles collapse to ``.../<basename>``.
    """
    if not path:
        return ""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    if len(path) <= max_len:
        return path
    return ".../" + Path(path).name


def _build_left(state: SessionState, version: str) -> Text:
    icon, label, _ = strings.MODE_CONFIG.get(
        state.permission_mode.value, ("🔒", "确认", "")
    )

    try:
        from chaos_agent.config.settings import settings
        model_name = settings.model_name or "unknown"
    except Exception:
        model_name = "unknown"

    t = Text(justify="center")
    t.append("\n\n")
    t.append("Welcome back!\n", style=Theme.text_secondary)
    t.append("\n")
    for line in _LOGO_LINES:
        t.append(line + "\n", style=f"bold {Theme.gradient_bright}")
    t.append("\n")
    t.append(f"{model_name}\n", style=Theme.gradient_mid)
    t.append(f"mode: {icon} {label}", style=Theme.text_muted)
    t.append("\n\n")
    return t


def _build_right(state: SessionState) -> Text:
    try:
        from chaos_agent.config.settings import settings
        kubeconfig = _pretty_path(settings.kubeconfig_path) or "(default)"
    except Exception:
        kubeconfig = "(default)"

    t = Text()
    t.append("Tips for getting started\n", style=f"bold {Theme.gradient_bright}")
    t.append("\n")
    for tip in strings.TIPS:
        t.append("    ", style=Theme.text_muted)
        t.append(f"•  ", style=Theme.text_muted)
        t.append(f"{tip}\n")
    t.append("\n")
    t.append("  ─────────────────────────────\n", style=Theme.divider)
    t.append("\n")
    t.append("Runtime\n", style=f"bold {Theme.gradient_bright}")
    t.append("\n")
    t.append("  kubeconfig: ", style=Theme.text_muted)
    t.append(f"{kubeconfig}\n", style=Theme.text_secondary)
    t.append("  namespace:  ", style=Theme.text_muted)
    t.append(f"{state.namespace}\n", style=Theme.text_secondary)
    t.append("\n")
    t.append(strings.WELCOME_CARD_HINT, style=f"italic {Theme.text_muted}")
    return t


def print_card(console: ChaosConsole, state: SessionState) -> None:
    try:
        from chaos_agent import __version__
        version = __version__
    except Exception:
        version = "dev"

    # Two-column layout with branded vertical separator.
    # box=MINIMAL draws a │ between columns; border_style matches the
    # outer panel border so the separator is visually part of the frame,
    # not a stray gray line.
    table = Table(
        show_header=False,
        show_edge=False,
        box=box.MINIMAL,
        border_style=Theme.border_focus,
        pad_edge=False,
        padding=(0, 4),
        expand=True,
    )
    table.add_column(justify="center", ratio=2, no_wrap=False)
    table.add_column(justify="left", ratio=3, no_wrap=False)
    table.add_row(_build_left(state, version), _build_right(state))

    panel = Panel(
        table,
        title=f"✻ {strings.BRAND_NAME} v{version}",
        title_align="left",
        border_style=Theme.border_focus,
        padding=(1, 2),
    )
    console.print(panel)