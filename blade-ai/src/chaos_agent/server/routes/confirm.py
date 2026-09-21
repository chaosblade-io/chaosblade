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
        final = await agents["pipeline"].ainvoke(Command(resume=resume_value), config)

        # Connected defect 2 (round-64): mirror the CLI runner — the resume
        # ran the pipeline to its own verdict, so read it through the same
        # single-source projection and return ``task_state`` alongside the
        # confirm ack. Without it an HTTP caller got a bare "approved" and
        # could not tell an injected drill from a rejected one.
        snapshot = await agents["pipeline"].aget_state(config)
        from chaos_agent.agent.result.operation_result import build_inject_data_from_state

        inject_data = build_inject_data_from_state(
            final if isinstance(final, dict) else {}, task_id, snapshot=snapshot,
        )
        task_state = inject_data.get("task_state")

        return JSONEnvelope.ok(
            data={
                "task_id": task_id,
                "action": request.action,
                "reason": request.reason,
                "confirmed_at": now_iso(),
                "task_state": task_state,
                "result": task_state,
                "error": inject_data.get("error", ""),
            },
            request_id=req_id,
        )

    except Exception as e:
        logger.exception(f"Confirm failed for task {task_id}")
        return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found or confirm failed: {type(e).__name__}: {e}", request_id=req_id)
