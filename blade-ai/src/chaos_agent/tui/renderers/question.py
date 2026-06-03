"""Question renderer — print a question + collect a free-text answer."""

from __future__ import annotations

from typing import Optional

from prompt_toolkit import PromptSession
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Theme


async def run(
    console: ChaosConsole,
    info: dict,
    session: Optional[PromptSession] = None,
) -> str:
    """Render a question prompt and return the user's free-text answer."""
    content = info.get("content", "Please provide your input:")
    panel = Panel(
        Text(content),
        title=f"ℹ {strings.QUESTION_TITLE}",
        border_style=Theme.gradient_bright,
    )
    console.print(panel)

    sess = session or PromptSession()
    answer = await sess.prompt_async("> ")
    return (answer or "").strip()
