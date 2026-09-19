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

from chaos_agent.agent.spec.fault_spec import SOURCE_TUI, FaultSpec
from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.config.settings import settings
from chaos_agent.server.routes.sessions import SessionStore, get_store, sessions_router
from chaos_agent.server.routes.turn_event_stream import (
    TurnContext,
    _acquire_turn_slot,
    event_generator,
)

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
    planning_mode: str | None = None


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

    # Per-session in-flight guard (concurrent /turn serialization): a
    # second turn on the same conversation thread would interleave
    # graph writes with the running one. 409s a still-running previous
    # turn; waits out a teardown within the grace window (the
    # supersede race: the client aborts the old stream and posts the
    # new turn in one synchronous sequence). Placed AFTER the cheap
    # 404/error prechecks, BEFORE any state mutation below.
    await _acquire_turn_slot(sid, turn_id)

    thread_id = sess.get("conversation_thread_id") or ""
    if not thread_id:
        # Sessions whose stored binding is empty: legacy session files
        # (pre-conversation_thread_id schema) and resumed sessions whose
        # thread never landed on disk. Mint a fresh thread AND backfill
        # it into the session JSON so the binding survives the next
        # server restart. Best-effort: a failed write must not fail the
        # turn — the thread still works for this process's lifetime.
        thread_id = f"conv-{uuid4().hex[:12]}"
        sess["conversation_thread_id"] = thread_id
        try:
            from chaos_agent.memory.tui_session_store import (
                get_global_tui_session_store,
            )

            _tui_store = get_global_tui_session_store()
            if _tui_store is not None:
                _tui_store.update_thread_id(sid, thread_id)
        except Exception as e:
            logger.debug(f"thread-id backfill skipped for {sid}: {e}")

    is_first_turn = not sess.get("first_turn_done", False)
    sess["first_turn_done"] = True

    if is_first_turn:
        spec = FaultSpec.placeholder_nl(
            user_description=body.input or "",
            source=SOURCE_TUI,
        )
        # Channel/transport fields: TurnRequest never carries them, so they
        # resolve from settings — the same fallback the pipeline handoff in
        # turn_event_stream.py and ``TransportTarget.from_state`` apply.
        # The intent prompt renders its `Capability Profile` section from
        # ``kube_connection_mode``; with the field unset the profile resolves
        # to "unknown", the section is skipped, and the Inject Flow rule that
        # tells the model to check it points at something absent — a host
        # fault on a k8s channel was then submitted with no warning. First
        # turn only: later turns inherit via checkpoint merge.
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
            "kube_connection_mode": settings.kube_connection_mode,
            "host_name": getattr(settings, "host_name", ""),
            "ssh_host": getattr(settings, "ssh_host", ""),
            "ssh_user": getattr(settings, "ssh_user", ""),
            "ssh_key_path": getattr(settings, "ssh_key_path", ""),
            "ssh_port": getattr(settings, "ssh_port", None),
            "dry_run": body.dry_run,
            "planning_mode": body.planning_mode,
        }
    else:
        initial_state = {
            "task_id": turn_id,
            "input": body.input,
            "confirmed_intent": "unset",
            "intent_confidence": 0.0,
            # Clear stale intent payload from checkpoint to prevent
            # intent_clarification fast-path from re-confirming an
            # abandoned/rejected batch intent on the next turn.
            "fault_spec": None,
            "batch_submit_args": None,
            "dry_run": body.dry_run,
            "planning_mode": body.planning_mode,
        }

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }

    from chaos_agent.observability.status_tracker import (
        TUI_TRACKER_PREFIX,
        subscribe as _status_subscribe,
    )
    tracker_key = f"{TUI_TRACKER_PREFIX}{sid}"
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


@sessions_router.post("/{sid}/turns/{turn_id}/early-recover")
async def early_recover(sid: str, turn_id: str, req: Request):
    """Break the fault-window hold of an active turn; recovery follows on the stream.

    Ctrl+R in the TS TUI during a hold posts here. The turn's SSE stream
    stays open — the hold loop wakes, the recover graph is dispatched on
    the SAME connection, so the recovery evidence stays in-band. Outside
    a hold the registry lookup misses: no hold to break, nothing to do —
    the turn recovers on its own schedule (window expiry or cancel).
    """
    from chaos_agent.server.routes.interrupt import _sanitize_id
    from chaos_agent.server.routes.turn_event_stream import get_active_hold

    _sanitize_id(sid, "sid")
    _sanitize_id(turn_id, "turn_id")
    ctx = get_active_hold(turn_id)
    if ctx is None or ctx.sid != sid:
        raise HTTPException(404, "No fault-window hold active for this turn")
    ctx.hold_early_recover.set()
    logger.info("early-recover sid=%s turn=%s", sid, turn_id)
    return {"ok": True, "turn_id": turn_id, "early_recover": True}
