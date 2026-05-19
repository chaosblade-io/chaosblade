"""Interrupt resolution + cancel endpoints for the TS TUI.

M3 status:
  - /interrupt resolves the per-task Future registered by /turn at the
    confirmation gate. /turn then runs Command(resume=...) on the
    paused graph and continues to stream events on the same SSE
    connection.
  - /cancel is functional: it cancels the asyncio.Task registered in
    TaskTracker for any in-flight task on this session.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from pydantic import BaseModel

from chaos_agent.server.routes.sessions import get_store, sessions_router

logger = logging.getLogger(__name__)


class InterruptResolve(BaseModel):
    interrupt_id: str  # M1: same as task_id
    answer: str        # "approved" | "rejected" | <free text answer>


@sessions_router.post("/{sid}/interrupt")
async def resolve_interrupt(
    sid: str, body: InterruptResolve, req: Request
) -> dict[str, Any]:
    """Submit an answer to a pending interrupt.

    The matching ``register_interrupt`` future inside ``/turn`` will
    receive the answer and the SSE generator resumes the LangGraph via
    ``Command(resume=...)``.

    ``delivered=False`` means the future was missing (already resolved,
    timed out, or never registered) — the client should treat this as
    a no-op that's safe to retry once.
    """
    store = get_store()
    delivered = store.resolve_interrupt(body.interrupt_id, body.answer)
    logger.info(
        f"interrupt[{body.interrupt_id}] answer={body.answer!r} delivered={delivered}"
    )
    return {"ok": True, "delivered": delivered}


@sessions_router.post("/{sid}/cancel")
async def cancel_turn(sid: str, req: Request) -> dict[str, Any]:
    """Cancel any in-flight turn task registered with TaskTracker.

    Reaches into ``task_tracker._active_tasks`` because the public
    surface doesn't expose iteration. Acceptable for the in-process
    server; revisit if TaskTracker grows a proper API.
    """
    task_tracker = req.app.state.task_tracker
    cancelled: list[str] = []
    for tid, task in list(task_tracker._active_tasks.items()):
        if task and not task.done():
            task.cancel()
            cancelled.append(tid)
    logger.info(f"cancel session={sid} cancelled_tasks={cancelled}")
    return {"ok": True, "cancelled": cancelled}
