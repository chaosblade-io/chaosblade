"""SlashCommandCompleter — feeds the slash registry into prompt_toolkit."""

from __future__ import annotations

from typing import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from rich.cells import cell_len

from chaos_agent.tui.commands import SlashCommandRegistry

_CMD_COL_WIDTH = 28


def _pad_cells(text: str, target: int) -> str:
    """Right-pad ``text`` to ``target`` terminal cells (CJK-safe).

    ``str.ljust(N)`` counts Python characters, but a Han glyph occupies
    2 terminal cells while being one char — using ``ljust`` would shift
    the description column for any future Chinese-named slash command.
    Today's slash names are ASCII, so this is mostly future-proofing,
    but it's the same fix §17.5 enforced everywhere else.
    """
    pad = target - cell_len(text)
    return text + " " * max(0, pad)


class SlashCommandCompleter(Completer):
    """Completer that activates only when the line starts with `/`.

    The registry is the source of truth — any commands added to it are
    automatically reflected in completions.
    """

    def __init__(self, registry: SlashCommandRegistry) -> None:
        self._registry = registry

    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Only complete the leading word (not arguments)
        if " " in text:
            return

        prefix = text
        for cmd in self._registry.list_commands():
            if cmd.name.startswith(prefix):
                yield Completion(
                    cmd.name,
                    start_position=-len(prefix),
                    display=_pad_cells(cmd.name, _CMD_COL_WIDTH),
                    display_meta=cmd.description,
                    style="class:slash-cmd",
                    selected_style="class:slash-cmd-selected",
                )
