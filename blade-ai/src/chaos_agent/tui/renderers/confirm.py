"""Confirm renderer — structured confirmation panel with double border.

Renders a visually prominent confirmation dialog with:
- Double-line border for attention
- Structured plan display (scope/target/action/labels)
- Color-coded safety status
- Clear action buttons
"""

from __future__ import annotations

from typing import Optional

from prompt_toolkit import PromptSession
from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from chaos_agent.tui import strings
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.renderers._layout import make_field_table
from chaos_agent.tui.theme import Borders, Colors, Icons


async def run(
    console: ChaosConsole,
    info: dict,
    session: Optional[PromptSession] = None,
) -> str:
    """Render a confirmation panel and return 'approved' or 'rejected'."""
    plan_summary = info.get("plan_summary", "(no plan summary)")
    safety_status = info.get("safety_status", "")
    safety_reason = info.get("safety_reason", "")

    # Plan preamble
    pre = Text()
    pre.append("\n")
    pre.append("  \U0001f4cb Plan\n", style="bold")

    # Plan fields — Rich Table replaces the old hand-drawn ┌─…─┐ with
    # ljust-padded rows so CJK values (Chinese namespaces, error texts)
    # don't shift the closing │ by half a column. ``make_field_table``
    # uses Rich's cell-aware width logic.
    plan_fields = info.get("plan_fields") or {}
    if plan_fields:
        plan_renderable = make_field_table(
            plan_fields.items(), label_min_width=12, value_min_width=40
        )
    else:
        plan_renderable = Text(f"  {plan_summary}\n")

    # Footer — safety badge + action hint
    post = Text()
    post.append("\n")
    if safety_status:
        post.append("  \U0001f6e1 Safety: ", style="bold")
        if safety_status == "safe":
            post.append(f"{Icons.SUCCESS} SAFE", style=f"bold {Colors.SUCCESS}")
        elif safety_status == "warning":
            post.append(f"{Icons.WARNING} WARNING", style=f"bold {Colors.WARNING}")
        elif safety_status == "blocked":
            post.append(f"{Icons.FAIL} BLOCKED", style=f"bold {Colors.ERROR}")
        else:
            post.append(f"{safety_status}", style=Colors.WARNING)
        if safety_reason:
            post.append(f" \u2014 {safety_reason}", style=Colors.DIM)
        post.append("\n")

    post.append("\n")
    post.append("  " + "\u2500" * 52 + "\n", style=Colors.DIM)
    post.append("  [Y] Approve    [N] Reject", style="bold")
    post.append("\n")

    body = Group(pre, plan_renderable, post)

    # Build title
    title = Text()
    title.append(f" {Icons.WARNING} ", style=f"bold {Colors.WARNING}")
    title.append(strings.CONFIRM_TITLE, style=f"bold {Colors.WARNING}")

    panel = Panel(
        body,
        title=title,
        border_style=Borders.CONFIRM,
        box=box.DOUBLE,
        padding=(0, 1),
    )
    console.print(panel)

    # Bell to get user attention
    console.bell()

    sess = session or PromptSession()
    answer = await sess.prompt_async("Approve? [Y/n]: ")
    answer = (answer or "").strip().lower()
    if answer in ("", "y", "yes"):
        return "approved"
    return "rejected"
