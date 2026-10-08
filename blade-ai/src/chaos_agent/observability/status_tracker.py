"""Real-time agent status tracking with pub/sub for CLI and Server consumption.

Provides an asyncio-based event bus where agent nodes and tools publish
status events, and consumers (CLI printer, Server SSE) subscribe to
receive them in real-time.

Usage in nodes/tools:
    from chaos_agent.observability.status_tracker import track_status, StatusEvent

    async def my_node(state):
        async with track_status(task_id, "my_node", "Processing...") as tracker:
            # do work
            tracker.update("Still working...")
        # automatically emits a "completed" event on exit

Usage in CLI:
    from chaos_agent.observability.status_tracker import subscribe, unsubscribe

    queue = subscribe(task_id)
    while True:
        event = await queue.get()
        print(event)
"""

import asyncio
import logging
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum

from chaos_agent.persistence.task_identity import is_real_task_id

# Thin re-export: elided_preview's canonical implementation moved to
# chaos_agent.utils.truncation (the shared home for truncation forms).
# Kept so the 8 existing call sites (shell / baseline / status_display ...)
# keep importing it from here — zero behavior change.
from chaos_agent.utils.truncation import elided_preview  # noqa: F401

logger = logging.getLogger(__name__)

# Per-tracker event history cap. History exists only to replay recent
# context to a late SSE subscriber, so events older than the cap have no
# consumer. Bounding it keeps a long-lived server process from growing
# without limit: ``tui-<sid>`` trackers (see ``is_event_channel_id``) are
# never explicitly removed — ``remove_tracker`` is only called on CLI
# paths — so an unbounded list would leak for the process lifetime.
_HISTORY_MAXLEN = 1000


class StatusPhase(str, Enum):
    """Phase of a status event."""

    STARTED = "started"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class StatusCategory(str, Enum):
    """Category of the status source."""

    NODE = "node"
    TOOL = "tool"
    LLM = "llm"
    SYSTEM = "system"


@dataclass
class StatusEvent:
    """A single status event emitted during agent execution."""

    task_id: str
    phase: str  # StatusPhase value
    category: str  # StatusCategory value
    source: str  # node name or tool name, e.g. "agent_loop", "blade_create"
    message: str  # human-readable description
    timestamp: float = 0.0
    duration_ms: float = 0.0
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = time.time()

    def to_dict(self) -> dict:
        return asdict(self)


class StatusTracker:
    """Per-task status tracker with fan-out to subscribers.

    Each task_id gets its own tracker instance. Subscribers receive events
    via asyncio.Queue. This enables both CLI (direct queue read) and
    Server SSE (async iteration) consumption patterns.
    """

    def __init__(self, task_id: str):
        self.task_id = task_id
        self._subscribers: list[asyncio.Queue[StatusEvent]] = []
        self._history: deque[StatusEvent] = deque(maxlen=_HISTORY_MAXLEN)
        self._current_source: str = ""
        self._start_time: float = 0.0
        # Sub-span bookkeeping, see :meth:`mark`.
        self._segments: list[tuple[str, float]] = []
        self._segment_start: float = 0.0

    def subscribe(self, maxsize: int = 100) -> asyncio.Queue[StatusEvent]:
        """Subscribe to status events for this task. Returns a Queue."""
        q: asyncio.Queue[StatusEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[StatusEvent]) -> None:
        """Remove a subscriber queue."""
        if queue in self._subscribers:
            self._subscribers.remove(queue)

    def emit(self, event: StatusEvent) -> None:
        """Publish a status event to all subscribers."""
        self._history.append(event)
        for q in self._subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning(
                    f"Status subscriber queue full for task {event.task_id}, dropping event"
                )

    def start(self, category: str, source: str, message: str, detail: dict = None) -> None:
        """Emit a STARTED event and track timing."""
        self._current_source = source
        self._start_time = time.monotonic()
        self._segments = []
        self._segment_start = self._start_time
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.STARTED,
            category=category,
            source=source,
            message=message,
            detail=detail or {},
        ))

    def mark(self, label: str) -> None:
        """Close the sub-span that just ended and name it.

        A node's COMPLETED event carries ONE number: everything from
        ``start()`` to ``complete()``. That number is honest, but the
        message it is rendered next to usually names a single phase —
        ``Iteration 5 LLM:`` — so a reader concludes the LLM call took
        that long. inject-6ebf341c turn 1 printed ``(71751ms)`` beside
        ``LLM:`` and the 67s was duly audited as model latency; the
        breakdown that would have shown where it actually went did not
        exist at any layer, so the mis-attribution was unfalsifiable
        rather than merely wrong.

        ``mark`` is the missing layer: call it right AFTER a phase's work
        returns and the elapsed interval since the previous mark is
        recorded under *label*. Call sites are one line each and need no
        knowledge of the tracker's internals.

        Segments ride in ``detail["segments"]`` of subsequent events and
        are logged on completion. They deliberately do NOT get their own
        rows in ``task_spans``: that table has a fixed column set across
        three backends, and a migration to answer one attribution
        question is not a proportionate cost while the log line answers
        it. Revisit only if segment-level SQL aggregation is needed.
        """
        now = time.monotonic()
        if self._segment_start:
            self._segments.append((label, (now - self._segment_start) * 1000))
        self._segment_start = now

    def _segments_snapshot(self) -> list[dict]:
        """Closed segments plus the still-open tail, labelled ``other``.

        The open tail is included so the parts always sum to the whole.
        A breakdown that quietly omitted the un-instrumented remainder
        would invite the same mis-attribution it exists to prevent —
        readers add up what is listed and assume it is everything.
        """
        segs = [{"label": lb, "ms": round(ms, 1)} for lb, ms in self._segments]
        if self._segment_start:
            segs.append({
                "label": "other",
                "ms": round((time.monotonic() - self._segment_start) * 1000, 1),
            })
        return segs

    def _detail_with_segments(self, detail: dict = None) -> dict:
        """Caller detail plus ``segments`` once this span has been marked.

        Gated on at least one ``mark`` having happened: every node in the graph
        runs through ``start``/``complete``, and attaching a lone ``other``
        segment to all of them would add a field that says nothing to every
        event the tracker already emits.
        """
        out = dict(detail or {})
        if self._segments and "segments" not in out:
            out["segments"] = self._segments_snapshot()
        return out

    def _log_segments(self, duration: float) -> None:
        """One log line per span close: the post-hoc attribution channel.

        Events reach a live subscriber or nothing; a slow turn in a run
        nobody was watching has to be answerable afterwards, and the
        logger is the only durable channel that needs no schema.
        """
        if not self._segments:
            return
        breakdown = " ".join(
            f"{s['label']}={s['ms']:.0f}ms" for s in self._segments_snapshot()
        )
        logger.info(
            f"span breakdown task={self.task_id} source={self._current_source} "
            f"total={duration:.0f}ms {breakdown}"
        )

    def update(self, message: str, detail: dict = None) -> None:
        """Emit a RUNNING update event."""
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.RUNNING,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=message,
            duration_ms=(time.monotonic() - self._start_time) * 1000 if self._start_time else 0,
            detail=self._detail_with_segments(detail),
        ))

    def complete(self, message: str = "", detail: dict = None) -> None:
        """Emit a COMPLETED event."""
        duration = (time.monotonic() - self._start_time) * 1000 if self._start_time else 0
        self._log_segments(duration)
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.COMPLETED,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=message or f"{self._current_source} completed",
            duration_ms=duration,
            detail=self._detail_with_segments(detail),
        ))

    def fail(self, error: str, detail: dict = None) -> None:
        """Emit a FAILED event."""
        duration = (time.monotonic() - self._start_time) * 1000 if self._start_time else 0
        self._log_segments(duration)
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.FAILED,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=error,
            duration_ms=duration,
            detail=self._detail_with_segments(detail),
        ))

    def intervention(
        self,
        kind: str,
        message: str,
        detail: dict = None,
    ) -> None:
        """Emit a FRAMEWORK-INTERVENTION fact on the SYSTEM channel.

        An intervention is the framework overriding what the model asked
        for: pinning a carrier's duration to the approved contract,
        clamping a parameter, rejecting a call at a guard. The rewrite
        itself is a guardrail action and must stay programmatic — but an
        action nobody can see is indistinguishable from the model having
        chosen the value itself, which is exactly how a CORRECT pin got
        audited as an unexplained drift (Case #64: the plan said 300, the
        dispatched call said 420, and the rewrite that bridged them was
        recorded only on the logger and the session ledger — neither of
        which the ``inject --stream`` surface renders).

        Two deliberate differences from :meth:`update`:

        * ``StatusCategory.SYSTEM``, not NODE. ``cli.status_display``
          drops NODE and TOOL events unless debug is on, so an
          intervention emitted through ``update`` would stay invisible in
          a normal run — reproducing the very gap this closes. SYSTEM is
          the one category that survives, and until this method existed
          it had no producer at all.
        * No ``debug`` marker in ``detail``. ``status_display`` also drops
          ``detail["debug"]`` events outside debug mode; an audit fact is
          not a debug aid.

        ``kind`` is the intervention family (``"pin"`` / ``"clamp"`` /
        ``"reject"`` …) and lands in ``detail["intervention"]`` so
        consumers can filter without parsing the message. ``detail``
        should carry the objective facts — ``before`` / ``after`` /
        ``authority`` — never a judgement about them.
        """
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.RUNNING,
            category=StatusCategory.SYSTEM,
            source=self._current_source,
            message=message,
            # A point event, not a span: leave duration at 0 so the
            # renderer omits it. ``StatusEvent.__post_init__`` stamps
            # ``timestamp`` with the wall clock, which is the fact's own
            # time and the only timing an intervention needs.
            detail={"intervention": kind, **(detail or {})},
        ))

    def get_history(self) -> list[dict]:
        """Return all recorded events as dicts."""
        return [e.to_dict() for e in self._history]

    @property
    def current_source(self) -> str:
        return self._current_source

    def save_state(self) -> tuple[str, float, list, float]:
        """Save current source, start_time and segment state for restoration.

        Used by sub-operations (e.g. conflict check) that need their own
        tracker lifecycle without corrupting the parent operation's state.

        The segment fields travel with the pair: a sub-op that calls
        ``start()`` clears them, and restoring only source/start_time
        would leave the parent's COMPLETED event reporting a breakdown
        that silently lost its first half — the same incomplete-sum trap
        :meth:`mark` documents.

        Returns:
            Opaque tuple to pass to restore_state().
        """
        return (
            self._current_source,
            self._start_time,
            list(self._segments),
            self._segment_start,
        )

    def restore_state(self, saved: tuple[str, float, list, float]) -> None:
        """Restore previously saved source, start_time and segment state.

        Args:
            saved: Tuple from save_state() to restore.
        """
        (
            self._current_source,
            self._start_time,
            _segments,
            self._segment_start,
        ) = saved
        self._segments = list(_segments)


# ---- Null tracker (no task → no state, no events, no growth) ----


class NullTracker(StatusTracker):
    """No-op tracker used when there is no real task to track.

    Intent clarification / chat turns run the same graph nodes as the
    inject pipeline, but they own no task identity (see
    ``persistence.task_identity``).  Handing those callers a normal
    :class:`StatusTracker` was harmful in two ways:

    * its events flowed into the tracer, which then fabricated ``tasks``
      rows for dialogue-level ids ("ghost" experiments), and
    * a single shared placeholder key (``""`` / ``"unknown"``) kept one
      global tracker alive whose ``_history`` list only ever grows — an
      unbounded leak in the long-lived server process.

    Subclassing :class:`StatusTracker` (rather than duck-typing) means
    the full method surface stays in sync automatically; only the
    side-effecting members are neutralised.  ``_history`` is kept
    permanently empty so nothing accumulates.
    """

    def __init__(self) -> None:
        super().__init__(task_id="")

    def subscribe(self, maxsize: int = 100) -> "asyncio.Queue[StatusEvent]":
        # Detached queue: never registered, so it stays empty and GC-able.
        return asyncio.Queue(maxsize=maxsize)

    def unsubscribe(self, queue: "asyncio.Queue[StatusEvent]") -> None:
        return None

    def emit(self, event: StatusEvent) -> None:
        # Swallow the event: no history, no subscribers, no persistence.
        return None

    def start(self, category: str, source: str, message: str, detail: dict = None) -> None:
        # Keep ``current_source`` meaningful for callers that read it back,
        # but record nothing and emit nothing.
        self._current_source = source

    def update(self, message: str, detail: dict = None) -> None:
        return None

    def mark(self, label: str) -> None:
        # No event will ever carry these segments, so recording them
        # would only grow a list nobody reads.
        return None

    def complete(self, message: str = "", detail: dict = None) -> None:
        return None

    def fail(self, error: str, detail: dict = None) -> None:
        return None


# ---- Global registry ----

_trackers: dict[str, StatusTracker] = {}

# Single shared no-op instance — intentionally NOT stored in ``_trackers``
# so dialogue turns leave no residue in the registry.
_NULL_TRACKER = NullTracker()

# Ephemeral event-channel key prefixes: ids that are NOT persistable tasks
# but DO need a live tracker, because a consumer subscribes to them:
#   ``tui-<sid>``      — the TS TUI's main /turn stream (``routes/turn.py``);
#                        ``PreReasoningHook`` fans compaction events here.
#   ``compact-<uuid>`` — the /compact progress stream (``routes/sessions.py``
#                        and ``tui/controllers/commands.py``), which mints the
#                        id and overrides ``state.task_id`` with it.
# Each prefix is a contract between producer, consumer and the gate below —
# keep these as the single literals and build keys from them.
TUI_TRACKER_PREFIX = "tui-"
COMPACT_TRACKER_PREFIX = "compact-"
_EVENT_CHANNEL_PREFIXES = (TUI_TRACKER_PREFIX, COMPACT_TRACKER_PREFIX)

# Shared tracing callback reference (set by factory.py during init)
_tracing_callback = None
_otel_callback = None


def is_event_channel_id(task_id: object) -> bool:
    """True for ids that deserve a real, in-memory tracker.

    Two distinct concepts must not be conflated:

    * **persistable task identity** — :func:`is_real_task_id`; gates the
      ``tasks`` / ``task_details`` / ``task_spans`` writes. Enforced
      independently by ``tracer`` / ``task_store`` / ``_store_sync``.
    * **live event channel** — this function; gates whether a caller gets
      a working in-memory tracker (history + subscriber queues).

    Every real task is also an event channel. An ephemeral id
    (:data:`_EVENT_CHANNEL_PREFIXES`) is an event channel WITHOUT being a
    task: it has a bounded lifetime and must never reach the ``tasks``
    tables — which it cannot, because those writes gate on
    :func:`is_real_task_id` separately. Everything else (dialogue turns,
    ``""``, ``"unknown"``) is neither, and gets the :class:`NullTracker`.
    """
    if is_real_task_id(task_id):
        return True
    return isinstance(task_id, str) and task_id.startswith(_EVENT_CHANNEL_PREFIXES)


def get_tracker(task_id: str) -> StatusTracker:
    """Get or create a StatusTracker for a real task or TUI event channel.

    Callers with neither (intent clarification, chat, capability Q&A — or
    any code path that never received an id) get the shared
    :class:`NullTracker`, so ``tracker.start(...)`` and friends remain safe
    to call unconditionally without fabricating task state. See
    :func:`is_event_channel_id` for the two-concept split.
    """
    if not is_event_channel_id(task_id):
        return _NULL_TRACKER
    if task_id not in _trackers:
        _trackers[task_id] = StatusTracker(task_id)
    return _trackers[task_id]


def remove_tracker(task_id: str) -> None:
    """Remove a tracker when the task is done."""
    _trackers.pop(task_id, None)


def subscribe(task_id: str, maxsize: int = 100) -> asyncio.Queue[StatusEvent]:
    """Convenience: subscribe to a task's status events."""
    return get_tracker(task_id).subscribe(maxsize)


def unsubscribe(task_id: str, queue: asyncio.Queue[StatusEvent]) -> None:
    """Convenience: unsubscribe from a task's status events."""
    get_tracker(task_id).unsubscribe(queue)


@asynccontextmanager
async def track_status(task_id: str, source: str, message: str, category: str = StatusCategory.NODE):
    """Context manager to automatically emit start/complete/fail events.

    Also creates a tracer span for the node, so metric queries can see
    per-node timing and tool call counts.

    Usage:
        async with track_status(task_id, "agent_loop", "Planning fault injection...") as tracker:
            tracker.update("Activating skill pod-kill...")
            # do work
        # emits "completed" on normal exit, "failed" on exception
    """
    tracker = get_tracker(task_id)
    tracker.start(category, source, message)

    # Set the tracing callback's current task_id so LLM calls are attributed correctly
    if _tracing_callback is not None:
        _tracing_callback.set_task_id(task_id)
    if _otel_callback is not None:
        _otel_callback.set_task_id(task_id)

    # Create a tracer span for this node execution
    from chaos_agent.observability.tracer import get_trace
    trace = await get_trace(task_id)
    span = trace.start_span(source)

    try:
        yield tracker
        tracker.complete()
    except Exception as e:
        tracker.fail(str(e))
        await trace.end_span(span, error=str(e))
        raise
    else:
        # Collect tool call names from the tracker history for this span
        tool_names = []
        for ev in tracker._history:
            if ev.phase == StatusPhase.RUNNING and ev.detail.get("tool_calls"):
                tool_names.extend(ev.detail["tool_calls"])
        span.tool_calls = tool_names
        await trace.end_span(span)
