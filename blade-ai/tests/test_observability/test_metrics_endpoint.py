"""Tests for the /metrics Prometheus scrape endpoint."""

import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client import REGISTRY


@pytest.fixture
def app_with_route():
    """FastAPI instance with just the prometheus router — no lifespan."""
    app = FastAPI()
    from chaos_agent.server.routes.prometheus import prometheus_router
    app.include_router(prometheus_router)
    return TestClient(app)


class TestMetricsEndpoint:
    def test_returns_404_when_disabled(self, app_with_route, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "prometheus_enabled", False)
        response = app_with_route.get("/metrics")
        assert response.status_code == 404

    def test_returns_prometheus_text_when_enabled(self, app_with_route, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "prometheus_enabled", True)
        response = app_with_route.get("/metrics")
        assert response.status_code == 200
        # CONTENT_TYPE_LATEST is text/plain with version + charset.
        assert response.headers["content-type"].startswith("text/plain")
        # Body must carry valid Prometheus exposition. The default
        # REGISTRY ships with python_info regardless of OTel state,
        # so even a fresh process should emit at least its HELP line.
        assert "# HELP" in response.text
        assert len(response.text) > 0

    def test_response_contains_genai_token_usage(self, app_with_route, monkeypatch):
        """End-to-end: record a token usage via the OTel callback, then
        scrape /metrics and confirm the series surface in Prometheus text.

        Uses a dedicated MeterProvider + PrometheusMetricReader so the
        global REGISTRY collects something real.
        """
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "prometheus_enabled", True)

        from opentelemetry.exporter.prometheus import PrometheusMetricReader
        from opentelemetry import metrics as otel_metrics
        reader = PrometheusMetricReader()
        provider = MeterProvider(
            resource=Resource.create({"service.name": "test"}),
            metric_readers=[reader],
        )
        monkeypatch.setattr(otel_metrics, "_PROXY_METER_PROVIDER", None, raising=False)
        original_provider = otel_metrics.get_meter_provider()
        otel_metrics.set_meter_provider(provider)

        try:
            import chaos_agent.observability.otel_genai as mod
            monkeypatch.setattr(mod, "_initialized", True)

            cb = mod.OTelGenAICallback()
            run_id = uuid.uuid4()
            cb.on_llm_start({}, ["hi"], run_id=run_id)
            response = MagicMock()
            response.llm_output = {"token_usage": {"prompt_tokens": 42, "completion_tokens": 13}}
            cb.on_llm_end(response, run_id=run_id)

            text = app_with_route.get("/metrics").text
            assert "gen_ai_client_token_usage" in text
            assert "gen_ai_client_operation_duration" in text
            # Prometheus exposition format — every series block opens
            # with HELP + TYPE lines. Their presence confirms the
            # exporter produced real text/plain, not a stub.
            assert "# HELP gen_ai_client_token_usage" in text
            assert "# TYPE gen_ai_client_token_usage" in text
        finally:
            provider.shutdown()
            # Restore global meter provider so we don't leak state across tests
            try:
                otel_metrics.set_meter_provider(original_provider)
            except Exception:
                pass
