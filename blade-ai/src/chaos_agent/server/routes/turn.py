"""Unified SSE turn endpoint for the TS TUI front-end.

Frame format reuses the existing ``StreamEvent.to_sse()``: each frame is a
single ``data: {...json...}\\n\\n`` line whose JSON carries a ``type``
field (no ``event:`` row). This matches the legacy ``/inject-stream``
shape so a TS client can decode either endpoint with the same parser.

Event types yielded:
  token / thinking / tool_start / tool_end / node_start / node_end
  confirm / result / error / done

The ``done`` event is the explicit terminator so the client knows the
turn is complete (vs. just the connection closing).
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from chaos_agent.agent.fault_spec import SOURCE_TUI, FaultSpec
from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.config.settings import settings
from chaos_agent.server.routes.sessions import SessionStore, get_store, sessions_router
from chaos_agent.server.routes.turn_event_stream import TurnContext, event_generator

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class TurnRequest(BaseModel):
    input: str
    permission_mode: str = "confirm"
    display_mode: str | None = "calm"
    dry_run: bool = False


@sessions_router.post("/{sid}/turn")
async def turn(sid: str, body: TurnRequest, req: Request):
    """Run one conversation turn and stream events as SSE."""
    store: SessionStore = get_store()
    sess = store.get(sid)
    if sess is None:
        raise HTTPException(404, "Session not found")

    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker

    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([StreamEvent(type="error", content="Server is shutting down").to_sse()]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    if agents is None:
        return StreamingResponse(
            iter([StreamEvent(type="error", content="LLM config missing; run the setup wizard first.").to_sse()]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    turn_id = f"turn-{uuid4().hex[:12]}"

    thread_id = sess.get("conversation_thread_id") or ""
    if not thread_id:
        thread_id = f"conv-{uuid4().hex[:12]}"
        sess["conversation_thread_id"] = thread_id

    is_first_turn = not sess.get("first_turn_done", False)
    sess["first_turn_done"] = True

    if is_first_turn:
        spec = FaultSpec.placeholder_nl(
            user_description=body.input or "",
            source=SOURCE_TUI,
        )
        initial_state = {
            "task_id": turn_id,
            "tui_session_id": sid,
            "interaction_mode": "tui",
            "fault_spec": spec.to_dict(),
            "needs_confirmation": True,
            "kubeconfig": settings.kubeconfig_path,
            "kube_context": settings.kube_context,
            "kubewiz_cluster_uuid": settings.kubewiz_cluster_uuid,
            "kubewiz_profile": settings.kubewiz_profile,
            "dry_run": body.dry_run,
        }
    else:
        initial_state = {
            "task_id": turn_id,
            "input": body.input,
            "confirmed_intent": "unset",
            "intent_confidence": 0.0,
            "dry_run": body.dry_run,
        }

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }

    from chaos_agent.observability.status_tracker import subscribe as _status_subscribe
    tracker_key = f"tui-{sid}"
    tracker_queue = _status_subscribe(tracker_key)

    ctx = TurnContext(
        sid=sid,
        turn_id=turn_id,
        thread_id=thread_id,
        input_text=body.input,
        permission_mode=body.permission_mode,
        dry_run=body.dry_run,
        req=req,
        store=store,
        agents=agents,
        task_tracker=task_tracker,
        intent_graph=agents["intent"],
        pipeline_graph=agents["pipeline"],
        graph_config=config,
        initial_state=initial_state,
        tracker_key=tracker_key,
        tracker_queue=tracker_queue,
    )

    return StreamingResponse(
        event_generator(ctx),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
