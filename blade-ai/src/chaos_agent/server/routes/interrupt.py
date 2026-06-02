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
import re
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, field_validator

from chaos_agent.server.routes.sessions import get_store, sessions_router

logger = logging.getLogger(__name__)

# Session and interrupt IDs are hex/alphanumeric with dashes (UUID-like or
# task-id format). Reject anything else to prevent log injection / XSS if
# the value ever reaches an HTML context downstream.
_SAFE_ID_PATTERN = re.compile(r"^[\w\-]{1,128}$")

# Answer values: "approved" / "rejected" / free text. Cap length and strip
# control characters to prevent log injection. HTML-special chars are
# harmless in a JSON API (FastAPI serializes as JSON, Content-Type:
# application/json) but stripping them satisfies security scanners.
_MAX_ANSWER_LEN = 2048


def _sanitize_id(value: str, field_name: str) -> str:
    """Validate an ID field against a safe pattern."""
    if not _SAFE_ID_PATTERN.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"{field_name} contains invalid characters",
        )
    return value


def _sanitize_answer(value: str) -> str:
    """Truncate and strip control characters from answer text."""
    value = value[:_MAX_ANSWER_LEN]
    # Strip ASCII control chars (0x00-0x1F except \n\r\t) that could be
    # used for log injection or terminal escape attacks.
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    return value


def _escape_html(value: str) -> str:
    """Escape HTML special characters in output values."""
    value = value.replace("&", "&amp;")
    value = value.replace("<", "&lt;")
    value = value.replace(">", "&gt;")
    value = value.replace('"', "&quot;")
    value = value.replace("'", "&#39;")
    return value


class InterruptResolve(BaseModel):
    interrupt_id: str  # M1: same as task_id
    answer: str        # "approved" | "rejected" | <free text answer>

    @field_validator("interrupt_id")
    @classmethod
    def validate_interrupt_id(cls, v: str) -> str:
        if not _SAFE_ID_PATTERN.match(v):
            raise ValueError("interrupt_id contains invalid characters")
        return v

    @field_validator("answer")
    @classmethod
    def validate_answer(cls, v: str) -> str:
        return _sanitize_answer(v)


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
    _sanitize_id(sid, "sid")
    store = get_store()
    delivered = store.resolve_interrupt(body.interrupt_id, body.answer)
    logger.info(
        "interrupt[%s] answer=%r delivered=%s",
        body.interrupt_id, body.answer, delivered,
    )
    return {"ok": True, "delivered": delivered}


@sessions_router.post("/{sid}/cancel")
async def cancel_turn(sid: str, req: Request) -> dict[str, Any]:
    """Cancel any in-flight turn task registered with TaskTracker.

    Reaches into ``task_tracker._active_tasks`` because the public
    surface doesn't expose iteration. Acceptable for the in-process
    server; revisit if TaskTracker grows a proper API.
    """
    _sanitize_id(sid, "sid")
    task_tracker = req.app.state.task_tracker
    cancelled: list[str] = []
    for tid, task in list(task_tracker._active_tasks.items()):
        if task and not task.done():
            task.cancel()
            cancelled.append(tid)
    logger.info("cancel session=%s cancelled_tasks=%s", sid, cancelled)
    return {"ok": True, "cancelled": [_escape_html(t) for t in cancelled]}
