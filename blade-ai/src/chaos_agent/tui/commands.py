"""SlashCommandRegistry — extensible slash command system for the TUI.

Two-level command tree:
- Root command (e.g. ``/skills``) with optional subcommands (``/skills install``).
- Each command carries a ``group`` so the help panel and slash menu can
  render them in the four user-visible buckets: general / business / skills
  / dynamic.

Dynamic commands (one per loaded skill) are managed as a single group and
replaced atomically via :meth:`replace_dynamic` when skills reload.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Literal

logger = logging.getLogger(__name__)

CommandGroup = Literal["general", "business", "skills", "dynamic"]
_GROUP_ORDER: tuple[CommandGroup, ...] = ("general", "business", "skills", "dynamic")
_GROUP_LABELS: dict[CommandGroup, str] = {
    "general": "通用",
    "business": "业务",
    "skills": "技能",
    "dynamic": "技能",
}


@dataclass
class SlashCommand:
    """Definition of a slash command (root or subcommand).

    Subcommands are themselves ``SlashCommand`` instances stored in the
    parent's ``subcommands`` dict; their ``name`` is the bare sub token
    (no leading slash, e.g. ``"install"``).
    """

    name: str
    description: str
    handler: Callable[..., Coroutine[Any, Any, None]]
    usage: str = ""
    group: CommandGroup = "general"
    subcommands: dict[str, "SlashCommand"] = field(default_factory=dict)
    hidden: bool = False
    origin: str = ""  # source path for dynamic skill commands; empty for built-ins


class SlashCommandRegistry:
    """Registry for slash commands with two-level parsing."""

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    # ── registration ──────────────────────────────────────────────

    def register(
        self,
        name: str,
        description: str,
        handler: Callable,
        usage: str = "",
        group: CommandGroup = "general",
        hidden: bool = False,
        origin: str = "",
    ) -> SlashCommand:
        """Register (or replace) a root command. Returns the inserted record."""
        cmd = SlashCommand(
            name=name,
            description=description,
            handler=handler,
            usage=usage,
            group=group,
            hidden=hidden,
            origin=origin,
        )
        self._commands[name] = cmd
        return cmd

    def register_subcommand(
        self,
        root: str,
        sub_name: str,
        description: str,
        handler: Callable,
        usage: str = "",
    ) -> None:
        """Register ``/root <sub_name>``. The root must already exist."""
        parent = self._commands.get(root)
        if parent is None:
            raise KeyError(f"Cannot register subcommand on unknown root: {root}")
        parent.subcommands[sub_name] = SlashCommand(
            name=sub_name,
            description=description,
            handler=handler,
            usage=usage,
            group=parent.group,
        )

    def replace_dynamic(self, commands: list[SlashCommand]) -> None:
        """Atomically replace every entry with ``group=='dynamic'``.

        Used after skills reload — drop all stale ``/<skill-name>`` entries
        and install the freshly built ones in one shot. Built-in commands
        with the same name are not displaced; conflicting dynamic commands
        are skipped with a warning so the user can rename their skill.
        """
        # Drop existing dynamic entries.
        self._commands = {
            n: c for n, c in self._commands.items() if c.group != "dynamic"
        }
        for cmd in commands:
            if cmd.name in self._commands:
                logger.warning(
                    "Dynamic command %s blocked by built-in; skipping (origin=%s)",
                    cmd.name,
                    cmd.origin,
                )
                continue
            cmd.group = "dynamic"
            self._commands[cmd.name] = cmd

    # ── lookup ────────────────────────────────────────────────────

    def get(self, name: str) -> SlashCommand | None:
        """Return the root command record, or None."""
        return self._commands.get(name)

    def list_commands(self, *, include_hidden: bool = False) -> list[SlashCommand]:
        """List registered root commands, alphabetically by name."""
        items = list(self._commands.values())
        if not include_hidden:
            items = [c for c in items if not c.hidden]
        return sorted(items, key=lambda c: c.name)

    def list_by_group(
        self, *, include_hidden: bool = False
    ) -> dict[CommandGroup, list[SlashCommand]]:
        """Return commands grouped in display order."""
        out: dict[CommandGroup, list[SlashCommand]] = {g: [] for g in _GROUP_ORDER}
        for cmd in self.list_commands(include_hidden=include_hidden):
            out.setdefault(cmd.group, []).append(cmd)
        return out

    @staticmethod
    def group_label(group: CommandGroup) -> str:
        return _GROUP_LABELS.get(group, group)

    @staticmethod
    def group_order() -> tuple[CommandGroup, ...]:
        return _GROUP_ORDER

    @staticmethod
    def display_order_key(cmd: "SlashCommand") -> tuple[int, str]:
        """Sort key matching the visual layout: group order first, then name.

        Used by the slash-menu so the flat candidate index advances in
        the same order the user sees on screen.
        """
        try:
            group_idx = _GROUP_ORDER.index(cmd.group)
        except ValueError:
            group_idx = len(_GROUP_ORDER)
        return (group_idx, cmd.name)

    # ── parsing ───────────────────────────────────────────────────

    def parse_command(self, text: str) -> tuple[str, str, str]:
        """Parse a slash command string into ``(root, sub, args)``.

        - If the matched root has registered subcommands AND the next token
          matches one of them, ``sub`` is that token and ``args`` is the
          remainder. Sub names are matched case-insensitively.
        - Otherwise ``sub`` is the empty string and ``args`` is everything
          after the root token (preserving backward compatibility for
          handlers that parse arguments themselves).

        Examples (assuming ``/skills`` has subs ``list/install``)::

            '/help'                       -> ('/help', '', '')
            '/run CPU fault'              -> ('/run', '', 'CPU fault')
            '/skills install foo'         -> ('/skills', 'install', 'foo')
            '/skills'                     -> ('/skills', '', '')
            '/skills nonsense'            -> ('/skills', '', 'nonsense')
            '/config set key value'       -> ('/config', '', 'set key value')
        """
        text = text.strip()
        if not text:
            return "", "", ""
        parts = text.split(maxsplit=1)
        root = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        cmd = self._commands.get(root)
        if cmd and cmd.subcommands:
            sub_parts = rest.split(maxsplit=1)
            if sub_parts:
                candidate = sub_parts[0].lower()
                if candidate in cmd.subcommands:
                    sub_args = sub_parts[1] if len(sub_parts) > 1 else ""
                    return root, candidate, sub_args
        return root, "", rest

    # ── help text ─────────────────────────────────────────────────

    def format_help(self) -> str:
        """Plain-text help, grouped by category."""
        lines: list[str] = ["Available commands:"]
        grouped = self.list_by_group()
        last_label: str | None = None
        for group in _GROUP_ORDER:
            cmds = grouped.get(group) or []
            if not cmds:
                continue
            label = _GROUP_LABELS[group]
            if label != last_label:
                lines.append("")
                lines.append(f"[{label}]")
                last_label = label
            for cmd in cmds:
                usage_suffix = f" {cmd.usage}" if cmd.usage else ""
                lines.append(f"  {cmd.name}{usage_suffix} - {cmd.description}")
                for sub in sorted(cmd.subcommands.values(), key=lambda c: c.name):
                    sub_usage = f" {sub.usage}" if sub.usage else ""
                    lines.append(
                        f"    {cmd.name} {sub.name}{sub_usage} - {sub.description}"
                    )
        return "\n".join(lines)
