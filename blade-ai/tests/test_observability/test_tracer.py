"""Tests for observability tracer."""

import time
from unittest.mock import MagicMock

import pytest

from chaos_agent.observability.tracer import (
    NodeSpan,
    TaskTrace,
    TracingCallback,
    clear_trace,
    flush_trace,
    get_all_metrics,
    get_all_trace_ids,
    get_trace,
    get_trace_dict,
    init_tracer,
)


class TestNodeSpan:
    """Test NodeSpan dataclass."""

    def test_defaults(self):
        span = NodeSpan(node_name="test")
        assert span.start_time == 0.0
        assert span.end_time == 0.0
        assert span.duration_ms == 0.0
        assert span.token_input == 0
        assert span.token_output == 0
        assert span.tool_calls == []
        assert span.error is None


class TestTaskTrace:
    """Test TaskTrace dataclass."""

    def test_start_span(self):
        trace = TaskTrace(task_id="t1")
        span = trace.start_span("agent_loop")
        assert span.node_name == "agent_loop"
        assert span.start_time > 0

    @pytest.mark.asyncio
    async def test_end_span(self):
        trace = TaskTrace(task_id="t1")
        span = trace.start_span("agent_loop")
        time.sleep(0.01)
        # Patch _persist_span and _persist_summary to avoid TaskStore dependency
        import chaos_agent.observability.tracer as tracer_mod
        orig_persist_span = tracer_mod._persist_span
        orig_persist_summary = tracer_mod._persist_summary
        async def _noop_span(*a, **kw): pass
        async def _noop_summary(*a, **kw): pass
        tracer_mod._persist_span = _noop_span
        tracer_mod._persist_summary = _noop_summary
        try:
            await trace.end_span(span)
        finally:
            tracer_mod._persist_span = orig_persist_span
            tracer_mod._persist_summary = orig_persist_summary
        assert span.end_time >= span.start_time
        assert span.duration_ms > 0
        assert span in trace.spans

    @pytest.mark.asyncio
    async def test_end_span_with_error(self):
        trace = TaskTrace(task_id="t1")
        span = trace.start_span("test")
        import chaos_agent.observability.tracer as tracer_mod
        orig_persist_span = tracer_mod._persist_span
        orig_persist_summary = tracer_mod._persist_summary
        async def _noop_span(*a, **kw): pass
        async def _noop_summary(*a, **kw): pass
        tracer_mod._persist_span = _noop_span
        tracer_mod._persist_summary = _noop_summary
        try:
            await trace.end_span(span, error="something failed")
        finally:
            tracer_mod._persist_span = orig_persist_span
            tracer_mod._persist_summary = orig_persist_summary
        assert span.error == "something failed"

    @pytest.mark.asyncio
    async def test_add_span(self):
        trace = TaskTrace(task_id="t1")
        span = NodeSpan(node_name="test", token_input=100, token_output=50)
        import chaos_agent.observability.tracer as tracer_mod
        orig_persist_span = tracer_mod._persist_span
        orig_persist_summary = tracer_mod._persist_summary
        async def _noop_span(*a, **kw): pass
        async def _noop_summary(*a, **kw): pass
        tracer_mod._persist_span = _noop_span
        tracer_mod._persist_summary = _noop_summary
        try:
            await trace.add_span(span)
        finally:
            tracer_mod._persist_span = orig_persist_span
            tracer_mod._persist_summary = orig_persist_summary
        assert trace.total_token_input == 100
        assert trace.total_token_output == 50

    def test_to_dict(self):
        trace = TaskTrace(task_id="t1")
        span = NodeSpan(node_name="test", duration_ms=100.0, token_input=50, token_output=25)
        trace.spans.append(span)

        result = trace.to_dict()
        assert result["task_id"] == "t1"
        assert len(result["spans"]) == 1
        assert result["spans"][0]["node_name"] == "test"
        assert result["summary"]["total_duration_ms"] == 100.0

    def test_to_dict_summary_totals(self):
        trace = TaskTrace(task_id="t1")
        trace.total_llm_calls = 3
        trace.total_tool_calls = 5

        result = trace.to_dict()
        assert result["summary"]["total_llm_calls"] == 3
        assert result["summary"]["total_tool_calls"] == 5

    @pytest.mark.asyncio
    async def test_end_span_computes_token_delta(self):
        """Token delta: end_span should compute per-span token consumption
        from the difference between current trace totals and the baseline
        recorded at start_span time.
        """
        trace = TaskTrace(task_id="t1")
        # Patch persistence to avoid TaskStore dependency
        import chaos_agent.observability.tracer as tracer_mod
        orig = tracer_mod._persist_span
        async def _noop(*a, **kw): pass
        tracer_mod._persist_span = _noop
        try:
            # Span 1: simulate 2 LLM calls within this span
            span1 = trace.start_span("plan")
            assert span1._token_input_start == 0
            assert span1._token_output_start == 0
            # Simulate TracingCallback.on_llm_end() updates
            trace.total_token_input += 100
            trace.total_token_output += 50
            trace.total_llm_calls += 1
            trace.total_token_input += 80
            trace.total_token_output += 40
            trace.total_llm_calls += 1
            await trace.end_span(span1)
            assert span1.token_input == 180
            assert span1.token_output == 90

            # Span 2: another span with more LLM calls
            span2 = trace.start_span("execute")
            # Baseline should be the current totals after span 1
            assert span2._token_input_start == 180
            assert span2._token_output_start == 90
            trace.total_token_input += 200
            trace.total_token_output += 100
            await trace.end_span(span2)
            assert span2.token_input == 200
            assert span2.token_output == 100

            # Trace totals should reflect all LLM calls
            assert trace.total_token_input == 380
            assert trace.total_token_output == 190
        finally:
            tracer_mod._persist_span = orig


class TestTracingCallback:
    """Test TracingCallback for LLM token tracking."""

    def test_on_llm_end_records_tokens(self):
        trace = TaskTrace(task_id="t1")
        callback = TracingCallback(trace)

        response = MagicMock()
        response.llm_output = {
            "token_usage": {"prompt_tokens": 100, "completion_tokens": 50}
        }
        callback.on_llm_end(response)
        assert trace.total_llm_calls == 1
        assert trace.total_token_input == 100
        assert trace.total_token_output == 50

    def test_on_llm_end_no_llm_output(self):
        trace = TaskTrace(task_id="t1")
        callback = TracingCallback(trace)

        response = MagicMock()
        response.llm_output = None
        callback.on_llm_end(response)
        assert trace.total_llm_calls == 1
        assert trace.total_token_input == 0

    def test_on_llm_end_exception_handled(self):
        trace = TaskTrace(task_id="t1")
        callback = TracingCallback(trace)

        response = MagicMock()
        response.llm_output = "not a dict"
        callback.on_llm_end(response)
        assert trace.total_llm_calls == 1


class TestGlobalTraceManagement:
    """Test global trace store functions."""

    def setup_method(self):
        clear_trace("test-trace")

    @pytest.mark.asyncio
    async def test_get_trace_creates_new(self):
        trace = await get_trace("test-trace")
        assert trace.task_id == "test-trace"

    @pytest.mark.asyncio
    async def test_get_trace_returns_existing(self):
        trace1 = await get_trace("test-trace")
        trace1.total_llm_calls = 5
        trace2 = await get_trace("test-trace")
        assert trace2.total_llm_calls == 5

    @pytest.mark.asyncio
    async def test_get_trace_dict_returns_dict(self):
        await get_trace("test-trace")
        result = await get_trace_dict("test-trace")
        assert result is not None
        assert result["task_id"] == "test-trace"

    @pytest.mark.asyncio
    async def test_get_trace_dict_nonexistent(self):
        result = await get_trace_dict("nonexistent-trace-xyz")
        assert result is None

    @pytest.mark.asyncio
    async def test_clear_trace(self):
        await get_trace("test-trace")
        clear_trace("test-trace")
        result = await get_trace_dict("test-trace")
        assert result is None


class TestTracePersistence:
    """Test TaskStore-based trace persistence (async).

    Uses a fresh DB for each test by resetting the TaskStore singleton
    and pointing settings to a tmp_path.
    """

    def setup_method(self):
        clear_trace("persist-test")
        clear_trace("persist-test-2")

    async def _setup_store(self, tmp_path, monkeypatch):
        """Helper: reset singleton + point settings to a temp DB."""
        import chaos_agent.persistence.task_store as store_mod
        await store_mod.reset_task_store()
        monkeypatch.setattr(store_mod.settings, "tasks_db_path", tmp_path / "tasks.db")
        # Ensure resolved_tasks_db_path returns the temp path
        monkeypatch.setattr(store_mod.settings, "memory_dir", tmp_path)

    async def _teardown_store(self):
        import chaos_agent.persistence.task_store as store_mod
        await store_mod.reset_task_store()

    @pytest.mark.asyncio
    async def test_init_tracer_initializes_taskstore(self, tmp_path, monkeypatch):
        """init_tracer should initialize TaskStore without error."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()
        finally:
            await self._teardown_store()

    @pytest.mark.asyncio
    async def test_end_span_persists_to_taskstore(self, tmp_path, monkeypatch):
        """end_span should persist span data to TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()
            trace = await get_trace("persist-test")
            span = trace.start_span("agent_loop")
            await trace.end_span(span)

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            spans = await store.get_spans("persist-test")
            assert len(spans) == 1
            assert spans[0]["node_name"] == "agent_loop"
        finally:
            await self._teardown_store()
            clear_trace("persist-test")

    @pytest.mark.asyncio
    async def test_load_trace_from_taskstore(self, tmp_path, monkeypatch):
        """After clearing memory, get_trace should load from TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("persist-test")
            trace.total_llm_calls = 3
            trace.total_token_input = 100
            span = trace.start_span("verifier")
            await trace.end_span(span)

            clear_trace("persist-test")

            loaded = await get_trace("persist-test")
            assert loaded.task_id == "persist-test"
            assert len(loaded.spans) == 1
            assert loaded.spans[0].node_name == "verifier"
        finally:
            await self._teardown_store()
            clear_trace("persist-test")

    @pytest.mark.asyncio
    async def test_get_all_metrics_merges_store_and_memory(self, tmp_path, monkeypatch):
        """get_all_metrics should include both in-memory and TaskStore-persisted traces."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace1 = await get_trace("persist-test")
            trace1.total_llm_calls = 2
            span = trace1.start_span("node1")
            await trace1.end_span(span)
            clear_trace("persist-test")

            trace2 = await get_trace("persist-test-2")
            trace2.total_llm_calls = 1

            metrics = await get_all_metrics()
            assert metrics["total"] >= 2
            task_ids = [t["task_id"] for t in metrics["tasks"]]
            assert "persist-test" in task_ids
            assert "persist-test-2" in task_ids
        finally:
            await self._teardown_store()
            clear_trace("persist-test")
            clear_trace("persist-test-2")

    @pytest.mark.asyncio
    async def test_get_all_trace_ids_includes_store(self, tmp_path, monkeypatch):
        """get_all_trace_ids should include TaskStore-persisted task IDs."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("persist-test")
            span = trace.start_span("node1")
            await trace.end_span(span)
            clear_trace("persist-test")

            ids = await get_all_trace_ids()
            assert "persist-test" in ids
        finally:
            await self._teardown_store()
            clear_trace("persist-test")

    @pytest.mark.asyncio
    async def test_flush_trace(self, tmp_path, monkeypatch):
        """flush_trace should persist summary to TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("persist-test")
            trace.total_llm_calls = 5
            await flush_trace("persist-test")

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            summary = await store.get_summary("persist-test")
            assert summary is not None
            assert summary["total_llm_calls"] == 5
        finally:
            await self._teardown_store()
            clear_trace("persist-test")

    @pytest.mark.asyncio
    async def test_no_init_tracer_graceful_degradation(self):
        """Without calling init_tracer, end_span should not crash (pure in-memory mode)."""
        trace = TaskTrace(task_id="no-disk-test")
        span = trace.start_span("test")
        import chaos_agent.observability.tracer as tracer_mod
        orig_persist_span = tracer_mod._persist_span
        orig_persist_summary = tracer_mod._persist_summary
        async def _noop_span(*a, **kw): pass
        async def _noop_summary(*a, **kw): pass
        tracer_mod._persist_span = _noop_span
        tracer_mod._persist_summary = _noop_summary
        try:
            await trace.end_span(span)
        finally:
            tracer_mod._persist_span = orig_persist_span
            tracer_mod._persist_summary = orig_persist_summary

    @pytest.mark.asyncio
    async def test_clear_trace_does_not_delete_store(self, tmp_path, monkeypatch):
        """clear_trace should only remove from memory, not delete from TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("persist-test")
            span = trace.start_span("node1")
            await trace.end_span(span)

            clear_trace("persist-test")

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            spans = await store.get_spans("persist-test")
            assert len(spans) == 1

            loaded = await get_trace("persist-test")
            assert loaded.task_id == "persist-test"
        finally:
            await self._teardown_store()
            clear_trace("persist-test")
