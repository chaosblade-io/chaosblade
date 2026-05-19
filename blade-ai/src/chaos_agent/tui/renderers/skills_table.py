"""Skills renderers — `/skills list` and `/skills show`.

Both renderers source their data directly from a live :class:`SkillRegistry`
plus the disabled-list and resolved skills directory; the dispatcher passes
those in so this module stays free of side effects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chaos_agent.skills.models import SkillMetadata
from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.theme import Colors


def _short(text: str, n: int) -> str:
    if not text:
        return "—"
    return text if len(text) <= n else text[: n - 1] + "…"


def render_skills_list(
    console: ChaosConsole,
    *,
    registry,
    skills_dir: Path,
    disabled: Iterable[str],
    builtin_blocked: Iterable[str] = (),
) -> None:
    """Render `/skills list` — every loaded + disabled skill.

    Args:
        registry: ``SkillRegistry`` with already loaded skills.
        skills_dir: Resolved skills directory (for the header line).
        disabled: Names of skills the user has disabled in config.
        builtin_blocked: Skill names that conflict with built-in slash
            commands (filtered out of the dynamic command set).
    """
    metas: dict[str, SkillMetadata] = registry.metadata
    disabled_set = set(disabled or ())
    blocked_set = set(builtin_blocked or ())

    console.print_text(
        f"skills_dir: {skills_dir}",
        style=Colors.MUTED,
    )

    table = Table(
        title=Text(
            f"已加载技能（{len(metas)} 项；禁用 {len(disabled_set)} 项）",
            style=f"bold {Colors.BRAND}",
        ),
        title_justify="left",
        header_style="bold",
        border_style=Colors.MUTED,
        expand=False,
        pad_edge=False,
    )
    table.add_column("Name", overflow="fold")
    table.add_column("Version", overflow="fold")
    table.add_column("Category", overflow="fold")
    table.add_column("Target", overflow="fold")
    table.add_column("Source", overflow="fold")
    table.add_column("Status", overflow="fold")

    rows = sorted(metas.items(), key=lambda kv: kv[0])
    if not rows and not disabled_set:
        console.print_text("  当前没有可用技能", style=Colors.WARNING)
        return

    for name, meta in rows:
        skill_dir = registry.get_skill_dir(name)
        flags: list[str] = []
        status_style = Colors.SUCCESS
        if name in blocked_set:
            flags.append("(blocked by built-in)")
            status_style = Colors.WARNING
        flags_str = " ".join(flags) if flags else "enabled"
        table.add_row(
            name,
            meta.version or "—",
            _short(meta.category or "—", 18),
            _short(meta.target or "—", 24),
            _short(str(skill_dir) if skill_dir else "—", 60),
            Text(flags_str, style=status_style),
        )

    # Disabled skills are not in the registry; surface them so users can
    # find the canonical name to re-enable.
    for name in sorted(disabled_set - set(metas.keys())):
        table.add_row(
            name,
            "—",
            "—",
            "—",
            "—",
            Text("disabled", style=Colors.MUTED),
        )

    console.print(table)


def render_skill_show(
    console: ChaosConsole,
    *,
    registry,
    skill_name: str,
) -> None:
    """Render `/skills show <name>` — metadata + scripts + resources head."""
    meta = registry.get_metadata(skill_name)
    if meta is None:
        console.print_text(
            f"  未找到技能: {skill_name}", style=Colors.ERROR
        )
        return

    skill_dir = registry.get_skill_dir(skill_name)

    info = Table(show_header=False, box=None, pad_edge=False, expand=False)
    info.add_column(style=Colors.MUTED, no_wrap=True)
    info.add_column(overflow="fold")
    info.add_row("name", meta.name)
    info.add_row("version", meta.version or "—")
    info.add_row("category", meta.category or "—")
    info.add_row("target", meta.target or "—")
    info.add_row("required_tools", ", ".join(meta.required_tools) or "—")
    info.add_row("tags", ", ".join(meta.tags) or "—")
    info.add_row("description", meta.description or "—")
    info.add_row("dir", str(skill_dir or "—"))

    blocks = [info]

    if meta.scripts:
        scripts_t = Table(
            title=Text("Scripts", style=f"bold {Colors.ACCENT}"),
            title_justify="left",
            header_style="bold",
            border_style=Colors.MUTED,
            expand=False,
            pad_edge=False,
        )
        scripts_t.add_column("Name")
        scripts_t.add_column("Description", overflow="fold")
        scripts_t.add_column("Params", overflow="fold")
        for s in meta.scripts:
            params = ", ".join(
                f"{p.name}{'*' if p.required else ''}:{p.type}"
                for p in s.parameters
            ) or "—"
            scripts_t.add_row(s.name, _short(s.description, 60), params)
        blocks.append(scripts_t)

    try:
        resources = registry.list_resources(skill_name)
    except Exception:
        resources = []
    if resources:
        head = resources[:12]
        tail = len(resources) - len(head)
        text = "\n".join(f"  - {r}" for r in head)
        if tail > 0:
            text += f"\n  ... 还有 {tail} 个文件"
        blocks.append(
            Panel(
                Text(text),
                title=Text("Resources", style=f"bold {Colors.ACCENT}"),
                title_align="left",
                border_style=Colors.MUTED,
                expand=False,
            )
        )

    console.print(
        Panel(
            Group(*blocks),
            title=Text(f"技能: {skill_name}", style=f"bold {Colors.BRAND}"),
            title_align="left",
            border_style=Colors.BRAND,
            expand=False,
        )
    )
