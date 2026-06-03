"""Tests for OTel GenAI semantic conventions parallel export layer."""

import uuid
from unittest.mock import MagicMock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode


class TestInferProvider:
    """Test _infer_provider URL → provider name mapping."""

    def test_dashscope(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://dashscope.aliyuncs.com/compatible-mode/v1") == "alibaba_cloud"

    def test_openai(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://api.openai.com/v1") == "openai"

    def test_anthropic(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://api.anthropic.com/v1") == "anthropic"

    def test_deepseek(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://api.deepseek.com/v1") == "deepseek"

    def test_azure(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://my-deployment.azure.openai.com/v1") == "azure.ai.openai"

    def test_unknown_defaults_to_openai(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("https://my-local-proxy.internal/v1") == "openai"

    def test_empty_url(self):
        from chaos_agent.observability.otel_genai import _infer_provider
        assert _infer_provider("") == "openai"


class TestNoOpWhenDisabled:
    """Test that init/shutdown do nothing when both flags are off."""

    def test_is_otel_available_false_when_both_disabled(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "otel_enabled", False)
        monkeypatch.setattr(settings, "prometheus_enabled", False)
        from chaos_agent.observability.otel_genai import is_otel_available
        assert is_otel_available() is False

    def test_is_otel_available_true_when_prometheus_only(self, monkeypatch):
        """E7 — Prometheus-only should still install the callback."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "otel_enabled", False)
        monkeypatch.setattr(settings, "prometheus_enabled", True)
        from chaos_agent.observability.otel_genai import is_otel_available
        assert is_otel_available() is True

    def test_init_noop_when_both_disabled(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "otel_enabled", False)
        monkeypatch.setattr(settings, "prometheus_enabled", False)
        mod.init_otel_genai()
        assert mod._initialized is False

    def test_shutdown_noop_without_init(self):
        import chaos_agent.observability.otel_genai as mod
        mod.shutdown_otel_genai()  # should not raise

    def test_init_prometheus_only_skips_tracer(self, monkeypatch):
        """E7 — prometheus_only must build MeterProvider but NOT TracerProvider.

        OTLPSpanExporter would try to dial localhost:4317; building a
        TracerProvider in metrics-only mode is wasteful + misleading.
        """
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        monkeypatch.setattr(mod, "_tracer_provider", None)
        monkeypatch.setattr(mod, "_meter_provider", None)
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "otel_enabled", False)
        monkeypatch.setattr(settings, "prometheus_enabled", True)
        try:
            mod.init_otel_genai()
            assert mod._initialized is True
            assert mod._tracer_provider is None
            assert mod._meter_provider is not None
        finally:
            mod.shutdown_otel_genai()


class TestTaskSpanManager:
    """Test task-level root span lifecycle."""

    def test_start_end_noop_when_not_initialized(self):
        from chaos_agent.observability.otel_genai import TaskSpanManager
        mgr = TaskSpanManager()
        mgr.start_task_span("task-123")
        mgr.end_task_span("task-123")

    def test_end_nonexistent_task_noop(self):
        from chaos_agent.observability.otel_genai import TaskSpanManager
        mgr = TaskSpanManager()
        mgr.end_task_span("nonexistent")


class TestOTelGenAICallback:
    """Test the LangChain callback when not initialized (no spans created)."""

    def test_on_llm_start_noop_when_not_initialized(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.on_llm_start({}, ["hello"], run_id=uuid.uuid4())

    def test_on_llm_end_noop_when_no_span(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        response = MagicMock()
        response.llm_output = {"token_usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        cb.on_llm_end(response, run_id=uuid.uuid4())

    def test_on_llm_error_noop_when_no_span(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.on_llm_error(RuntimeError("test"), run_id=uuid.uuid4())

    def test_on_tool_start_noop_when_not_initialized(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.on_tool_start({"name": "kubectl"}, "input", run_id=uuid.uuid4())

    def test_on_tool_end_noop_when_no_span(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.on_tool_end("output", run_id=uuid.uuid4())

    def test_on_tool_error_noop_when_no_span(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.on_tool_error(RuntimeError("fail"), run_id=uuid.uuid4())

    def test_set_task_id(self, monkeypatch):
        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", False)
        cb = mod.OTelGenAICallback()
        cb.set_task_id("task-abc")
        assert cb._current_task_id == "task-abc"


class TestOTelWithSDK:
    """Integration tests with real OTel SDK.

    Each test creates its own TracerProvider and patches get_otel_tracer()
    to return a tracer from that provider, avoiding the global
    set_tracer_provider() which can only be called once per process.
    """

    def _make_provider(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        return provider, exporter

    def test_full_llm_span_lifecycle(self, monkeypatch):
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None

        run_id = uuid.uuid4()
        cb.on_llm_start({}, ["test prompt"], run_id=run_id)
        response = MagicMock()
        response.llm_output = {"token_usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        cb.on_llm_end(response, run_id=run_id)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        span = spans[0]
        assert "chat" in span.name
        assert span.attributes["gen_ai.operation.name"] == "chat"
        assert span.attributes["gen_ai.usage.input_tokens"] == 100
        assert span.attributes["gen_ai.usage.output_tokens"] == 50

        provider.shutdown()

    def test_tool_span_lifecycle(self, monkeypatch):
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        cb = mod.OTelGenAICallback()
        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "kubectl"}, "get pods", run_id=run_id)
        cb.on_tool_end("NAME  READY  STATUS", run_id=run_id)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert "execute_tool kubectl" in spans[0].name
        assert spans[0].attributes["gen_ai.tool.name"] == "kubectl"

        provider.shutdown()

    def test_error_span(self, monkeypatch):
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None
        run_id = uuid.uuid4()
        cb.on_llm_start({}, ["test"], run_id=run_id)
        cb.on_llm_error(TimeoutError("connection timed out"), run_id=run_id)

        spans = exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].status.status_code == StatusCode.ERROR
        assert spans[0].attributes["error.type"] == "TimeoutError"

        provider.shutdown()

    def test_llm_span_carries_genai_attributes(self, monkeypatch):
        """gen_ai.provider.name / request.model / server.address must be set."""
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None

        run_id = uuid.uuid4()
        cb.on_llm_start({}, ["x"], run_id=run_id)
        cb.on_llm_end(MagicMock(llm_output={}), run_id=run_id)

        attrs = exporter.get_finished_spans()[0].attributes
        assert "gen_ai.provider.name" in attrs
        assert "gen_ai.request.model" in attrs
        assert "server.address" in attrs
        assert "server.port" in attrs

        provider.shutdown()

    def test_set_task_id_propagates_via_contextvar(self, monkeypatch):
        """set_task_id stamps gen_ai.conversation.id on the LLM span."""
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None

        cb.set_task_id("task-xyz")
        run_id = uuid.uuid4()
        cb.on_llm_start({}, ["x"], run_id=run_id)
        cb.on_llm_end(MagicMock(llm_output={}), run_id=run_id)

        attrs = exporter.get_finished_spans()[0].attributes
        assert attrs["gen_ai.conversation.id"] == "task-xyz"

        provider.shutdown()

    def test_llm_span_nested_under_task_span(self, monkeypatch):
        """When task span is active, LLM span becomes its child."""
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        tsm = mod.TaskSpanManager()
        monkeypatch.setattr(mod, "_task_span_manager", tsm)

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None

        task_id = "task-parent"
        tsm.start_task_span(task_id)
        cb.set_task_id(task_id)

        run_id = uuid.uuid4()
        cb.on_llm_start({}, ["x"], run_id=run_id)
        cb.on_llm_end(MagicMock(llm_output={}), run_id=run_id)
        tsm.end_task_span(task_id)

        spans = exporter.get_finished_spans()
        # 2 spans expected: LLM (closed first) then task root
        assert len(spans) == 2
        llm_span = next(s for s in spans if "chat" in s.name)
        task_span = next(s for s in spans if s.name == "invoke_agent blade-ai")
        # LLM span's parent SpanContext should reference the task span
        assert llm_span.parent is not None
        assert llm_span.parent.span_id == task_span.context.span_id

        provider.shutdown()

    def test_tool_span_nested_under_task_span(self, monkeypatch):
        """Tool spans are children of the task root, not orphans."""
        provider, exporter = self._make_provider()

        import chaos_agent.observability.otel_genai as mod
        monkeypatch.setattr(mod, "_initialized", True)
        monkeypatch.setattr(mod, "_tracer_provider", provider)
        monkeypatch.setattr(mod, "get_otel_tracer", lambda: provider.get_tracer("test"))

        tsm = mod.TaskSpanManager()
        monkeypatch.setattr(mod, "_task_span_manager", tsm)

        cb = mod.OTelGenAICallback()
        cb._token_usage_histogram = None
        cb._operation_duration_histogram = None

        task_id = "task-tool-parent"
        tsm.start_task_span(task_id)
        cb.set_task_id(task_id)

        run_id = uuid.uuid4()
        cb.on_tool_start({"name": "kubectl"}, "get pods", run_id=run_id)
        cb.on_tool_end("ok", run_id=run_id)
        tsm.end_task_span(task_id)

        spans = exporter.get_finished_spans()
        tool_span = next(s for s in spans if "execute_tool" in s.name)
        task_span = next(s for s in spans if s.name == "invoke_agent blade-ai")
        assert tool_span.parent is not None
        assert tool_span.parent.span_id == task_span.context.span_id

        provider.shutdown()

    def test_end_task_span_is_idempotent(self):
        """end_task_span called twice on same id must not raise."""
        from chaos_agent.observability.otel_genai import TaskSpanManager
        mgr = TaskSpanManager()
        mgr.end_task_span("nonexistent")
        mgr.end_task_span("nonexistent")  # second call must be a no-op
