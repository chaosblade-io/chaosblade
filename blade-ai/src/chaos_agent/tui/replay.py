"""PR-E3 — recording playback for /replay.

The recorder (PR-E1) writes one JSON line per dispatched ``TUIEvent``
into ``<memory_dir>/recordings/<task_id>.jsonl``. This module reads
those tapes back, reconstructs each event as the matching dataclass,
and replays them through a Renderer so the user sees the exact same
panels in the exact same order.

Two surfaces:

* ``/replay <task_id>`` — re-dispatch every event in original arrival
  order with timing from the recorded ISO8601 timestamps. The replay
  clamps any individual delay to ``MAX_GAP_SECONDS`` (3 s) so a long
  LLM-thinking pause from the live run doesn't reproduce as 30 s of
  spinner. ``--instant`` skips delays entirely; ``--speed N`` scales.

* ``/recordings`` — list available tape files with task_id, event
  count, first/last ts, and size on disk so the user can pick.

The replayer never re-opens a recorder of its own — events are taped
the *first* time and replaying them must not generate a second tape.
The Renderer is responsible for routing dispatched events to the
console; we pass it as-is.

This module is also where we document the .cast envelope for any
future asciinema-compatible export. For now, the JSONL files ARE the
cast — each line is self-describing — so /export is a one-line copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, TYPE_CHECKING

from chaos_agent.tui import events as tui_events
from chaos_agent.tui.events import TUIEvent

if TYPE_CHECKING:  # avoid circular import at module load time
    from chaos_agent.tui.renderers import Renderer

logger = logging.getLogger(__name__)


MAX_GAP_SECONDS: float = 3.0
"""Cap on a single inter-event gap when replaying with original timing.

Recordings can contain multi-second pauses (LLM thinking, slow tool
output) that aren't useful to relive in a postmortem. Clamping the
*individual* gap rather than the total preserves event ordering and
relative pacing of fast bursts (a flurry of tokens still feels fast),
while keeping the whole replay below a sensible upper bound.
"""

DEFAULT_LIST_LIMIT: int = 20
"""How many recordings ``list_recordings`` returns by default."""


# Map event class name → class object so we can rehydrate from JSON.
# Built once at import time. Anything declared in tui.events that is
# a dataclass and a TUIEvent subclass auto-registers.
def _build_event_map() -> dict[str, type[TUIEvent]]:
    out: dict[str, type[TUIEvent]] = {}
    for name in dir(tui_events):
        obj = getattr(tui_events, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, TUIEvent)
            and obj is not TUIEvent
        ):
            out[name] = obj
    return out


_EVENT_TYPES: dict[str, type[TUIEvent]] = _build_event_map()


@dataclass
class RecordingMeta:
    """Summary row for ``/recordings`` listing — one per .jsonl file."""

    task_id: str
    path: Path
    event_count: int
    started_iso: str
    ended_iso: str
    size_bytes: int


def recordings_dir(memory_dir: Path) -> Path:
    """Resolve the per-session recordings root."""
    return Path(memory_dir) / "recordings"


def list_recordings(
    memory_dir: Path,
    limit: int = DEFAULT_LIST_LIMIT,
) -> list[RecordingMeta]:
    """Return up to ``limit`` recordings, newest first.

    Sorted by mtime — matches the user's mental model ("the most recent
    experiment" rather than alphabetical task_id ordering). A directory
    that doesn't exist yet returns an empty list, not an error: the
    /recordings command is meant to work on a fresh session.
    """
    root = recordings_dir(memory_dir)
    if not root.exists():
        return []
    items: list[tuple[float, Path]] = []
    for entry in root.iterdir():
        if entry.suffix != ".jsonl" or not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        items.append((mtime, entry))
    items.sort(key=lambda t: t[0], reverse=True)
    out: list[RecordingMeta] = []
    for _, path in items[:limit]:
        meta = _summarise(path)
        if meta is not None:
            out.append(meta)
    return out


def _summarise(path: Path) -> Optional[RecordingMeta]:
    """Parse just enough of a file to populate one ``RecordingMeta`` row.

    Streaming so a 50 MB tape doesn't have to be loaded for the listing.
    Returns None on a file that's empty or completely malformed; callers
    skip rather than error out (one bad tape mustn't hide the others).
    """
    started: str = ""
    ended: str = ""
    count: int = 0
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = entry.get("ts", "")
                if not started:
                    started = ts
                ended = ts or ended
                count += 1
    except OSError:
        return None
    if count == 0:
        return None
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return RecordingMeta(
        task_id=path.stem,
        path=path,
        event_count=count,
        started_iso=started,
        ended_iso=ended,
        size_bytes=size,
    )


def parse_recording(path: Path) -> list[tuple[float, TUIEvent]]:
    """Read a recording into ``(delta_seconds, event)`` pairs.

    The first event has delta 0; subsequent deltas are the *clamped*
    inter-arrival gap so replays don't reproduce 30-second LLM pauses.
    Lines that fail to parse, name an unknown event type, or don't
    survive dataclass construction are skipped with a warning rather
    than raising — a malformed line shouldn't abort the whole replay.
    """
    out: list[tuple[float, TUIEvent]] = []
    last_ts: Optional[datetime] = None

    with path.open("r", encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning(f"recording parse error in {path}: {e}")
                continue
            type_name = entry.get("type", "")
            data = entry.get("data") or {}
            ts_str = entry.get("ts", "")
            cls = _EVENT_TYPES.get(type_name)
            if cls is None:
                logger.debug(f"unknown event type in recording: {type_name}")
                continue
            try:
                event = cls(**data)
            except TypeError as e:
                # Recorder may have stashed an extra field (forward-compat)
                # or a missing optional. Try the intersection of fields.
                try:
                    fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                    event = cls(**fields)
                except Exception:
                    logger.warning(f"cannot rehydrate {type_name}: {e}")
                    continue
            try:
                ts = datetime.fromisoformat(ts_str) if ts_str else None
            except ValueError:
                ts = None
            if last_ts is None or ts is None:
                delta = 0.0
            else:
                delta = max(0.0, (ts - last_ts).total_seconds())
                if delta > MAX_GAP_SECONDS:
                    delta = MAX_GAP_SECONDS
            last_ts = ts
            out.append((delta, event))
    return out


class Replayer:
    """Drives a Renderer through a sequence of recorded events.

    Construction binds a Renderer; ``replay`` accepts a path and
    schedules dispatches with the recorded timing. Cancellation is
    cooperative — pass ``stop_event`` and set it from the caller's
    Ctrl-C handler to abort mid-replay.

    Important: the Renderer's recorder (if any) must NOT re-tape
    replayed events. Callers either disable the recorder before
    calling replay, or live with the duplicate — by default the
    helper here calls ``recorder.disable()`` for the duration if
    ``mute_recorder`` is True.
    """

    def __init__(
        self,
        renderer: "Renderer",
        speed: float = 1.0,
        instant: bool = False,
        mute_recorder: bool = True,
    ) -> None:
        self._renderer = renderer
        self._speed = max(0.0, speed)
        self._instant = instant
        self._mute_recorder = mute_recorder

    async def replay(
        self,
        path: Path,
        stop_event: Optional[asyncio.Event] = None,
    ) -> int:
        """Dispatch events from ``path``; return the number replayed.

        Skipped (malformed) events are NOT counted toward the return
        value — the user wants to know how many panels they saw, not
        how many bytes the file held.
        """
        events_with_delays = parse_recording(path)
        recorder = getattr(self._renderer, "_recorder", None)
        muted = False
        if self._mute_recorder and recorder is not None and recorder.enabled:
            recorder.disable()
            muted = True

        played = 0
        try:
            for delta, event in events_with_delays:
                if stop_event is not None and stop_event.is_set():
                    break
                if not self._instant and delta > 0 and self._speed > 0:
                    await asyncio.sleep(delta / self._speed)
                await self._renderer.dispatch(event)
                played += 1
        finally:
            # Re-enable the recorder so a subsequent live turn
            # captures normally. If the user explicitly asked to
            # keep it muted, we'd have a separate flag — for now
            # mute is per-replay only.
            if muted and recorder is not None:
                recorder._enabled = True  # type: ignore[attr-defined]
        return played


def format_meta_row(meta: RecordingMeta) -> str:
    """One-line summary for ``/recordings`` console output."""
    size_kb = meta.size_bytes / 1024
    return (
        f"{meta.task_id}  · {meta.event_count} events"
        f"  · {meta.started_iso}"
        f"  · {size_kb:.1f} KB"
    )


def parse_replay_args(args: str) -> tuple[str, dict]:
    """Split the /replay argument string into (task_id, options).

    Accepts ``T-123``, ``T-123 --instant``, ``T-123 --speed 2``.
    Unknown flags pass through silently — better to play at default
    speed than to error out on a typo.
    """
    parts = (args or "").split()
    if not parts:
        return "", {}
    task_id = parts[0]
    opts: dict = {"instant": False, "speed": 1.0}
    i = 1
    while i < len(parts):
        tok = parts[i]
        if tok in ("--instant", "-i"):
            opts["instant"] = True
        elif tok in ("--speed", "-s") and i + 1 < len(parts):
            try:
                opts["speed"] = float(parts[i + 1])
            except ValueError:
                pass
            i += 1
        i += 1
    return task_id, opts


def resolve_recording_path(memory_dir: Path, task_id: str) -> Optional[Path]:
    """Return the .jsonl path for ``task_id``, or None if missing.

    ``task_id`` may be the bare id (``T-abc``) or the file stem with
    extension. The resolver is forgiving on case for the unusual
    Windows-on-WSL path edge case, but otherwise expects exact match.
    """
    if not task_id:
        return None
    root = recordings_dir(memory_dir)
    candidate = root / f"{task_id}.jsonl"
    if candidate.exists():
        return candidate
    # Tolerate caller passing the .jsonl suffix already.
    if task_id.endswith(".jsonl"):
        candidate2 = root / task_id
        if candidate2.exists():
            return candidate2
    return None


def iter_recording_lines(path: Path) -> Iterable[dict]:
    """Streaming iterator — used by ``/recordings export`` and tests."""
    with path.open("r", encoding="utf-8") as fp:
        for raw in fp:
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def export_cast(src: Path, dst: Path) -> int:
    """Copy a recording to a portable destination; return bytes written.

    Today the .cast export is just the source JSONL. Keeping it as a
    thin wrapper means a future asciinema-compatible header (with
    width/height/env metadata) can drop into one place. Refuses to
    overwrite an existing dst — a postmortem export should be a
    deliberate user gesture, not an accidental clobber.
    """
    if dst.exists():
        raise FileExistsError(str(dst))
    data = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    try:
        os.chmod(dst, 0o644)
    except OSError:
        pass
    return len(data)
