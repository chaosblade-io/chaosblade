"""Header renderer — one-shot brand banner at startup."""

from __future__ import annotations

from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.state import SessionState
from chaos_agent.tui.theme import Theme


def print_banner(console: ChaosConsole, state: SessionState) -> None:
    try:
        from chaos_agent import __version__
        version = __version__
    except Exception:
        version = "dev"

    icon, label, _ = strings.MODE_CONFIG.get(
        state.permission_mode.value, ("🔒", "确认", "")
    )

    text = Text()
    text.append(f"✻ {strings.BRAND_NAME}", style=f"bold {Theme.gradient_bright}")
    text.append(f"  v{version}", style=f"italic {Theme.gradient_dim}")
    text.append("  │  ", style=Theme.gradient_dim)
    text.append(f"cluster: {state.cluster_name or '(auto)'}", style=Theme.gradient_mid)
    text.append("  │  ", style=Theme.gradient_dim)
    text.append(f"ns: {state.namespace}", style=Theme.gradient_mid)
    text.append("  │  ", style=Theme.gradient_dim)
    text.append(f"mode: {icon} {label}", style=Theme.gradient_mid)
    console.print(text)
