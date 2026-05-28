"""OpenTelemetry GenAI semantic conventions — parallel export layer.

Emits OTel spans and metrics following the GenAI semantic conventions
(https://github.com/open-telemetry/semantic-conventions-genai) alongside
the existing built-in tracing system.

Two independent flags drive what gets initialised:
- ``settings.otel_enabled``: TracerProvider + OTLP span/metric exporters.
- ``settings.prometheus_enabled``: PrometheusMetricReader feeding
  ``prometheus_client.REGISTRY`` for the ``/metrics`` scrape endpoint.

Both off = full no-op, zero overhead.
"""

import contextvars
import logging
import time
from typing import Any
from urllib.parse import urlparse

from opentelemetry import trace, metrics
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.prometheus import PrometheusMetricReader
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import SpanKind, StatusCode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_initialized = False
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None

# ContextVar for task_id — safe across concurrent asyncio tasks
_current_otel_task_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_otel_task_id", default=""
)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_otel_available() -> bool:
    """True if OTel callback should be installed.

    Either otel_enabled (traces + OTLP metrics) or prometheus_enabled
    (metrics-only via /metrics scrape) requires the callback because
    both code paths flow through the meter instruments it owns.
    """
    from chaos_agent.config.settings import settings
    return settings.otel_enabled or settings.prometheus_enabled


def init_otel_genai() -> None:
    """Initialize OTel providers based on otel_enabled / prometheus_enabled.

    - otel_enabled: TracerProvider + OTLP span/metric exporters.
    - prometheus_enabled: PrometheusMetricReader writes to the global
      ``prometheus_client.REGISTRY`` which the /metrics route serves.

    No-op if both flags are off.
    """
    global _initialized, _tracer_provider, _meter_provider

    from chaos_agent.config.settings import settings
    if not (settings.otel_enabled or settings.prometheus_enabled):
        return
    if _initialized:
        return

    resource = Resource.create({
        "service.name": settings.otel_service_name,
        "service.version": _get_version(),
        "gen_ai.agent.name": "blade-ai",
    })

    metric_readers: list[MetricReader] = []

    if settings.otel_enabled:
        endpoint = settings.otel_endpoint or "http://localhost:4317"
        insecure = not endpoint.startswith("https://")

        _tracer_provider = TracerProvider(resource=resource)
        _tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
        )
        trace.set_tracer_provider(_tracer_provider)

        metric_readers.append(
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=endpoint, insecure=insecure),
                export_interval_millis=10000,
            )
        )

    if settings.prometheus_enabled:
        metric_readers.append(PrometheusMetricReader())

    # The early `return` above plus the two flag checks guarantee
    # metric_readers is non-empty here, so no defensive guard.
    _meter_provider = MeterProvider(resource=resource, metric_readers=metric_readers)
    metrics.set_meter_provider(_meter_provider)

    _initialized = True
    logger.info(
        "OTel init (otel_enabled=%s, prometheus_enabled=%s)",
        settings.otel_enabled,
        settings.prometheus_enabled,
    )


def shutdown_otel_genai() -> None:
    """Flush and shutdown OTel providers. Call during app shutdown."""
    global _initialized
    if not _initialized:
        return
    try:
        if _tracer_provider:
            _tracer_provider.shutdown()
        if _meter_provider:
            _meter_provider.shutdown()
    except Exception as e:
        logger.warning("OTel shutdown error: %s", e)
    _initialized = False


def get_otel_tracer():
    """Get the GenAI tracer."""
    return trace.get_tracer("blade-ai.genai", _get_version())


def get_otel_meter():
    """Get the GenAI meter."""
    return metrics.get_meter("blade-ai.genai", _get_version())


# ---------------------------------------------------------------------------
# Provider inference
# ---------------------------------------------------------------------------

_PROVIDER_MAP = {
    "dashscope": "alibaba_cloud",
    "anthropic": "anthropic",
    "deepseek": "deepseek",
    "azure": "azure.ai.openai",
    "googleapis": "gcp.gen_ai",
    "openai": "openai",
}


def _infer_provider(api_base_url: str) -> str:
    """Infer gen_ai.provider.name from API base URL hostname."""
    host = (urlparse(api_base_url).hostname or "").lower()
    for keyword, provider in _PROVIDER_MAP.items():
        if keyword in host:
            return provider
    return "openai"


# ---------------------------------------------------------------------------
# TaskSpanManager
# ---------------------------------------------------------------------------

class TaskSpanManager:
    """Manage task-level root spans (invoke_agent blade-ai)."""

    def __init__(self):
        self._spans: dict[str, Any] = {}

    def start_task_span(self, task_id: str) -> None:
        if not _initialized:
            return
        tracer = get_otel_tracer()
        span = tracer.start_span(
            "invoke_agent blade-ai",
            kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "blade-ai",
                "gen_ai.conversation.id": task_id,
            },
        )
        self._spans[task_id] = span

    def end_task_span(self, task_id: str) -> None:
        span = self._spans.pop(task_id, None)
        if span is None:
            return
        span.end()

    def get_current_context(self, task_id: str):
        """Get trace context for nesting child spans under the task span."""
        span = self._spans.get(task_id)
        if span is None:
            return None
        return trace.set_span_in_context(span)


_task_span_manager = TaskSpanManager()


def get_task_span_manager() -> TaskSpanManager:
    return _task_span_manager


# ---------------------------------------------------------------------------
# OTelGenAICallback (LangChain BaseCallbackHandler)
# ---------------------------------------------------------------------------

class OTelGenAICallback:
    """LangChain callback that emits OTel GenAI spans and metrics.

    Runs in parallel with the existing _DynamicTracingCallback.
    Span dicts are instance-scoped; keyed by unique run_id so concurrent
    tasks sharing this callback instance don't collide.
    """

    def __init__(self):
        from chaos_agent.config.settings import settings
        self._model = settings.model_name
        self._provider = (
            settings.otel_provider_name
            or _infer_provider(settings.api_base_url)
        )
        self._temperature = settings.llm_temperature
        _parsed = urlparse(settings.api_base_url)
        self._server_address = _parsed.hostname or ""
        # Scheme-based default: 80 for http, 443 for https/empty.
        self._server_port = _parsed.port or (80 if _parsed.scheme == "http" else 443)
        self._current_task_id: str = ""

        # Per-instance span storage (keyed by run_id UUID — always unique)
        self._llm_spans: dict[str, Any] = {}
        self._llm_start_times: dict[str, float] = {}
        self._tool_spans: dict[str, Any] = {}

        self._token_usage_histogram = None
        self._operation_duration_histogram = None
        self._setup_metrics()

    def _setup_metrics(self):
        meter = get_otel_meter()
        self._token_usage_histogram = meter.create_histogram(
            name="gen_ai.client.token.usage",
            unit="{token}",
            description="Number of input and output tokens used.",
        )
        self._operation_duration_histogram = meter.create_histogram(
            name="gen_ai.client.operation.duration",
            unit="s",
            description="GenAI operation duration.",
        )

    def set_task_id(self, task_id: str):
        self._current_task_id = task_id
        _current_otel_task_id.set(task_id)

    def _common_attributes(self) -> dict:
        return {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self._provider,
            "gen_ai.request.model": self._model,
            "gen_ai.request.temperature": self._temperature,
            "gen_ai.request.stream": True,
            "server.address": self._server_address,
            "server.port": self._server_port,
        }

    def on_llm_start(self, serialized: Any, prompts: Any, *, run_id, **kwargs) -> None:
        if not _initialized:
            return
        tracer = get_otel_tracer()

        run_key = str(run_id)
        task_id = _current_otel_task_id.get()
        ctx = _task_span_manager.get_current_context(task_id)
        span = tracer.start_span(
            f"chat {self._model}",
            kind=SpanKind.CLIENT,
            attributes=self._common_attributes(),
            context=ctx,
        )
        if task_id:
            span.set_attribute("gen_ai.conversation.id", task_id)
        self._llm_spans[run_key] = span
        self._llm_start_times[run_key] = time.perf_counter()

    def on_llm_end(self, response: Any, *, run_id, **kwargs) -> None:
        run_key = str(run_id)
        span = self._llm_spans.pop(run_key, None)
        start_time = self._llm_start_times.pop(run_key, None)
        if span is None:
            return

        from chaos_agent.observability.tracer import _extract_token_usage
        prompt_tokens, completion_tokens = _extract_token_usage(response)

        span.set_attribute("gen_ai.usage.input_tokens", prompt_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", completion_tokens)
        span.end()

        duration = time.perf_counter() - start_time if start_time else 0.0
        metric_attrs = {
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": self._provider,
            "gen_ai.request.model": self._model,
        }

        if self._token_usage_histogram:
            if prompt_tokens:
                self._token_usage_histogram.record(
                    prompt_tokens,
                    {**metric_attrs, "gen_ai.token.type": "input"},
                )
            if completion_tokens:
                self._token_usage_histogram.record(
                    completion_tokens,
                    {**metric_attrs, "gen_ai.token.type": "output"},
                )
        if self._operation_duration_histogram and duration > 0:
            self._operation_duration_histogram.record(duration, metric_attrs)

    def on_llm_error(self, error: BaseException, *, run_id, **kwargs) -> None:
        run_key = str(run_id)
        span = self._llm_spans.pop(run_key, None)
        self._llm_start_times.pop(run_key, None)
        if span is None:
            return
        span.set_attribute("error.type", type(error).__name__)
        span.set_status(StatusCode.ERROR, str(error))
        span.end()

    def on_tool_start(
        self, serialized: Any, input_str: Any, *, run_id, **kwargs
    ) -> None:
        if not _initialized:
            return
        tracer = get_otel_tracer()

        run_key = str(run_id)
        tool_name = ""
        if isinstance(serialized, dict):
            tool_name = serialized.get("name", "")
        if not tool_name:
            tool_name = kwargs.get("name", "unknown")

        ctx = _task_span_manager.get_current_context(_current_otel_task_id.get())

        span = tracer.start_span(
            f"execute_tool {tool_name}",
            kind=SpanKind.INTERNAL,
            attributes={
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": tool_name,
            },
            context=ctx,
        )
        self._tool_spans[run_key] = span

    def on_tool_end(self, output: Any, *, run_id, **kwargs) -> None:
        run_key = str(run_id)
        span = self._tool_spans.pop(run_key, None)
        if span is None:
            return
        span.end()

    def on_tool_error(self, error: BaseException, *, run_id, **kwargs) -> None:
        run_key = str(run_id)
        span = self._tool_spans.pop(run_key, None)
        if span is None:
            return
        span.set_attribute("error.type", type(error).__name__)
        span.set_status(StatusCode.ERROR, str(error))
        span.end()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_version() -> str:
    try:
        from chaos_agent import __version__
        return __version__
    except Exception:
        return "0.0.0"
