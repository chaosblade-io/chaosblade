"""ChaosConsole — single Rich Console wrapper for all TUI output.

Centralizes stdout writes so that prompt_toolkit prompts and Rich Live
blocks don't compete for the cursor. Renderers and controllers should
go through this object only; nothing else should print directly.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme as RichTheme

from chaos_agent.tui.markdown import render_markdown
from chaos_agent.tui.theme import Highlights

# Style names referenced by the named-capture groups of the chaos
# inline highlighter (see ``tui/markdown.py``). Registered on the
# Rich Console so ``style="chaos_uid"`` etc. resolve everywhere.
_CHAOS_THEME = RichTheme(
    {
        "chaos_uid": Highlights.UID,
        "chaos_ip": Highlights.IP,
        "chaos_ns": Highlights.NAMESPACE,
        "chaos_ft": Highlights.FAULT_TYPE,
    }
)


class ChaosConsole:
    """Thin wrapper around `rich.console.Console`.

    The underlying Console is exposed via `.console` for use with
    `rich.live.Live`, `rich.progress.Progress`, etc.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("highlight", False)
        kwargs.setdefault("theme", _CHAOS_THEME)
        self._console = Console(**kwargs)

    @property
    def console(self) -> Console:
        return self._console

    def print(self, *args: Any, **kwargs: Any) -> None:
        self._console.print(*args, **kwargs)

    def print_markdown(self, content: str) -> None:
        if not content:
            return
        self._console.print(render_markdown(content))

    def print_text(self, content: str, style: str | None = None) -> None:
        if style:
            self._console.print(Text(content, style=style))
        else:
            self._console.print(content)

    def print_panel(
        self,
        renderable: Any,
        title: str | None = None,
        border_style: str = "",
    ) -> None:
        self._console.print(
            Panel(renderable, title=title, border_style=border_style or "dim")
        )

    def rule(self, title: str = "", style: str = "dim") -> None:
        self._console.rule(title, style=style)

    def bell(self) -> None:
        try:
            self._console.bell()
        except Exception:
            pass

    def clear(self) -> None:
        try:
            self._console.clear()
        except Exception:
            pass
