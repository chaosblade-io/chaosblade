"""Tests for StreamingPrinter — token buffering and Markdown finalize."""

import io
import time
from typing import List
from unittest.mock import MagicMock

from rich.console import Console

from chaos_agent.tui.console import ChaosConsole
from chaos_agent.tui.live_coordinator import (
    LiveCoordinator,
    OWNER_THINKING,
    OWNER_TOKEN_STREAM,
    OWNER_TOOL_PANEL,
)
from chaos_agent.tui.streaming import (
    StreamingPrinter,
    ThinkingPrinter,
    _extract_last_sentence,
    _phase_label_for_node,
)


def _capture_console() -> ChaosConsole:
    cc = ChaosConsole()
    cc._console = Console(file=io.StringIO(), force_terminal=False, width=80)
    return cc


class TestStreamingPrinter:
    def test_finalize_without_tokens_is_noop(self):
        cc = _capture_console()
        printer = StreamingPrinter(cc)
        printer.finalize()
        # No exception, no output
        assert cc._console.file.getvalue() == ""

    def test_append_then_finalize_writes_markdown(self):
        cc = _capture_console()
        printer = StreamingPrinter(cc)
        printer.append("hello ")
        printer.append("world")
        printer.finalize()
        out = cc._console.file.getvalue()
        assert "hello" in out and "world" in out

    def test_is_active_reflects_state(self):
        cc = _capture_console()
        printer = StreamingPrinter(cc)
        assert printer.is_active is False
        printer.append("x")
        assert printer.is_active is True
        printer.finalize()
        assert printer.is_active is False

    def test_finalize_does_not_emit_vline_rail(self):
        """PR-B1 — the per-line ┃ left rail and ⏳ streaming status row are
        replaced by a one-shot ⏺ leader. Locking this with a string check so
        a regression that re-introduces the rail is caught immediately."""
        cc = _capture_console()
        printer = StreamingPrinter(cc)
        printer.append("hello world")
        printer.finalize()
        out = cc._console.file.getvalue()
        assert "\u2503" not in out  # ┃ U+2503
        assert "streaming..." not in out
        assert "\u23fa" in out  # ⏺ U+23FA leader

    def test_finalize_does_not_emit_console_rule(self):
        """PR-B1 — finalize must not draw `console.rule(" 1.2s ")`. The next
        turn carries its own blank-line separation. A telltale rule line is
        a long run of horizontal box-drawing chars, so we check that the
        elapsed-time pattern is absent."""
        cc = _capture_console()
        printer = StreamingPrinter(cc)
        printer.append("done.")
        printer.finalize()
        out = cc._console.file.getvalue()
        # Heuristic: rule() emits ≥10 contiguous ─ chars when title is short.
        assert "\u2500" * 10 not in out


class TestThinkingPrinter:
    def test_finalize_discards_buffer_no_scrollback_leak(self):
        """PR-B2 / §9.4 — chain-of-thought tokens must NOT land in scrollback.
        We feed a recognizable string through ``append`` then call
        ``finalize``; the captured stdout must contain neither the marker
        substring nor the legacy ``thinking`` label.
        """
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer.append("first I will check namespace existence ")
        printer.append("then call kubectl get pods")
        printer.finalize()
        out = cc._console.file.getvalue()
        assert "first I will check" not in out
        assert "kubectl get pods" not in out
        # The legacy "thinking" English label was emitted into scrollback;
        # the new contract is *no* trailing line at all.
        assert "thinking " not in out

    def test_is_active_lifecycle(self):
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        assert printer.is_active is False
        printer.append("a")
        assert printer.is_active is True
        printer.finalize()
        assert printer.is_active is False

    def test_finalize_without_tokens_is_noop(self):
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer.finalize()
        assert cc._console.file.getvalue() == ""


class TestThinkingPrinterStructureAndJustification:
    """PR-B2 / §9.4 — the thinking line is "1 行结构化 + 1 行正当性":
    line 1 names the phase (so the user knows what's being decided),
    line 2 surfaces the latest complete CoT sentence (so they know why).
    Locking the contract here so a future regression that drops either
    half is caught immediately."""

    def test_phase_label_uses_phase_names_for_known_node(self):
        # intent_clarification → 意图识别 (PHASE_NAMES["intent"]).
        assert _phase_label_for_node("intent_clarification") == "意图识别"
        assert _phase_label_for_node("safety_check") == "安全检查"
        assert _phase_label_for_node("verifier_loop") == "注入验证"
        assert _phase_label_for_node("recover_verifier_loop") == "恢复就绪"

    def test_phase_label_falls_back_to_thinking_when_no_node(self):
        # Empty / unknown node → "思考". Better than leaking the raw node id.
        assert _phase_label_for_node("") == "思考"
        assert _phase_label_for_node("unknown_node") == "思考"

    def test_extract_last_sentence_returns_empty_until_terminator(self):
        # No terminator yet → line 2 must stay hidden (no placeholder).
        assert _extract_last_sentence("我先去看看 default 命名空间") == ""
        assert _extract_last_sentence("") == ""
        assert _extract_last_sentence(None or "") == ""

    def test_extract_last_sentence_returns_the_latest_complete_chunk(self):
        # Multiple sentences → return the most recent one (current reasoning).
        buf = "我需要先确认命名空间存在。再 kubectl get pods 看运行状态。"
        assert _extract_last_sentence(buf) == "再 kubectl get pods 看运行状态"

    def test_extract_last_sentence_drops_dangling_fragment(self):
        # Last char is mid-sentence → trim the fragment, return the prior
        # complete one. Prevents flicker as tokens stream in word-by-word.
        buf = "已确认 default 命名空间存在。下一步我考虑"
        assert _extract_last_sentence(buf) == "已确认 default 命名空间存在"

    def test_extract_last_sentence_truncates_long_text_with_ellipsis(self):
        long = "x" * 200 + "。"
        out = _extract_last_sentence(long)
        # Capped to ≤ _JUSTIFICATION_MAX_LEN, ellipsis marks the cut.
        assert "\u2026" in out
        assert "x" * 200 not in out
        assert len(out) <= 80

    def test_render_first_line_includes_phase_and_verb(self):
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer._node = "intent_clarification"
        printer._verb = "拆解中"
        printer._verb_picked_at = time.monotonic()  # within TTL → no re-pick
        rendered = printer._render().plain
        # Structure half: phase label + verb ellipsis.
        assert "意图识别" in rendered
        assert "拆解中..." in rendered
        # No justification yet (empty buffer) → no second line.
        assert "\u23bf" not in rendered

    def test_render_second_line_appears_once_buffer_has_sentence(self):
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer._node = "intent_clarification"
        printer._buffer = "用户提到 default 命名空间，需要先确认存在。"
        printer._verb = "拆解中"
        printer._verb_picked_at = time.monotonic()
        rendered = printer._render().plain
        assert "意图识别" in rendered
        # The justification half rendered with the ⎿ tree-branch marker.
        assert "\u23bf" in rendered
        assert "用户提到 default 命名空间" in rendered

    def test_render_second_line_hidden_until_terminator(self):
        # Mirror of the above — buffer has content but no terminator,
        # so line 2 must stay hidden. The whole point of the §9.4 design:
        # don't show a placeholder, don't leak partial CoT.
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer._node = "safety_check"
        printer._buffer = "我先看看 namespace 是否存在"  # no period
        printer._verb = "推敲中"
        printer._verb_picked_at = time.monotonic()
        rendered = printer._render().plain
        assert "安全检查" in rendered
        assert "\u23bf" not in rendered

    def test_append_captures_node_for_subsequent_renders(self):
        # The renderer dispatch passes ``event.node`` as the second arg —
        # without this the structure half stays at the "思考" fallback
        # forever and the whole feature degrades silently.
        cc = _capture_console()
        printer = ThinkingPrinter(cc)
        printer.append("a sentence.", node="verifier_loop")
        try:
            assert printer._node == "verifier_loop"
        finally:
            printer.finalize()


class _FakeLive:
    """Minimal stand-in for rich.live.Live used by StreamingPrinter coord tests."""

    def __init__(self) -> None:
        self.start_count = 0
        self.stop_count = 0
        self.updates: List[object] = []

    def start(self) -> None:
        self.start_count += 1

    def stop(self) -> None:
        self.stop_count += 1

    def update(self, renderable: object) -> None:
        self.updates.append(renderable)


class TestStreamingPrinterWithCoordinator:
    """PR-E2 — when injected with a coordinator, the printer must NOT
    spin up its own ``Live`` block. All start / stop / update calls
    go through the shared coordinator, identified by the
    ``OWNER_TOKEN_STREAM`` token.

    The flicker fix relies on this contract: if a ToolPanel handoff
    arrives while the coordinator is held by ``OWNER_TOKEN_STREAM``,
    the coordinator can rotate ownership without ``stop`` / ``start``
    cycling. Any code path that side-steps the coordinator and starts
    its own Live would defeat the whole change.
    """

    def _make(self) -> tuple[StreamingPrinter, LiveCoordinator, List[_FakeLive]]:
        cc = _capture_console()
        created: List[_FakeLive] = []

        def _factory() -> _FakeLive:
            live = _FakeLive()
            created.append(live)
            return live  # type: ignore[return-value]

        coord = LiveCoordinator(MagicMock(), live_factory=_factory)  # type: ignore[arg-type]
        printer = StreamingPrinter(cc, coordinator=coord)
        return printer, coord, created

    def test_append_acquires_coord_no_local_live(self):
        printer, coord, created = self._make()
        printer.append("hello ")
        assert printer._live is None  # local Live untouched
        assert coord.current_owner == OWNER_TOKEN_STREAM
        assert len(created) == 1
        # The buffer paint propagated through the coordinator.
        assert created[0].updates  # at least one update

    def test_finalize_releases_coord_then_flushes_markdown(self):
        printer, coord, created = self._make()
        printer.append("hello world")
        printer.finalize()
        assert coord.is_active is False
        assert created[0].stop_count == 1
        # The marker leader landed in scrollback after release.
        out = printer._console._console.file.getvalue()
        assert "\u23fa" in out  # ⏺
        assert "hello world" in out

    def test_finalize_without_buffer_does_not_emit_marker(self):
        printer, coord, created = self._make()
        # No append → finalize is a no-op.
        printer.finalize()
        assert created == []
        out = printer._console._console.file.getvalue()
        assert out == ""

    def test_discard_releases_without_flushing(self):
        printer, coord, _ = self._make()
        printer.append("about to be replaced")
        printer.discard()
        assert coord.is_active is False
        out = printer._console._console.file.getvalue()
        # discard must NOT leak the buffer to scrollback.
        assert "about to be replaced" not in out

    def test_repeated_append_reuses_one_acquire(self):
        # Streaming hits append() per-token; we must not call .start()
        # again — that's the exact flicker we're trying to avoid.
        printer, coord, created = self._make()
        for token in "hello world".split():
            printer.append(token + " ")
        assert len(created) == 1
        assert created[0].start_count == 1

    def test_is_active_tracks_coord_ownership(self):
        printer, coord, _ = self._make()
        assert printer.is_active is False
        printer.append("x")
        assert printer.is_active is True
        printer.finalize()
        assert printer.is_active is False


class TestThinkingPrinterWithCoordinator:
    """PR-E2 — ThinkingPrinter routes through ``OWNER_THINKING`` region.

    Mirror of ``TestStreamingPrinterWithCoordinator``. The contracts:

      * ``append`` acquires the region and starts the tick thread; no
        local Live block is constructed.
      * ``finalize`` releases without an ``on_release`` callback —
        thinking content is discarded (per §9.4), not flushed.
      * Rotation: when another region owner takes over (e.g. tool
        panel or token stream), subsequent thinking tick updates are
        silently dropped by the coordinator. ``finalize`` is still
        safe to call; release on a non-owner is a no-op.
      * The tick thread exits on next iteration after ``finalize``
        sets the stop event.
    """

    def _make(
        self,
    ) -> tuple[ThinkingPrinter, LiveCoordinator, List[_FakeLive]]:
        cc = _capture_console()
        created: List[_FakeLive] = []

        def _factory() -> _FakeLive:
            live = _FakeLive()
            created.append(live)
            return live  # type: ignore[return-value]

        coord = LiveCoordinator(MagicMock(), live_factory=_factory)  # type: ignore[arg-type]
        printer = ThinkingPrinter(cc, coordinator=coord)
        return printer, coord, created

    def test_append_acquires_coord_no_local_live(self):
        printer, coord, created = self._make()
        printer.append("一些推理。")
        try:
            assert printer._live is None  # local Live untouched
            assert coord.current_owner == OWNER_THINKING
            assert len(created) == 1
            assert created[0].start_count == 1
            # The coord paint propagated.
            assert created[0].updates  # at least one update
        finally:
            printer.finalize()

    def test_repeated_append_reuses_one_acquire(self):
        # The thinking spinner ticks at 6 Hz and the LLM streams many
        # CoT tokens; we must not call .start() per token.
        printer, coord, created = self._make()
        try:
            for token in ("我", "需要", "先", "识别", "意图。"):
                printer.append(token)
            assert len(created) == 1
            assert created[0].start_count == 1
        finally:
            printer.finalize()

    def test_finalize_releases_without_flush(self):
        # Thinking is intentionally discarded — no markdown / no
        # console.print call is expected on finalize.
        printer, coord, created = self._make()
        printer.append("一些推理。")
        printer.finalize()
        assert coord.is_active is False
        assert created[0].stop_count == 1
        # No marker / content landed in scrollback.
        out = printer._console._console.file.getvalue()
        assert "一些推理" not in out
        # Local flag cleared.
        assert printer._coord_active is False
        assert printer.is_active is False

    def test_finalize_without_append_is_noop(self):
        printer, coord, created = self._make()
        printer.finalize()
        # No append → no acquire → no Live ever created.
        assert created == []
        out = printer._console._console.file.getvalue()
        assert out == ""

    def test_rotation_does_not_break_finalize(self):
        # Another region owner takes over while thinking is mid-paint.
        # finalize must still clean up local state (tick thread,
        # buffer) even though release is now a no-op against the coord.
        printer, coord, created = self._make()
        printer.append("一些推理。")
        # Simulate the dispatch reorder where another printer rotates
        # the region away from thinking before finalize runs.
        coord.acquire(OWNER_TOOL_PANEL)
        # Local flag is still True (lagging the rotation), but
        # finalize must not raise on stale ownership.
        printer.finalize()
        assert printer._coord_active is False
        assert printer.is_active is False
        # The other region owner survives — release on stale owner
        # was a coord-side no-op.
        assert coord.current_owner == OWNER_TOOL_PANEL
        coord.release(OWNER_TOOL_PANEL)

    def test_tick_thread_exits_after_finalize(self):
        # Background tick thread must not outlive finalize. Otherwise
        # a second turn's append would race with stale tick paints.
        printer, coord, _ = self._make()
        printer.append("一些推理。")
        thread = printer._tick_thread
        assert thread is not None
        assert thread.is_alive()
        printer.finalize()
        # Give the daemon a tick interval to exit.
        thread.join(timeout=1.0)
        assert thread.is_alive() is False

    def test_is_active_lifecycle_under_coord(self):
        printer, coord, _ = self._make()
        assert printer.is_active is False
        printer.append("x。")
        assert printer.is_active is True
        printer.finalize()
        assert printer.is_active is False

    def test_append_after_finalize_starts_fresh(self):
        # A new turn / new thinking burst arrives after the previous
        # one finalized. The printer must acquire again cleanly.
        printer, coord, created = self._make()
        printer.append("first burst。")
        printer.finalize()
        printer.append("second burst。")
        try:
            # Two Live blocks across the two bursts.
            assert len(created) == 2
            assert created[0].stop_count == 1
            assert created[1].start_count == 1
            assert printer._coord_active is True
        finally:
            printer.finalize()
