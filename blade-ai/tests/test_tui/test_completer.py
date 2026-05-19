"""Tests for SlashCommandCompleter."""

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from chaos_agent.tui.commands import SlashCommandRegistry
from chaos_agent.tui.completer import SlashCommandCompleter


def _make_registry() -> SlashCommandRegistry:
    registry = SlashCommandRegistry()

    async def _noop(args: str = "") -> None:
        return None

    registry.register("/help", "Show help", _noop)
    registry.register("/clear", "Clear screen", _noop)
    registry.register("/exit", "Exit", _noop)
    return registry


def _completions(text: str, registry: SlashCommandRegistry) -> list[str]:
    completer = SlashCommandCompleter(registry)
    doc = Document(text=text, cursor_position=len(text))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


class TestSlashCommandCompleter:
    def test_no_completion_when_no_slash(self):
        assert _completions("hello", _make_registry()) == []

    def test_lone_slash_returns_all(self):
        completions = _completions("/", _make_registry())
        assert "/help" in completions
        assert "/clear" in completions
        assert "/exit" in completions

    def test_prefix_filters_commands(self):
        completions = _completions("/he", _make_registry())
        assert completions == ["/help"]

    def test_no_completion_after_space(self):
        assert _completions("/help ", _make_registry()) == []


class TestSlashCommandDisplayFormatting:
    """Verify fixed-width display and description metadata on Completion objects."""

    def test_display_fixed_width(self):
        registry = _make_registry()
        completer = SlashCommandCompleter(registry)
        doc = Document(text="/", cursor_position=1)
        completions = list(completer.get_completions(doc, CompleteEvent()))

        for c in completions:
            # display is wrapped in FormattedText by prompt_toolkit
            display_text = c.display[0][1]
            assert len(display_text) == 28, f"display for {c.text!r} should be 28 chars, got {len(display_text)}"

    def test_display_meta_carries_description(self):
        registry = _make_registry()
        completer = SlashCommandCompleter(registry)
        doc = Document(text="/", cursor_position=1)
        completions = list(completer.get_completions(doc, CompleteEvent()))

        by_name = {c.text: c for c in completions}
        assert by_name["/help"].display_meta[0][1] == "Show help"
        assert by_name["/clear"].display_meta[0][1] == "Clear screen"
