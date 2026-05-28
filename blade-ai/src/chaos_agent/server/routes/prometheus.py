"""GET /metrics — Prometheus scrape endpoint.

Serves OTel meter data (E6's gen_ai.* histograms) in Prometheus text
format. Single-port: shares the FastAPI port instead of spawning the
PrometheusMetricReader's bundled HTTP server.

Handler is declared ``def`` (not ``async def``) on purpose:
``generate_latest`` walks the registry synchronously and would block
the event loop if called from an ``async`` handler. Starlette routes
sync handlers through a threadpool, keeping SSE streams and other
async work responsive even during high-cardinality scrapes.
"""

from fastapi import Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, generate_latest

from chaos_agent.config.settings import settings
from chaos_agent.server.routes import prometheus_router


@prometheus_router.get("/metrics", include_in_schema=False)
def prometheus_metrics() -> Response:
    if not settings.prometheus_enabled:
        return Response(status_code=404)
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
