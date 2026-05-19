"""Tests for PR-E1 — the per-task event recorder.

The recorder is a sidecar on ``Renderer.dispatch``: every dispatched
event lands as one JSON line in
``<memory_dir>/recordings/<task_id>.jsonl``. The replay controller
(PR-E3) reads that file and re-dispatches the events.

Five orthogonal pieces of behavior to pin:

1. Events that arrive BEFORE a task_id is known are buffered and
   flushed in arrival order once a task_id event lands. (Otherwise we'd
   lose the early ``TokenReceived`` / ``PhaseChanged`` frames that
   precede the first ``InterruptRequired``.)
2. Buffer is bounded — a chat turn that never gets a task_id can't
   blow up memory.
3. The file format is one JSON object per line with ``ts``, ``type``,
   ``data`` keys.
4. ``stop()`` is idempotent and re-arms the recorder for a fresh task
   on the next event with a task_id.
5. Disk failures (read-only filesystem, fd exhaustion) disable the
   recorder rather than crash the dispatch path.
"""

from __future__ import annotations

import json
from pathlib import Path

from chaos_agent.tui.events import (
    InterruptRequired,
    PhaseChanged,
    TaskError,
    TaskResult,
    ThinkingReceived,
    TokenReceived,
    ToolCompleted,
    ToolStarted,
)
from chaos_agent.tui.recording import EventRecorder, _MAX_BUFFER


class TestRecorderLifecycle:
    def test_records_to_per_task_jsonl(self, tmp_path: Path) -> None:
        rec = EventRecorder(tmp_path)
        rec.record(TokenReceived(content="hello", node="x"))
        rec.record(TaskResult(data={"ok": True}, task_id="T-1"))
        rec.stop()

        path = tmp_path / "recordings" / "T-1.jsonl"
        assert path.exists()
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        second = json.loads(lines[1])
        assert first["type"] == "TokenReceived"
        assert first["data"]["content"] == "hello"
        assert second["type"] == "TaskResult"
        assert second["data"]["task_id"] == "T-1"
        # Each line carries an ISO timestamp (sanity check, not exact match).
        assert "T" in first["ts"] and ":" in first["ts"]

    def test_pre_task_events_flush_in_arrival_order(self, tmp_path: Path) -> None:
        rec = EventRecorder(tmp_path)
        # Three pre-task-id events — none have a task_id so they buffer.
        rec.record(PhaseChanged(phase="intent", source="intent_clarification"))
        rec.record(ThinkingReceived(content="weighing options", node="intent"))
        rec.record(ToolStarted(tool_name="kubectl", node="intent"))
        # The fourth event has the task_id — opens the file and flushes.
        rec.record(InterruptRequired(interrupt_info={"type": "confirmation"}, task_id="T-2"))
        rec.stop()

        path = tmp_path / "recordings" / "T-2.jsonl"
        types = [json.loads(line)["type"] for line in path.read_text().splitlines()]
        # Order preserved — the InterruptRequired (which carried the
        # task_id) must come last, after the buffered three.
        assert types == [
            "PhaseChanged",
            "ThinkingReceived",
            "ToolStarted",
            "InterruptRequired",
        ]

    def test_stop_then_record_starts_a_fresh_task(self, tmp_path: Path) -> None:
        # The recorder is reused across turns — after stop() the next
        # event with a task_id should open a NEW file.
        rec = EventRecorder(tmp_path)
        rec.record(TaskResult(data={"x": 1}, task_id="T-A"))
        rec.stop()
        rec.record(TaskResult(data={"x": 2}, task_id="T-B"))
        rec.stop()

        path_a = tmp_path / "recordings" / "T-A.jsonl"
        path_b = tmp_path / "recordings" / "T-B.jsonl"
        assert path_a.exists() and path_b.exists()
        # Each file is independent, no cross-contamination. The outer
        # ``data`` field is the recorder envelope; the inner ``data`` is
        # ``TaskResult.data``.
        assert json.loads(path_a.read_text().strip())["data"]["data"]["x"] == 1
        assert json.loads(path_b.read_text().strip())["data"]["data"]["x"] == 2

    def test_double_stop_is_idempotent(self, tmp_path: Path) -> None:
        rec = EventRecorder(tmp_path)
        rec.record(TaskError(message="boom", task_id="T-3"))
        rec.stop()
        # No exception on a second stop().
        rec.stop()


class TestRecorderBuffer:
    def test_buffer_drops_overflow_events(self, tmp_path: Path) -> None:
        # A turn that never gets a task_id (rare — pure preflight chatter
        # before the graph fires) shouldn't grow unbounded. After the cap
        # excess events are silently dropped — we'd rather lose a few
        # frames than OOM.
        rec = EventRecorder(tmp_path)
        for i in range(_MAX_BUFFER + 50):
            rec.record(TokenReceived(content=str(i), node=""))
        # Buffer is full; nothing on disk yet.
        assert not (tmp_path / "recordings").exists()
        # An event with a task_id flushes — only the first _MAX_BUFFER
        # made it past the cap, plus the trigger event.
        rec.record(TaskResult(data={}, task_id="T-cap"))
        rec.stop()
        path = tmp_path / "recordings" / "T-cap.jsonl"
        lines = path.read_text().splitlines()
        assert len(lines) == _MAX_BUFFER + 1
        # First line is the very first TokenReceived (content="0").
        assert json.loads(lines[0])["data"]["content"] == "0"


class TestRecorderSerialisation:
    def test_tool_completed_payload_serialises(self, tmp_path: Path) -> None:
        rec = EventRecorder(tmp_path)
        rec.record(
            ToolCompleted(
                tool_name="kubectl",
                content='{"status":"Active","message":"ok"}',
                node="agent_loop",
            )
        )
        rec.record(TaskResult(data={"ok": True}, task_id="T-tool"))
        rec.stop()
        path = tmp_path / "recordings" / "T-tool.jsonl"
        first = json.loads(path.read_text().splitlines()[0])
        assert first["type"] == "ToolCompleted"
        assert first["data"]["tool_name"] == "kubectl"
        assert first["data"]["node"] == "agent_loop"
        # Content is preserved verbatim — not re-parsed as JSON.
        assert first["data"]["content"].startswith("{")

    def test_unserialisable_field_falls_back_to_str(self, tmp_path: Path) -> None:
        # The recorder must not crash if a future event carries a
        # non-JSON-able payload (e.g. a Path or a class instance). It
        # should str()-fallback the field instead.
        rec = EventRecorder(tmp_path)
        ev = TaskResult(data={"path": Path("/tmp/x")}, task_id="T-bad")
        rec.record(ev)
        rec.stop()
        path = tmp_path / "recordings" / "T-bad.jsonl"
        line = json.loads(path.read_text().strip())
        # The whole `data` field was unserialisable as a dict-of-Path,
        # so it falls back to str() — which is fine; the record exists.
        assert line["type"] == "TaskResult"


class TestRecorderDisk:
    def test_disable_then_record_is_noop(self, tmp_path: Path) -> None:
        rec = EventRecorder(tmp_path)
        rec.disable()
        rec.record(TaskResult(data={}, task_id="T-x"))
        rec.stop()
        # No directory was created because the recorder was off.
        assert not (tmp_path / "recordings").exists()

    def test_unwritable_dir_disables_recorder(self, tmp_path: Path) -> None:
        # Point the recorder at a path where directory creation will fail
        # — a regular file with the recordings name beneath it. The
        # recorder must disable itself silently.
        clash = tmp_path / "recordings"
        clash.write_text("not a dir")
        rec = EventRecorder(tmp_path)
        rec.record(TaskResult(data={}, task_id="T-fs"))
        # Disable kicked in; no exception escaped.
        assert not rec.enabled
        rec.stop()
