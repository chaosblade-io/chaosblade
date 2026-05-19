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
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from enum import Enum

logger = logging.getLogger(__name__)


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
        self._history: list[StatusEvent] = []
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


# ---- Global registry ----

_trackers: dict[str, StatusTracker] = {}

# Shared tracing callback reference (set by factory.py during init)
_tracing_callback = None


def get_tracker(task_id: str) -> StatusTracker:
    """Get or create a StatusTracker for a task."""
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
