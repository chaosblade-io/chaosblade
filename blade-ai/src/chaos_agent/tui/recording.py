"""Event recording — append every dispatched TUIEvent to a per-task JSONL file.

Foundation for PR-E3 (``/replay`` controller). The recorder is a passive
sidecar on ``Renderer.dispatch``: it serialises each event as one
``{ts, type, data}`` line so the replay controller can later read the
file back, reconstruct the events, and re-dispatch them at original
pacing — or stepped through manually for postmortem.

Design choices:
  - One file per task at ``<memory_dir>/recordings/<task_id>.jsonl``.
    Per-file granularity matches ``memory/tasks/<task_id>.json`` already
    in use, so all per-task artefacts live side-by-side.
  - The recorder owns NO state about the rendering — recording is purely
    additive and never blocks dispatch. Failure to write a line is logged
    and swallowed; a broken disk must not break the TUI.
  - ``task_id`` is derived from the first event that carries one
    (TaskResult / TaskError / InterruptRequired / TaskResumed /
    RecoveryTriggered). Events that arrive earlier (TokenReceived,
    PhaseChanged on first phase) are buffered in memory until a task_id
    is known, then flushed in arrival order. This lets us capture the
    *full* turn including the lead-up to task_id assignment.
  - Buffer is bounded to ``_MAX_BUFFER`` events to avoid unbounded
    growth on chat turns that never get a task_id — those events are
    silently dropped after the cap.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, TextIO

from chaos_agent.tui.events import TUIEvent

logger = logging.getLogger(__name__)


_MAX_BUFFER = 256
"""Upper bound on pre-task-id buffer. A normal injection turn fires far
fewer than this — a chat turn that never gets a task_id won't grow
unbounded on a long thinking stream."""


def _now_iso() -> str:
    """ISO8601 timestamp with timezone — matches utils.time.now_iso()."""
    return datetime.now(timezone.utc).isoformat()


def _serialise_event(event: TUIEvent) -> dict:
    """Convert a TUIEvent dataclass into a JSON-friendly dict.

    Falls back to ``str()`` for any non-serialisable field so a recorder
    can never crash on an unexpected payload.
    """
    type_name = event.__class__.__name__
    if is_dataclass(event):
        try:
            data = asdict(event)
        except Exception:
            data = {k: getattr(event, k, None) for k in vars(event)}
    else:
        data = {k: getattr(event, k, None) for k in vars(event)}

    safe: dict = {}
    for k, v in data.items():
        try:
            json.dumps(v)
            safe[k] = v
        except (TypeError, ValueError):
            safe[k] = str(v)
    return {"ts": _now_iso(), "type": type_name, "data": safe}


class EventRecorder:
    """Append-only per-task event log.

    Lifecycle:
        recorder.record(event)   # buffers / writes a line
        recorder.stop()          # closes the file (idempotent)
    """

    def __init__(self, memory_dir: Path, enabled: bool = True) -> None:
        self._dir: Path = Path(memory_dir) / "recordings"
        self._enabled: bool = enabled
        self._task_id: str = ""
        self._fp: Optional[TextIO] = None
        self._buffer: list[dict] = []
        # Public so tests can verify file path resolution. Don't write to it.
        self.last_path: Optional[Path] = None
        # Recording happens on the dispatch thread (asyncio loop), but
        # signals/aborts can come from elsewhere. A lock keeps the file
        # handle and buffer consistent.
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def disable(self) -> None:
        """Permanently disable this recorder (e.g. on disk error)."""
        self._enabled = False

    @property
    def is_recording(self) -> bool:
        return self._fp is not None

    @property
    def current_task_id(self) -> str:
        return self._task_id

    def record(self, event: TUIEvent) -> None:
        """Append the event to the per-task log.

        Buffers in memory until a task_id is observed; on first arrival
        the buffer is flushed in original order. Always swallows IO
        errors — recording is best-effort.
        """
        if not self._enabled:
            return
        with self._lock:
            entry = _serialise_event(event)

            # Look for a task_id on this event so we can open the file
            # eagerly; events without one stay in the buffer.
            task_id = entry.get("data", {}).get("task_id", "")
            if task_id and not self._task_id:
                self._open(task_id)

            if self._fp is None:
                if len(self._buffer) >= _MAX_BUFFER:
                    return
                self._buffer.append(entry)
                return

            self._write(entry)

    def stop(self) -> None:
        """Close the file. Idempotent; safe to call without start."""
        with self._lock:
            self._close_locked()

    # ------------------------------------------------------------------
    # Internal — must be called with self._lock held
    # ------------------------------------------------------------------

    def _open(self, task_id: str) -> None:
        """Open the JSONL file for ``task_id`` and flush the pre-buffer."""
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning(f"Cannot create recordings dir {self._dir}: {e}")
            self.disable()
            return

        path = self._dir / f"{task_id}.jsonl"
        try:
            # Append mode: a resumed task continues its tape rather than
            # losing the pre-resume frames.
            self._fp = path.open("a", encoding="utf-8")
        except OSError as e:
            logger.warning(f"Cannot open recording file {path}: {e}")
            self.disable()
            return

        self._task_id = task_id
        self.last_path = path

        for entry in self._buffer:
            self._write(entry)
        self._buffer.clear()

    def _write(self, entry: dict) -> None:
        if self._fp is None:
            return
        try:
            self._fp.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._fp.flush()
        except (OSError, ValueError) as e:
            # Disk full, fd reused, etc. — disable rather than spam logs
            # for every subsequent event.
            logger.warning(f"Recording write failed for {self._task_id}: {e}")
            self._close_locked()
            self.disable()

    def _close_locked(self) -> None:
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
            self._fp = None
        self._task_id = ""
        self._buffer.clear()
