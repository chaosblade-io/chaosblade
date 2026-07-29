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
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.STARTED,
            category=category,
            source=source,
            message=message,
            detail=detail or {},
        ))

    def update(self, message: str, detail: dict = None) -> None:
        """Emit a RUNNING update event."""
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.RUNNING,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=message,
            duration_ms=(time.monotonic() - self._start_time) * 1000 if self._start_time else 0,
            detail=detail or {},
        ))

    def complete(self, message: str = "", detail: dict = None) -> None:
        """Emit a COMPLETED event."""
        duration = (time.monotonic() - self._start_time) * 1000 if self._start_time else 0
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.COMPLETED,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=message or f"{self._current_source} completed",
            duration_ms=duration,
            detail=detail or {},
        ))

    def fail(self, error: str, detail: dict = None) -> None:
        """Emit a FAILED event."""
        duration = (time.monotonic() - self._start_time) * 1000 if self._start_time else 0
        self.emit(StatusEvent(
            task_id=self.task_id,
            phase=StatusPhase.FAILED,
            category=StatusCategory.NODE,
            source=self._current_source,
            message=error,
            duration_ms=duration,
            detail=detail or {},
        ))

    def get_history(self) -> list[dict]:
        """Return all recorded events as dicts."""
        return [e.to_dict() for e in self._history]

    @property
    def current_source(self) -> str:
        return self._current_source

    def save_state(self) -> tuple[str, float]:
        """Save current source and start_time for later restoration.

        Used by sub-operations (e.g. conflict check) that need their own
        tracker lifecycle without corrupting the parent operation's state.

        Returns:
            Tuple of (current_source, start_time) to pass to restore_state().
        """
        return self._current_source, self._start_time

    def restore_state(self, saved: tuple[str, float]) -> None:
        """Restore previously saved source and start_time.

        Args:
            saved: Tuple from save_state() to restore.
        """
        self._current_source, self._start_time = saved


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
