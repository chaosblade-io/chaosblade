"""Tests for the PR-D5 physical timeline in the result panel.

The timeline is *purely* a re-render of timestamps that already live on
the result envelope (``created_at`` / ``injection_start_time`` /
``finished_at``). The agent has been collecting those for a long time —
this PR only makes them visible. The tests pin five separable behaviors:

1. ``calm`` mode prints nothing for the timeline section, even when
   timestamps are present.
2. ``working`` collapses to a single-line summary that names the events
   without dumping each ``T+offset``.
3. ``dense`` prints the full ``T+0 / T+inject / T+done`` rows so a
   postmortem reader can lift the table into a doc.
4. Missing ``created_at`` (older crash recovery records) → silent. We
   refuse to fabricate a "T+0" anchor.
5. Failed runs render with the ``任务结束`` label (not ``任务完成``)
   so a quick-skim reader doesn't read green where there was none.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from chaos_agent.tui.renderers.result import (
    _build_timeline_events,
    _format_offset,
    _render_physical_timeline,
    render_result,
)
from chaos_agent.tui.state import DisplayMode


def _render_to_string(renderable_callable) -> str:
    """Capture render output by routing a real Console through StringIO."""
    buf = io.StringIO()

    class _StubChaosConsole:
        def __init__(self) -> None:
            self.console = Console(file=buf, color_system=None, width=120)

        def print(self, *args, **kwargs):
            self.console.print(*args, **kwargs)

        def print_text(self, *args, **kwargs):
            self.console.print(*args, **kwargs)

        def bell(self) -> None:
            pass

    renderable_callable(_StubChaosConsole())
    return buf.getvalue()


def _result_data(
    *,
    created_at: str = "2026-05-14T08:00:00",
    injection_start_time: str = "2026-05-14T08:00:12",
    finished_at: str = "2026-05-14T08:01:23",
    duration_ms: int = 83000,
    status: str = "success",
    task_state: str = "injected",
) -> dict:
    return {
        "task_id": "tsk-d5",
        "fault_type": "pod-pod-fullload",
        "status": status,
        "task_state": task_state,
        "created_at": created_at,
        "injection_start_time": injection_start_time,
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }


class TestFormatOffset:
    def test_zero_or_negative(self):
        # Negative offset is almost always clock skew between writers; we
        # collapse to T+0 rather than print a confusing T-…
        assert _format_offset(0) == "T+0"
        assert _format_offset(-5) == "T+0"

    def test_sub_minute(self):
        assert _format_offset(12.0) == "T+12.0s"

    def test_minute_seconds(self):
        # 83s = 1m23s → T+1:23 (zero-padded seconds for monospace
        # alignment in the dense table).
        assert _format_offset(83.0) == "T+1:23"


class TestBuildTimelineEvents:
    def test_full_envelope_yields_three_events(self):
        events = _build_timeline_events(_result_data())
        labels = [label for label, _o, _c in events]
        assert labels[0] == "\u4efb\u52a1\u542f\u52a8"  # 任务启动
        assert "\u6545\u969c\u6ce8\u5165" in labels       # 故障注入
        assert "\u4efb\u52a1\u5b8c\u6210" in labels       # 任务完成

    def test_missing_created_at_returns_empty(self):
        # Without an anchor we can't compute offsets; the timeline is
        # silent rather than printing "T+? task started".
        data = _result_data()
        data["created_at"] = ""
        assert _build_timeline_events(data) == []

    def test_failed_status_uses_ended_label(self):
        events = _build_timeline_events(_result_data(status="failed"))
        labels = [label for label, _o, _c in events]
        assert "\u4efb\u52a1\u7ed3\u675f" in labels       # 任务结束
        assert "\u4efb\u52a1\u5b8c\u6210" not in labels   # 任务完成


class TestRenderPhysicalTimelineByMode:
    def test_calm_renders_nothing(self):
        from rich.text import Text

        body = Text()
        _render_physical_timeline(body, _result_data(), DisplayMode.CALM)
        assert body.plain == ""

    def test_working_renders_single_line_summary(self):
        from rich.text import Text

        body = Text()
        _render_physical_timeline(body, _result_data(), DisplayMode.WORKING)
        out = body.plain
        assert "Timeline" in out
        # Working collapses: total duration + arrow-joined event names.
        assert "T+1:23" in out
        assert "\u2192" in out  # → arrow joiner
        # No per-event T+offset rows.
        assert "T+12" not in out

    def test_dense_renders_per_event_offsets(self):
        from rich.text import Text

        body = Text()
        _render_physical_timeline(body, _result_data(), DisplayMode.DENSE)
        out = body.plain
        assert "T+0" in out
        assert "T+12.0s" in out
        assert "T+1:23" in out
        # Should NOT collapse into the working "duration · summary" form.
        assert " \u00b7 " not in out


class TestRenderResultIntegration:
    """End-to-end via ``render_result`` so we cover the kwarg plumbing."""

    def test_dense_includes_timeline_block(self):
        out = _render_to_string(
            lambda c: render_result(
                c, _result_data(), task_id="tsk-d5",
                display_mode=DisplayMode.DENSE,
            )
        )
        assert "Timeline" in out
        assert "T+12.0s" in out

    def test_calm_omits_timeline_block_but_keeps_panel(self):
        out = _render_to_string(
            lambda c: render_result(
                c, _result_data(), task_id="tsk-d5",
                display_mode=DisplayMode.CALM,
            )
        )
        assert "Timeline" not in out
        # Panel itself still renders (success title is up top).
        assert "INJECTION SUCCESS" in out

    def test_default_mode_is_working(self):
        # Older callers that pass no display_mode get the daily-driver
        # experience (timeline visible) — never the calm omission.
        out = _render_to_string(
            lambda c: render_result(c, _result_data(), task_id="tsk-d5")
        )
        assert "Timeline" in out
