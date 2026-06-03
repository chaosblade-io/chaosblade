"""GET /api/v1/status-stream/{task_id} - SSE endpoint for real-time agent status."""

import asyncio
import json
import logging

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.observability.status_tracker import (
    subscribe,
    unsubscribe,
    get_tracker,
    StatusPhase,
)
from chaos_agent.server.routes import inject_router

logger = logging.getLogger(__name__)


@inject_router.get("/status-stream/{task_id}")
async def status_stream(task_id: str, request: Request):
    """SSE endpoint that streams real-time agent status events.

    Usage:
        curl -N http://localhost:8089/api/v1/status-stream/task-20260421-120000-abc123

    Each event is a JSON object with fields:
        task_id, phase, category, source, message, timestamp, duration_ms, detail
    """

    async def event_generator():
        queue = subscribe(task_id)
        try:
            # First, send any historical events already recorded
            tracker = get_tracker(task_id)
            for event_dict in tracker.get_history():
                yield f"data: {json.dumps(event_dict)}\n\n"

            # Then stream live events
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield f"data: {json.dumps(event.to_dict())}\n\n"

                    # If terminal event, drain remaining and close
                    if event.phase in (StatusPhase.COMPLETED, StatusPhase.FAILED):
                        await asyncio.sleep(0.5)
                        while not queue.empty():
                            try:
                                remaining = queue.get_nowait()
                                yield f"data: {json.dumps(remaining.to_dict())}\n\n"
                            except asyncio.QueueEmpty:
                                break
                        break
                except asyncio.TimeoutError:
                    # Send keepalive comment
                    yield ": keepalive\n\n"
        finally:
            unsubscribe(task_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
