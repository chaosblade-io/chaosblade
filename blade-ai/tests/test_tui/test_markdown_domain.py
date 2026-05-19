"""Tests for chaos-eng markdown customizations (PR-C3 / §9.6).

Lock four behaviors that are otherwise easy to silently regress:

1. yaml/json fences route to ``rich.syntax.Syntax``, not the stock
   ``Markdown`` code block (so we get real syntax highlighting).
2. Bare-fence content shaped like ``kubectl get …`` output gets parsed
   into a ``rich.table.Table`` with one column per header token.
3. Inline ``blade <uid>`` / IP-port pairs in prose get wrapped in
   markdown backticks (which Rich renders as a distinct inline code
   span) so they're scannable in a wall of text.
4. Plain prose with none of the above falls through to the stock
   ``Markdown`` — i.e. we never make ordinary prose worse.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from chaos_agent.tui.markdown import render_markdown

pytestmark = pytest.mark.usefixtures("require_unicode_locale")


def _flatten(renderable) -> list:
    if isinstance(renderable, Group):
        return list(renderable.renderables)
    return [renderable]


def _render(renderable) -> str:
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=80).print(renderable)
    return buf.getvalue()


class TestYamlFence:
    def test_yaml_fence_returns_syntax(self):
        out = render_markdown("```yaml\nkey: value\nnested:\n  inner: 1\n```")
        # A bare yaml fence ends up as a single Syntax object, no Group.
        assert isinstance(out, Syntax)
        assert "yaml" in out.lexer.aliases

    def test_yml_alias_also_yaml(self):
        out = render_markdown("```yml\na: b\n```")
        assert isinstance(out, Syntax)
        assert "yaml" in out.lexer.aliases

    def test_json_fence_returns_syntax(self):
        out = render_markdown('```json\n{"k": 1}\n```')
        assert isinstance(out, Syntax)
        assert "json" in out.lexer.aliases

    def test_yaml_inside_prose_renders_as_group(self):
        # Prose surrounds the fence — output should be Group(prose, syntax, prose).
        # Pure-prose segments now return Text (PR-C3 theme-color path).
        out = render_markdown("Plan:\n```yaml\nx: 1\n```\nDone.")
        parts = _flatten(out)
        assert len(parts) == 3
        assert isinstance(parts[0], Text)
        assert isinstance(parts[1], Syntax)
        assert isinstance(parts[2], Text)


class TestKubectlAutoDetect:
    def test_get_pods_header_becomes_table(self):
        block = (
            "```\n"
            "NAME    READY   STATUS    RESTARTS   AGE\n"
            "web-1   1/1     Running   0          12m\n"
            "web-2   1/1     Running   2          1h\n"
            "```"
        )
        out = render_markdown(block)
        assert isinstance(out, Table)
        assert [c.header for c in out.columns] == [
            "NAME",
            "READY",
            "STATUS",
            "RESTARTS",
            "AGE",
        ]
        # Rows preserved
        assert out.row_count == 2

    def test_lowercase_header_does_not_trigger(self):
        # Looks vaguely tabular but lowercase — keep as plain code block.
        out = render_markdown("```\nname  age\nfoo   1\n```")
        assert not isinstance(out, Table)

    def test_two_column_header_does_not_trigger(self):
        # We require >=3 columns to avoid false-positives on single-key
        # fixtures like "KEY  VALUE".
        out = render_markdown("```\nKEY    VALUE\nfoo    bar\n```")
        assert not isinstance(out, Table)

    def test_kubectl_renders_columns_aligned(self):
        out = render_markdown(
            "```\n"
            "NAME    READY   STATUS    RESTARTS   AGE\n"
            "web-1   1/1     Running   0          12m\n"
            "```"
        )
        text = _render(out)
        # Column headers all on the same line as a sanity check.
        first_line = next(line for line in text.splitlines() if "NAME" in line)
        for col in ("NAME", "READY", "STATUS", "RESTARTS", "AGE"):
            assert col in first_line


class TestInlineHighlightRegex:
    """Regex-only checks (cheap; verify pattern intent without rendering)."""

    def test_blade_uid_pattern_matches_32_hex(self):
        from chaos_agent.tui.markdown import _BLADE_UID_RE

        assert _BLADE_UID_RE.search("Started a1b2c3d4e5f6789012345678901234ab now.")

    def test_blade_uid_pattern_skips_already_backticked(self):
        # Negative-lookbehind prevents double-wrapping in block-md fallback.
        from chaos_agent.tui.markdown import _BLADE_UID_RE

        src = "Started `a1b2c3d4e5f6789012345678901234ab` now."
        wrapped = _BLADE_UID_RE.sub(r"`\1`", src)
        assert "``" not in wrapped

    def test_ip_port_pattern_matches(self):
        from chaos_agent.tui.markdown import _IP_RE

        m = _IP_RE.search("probe 10.244.0.5:8080 from 192.168.1.1")
        assert m and "10.244.0.5:8080" in m.group(0)

    def test_version_string_not_treated_as_ip(self):
        from chaos_agent.tui.markdown import _IP_RE

        assert _IP_RE.search("v1.2.3.4.5") is None


class TestInlineHighlightThemeColors:
    """Verify the highlighter emits *theme-color* ANSI escapes, not just
    backtick-wrapped inline code. PR-C3 §9.6 requires UID/IP/namespace/
    fault_type get their semantic theme color in pure-prose paths."""

    def _truecolor_output(self, content: str) -> str:
        # Build a temp truecolor console with the chaos theme installed,
        # so we can assert on raw RGB ANSI rather than relying on Rich's
        # color-system detection.
        import io

        from rich.console import Console
        from chaos_agent.tui.console import _CHAOS_THEME
        from chaos_agent.tui.markdown import render_markdown

        buf = io.StringIO()
        Console(
            file=buf,
            force_terminal=True,
            width=80,
            color_system="truecolor",
            theme=_CHAOS_THEME,
            highlight=False,
        ).print(render_markdown(content))
        return buf.getvalue()

    def test_uid_emits_role_agent_purple(self):
        # Sourced from Theme so a future palette swap auto-tracks
        # without rewriting the RGB tuple per change.
        from chaos_agent.tui.theme import Theme

        r, g, b = (
            int(Theme.role_agent[1:3], 16),
            int(Theme.role_agent[3:5], 16),
            int(Theme.role_agent[5:7], 16),
        )
        out = self._truecolor_output(
            "Started a1b2c3d4e5f6789012345678901234ab now."
        )
        assert f"{r};{g};{b}" in out

    def test_ip_emits_text_accent_blue(self):
        from chaos_agent.tui.theme import Theme

        r, g, b = (
            int(Theme.text_accent[1:3], 16),
            int(Theme.text_accent[3:5], 16),
            int(Theme.text_accent[5:7], 16),
        )
        out = self._truecolor_output("probe 10.244.0.5:8080")
        assert f"{r};{g};{b}" in out

    def test_namespace_emits_state_active_orange(self):
        from chaos_agent.tui.theme import Theme

        r, g, b = (
            int(Theme.state_active[1:3], 16),
            int(Theme.state_active[3:5], 16),
            int(Theme.state_active[5:7], 16),
        )
        out = self._truecolor_output("Inject into namespace cms-demo")
        assert f"{r};{g};{b}" in out

    def test_fault_type_emits_state_err_red(self):
        # Theme.state_err = Okabe-Ito vermilion #D55E00 = (213, 94, 0).
        # Sourced from Theme so a future palette swap auto-tracks here
        # rather than silently skipping the assertion against a stale
        # RGB tuple.
        from chaos_agent.tui.theme import Theme

        r, g, b = (
            int(Theme.state_err[1:3], 16),
            int(Theme.state_err[3:5], 16),
            int(Theme.state_err[5:7], 16),
        )
        out = self._truecolor_output("fault_type: cpu-fullload")
        assert f"{r};{g};{b}" in out

    def test_pure_prose_returns_text_not_markdown(self):
        # Pure-prose path emits a Text (so the highlighter can fire).
        # Anything that triggers _MD_BLOCK_RE would fall to Markdown.
        out = render_markdown("Started a1b2c3d4 in namespace foo.")
        assert isinstance(out, Text)


class TestBlockMarkdownFallback:
    """When prose has block-level markdown, we keep stock Markdown to
    preserve list/heading/blockquote structure. UID/IP get backticks
    in this path because Rich's Markdown bypasses console highlighters."""

    def test_unordered_list_routes_to_markdown(self):
        out = render_markdown("Status:\n- pod-1 down\n- pod-2 up")
        assert isinstance(out, Markdown)

    def test_heading_routes_to_markdown(self):
        out = render_markdown("# Title\nSome body.")
        assert isinstance(out, Markdown)

    def test_blockquote_routes_to_markdown(self):
        out = render_markdown("> warning text")
        assert isinstance(out, Markdown)


class TestPlainProse:
    def test_plain_prose_becomes_text(self):
        # PR-C3: pure-prose without block-md markers → Text + Highlighter
        # so theme colors apply. (Was Markdown before §9.6 finishing.)
        from rich.text import Text

        out = render_markdown("Hello world — just prose.")
        assert isinstance(out, Text)

    def test_empty_input_returns_text(self):
        from rich.text import Text

        out = render_markdown("")
        assert isinstance(out, Text)

    def test_whitespace_only_input(self):
        from rich.text import Text

        out = render_markdown("   \n  \n")
        assert isinstance(out, Text)


class TestBareFenceFallthrough:
    def test_unknown_lang_uses_syntax(self):
        # bash, python, etc. — honor the lang via Syntax.
        out = render_markdown("```bash\nls -la\n```")
        assert isinstance(out, Syntax)
        assert "bash" in out.lexer.aliases

    def test_no_lang_no_kubectl_keeps_markdown_code(self):
        # Prose-shaped fenced block: we want stock Code styling, not Syntax
        # (don't pretend we know the language).
        out = render_markdown("```\nthe quick brown fox\n```")
        assert isinstance(out, Markdown)


class TestConsoleIntegration:
    def test_print_markdown_uses_render_markdown(self, captured_console):
        captured_console.print_markdown("```yaml\nfoo: bar\n```")
        text = captured_console._console.file.getvalue()
        # yaml content shows up in the output stream; the framing is gone
        # (Syntax doesn't add ``` delimiters).
        assert "foo: bar" in text
        assert "```" not in text

    def test_print_markdown_empty_does_nothing(self, captured_console):
        captured_console.print_markdown("")
        assert captured_console._console.file.getvalue() == ""
