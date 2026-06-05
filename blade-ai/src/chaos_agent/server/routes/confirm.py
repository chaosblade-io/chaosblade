"""POST /api/v1/confirm/{task_id} - Confirm or reject a pending task."""

import logging

from fastapi import Request

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import confirm_router
from chaos_agent.server.schemas import ConfirmRequest
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


@confirm_router.post("/confirm/{task_id}")
async def confirm_task(task_id: str, request: ConfirmRequest, req: Request):
    """Confirm or reject a pending task that is waiting for approval."""
    agents = req.app.state.agents
    req_id = getattr(req.state, "request_id", "")

    # First-run gate — agents are deferred until the wizard completes.
    if agents is None:
        return JSONEnvelope.fail(
            code=ResponseCode.NEEDS_SETUP,
            message="LLM config missing; run the setup wizard first.",
            request_id=req_id,
        )

    if request.action not in ("approve", "reject"):
        return JSONEnvelope.fail(code=ResponseCode.INVALID_ACTION, message="Invalid action, must be 'approve' or 'reject'", request_id=req_id)

    config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}

    try:
        from langgraph.types import Command

        resume_value = "approved" if request.action == "approve" else "rejected"
        await agents["pipeline"].ainvoke(Command(resume=resume_value), config)

        new_state = "injecting" if request.action == "approve" else "cancelled"

        return JSONEnvelope.ok(
            data={
                "task_id": task_id,
                "action": request.action,
                "reason": request.reason,
                "confirmed_at": now_iso(),
            },
            request_id=req_id,
        )

    except Exception as e:
        logger.exception(f"Confirm failed for task {task_id}")
        return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found or confirm failed: {type(e).__name__}: {e}", request_id=req_id)
