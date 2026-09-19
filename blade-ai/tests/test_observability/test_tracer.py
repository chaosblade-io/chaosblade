"""Tests for observability tracer."""

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from chaos_agent.observability.tracer import (
    NodeSpan,
    TaskTrace,
    TracingCallback,
    _cache_read_from_token_usage,
    _cache_read_from_usage_metadata,
    _extract_token_usage,
    _log_cache_read_source,
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


class TestCacheReadExtraction:
    """Prompt-cache-hit token extraction — vendor-agnostic union.

    ``cache_read`` is a SUBSET of ``input_tokens`` (not additive); the
    authoritative source is ``usage_metadata.input_token_details.cache_read``
    (real-run verified for DashScope), with service-tier prefix variants and
    raw ``token_usage`` shapes kept as zero-risk defensive fallbacks.
    """

    def test_usage_metadata_plain_cache_read(self):
        resp = SimpleNamespace(usage_metadata={
            "input_tokens": 2990,
            "output_tokens": 120,
            "input_token_details": {"cache_read": 2176},
        })
        assert _extract_token_usage(resp) == (2990, 120, 2176)

    def test_usage_metadata_service_tier_prefix(self):
        # LangChain prefixes the key when service_tier ∈ {priority, flex}.
        prio = SimpleNamespace(usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 10,
            "input_token_details": {"priority_cache_read": 800},
        })
        assert _extract_token_usage(prio)[2] == 800
        flex = SimpleNamespace(usage_metadata={
            "input_tokens": 1000,
            "output_tokens": 10,
            "input_token_details": {"flex_cache_read": 640},
        })
        assert _extract_token_usage(flex)[2] == 640

    def test_response_metadata_openai_shape_fallback(self):
        # Defensive fallback (empty on the production streaming path).
        resp = SimpleNamespace(
            usage_metadata=None,
            llm_output=None,
            response_metadata={"token_usage": {
                "prompt_tokens": 500,
                "completion_tokens": 20,
                "prompt_tokens_details": {"cached_tokens": 300},
            }},
        )
        assert _extract_token_usage(resp) == (500, 20, 300)

    def test_deepseek_native_field_fallback(self):
        # Unverified against a live key, but read-side probing is zero-risk.
        resp = SimpleNamespace(
            usage_metadata=None,
            llm_output=None,
            response_metadata={"token_usage": {
                "prompt_tokens": 500,
                "completion_tokens": 20,
                "prompt_cache_hit_tokens": 450,
            }},
        )
        assert _extract_token_usage(resp)[2] == 450

    def test_no_cache_field_degrades_to_zero(self):
        resp = SimpleNamespace(usage_metadata={
            "input_tokens": 100, "output_tokens": 5,
        })
        assert _extract_token_usage(resp) == (100, 5, 0)

    def test_callback_accumulates_total_token_cached(self):
        trace = TaskTrace(task_id="t1")
        cb = TracingCallback(trace)
        cb.on_llm_end(SimpleNamespace(usage_metadata={
            "input_tokens": 2990, "output_tokens": 120,
            "input_token_details": {"cache_read": 2176},
        }))
        cb.on_llm_end(SimpleNamespace(usage_metadata={
            "input_tokens": 3000, "output_tokens": 100,
            "input_token_details": {"cache_read": 824},
        }))
        assert trace.total_token_input == 5990
        assert trace.total_token_cached == 3000

    def test_to_dict_summary_exposes_total_token_cached(self):
        trace = TaskTrace(task_id="t1")
        trace.total_token_input = 2990
        trace.total_token_cached = 2176
        summary = trace.to_dict()["summary"]
        assert summary["total_token_cached"] == 2176


class TestCacheReadSourceDiagnostics:
    """The cache-read helpers report WHICH vendor field matched, so an
    unmapped provider is diagnosable from logs instead of silently reporting
    a 0 hit rate (tasks.md 1.4 source-diagnostic requirement).
    """

    def test_usage_metadata_helper_returns_source(self):
        assert _cache_read_from_usage_metadata(
            {"input_token_details": {"cache_read": 2176}}
        ) == (2176, "usage_metadata.input_token_details.cache_read")

    def test_usage_metadata_helper_service_tier_source(self):
        assert _cache_read_from_usage_metadata(
            {"input_token_details": {"priority_cache_read": 800}}
        ) == (800, "usage_metadata.input_token_details.priority_cache_read")

    def test_usage_metadata_helper_flat_source(self):
        assert _cache_read_from_usage_metadata({"cache_read": 50}) == (
            50,
            "usage_metadata.cache_read",
        )

    def test_usage_metadata_helper_miss_returns_none_source(self):
        assert _cache_read_from_usage_metadata({"input_tokens": 100}) == (0, None)

    def test_token_usage_helper_returns_source(self):
        assert _cache_read_from_token_usage(
            {"prompt_tokens_details": {"cached_tokens": 300}}
        ) == (300, "token_usage.prompt_tokens_details.cached_tokens")

    def test_token_usage_helper_deepseek_source(self):
        assert _cache_read_from_token_usage({"prompt_cache_hit_tokens": 450}) == (
            450,
            "token_usage.prompt_cache_hit_tokens",
        )

    def test_token_usage_helper_miss_returns_none_source(self):
        assert _cache_read_from_token_usage({"prompt_tokens": 100}) == (0, None)

    def test_log_names_source_on_hit(self, caplog):
        with caplog.at_level(logging.DEBUG, logger="chaos_agent.observability.tracer"):
            _log_cache_read_source(
                2176, "usage_metadata.input_token_details.cache_read", 2990
            )
        assert "usage_metadata.input_token_details.cache_read" in caplog.text
        assert "cache_read=2176" in caplog.text

    def test_log_neutral_on_zero_with_prompt(self, caplog):
        # The diagnostic case: a non-zero prompt but no cache field mapped —
        # logged with NEUTRAL wording (not "missed/failed") so a healthy cold
        # start doesn't read as an error, while still naming both benign
        # explanations (cold turn vs unmapped vendor field).
        with caplog.at_level(logging.DEBUG, logger="chaos_agent.observability.tracer"):
            _log_cache_read_source(0, None, 2990)
        assert "no cache-hit field mapped" in caplog.text
        assert "cold/no-cache turn" in caplog.text
        # the old failure-framed wording must be gone
        assert "all mapped sources missed" not in caplog.text

    def test_no_log_when_zero_prompt(self, caplog):
        # Nothing to diagnose when there was no prompt at all.
        with caplog.at_level(logging.DEBUG, logger="chaos_agent.observability.tracer"):
            _log_cache_read_source(0, None, 0)
        assert "cache_read" not in caplog.text

    def test_extract_logs_source_end_to_end(self, caplog):
        # The choke point wires the source through to the diagnostic log.
        resp = SimpleNamespace(usage_metadata={
            "input_tokens": 2990,
            "output_tokens": 120,
            "input_token_details": {"cache_read": 2176},
        })
        with caplog.at_level(logging.DEBUG, logger="chaos_agent.observability.tracer"):
            assert _extract_token_usage(resp) == (2990, 120, 2176)
        assert "usage_metadata.input_token_details.cache_read" in caplog.text


class TestCacheSummaryPersistence:
    """tracer ↔ DB wiring for the task-level cache aggregate (design D4).

    ``_persist_summary`` MUST write ``trace.total_token_cached`` absolutely at
    finalize; ``_load_trace_from_store`` MUST restore it — so the per-task hit
    rate survives a restart. Stubbed store (no real DB) to isolate the wiring.
    """

    @pytest.mark.asyncio
    async def test_persist_summary_writes_total_token_cached(self, monkeypatch):
        import chaos_agent.observability.tracer as tracer_mod
        import chaos_agent.persistence.task_store as ts_mod

        captured: dict = {}

        class _FakeStore:
            async def upsert(self, task_id, **fields):
                captured.update(fields)

        async def _fake_get_store():
            return _FakeStore()

        monkeypatch.setattr(ts_mod, "get_task_store", _fake_get_store)

        trace = tracer_mod.TaskTrace(task_id="task-cache1")
        trace.total_token_input = 2990
        trace.total_token_cached = 2176
        await tracer_mod._persist_summary("task-cache1", trace)

        assert captured["total_token_cached"] == 2176
        assert captured["total_token_input"] == 2990

    @pytest.mark.asyncio
    async def test_load_trace_restores_total_token_cached(self, monkeypatch):
        import chaos_agent.observability.tracer as tracer_mod
        import chaos_agent.persistence.task_store as ts_mod

        class _FakeStore:
            async def get(self, task_id):
                return {"task_id": task_id, "task_state": "injected"}

            async def get_summary(self, task_id):
                return {
                    "total_token_input": 2990,
                    "total_token_output": 500,
                    "total_token_cached": 2176,
                    "total_llm_calls": 3,
                    "total_tool_calls": 2,
                }

            async def get_spans(self, task_id):
                return []

        async def _fake_get_store():
            return _FakeStore()

        monkeypatch.setattr(ts_mod, "get_task_store", _fake_get_store)

        trace = await tracer_mod._load_trace_from_store("task-cache1")
        assert trace is not None
        assert trace.total_token_cached == 2176
        assert trace.total_token_input == 2990

    @pytest.mark.asyncio
    async def test_load_trace_defaults_cached_to_zero_when_absent(self, monkeypatch):
        # A legacy row whose summary predates the column reads 0, never KeyError.
        import chaos_agent.observability.tracer as tracer_mod
        import chaos_agent.persistence.task_store as ts_mod

        class _FakeStore:
            async def get(self, task_id):
                return {"task_id": task_id, "task_state": "injected"}

            async def get_summary(self, task_id):
                return {"total_token_input": 100, "total_token_output": 20}

            async def get_spans(self, task_id):
                return []

        async def _fake_get_store():
            return _FakeStore()

        monkeypatch.setattr(ts_mod, "get_task_store", _fake_get_store)

        trace = await tracer_mod._load_trace_from_store("task-cache1")
        assert trace is not None
        assert trace.total_token_cached == 0


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
        clear_trace("task-persist-test")
        clear_trace("task-persist-test-2")

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
            trace = await get_trace("task-persist-test")
            span = trace.start_span("agent_loop")
            await trace.end_span(span)

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            spans = await store.get_spans("task-persist-test")
            assert len(spans) == 1
            assert spans[0]["node_name"] == "agent_loop"
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")

    @pytest.mark.asyncio
    async def test_load_trace_from_taskstore(self, tmp_path, monkeypatch):
        """After clearing memory, get_trace should load from TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("task-persist-test")
            trace.total_llm_calls = 3
            trace.total_token_input = 100
            span = trace.start_span("verifier")
            await trace.end_span(span)

            clear_trace("task-persist-test")

            loaded = await get_trace("task-persist-test")
            assert loaded.task_id == "task-persist-test"
            assert len(loaded.spans) == 1
            assert loaded.spans[0].node_name == "verifier"
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")

    @pytest.mark.asyncio
    async def test_get_all_metrics_merges_store_and_memory(self, tmp_path, monkeypatch):
        """get_all_metrics should include both in-memory and TaskStore-persisted traces."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace1 = await get_trace("task-persist-test")
            trace1.total_llm_calls = 2
            span = trace1.start_span("node1")
            await trace1.end_span(span)
            clear_trace("task-persist-test")

            trace2 = await get_trace("task-persist-test-2")
            trace2.total_llm_calls = 1

            metrics = await get_all_metrics()
            assert metrics["total"] >= 2
            task_ids = [t["task_id"] for t in metrics["tasks"]]
            assert "task-persist-test" in task_ids
            assert "task-persist-test-2" in task_ids
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")
            clear_trace("task-persist-test-2")

    @pytest.mark.asyncio
    async def test_get_all_trace_ids_includes_store(self, tmp_path, monkeypatch):
        """get_all_trace_ids should include TaskStore-persisted task IDs."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("task-persist-test")
            span = trace.start_span("node1")
            await trace.end_span(span)
            clear_trace("task-persist-test")

            ids = await get_all_trace_ids()
            assert "task-persist-test" in ids
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")

    @pytest.mark.asyncio
    async def test_flush_trace(self, tmp_path, monkeypatch):
        """flush_trace should persist summary to TaskStore."""
        await self._setup_store(tmp_path, monkeypatch)
        try:
            await init_tracer()

            trace = await get_trace("task-persist-test")
            trace.total_llm_calls = 5
            await flush_trace("task-persist-test")

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            summary = await store.get_summary("task-persist-test")
            assert summary is not None
            assert summary["total_llm_calls"] == 5
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")

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

            trace = await get_trace("task-persist-test")
            span = trace.start_span("node1")
            await trace.end_span(span)

            clear_trace("task-persist-test")

            import chaos_agent.persistence.task_store as store_mod
            store = await store_mod.get_task_store()
            spans = await store.get_spans("task-persist-test")
            assert len(spans) == 1

            loaded = await get_trace("task-persist-test")
            assert loaded.task_id == "task-persist-test"
        finally:
            await self._teardown_store()
            clear_trace("task-persist-test")
