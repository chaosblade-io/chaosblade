"""Tool panel renderer tests (PR-C1: inline two-line for generic tools).

The inline format collapses the old framed Panel for generic tools (kubectl,
http, blade-* RPCs) into two flat lines:

    ⏺ kubectl
      ⎿  status · message  (1.0s)

Multi-line outputs append a ``/expand <n>`` hint on line 2 instead of
spilling extra body lines. Three categories still render as Panels because
their payload is structurally multi-line:

    - TodoWrite (checklist)
    - Agent / Explore (sub-agent report)
    - error completions (red border + traceback)

Tests are written so the behaviour is enforceable independent of which
icon palette is active — they assert presence of the agent marker glyph
(``Icons.MARKER``) and the tree-branch glyph (``Icons.TREE_BRANCH``)
rather than literal Unicode codepoints.
"""

from __future__ import annotations

import json

import pytest

from chaos_agent.tui.renderers.tool_panel import (
    ToolPanelRenderer,
    _line_count,
    _summarize_envelope,
    _summarize_output,
    _truncate,
)
from chaos_agent.tui.theme import Icons

pytestmark = pytest.mark.usefixtures("require_unicode_locale")


# ── Helpers ──────────────────────────────────────────────────────────────


class TestSummarizeOutput:
    """``_summarize_output`` is the inline-line preview function. It must:

      1. Collapse the JSON envelope shape ``{status, code, message, ...}``
         to ``status · message`` (the format every internal tool returns).
      2. Fall back to first non-empty line for free-form text.
      3. Truncate to ~70 cols so the inline line never wraps at width=80.
    """

    def test_envelope_status_and_message(self):
        payload = json.dumps(
            {"status": "ok", "code": 0, "message": "namespace exists", "data": {}}
        )
        assert _summarize_output(payload) == "ok \u00b7 namespace exists"

    def test_envelope_status_only(self):
        payload = json.dumps({"status": "Active"})
        assert _summarize_output(payload) == "Active"

    def test_plain_text_first_line(self):
        out = "Active\n23 pods running\n3 pending"
        assert _summarize_output(out) == "Active"

    def test_blank_input_empty_string(self):
        assert _summarize_output("") == ""
        assert _summarize_output("   \n  \n") == ""

    def test_truncates_long_line(self):
        long = "a" * 200
        result = _summarize_output(long)
        assert len(result) <= 70
        assert result.endswith("\u2026")

    def test_envelope_message_only(self):
        payload = json.dumps({"message": "no fault detected"})
        assert _summarize_output(payload) == "no fault detected"

    def test_invalid_json_falls_through(self):
        # Looks JSON-ish but is malformed — must not crash, fall to text path.
        assert _summarize_output('{"status": "ok"') == '{"status": "ok"'


class TestLineCount:
    def test_blank_returns_zero(self):
        assert _line_count("") == 0
        assert _line_count("   \n   \n") == 0

    def test_single_line(self):
        assert _line_count("hello") == 1

    def test_skips_blank_lines(self):
        assert _line_count("a\n\nb\n\n\nc") == 3


class TestTruncate:
    def test_no_op_under_limit(self):
        assert _truncate("hello", 70) == "hello"

    def test_appends_ellipsis(self):
        assert _truncate("abcdefghij", 5) == "abcd\u2026"


class TestSummarizeEnvelope:
    def test_dict_with_status_message(self):
        assert (
            _summarize_envelope({"status": "ok", "message": "done"})
            == "ok \u00b7 done"
        )

    def test_non_dict_returns_empty(self):
        assert _summarize_envelope([1, 2, 3]) == ""
        assert _summarize_envelope("plain") == ""


# ── Inline rendering integration ─────────────────────────────────────────


class TestGenericInlineRendering:
    """``complete()`` for a generic (non-Todo, non-Agent) tool must emit
    two flat lines, not a Panel — that's the whole point of PR-C1."""

    def test_two_lines_no_panel_box(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete(
            "kubectl",
            json.dumps({"status": "ok", "message": "namespace cms-demo exists"}),
        )
        out = captured_console._console.file.getvalue()
        # No box-drawing means no Panel.
        assert "\u256d" not in out  # ╭
        assert "\u2570" not in out  # ╰
        assert "\u2502" not in out  # │
        # Marker glyph + tool name on line 1.
        assert Icons.MARKER in out
        assert "kubectl" in out
        # Branch glyph + summary on line 2.
        assert Icons.TREE_BRANCH in out
        assert "ok \u00b7 namespace cms-demo exists" in out

    def test_elapsed_time_appears(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer._start_time = 0.0  # complete() will compute elapsed=0.0 vs monotonic
        renderer.complete("kubectl", "Active")
        out = captured_console._console.file.getvalue()
        # Just confirm "Xs" suffix is present in some form (we don't lock
        # the exact float since it depends on test timing).
        assert "s)" in out

    def test_multi_line_output_offers_expand_hint(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        big = "\n".join(f"line-{i}" for i in range(20))
        renderer.complete("kubectl", big)
        out = captured_console._console.file.getvalue()
        assert "/expand" in out
        assert "20" in out

    def test_single_line_output_no_expand_hint(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete("kubectl", "Active")
        out = captured_console._console.file.getvalue()
        assert "/expand" not in out

    def test_blank_output_renders_no_output(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete("kubectl", "")
        out = captured_console._console.file.getvalue()
        assert "(no output)" in out

    def test_full_output_cached_for_expand(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        big = "\n".join(f"line-{i}" for i in range(20))
        renderer.complete("kubectl", big)
        # /expand 1 should be able to recall the full thing — the inline
        # preview drops detail but cache must not.
        full = renderer.get_full_output("1")
        assert full == big


class TestSpecialCasesStillUsePanel:
    """The 3 categories whose payload genuinely needs a frame must still
    render as Panels — losing them would lose the multi-row layout for
    todo lists and the red border for errors."""

    def test_todowrite_still_renders_panel(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete(
            "TodoWrite",
            json.dumps(
                {
                    "todos": [
                        {"status": "completed", "content": "fix bug"},
                        {"status": "in_progress", "content": "write tests"},
                    ]
                }
            ),
        )
        out = captured_console._console.file.getvalue()
        # Panel uses ╭/╰ corners.
        assert "\u256d" in out or "\u2502" in out

    def test_agent_still_renders_panel(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete("Agent", "summary line\n50 tokens")
        out = captured_console._console.file.getvalue()
        assert "\u256d" in out or "\u2502" in out

    def test_error_completion_still_renders_panel(self, captured_console):
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete_error("kubectl", "permission denied")
        out = captured_console._console.file.getvalue()
        assert "\u256d" in out or "\u2502" in out
        assert "permission denied" in out


class TestErrorCompletionLocator:
    """``complete_error`` must allocate a [T#] locator on parity with
    ``complete()`` — failed tool calls are exactly the ones a user wants
    to ``/show T3`` or ``/copy T3`` for postmortem.
    """

    def test_complete_error_allocates_t_locator(self, captured_console):
        from chaos_agent.tui.state import DisplayMode, SessionState

        state = SessionState()
        state.display_mode = DisplayMode.WORKING
        renderer = ToolPanelRenderer(captured_console, state=state)
        renderer.complete_error("kubectl", "permission denied")
        # A T# was allocated and the failure text is captured.
        rec = state.locators.get("T1")
        assert rec is not None
        assert rec.kind == "tool"
        assert rec.payload["tool_name"] == "kubectl"
        assert rec.payload["output"] == "permission denied"
        assert rec.payload["status"] == "error"
        # The label is visible in working mode.
        out = captured_console._console.file.getvalue()
        assert "[T1]" in out

    def test_complete_error_locator_hidden_in_calm(self, captured_console):
        from chaos_agent.tui.state import DisplayMode, SessionState

        state = SessionState()
        state.display_mode = DisplayMode.CALM
        renderer = ToolPanelRenderer(captured_console, state=state)
        renderer.complete_error("kubectl", "permission denied")
        # Allocation still happens (so /show T1 works after /mode dense)…
        assert state.locators.get("T1") is not None
        # …but the visible label is suppressed.
        out = captured_console._console.file.getvalue()
        assert "[T" not in out

    def test_complete_error_without_state_does_not_crash(self, captured_console):
        # Older callers / unit tests may construct the renderer with no
        # state — must continue to work, just without a locator.
        renderer = ToolPanelRenderer(captured_console)
        renderer.complete_error("kubectl", "boom")
        out = captured_console._console.file.getvalue()
        assert "boom" in out


# ── PR-E2: ToolPanelRenderer + LiveCoordinator integration ───────────────────


class _FakeLive:
    """Stand-in for rich.live.Live used by ToolPanel coord tests."""

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.updates: list = []

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)


class TestToolPanelWithCoordinator:
    """PR-E2 — ToolPanelRenderer routes through ``OWNER_TOOL_PANEL`` region.

    The contracts:

      * ``start`` acquires the region; no local Live block constructed.
      * ``complete`` / ``complete_error`` call ``_stop_live`` first
        (which ``release``s the coord region) and only then
        ``console.print`` the static panel / inline result. With a
        header still active, that ``console.print`` lands above the
        live header in scrollback (rich's transient-Live behavior).
      * ``cancel`` releases without printing anything.
      * Tick thread exits on the next iteration after ``_stop_live``
        sets the stop event.
      * Generic / TodoWrite / Agent / error paths all go through the
        same ``_stop_live`` so the static result lands in scrollback
        regardless of which sub-renderer is dispatched.
    """

    def _make(self, captured_console):
        from unittest.mock import MagicMock

        from chaos_agent.tui.live_coordinator import LiveCoordinator

        created: list[_FakeLive] = []

        def _factory() -> _FakeLive:
            live = _FakeLive()
            created.append(live)
            return live  # type: ignore[return-value]

        coord = LiveCoordinator(MagicMock(), live_factory=_factory)  # type: ignore[arg-type]
        renderer = ToolPanelRenderer(captured_console, coordinator=coord)
        return renderer, coord, created

    def test_start_acquires_coord_no_local_live(self, captured_console):
        from chaos_agent.tui.live_coordinator import OWNER_TOOL_PANEL

        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        try:
            assert renderer._live is None
            assert coord.current_owner == OWNER_TOOL_PANEL
            assert len(created) == 1
            assert created[0].start_count == 1
            # Spinner painted at least the first frame.
            assert created[0].updates
        finally:
            renderer.cancel()

    def test_complete_releases_then_prints_inline(self, captured_console):
        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        renderer.complete("kubectl", "Active")
        # Region was released; if no header was set, Live tears down.
        assert coord.is_active is False
        assert created[0].stop_count == 1
        # The inline two-line result landed in scrollback.
        out = captured_console._console.file.getvalue()
        assert Icons.MARKER in out
        assert "kubectl" in out
        assert Icons.TREE_BRANCH in out

    def test_complete_with_header_active_keeps_header(self, captured_console):
        # The differentiator: with phase-timeline holding the header,
        # tool completion must NOT tear the Live block down — the
        # stepper should keep painting under the static result.
        from chaos_agent.tui.live_coordinator import OWNER_PHASE_TIMELINE

        renderer, coord, created = self._make(captured_console)
        coord.acquire_header(OWNER_PHASE_TIMELINE)
        coord.update_header(OWNER_PHASE_TIMELINE, "stepper")
        renderer.start("kubectl")
        renderer.complete("kubectl", "Active")
        # Live block survived because the header is still active.
        assert coord.is_active is True
        assert coord.current_owner == ""
        assert coord.current_header_owner == OWNER_PHASE_TIMELINE
        # Static panel still landed in scrollback.
        out = captured_console._console.file.getvalue()
        assert "kubectl" in out
        # Cleanup.
        coord.release_header(OWNER_PHASE_TIMELINE)

    def test_complete_error_releases_then_prints_panel(self, captured_console):
        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        renderer.complete_error("kubectl", "permission denied")
        assert created[0].stop_count == 1
        out = captured_console._console.file.getvalue()
        assert "permission denied" in out
        # Error completion uses a Panel with a red border, so a box
        # corner glyph appears in the output.
        assert "╭" in out  # ╭

    def test_todowrite_still_renders_panel_under_coord(self, captured_console):
        renderer, coord, created = self._make(captured_console)
        renderer.start("TodoWrite")
        renderer.complete(
            "TodoWrite",
            json.dumps(
                {
                    "todos": [
                        {"status": "completed", "content": "x"},
                        {"status": "in_progress", "content": "y"},
                    ]
                }
            ),
        )
        out = captured_console._console.file.getvalue()
        # TodoWrite still gets a Panel.
        assert "╭" in out  # ╭
        # And the coord was released.
        assert coord.is_active is False

    def test_cancel_releases_without_printing(self, captured_console):
        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        renderer.cancel()
        # Live gone; no static output.
        assert coord.is_active is False
        out = captured_console._console.file.getvalue()
        assert out == ""

    def test_tick_thread_exits_after_stop(self, captured_console):
        renderer, coord, _ = self._make(captured_console)
        renderer.start("kubectl")
        thread = renderer._tick_thread
        assert thread is not None
        renderer.cancel()
        thread.join(timeout=1.0)
        assert thread.is_alive() is False

    def test_repeated_start_resets_cleanly(self, captured_console):
        # Two consecutive tool calls within the same turn — each one
        # must get its own start/cancel cycle without leaking the
        # previous one's tick thread.
        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        renderer.complete("kubectl", "Active")
        renderer.start("blade")
        renderer.complete("blade", "ok")
        assert len(created) == 2
        for live in created:
            assert live.start_count == 1
            assert live.stop_count == 1

    def test_rotation_does_not_break_complete(self, captured_console):
        # If a token-stream printer rotates the region away from
        # tool-panel before complete runs, complete must still clean up
        # local state and print the static result.
        from chaos_agent.tui.live_coordinator import OWNER_TOKEN_STREAM

        renderer, coord, created = self._make(captured_console)
        renderer.start("kubectl")
        # Simulate rotation (someone else takes the region).
        coord.acquire(OWNER_TOKEN_STREAM)
        # Complete still works — release on non-owner is a no-op,
        # local state cleared regardless.
        renderer.complete("kubectl", "Active")
        out = captured_console._console.file.getvalue()
        assert "kubectl" in out
        # The other owner survives.
        assert coord.current_owner == OWNER_TOKEN_STREAM
        coord.release(OWNER_TOKEN_STREAM)

    def test_legacy_path_unchanged_when_no_coord(self, captured_console):
        # No coordinator → original behavior: own Live block, original
        # _live attribute populated during start.
        renderer = ToolPanelRenderer(captured_console)
        renderer.start("kubectl")
        try:
            assert renderer._live is not None
            assert renderer._coord is None
        finally:
            renderer.cancel()
        assert renderer._live is None
