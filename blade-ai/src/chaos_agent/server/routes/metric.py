"""GET /api/v1/metric - Task execution metrics and status endpoint."""

import logging

from fastapi import Request

from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.persistence.task_store import get_task_store
from chaos_agent.server.routes import metric_router

logger = logging.getLogger(__name__)


@metric_router.get("/metric")
async def list_task_metrics(req: Request):
    """Get execution metrics and status for ALL tasks."""
    store = await get_task_store()
    data = await store.get_all_metrics()
    return JSONEnvelope.ok(data=data, request_id=getattr(req.state, "request_id", ""))


@metric_router.get("/metric/{task_id}")
async def get_task_metric(task_id: str, req: Request):
    """Get execution metrics and status for a single task."""
    store = await get_task_store()
    data = await store.get_metric(task_id)

    if data:
        return JSONEnvelope.ok(data=data, request_id=getattr(req.state, "request_id", ""))

    return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found: {task_id}", request_id=getattr(req.state, "request_id", ""))
