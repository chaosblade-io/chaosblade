"""Observability: structured tracing for Graph node execution and LLM token consumption.

Data sources:
  1. StatusTracker history (per-node timing, tool calls, errors)
  2. TracingCallback (LLM token usage via LangChain callback)

Persistence:
  Trace data is persisted to the TaskStore database
  (``memory_dir/tasks.db``) so that ``blade-ai metric`` can query
  historical metrics across CLI invocations.  Each ``end_span()`` call
  writes a single span row to the ``task_spans`` table and updates the
  summary fields on the ``task_details`` row.

Usage:
  - await init_tracer() → initialize TaskStore persistence (called by factory.py)
  - await get_trace(task_id)  → get or create a TaskTrace
  - await get_all_trace_ids() → list all known task IDs
  - await get_all_metrics()   → metrics for ALL tasks
  - await get_trace_dict(task_id) → metrics for a single task
"""

import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Optional

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger(__name__)


@dataclass
class NodeSpan:
    """Execution trace for a single Graph node."""

    node_name: str
    start_time: float = 0.0
    end_time: float = 0.0
    duration_ms: float = 0.0
    token_input: int = 0
    token_output: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class TaskTrace:
    """Execution trace for an entire task."""

    task_id: str = ""
    spans: list[NodeSpan] = field(default_factory=list)
    total_token_input: int = 0
    total_token_output: int = 0
    total_llm_calls: int = 0
    total_tool_calls: int = 0

    def start_span(self, node_name: str) -> NodeSpan:
        """Start timing a new span."""
        span = NodeSpan(node_name=node_name, start_time=time.monotonic())
        # Record token baseline so end_span can compute the delta
        # consumed by LLM calls within this span
        span._token_input_start = self.total_token_input
        span._token_output_start = self.total_token_output
        self.spans.append(span)
        return span

    async def end_span(self, span: NodeSpan, error: Optional[str] = None) -> None:
        """End a span and record duration, then persist to TaskStore."""
        span.end_time = time.monotonic()
        span.duration_ms = (span.end_time - span.start_time) * 1000
        span.error = error
        # Compute token delta since span started (set by TracingCallback)
        span.token_input = self.total_token_input - getattr(span, "_token_input_start", 0)
        span.token_output = self.total_token_output - getattr(span, "_token_output_start", 0)
        await _persist_span(self.task_id, span)

    async def add_span(self, span: NodeSpan) -> None:
        """Add a completed span, then persist to TaskStore."""
        self.spans.append(span)
        self.total_token_input += span.token_input
        self.total_token_output += span.token_output
        await _persist_span(self.task_id, span)

    def to_dict(self) -> dict:
        """Output structured trace for the metric endpoint."""
        return {
            "task_id": self.task_id,
            "spans": [asdict(s) for s in self.spans],
            "summary": {
                "total_token_input": self.total_token_input,
                "total_token_output": self.total_token_output,
                "total_llm_calls": self.total_llm_calls,
                "total_tool_calls": self.total_tool_calls,
                "total_duration_ms": sum(s.duration_ms for s in self.spans),
            },
        }


def _extract_token_usage(response) -> tuple[int, int]:
    """Extract (prompt_tokens, completion_tokens) from LLM response.

    Tries multiple response structures for DashScope / OpenAI compatibility,
    plus direct AIMessage handling for ``on_chat_model_end`` astream_events
    where ``data.output`` is the raw AIMessage rather than an LLMResult.
    """
    prompt = completion = 0

    # Path 0: direct AIMessage / BaseMessage with usage_metadata. This is
    # the shape LangGraph's astream_events delivers under
    # ``on_chat_model_end`` — ``data.output`` is the AIMessage itself, not
    # an LLMResult, so the LLMResult-shaped paths below all miss. Done
    # before the legacy paths so chat-model events take the fast lane.
    try:
        um = getattr(response, "usage_metadata", None)
        if um and isinstance(um, dict):
            prompt = um.get("input_tokens", 0) or 0
            completion = um.get("output_tokens", 0) or 0
    except Exception:
        pass

    if prompt or completion:
        return prompt, completion

    # Path 1: llm_output.token_usage (original path)
    try:
        if hasattr(response, "llm_output") and response.llm_output:
            usage = response.llm_output.get("token_usage", {})
            if isinstance(usage, dict):
                prompt = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                completion = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
    except Exception:
        pass

    if prompt or completion:
        return prompt, completion

    # Path 2: response_metadata (DashScope OpenAI-compat / langchain_openai)
    try:
        if hasattr(response, "response_metadata"):
            meta = response.response_metadata or {}
            usage = meta.get("token_usage", {})
            if isinstance(usage, dict):
                prompt = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                completion = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
    except Exception:
        pass

    if prompt or completion:
        return prompt, completion

    # Path 3: generations[0][0].message.usage_metadata (LangChain standard)
    try:
        if hasattr(response, "generations") and response.generations:
            gen = response.generations[0][0]
            msg = gen.message
            um = getattr(msg, "usage_metadata", None)
            if um and isinstance(um, dict):
                prompt = um.get("input_tokens", 0)
                completion = um.get("output_tokens", 0)
    except (IndexError, AttributeError):
        pass

    return prompt, completion


class TracingCallback(BaseCallbackHandler):
    """LangChain callback that automatically tracks LLM token consumption."""

    def __init__(self, trace: TaskTrace):
        self.trace = trace

    def on_llm_end(self, response, **kwargs) -> None:
        """Record token usage from LLM response."""
        self.trace.total_llm_calls += 1
        prompt, completion = _extract_token_usage(response)
        self.trace.total_token_input += prompt
        self.trace.total_token_output += completion


# ---------------------------------------------------------------------------
# In-memory trace store (keyed by task_id) + TaskStore persistence
# ---------------------------------------------------------------------------

_traces: dict[str, TaskTrace] = {}


async def init_tracer() -> None:
    """Initialize the tracer with TaskStore-backed persistence."""
    # Ensure TaskStore is initialized (creates DB if needed)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        await get_task_store()
    except Exception as e:
        logger.warning(f"TaskStore init via init_tracer failed: {e}")
    logger.info("Trace persistence initialized (TaskStore)")


# -- persistence helpers --------------------------------------------------------

async def _persist_span(task_id: str, span: NodeSpan) -> None:
    """Write a single span row to the TaskStore task_spans table.

    Also ensures the task row exists in the DB so that
    ``update_task_summary`` can find a row to update.
    """
    if not task_id:
        return
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        # Ensure task row exists before inserting span + updating summary
        await store.upsert(task_id)
        await store.append_span(
            task_id=task_id,
            node_name=span.node_name,
            start_time=span.start_time,
            end_time=span.end_time,
            duration_ms=span.duration_ms,
            token_input=span.token_input,
            token_output=span.token_output,
            tool_calls=span.tool_calls,
            error=span.error,
        )
    except Exception as e:
        logger.warning(f"Failed to persist span for task {task_id}: {e}")


async def _persist_summary(task_id: str, trace: TaskTrace) -> None:
    """Update the summary fields on the task_details row in TaskStore."""
    if not task_id:
        return
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        await store.upsert(
            task_id,
            total_token_input=trace.total_token_input,
            total_token_output=trace.total_token_output,
            total_llm_calls=trace.total_llm_calls,
            total_tool_calls=trace.total_tool_calls,
            total_duration_ms=int(sum(s.duration_ms for s in trace.spans)),
        )
    except Exception as e:
        logger.warning(f"Failed to persist summary for task {task_id}: {e}")


async def _load_trace_from_store(task_id: str) -> Optional[TaskTrace]:
    """Load a TaskTrace from the TaskStore database."""
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        task = await store.get(task_id)
        if task is None:
            return None
        trace = TaskTrace(task_id=task_id)
        summary = await store.get_summary(task_id) or {}
        trace.total_token_input = summary.get("total_token_input", 0)
        trace.total_token_output = summary.get("total_token_output", 0)
        trace.total_llm_calls = summary.get("total_llm_calls", 0)
        trace.total_tool_calls = summary.get("total_tool_calls", 0)
        for span_dict in await store.get_spans(task_id):
            span = NodeSpan(node_name=span_dict.get("node_name", ""))
            span.start_time = span_dict.get("start_time", 0.0)
            span.end_time = span_dict.get("end_time", 0.0)
            span.duration_ms = span_dict.get("duration_ms", 0.0)
            span.token_input = span_dict.get("token_input", 0)
            span.token_output = span_dict.get("token_output", 0)
            span.tool_calls = span_dict.get("tool_calls", [])
            span.error = span_dict.get("error")
            trace.spans.append(span)
        return trace
    except Exception as e:
        logger.warning(f"Failed to load trace from TaskStore for task {task_id}: {e}")
        return None


async def _list_store_trace_ids() -> list[str]:
    """List all task IDs that have trace data in TaskStore."""
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        return [row["task_id"] for row in await store.list_tasks(limit=10000)]
    except Exception:
        return []


# -- public API ----------------------------------------------------------------

async def get_trace(task_id: str) -> TaskTrace:
    """Get or create a TaskTrace for a task.

    If the trace is not in memory, attempts to load from TaskStore.
    """
    if task_id not in _traces:
        db_trace = await _load_trace_from_store(task_id)
        if db_trace:
            _traces[task_id] = db_trace
        else:
            _traces[task_id] = TaskTrace(task_id=task_id)
    return _traces[task_id]


async def get_trace_dict(task_id: str) -> Optional[dict]:
    """Get the trace dict for a single task, or None if not found."""
    if task_id in _traces:
        return _traces[task_id].to_dict()
    # Try loading from TaskStore
    db_trace = await _load_trace_from_store(task_id)
    if db_trace:
        _traces[task_id] = db_trace
        return db_trace.to_dict()
    return None


async def get_all_trace_ids() -> list[str]:
    """Get all known task IDs that have trace data (memory + store)."""
    all_ids = set(_traces.keys()) | set(await _list_store_trace_ids())
    return sorted(all_ids)


async def get_all_metrics() -> dict:
    """Get metrics for ALL tasks.

    Merges in-memory traces with TaskStore-persisted traces.
    In-memory traces take priority (they are more recent).

    Returns a dict with:
      - total: number of tasks
      - tasks: list of per-task metric dicts
    """
    all_task_ids = set(_traces.keys()) | set(await _list_store_trace_ids())

    tasks = []
    for task_id in sorted(all_task_ids):
        if task_id in _traces:
            tasks.append(_traces[task_id].to_dict())
        else:
            db_trace = await _load_trace_from_store(task_id)
            if db_trace:
                _traces[task_id] = db_trace
                tasks.append(db_trace.to_dict())
    return {
        "total": len(tasks),
        "tasks": tasks,
    }


async def flush_trace(task_id: str) -> None:
    """Explicitly persist the current in-memory trace to TaskStore.

    Useful at task completion to ensure the final trace snapshot
    (including any straggler TracingCallback updates) is saved.
    """
    trace = _traces.get(task_id)
    if trace:
        await _persist_summary(task_id, trace)


def clear_trace(task_id: str) -> None:
    """Remove a trace from memory after it's no longer needed.

    Note: TaskStore-persisted trace data is NOT deleted so that ``metric``
    can still query historical data across process restarts.
    """
    _traces.pop(task_id, None)
