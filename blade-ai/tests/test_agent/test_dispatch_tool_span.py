"""W-55-A: tool execution time must land in ``task_spans``.

The wall-clock gap this pins closed: a loop node (``execute_loop`` /
``verifier_loop`` / ``agent_loop``) only *emits* tool calls — its span covers
the LLM reasoning. The tools RUN in the next graph node, a prebuilt
``ToolNode`` that ``with_phase_events`` never wrapped. So every tool execution
— including a 60s ``time_wait`` — fell outside all spans, and the summary's
``total_duration_ms`` (the sum of span durations) systematically undercounted
the real wall clock. ``with_tool_span`` closes that gap.

These tests drive the wrapper with a fake ToolNode (no LangGraph, no DB) and
assert the four load-bearing invariants from its docstring:

  1. the invocation's wall time is recorded as a span;
  2. ``tool_calls`` stays EMPTY on that span (the names live on the loop node's
     span; the trace preview sums ``len(span.tool_calls)`` across spans, so
     populating them here would double-count);
  3. ``total_tool_calls`` is NOT incremented (fed once, in
     ``with_phase_events._end_span``);
  4. only real task identities get a span, config propagates to the ToolNode,
     and a tool error is recorded on the span and re-raised.
"""

import asyncio
from types import SimpleNamespace

import pytest

import chaos_agent.observability.tracer as tracer_mod
from chaos_agent.agent.dispatch import with_tool_span
from chaos_agent.observability.tracer import clear_trace, get_trace


class _FakeToolNode:
    """Stand-in for a LangGraph ``ToolNode``: an async ``ainvoke``."""

    def __init__(self, delay: float = 0.0, names=("time_wait",)):
        self.delay = delay
        self.names = names
        self.received_config = "unset"

    async def ainvoke(self, state, config=None):
        self.received_config = config
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"messages": [SimpleNamespace(name=n) for n in self.names]}


@pytest.fixture
def no_persist(monkeypatch):
    """Keep spans in memory only — no TaskStore/DB dependency."""
    async def _noop(*a, **kw):
        pass
    monkeypatch.setattr(tracer_mod, "_persist_span", _noop)
    monkeypatch.setattr(tracer_mod, "_persist_summary", _noop)


@pytest.mark.asyncio
async def test_tool_span_records_wall_time(no_persist):
    task_id = "inject-toolspan-duration"
    clear_trace(task_id)
    try:
        node = _FakeToolNode(delay=0.05)
        wrapped = with_tool_span("phase2_tools", node)
        await wrapped({"task_id": task_id})

        trace = await get_trace(task_id)
        tool_spans = [s for s in trace.spans if s.node_name == "phase2_tools"]
        assert len(tool_spans) == 1
        # the 50ms sleep must be accounted — this is the whole point of the fix
        assert tool_spans[0].duration_ms >= 50
    finally:
        clear_trace(task_id)


@pytest.mark.asyncio
async def test_tool_span_leaves_tool_calls_empty(no_persist):
    """Invariant 2: names stay on the loop span; empty here avoids the
    preview's cross-span ``sum(len(tool_calls))`` double-count."""
    task_id = "inject-toolspan-nocalls"
    clear_trace(task_id)
    try:
        node = _FakeToolNode(names=("time_wait", "kubectl_readonly"))
        wrapped = with_tool_span("phase2_tools", node)
        await wrapped({"task_id": task_id})

        trace = await get_trace(task_id)
        tool_spans = [s for s in trace.spans if s.node_name == "phase2_tools"]
        assert tool_spans[0].tool_calls == []
    finally:
        clear_trace(task_id)


@pytest.mark.asyncio
async def test_tool_span_does_not_increment_total_tool_calls(no_persist):
    """Invariant 3: the counter is fed once, in ``with_phase_events``."""
    task_id = "inject-toolspan-counter"
    clear_trace(task_id)
    try:
        node = _FakeToolNode(names=("a", "b", "c"))
        wrapped = with_tool_span("phase2_tools", node)
        await wrapped({"task_id": task_id})

        trace = await get_trace(task_id)
        assert trace.total_tool_calls == 0
    finally:
        clear_trace(task_id)


@pytest.mark.asyncio
async def test_non_real_task_id_records_no_span(no_persist):
    """Invariant 4a: dialogue turns (no task identity) leak no trace/span."""
    node = _FakeToolNode()
    wrapped = with_tool_span("phase2_tools", node)
    await wrapped({"task_id": "chaos-session-xyz"})
    assert "chaos-session-xyz" not in tracer_mod._traces


@pytest.mark.asyncio
async def test_config_propagates_to_tool_node(no_persist):
    """Invariant 4b: when LangGraph hands us the config, the ToolNode gets it
    (so streaming/callback context survives the wrapper)."""
    node = _FakeToolNode()
    wrapped = with_tool_span("phase2_tools", node)
    cfg = {"configurable": {"thread_id": "t"}}
    await wrapped({"task_id": "chaos-session-xyz"}, cfg)
    assert node.received_config is cfg


@pytest.mark.asyncio
async def test_single_arg_call_still_works(no_persist):
    """LangGraph may invoke the node with state only — the wrapper must not
    require config, and must fall back to the ambient config (None here)."""
    node = _FakeToolNode()
    wrapped = with_tool_span("phase2_tools", node)
    result = await wrapped({"task_id": "chaos-session-xyz"})
    assert node.received_config is None
    assert "messages" in result


@pytest.mark.asyncio
async def test_tool_error_recorded_and_reraised(no_persist):
    """Invariant 4c: a tool failure lands on the span AND propagates — the
    wrapper must never swallow a ToolNode exception."""
    class _Boom:
        async def ainvoke(self, state, config=None):
            raise RuntimeError("tool exploded")

    task_id = "inject-toolspan-error"
    clear_trace(task_id)
    try:
        wrapped = with_tool_span("phase2_tools", _Boom())
        with pytest.raises(RuntimeError, match="tool exploded"):
            await wrapped({"task_id": task_id})

        trace = await get_trace(task_id)
        err_spans = [s for s in trace.spans if s.node_name == "phase2_tools"]
        assert len(err_spans) == 1
        assert "tool exploded" in (err_spans[0].error or "")
    finally:
        clear_trace(task_id)
