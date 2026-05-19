"""Tests for PR-E3 — recording playback (/replay) and listing (/recordings).

Behaviour pinned:

1. ``parse_recording`` rehydrates each recorded event into the
   matching dataclass and computes inter-arrival deltas. The first
   event always has delta 0; subsequent gaps are clamped to
   ``MAX_GAP_SECONDS`` so a 30 s LLM pause doesn't reproduce on
   replay.
2. Malformed lines (bad JSON, unknown event type, missing required
   fields) are SKIPPED — replay is best-effort. One bad line must
   not abort the whole tape.
3. ``Replayer.replay`` dispatches every parsed event in order and
   returns the count played. With ``instant=True`` it does not sleep.
4. ``list_recordings`` returns metadata sorted newest-first by mtime;
   non-jsonl files in the dir are ignored.
5. ``parse_replay_args`` accepts ``T-1``, ``T-1 --instant``,
   ``T-1 --speed 2``; unknown flags pass through silently.
6. ``export_cast`` refuses to overwrite an existing file (postmortem
   exports must be deliberate).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from chaos_agent.tui.events import (
    TokenReceived,
    ToolCompleted,
    ToolStarted,
)
from chaos_agent.tui.replay import (
    MAX_GAP_SECONDS,
    Replayer,
    export_cast,
    format_meta_row,
    list_recordings,
    parse_recording,
    parse_replay_args,
    resolve_recording_path,
)


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fp:
        for entry in lines:
            fp.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _ts(base: datetime, offset: float) -> str:
    return (base + timedelta(seconds=offset)).isoformat()


class TestParseRecording:
    def test_rehydrates_known_events_in_order(self, tmp_path: Path):
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-1.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 0.5), "type": "ToolStarted", "data": {"tool_name": "kubectl"}},
                {"ts": _ts(base, 1.0), "type": "ToolCompleted", "data": {"tool_name": "kubectl", "content": "ok"}},
            ],
        )
        out = parse_recording(path)
        assert len(out) == 3
        assert isinstance(out[0][1], TokenReceived)
        assert out[0][1].content == "a"
        # First delta is always 0 (no prior event to compare to).
        assert out[0][0] == 0.0
        # Subsequent deltas reflect the recorded gap.
        assert 0.4 < out[1][0] < 0.6
        assert isinstance(out[2][1], ToolCompleted)
        assert out[2][1].tool_name == "kubectl"

    def test_clamps_long_gap_to_cap(self, tmp_path: Path):
        # A 60-second gap (slow LLM thinking) should clamp to MAX_GAP_SECONDS
        # so the replay doesn't sit there for a minute.
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-2.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 60), "type": "TokenReceived", "data": {"content": "b"}},
            ],
        )
        out = parse_recording(path)
        assert out[1][0] == MAX_GAP_SECONDS

    def test_skips_unknown_event_type(self, tmp_path: Path):
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-3.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "MadeUpEvent", "data": {}},
                {"ts": _ts(base, 0.1), "type": "TokenReceived", "data": {"content": "x"}},
            ],
        )
        out = parse_recording(path)
        # Unknown event dropped; the second one survives.
        assert len(out) == 1
        assert isinstance(out[0][1], TokenReceived)

    def test_skips_malformed_json_line(self, tmp_path: Path):
        path = tmp_path / "T-4.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'not-json\n'
            '{"ts":"2026-01-01T00:00:00+00:00","type":"TokenReceived","data":{"content":"a"}}\n',
            encoding="utf-8",
        )
        out = parse_recording(path)
        assert len(out) == 1

    def test_tolerates_extra_fields_via_intersection(self, tmp_path: Path):
        # If a future recorder adds a new field to TokenReceived, the
        # replayer should still construct the event from the fields it
        # knows about rather than crashing.
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-5.jsonl"
        _write_jsonl(
            path,
            [
                {
                    "ts": _ts(base, 0),
                    "type": "TokenReceived",
                    "data": {"content": "a", "future_field": "ignored"},
                }
            ],
        )
        out = parse_recording(path)
        assert len(out) == 1
        assert out[0][1].content == "a"


class TestListRecordings:
    def test_empty_dir_returns_empty_list(self, tmp_path: Path):
        # /recordings on a fresh install must not error.
        assert list_recordings(tmp_path) == []

    def test_returns_only_jsonl_files(self, tmp_path: Path):
        rec = tmp_path / "recordings"
        rec.mkdir()
        (rec / "T-1.jsonl").write_text(
            json.dumps({"ts": "2026-01-01T00:00:00+00:00", "type": "TokenReceived", "data": {}}) + "\n",
            encoding="utf-8",
        )
        # A stray file must be ignored, not crash the listing.
        (rec / "notes.txt").write_text("hello", encoding="utf-8")
        metas = list_recordings(tmp_path)
        assert len(metas) == 1
        assert metas[0].task_id == "T-1"
        assert metas[0].event_count == 1

    def test_sorted_newest_first(self, tmp_path: Path):
        rec = tmp_path / "recordings"
        rec.mkdir()
        old = rec / "T-old.jsonl"
        new = rec / "T-new.jsonl"
        old.write_text(
            json.dumps({"ts": "2026-01-01T00:00:00+00:00", "type": "TokenReceived", "data": {}}) + "\n",
            encoding="utf-8",
        )
        new.write_text(
            json.dumps({"ts": "2026-01-02T00:00:00+00:00", "type": "TokenReceived", "data": {}}) + "\n",
            encoding="utf-8",
        )
        # Make 'new' actually newer on disk.
        old_time = time.time() - 100
        new_time = time.time()
        os.utime(old, (old_time, old_time))
        os.utime(new, (new_time, new_time))
        metas = list_recordings(tmp_path)
        assert [m.task_id for m in metas] == ["T-new", "T-old"]


class TestParseReplayArgs:
    def test_no_args_returns_empty(self):
        task_id, opts = parse_replay_args("")
        assert task_id == ""
        assert opts == {}

    def test_bare_task_id(self):
        task_id, opts = parse_replay_args("T-abc")
        assert task_id == "T-abc"
        assert opts == {"instant": False, "speed": 1.0}

    def test_instant_flag(self):
        _, opts = parse_replay_args("T-1 --instant")
        assert opts["instant"] is True

    def test_speed_flag(self):
        _, opts = parse_replay_args("T-1 --speed 2.5")
        assert opts["speed"] == 2.5

    def test_unknown_flag_passes_through_silently(self):
        # A typo in the flag must not break the replay — better to play
        # at default speed than to refuse the request.
        task_id, opts = parse_replay_args("T-1 --typo")
        assert task_id == "T-1"
        assert opts == {"instant": False, "speed": 1.0}


class TestResolveRecordingPath:
    def test_missing_returns_none(self, tmp_path: Path):
        assert resolve_recording_path(tmp_path, "T-missing") is None

    def test_missing_task_id_returns_none(self, tmp_path: Path):
        assert resolve_recording_path(tmp_path, "") is None

    def test_existing_jsonl_resolves(self, tmp_path: Path):
        rec = tmp_path / "recordings"
        rec.mkdir()
        (rec / "T-here.jsonl").write_text("{}\n", encoding="utf-8")
        result = resolve_recording_path(tmp_path, "T-here")
        assert result is not None
        assert result.name == "T-here.jsonl"


class TestExportCast:
    def test_copies_file(self, tmp_path: Path):
        src = tmp_path / "src.jsonl"
        src.write_text("hello\n", encoding="utf-8")
        dst = tmp_path / "out.cast"
        size = export_cast(src, dst)
        assert size == len(b"hello\n")
        assert dst.read_text(encoding="utf-8") == "hello\n"

    def test_refuses_to_overwrite(self, tmp_path: Path):
        src = tmp_path / "src.jsonl"
        src.write_text("a\n", encoding="utf-8")
        dst = tmp_path / "out.cast"
        dst.write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            export_cast(src, dst)
        # Existing file is untouched.
        assert dst.read_text(encoding="utf-8") == "existing"


class _FakeRenderer:
    """Captures dispatched events; satisfies Replayer's renderer contract."""

    def __init__(self) -> None:
        self.dispatched: list = []
        self._recorder = None

    async def dispatch(self, event) -> None:
        self.dispatched.append(event)


class TestReplayer:
    def test_replays_events_in_order_and_returns_count(self, tmp_path: Path):
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-1.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 0.1), "type": "ToolStarted", "data": {"tool_name": "kubectl"}},
                {"ts": _ts(base, 0.2), "type": "ToolCompleted", "data": {"tool_name": "kubectl", "content": "ok"}},
            ],
        )
        renderer = _FakeRenderer()
        replayer = Replayer(renderer, instant=True)
        played = asyncio.run(replayer.replay(path))
        assert played == 3
        assert isinstance(renderer.dispatched[0], TokenReceived)
        assert isinstance(renderer.dispatched[1], ToolStarted)
        assert isinstance(renderer.dispatched[2], ToolCompleted)

    def test_instant_mode_does_not_sleep(self, tmp_path: Path):
        # Two events 2 seconds apart on tape — instant replay must
        # finish in well under that wall time.
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-2.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 2), "type": "TokenReceived", "data": {"content": "b"}},
            ],
        )
        renderer = _FakeRenderer()
        replayer = Replayer(renderer, instant=True)
        t0 = time.monotonic()
        asyncio.run(replayer.replay(path))
        assert time.monotonic() - t0 < 0.5

    def test_stop_event_aborts_midway(self, tmp_path: Path):
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-3.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 0.01), "type": "TokenReceived", "data": {"content": "b"}},
            ],
        )
        renderer = _FakeRenderer()
        replayer = Replayer(renderer, instant=True)
        stop = asyncio.Event()
        stop.set()  # already set — replay should bail before any dispatch
        async def _run() -> int:
            return await replayer.replay(path, stop_event=stop)

        played = asyncio.run(_run())
        assert played == 0
        assert renderer.dispatched == []


class TestFormatMetaRow:
    def test_format_includes_task_id_and_count(self, tmp_path: Path):
        base = datetime.now(timezone.utc)
        path = tmp_path / "T-fmt.jsonl"
        _write_jsonl(
            path,
            [
                {"ts": _ts(base, 0), "type": "TokenReceived", "data": {"content": "a"}},
                {"ts": _ts(base, 0.1), "type": "TokenReceived", "data": {"content": "b"}},
            ],
        )
        metas = list_recordings(path.parent.parent)
        # list_recordings expects a memory_dir, not the recordings dir
        # itself — re-arrange.
        # Re-stage in proper layout:
        rec = tmp_path / "memory" / "recordings"
        rec.mkdir(parents=True)
        (rec / "T-fmt.jsonl").write_text(path.read_text(), encoding="utf-8")
        metas = list_recordings(tmp_path / "memory")
        assert len(metas) == 1
        row = format_meta_row(metas[0])
        assert "T-fmt" in row
        assert "2 events" in row
