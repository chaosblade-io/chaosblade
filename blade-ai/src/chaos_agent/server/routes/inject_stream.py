"""POST /api/v1/inject-stream - SSE streaming inject endpoint."""

import asyncio
import json
import logging
import uuid

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.state_builders import build_inject_initial_state
from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import inject_router
from chaos_agent.server.schemas import InjectRequest
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


@inject_router.post("/inject-stream")
async def inject_stream(request: InjectRequest, req: Request):
    """Inject a fault with real-time SSE streaming.

    Returns a Server-Sent Events stream with events:
    - token: LLM output tokens
    - tool_start/tool_end: Tool invocations
    - confirm: Paused at confirmation gate
    - result: Final result envelope
    - error: Error message
    """
    task_id = f"task-{uuid.uuid4()}"
    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker

    # Check if server is shutting down
    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([StreamEvent(type="error", content="Server is shutting down", task_id=task_id).to_sse()]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # First-run gate — see inject.py for the rationale.
    if agents is None:
        return StreamingResponse(
            iter([StreamEvent(
                type="error",
                content="LLM config missing; run the setup wizard first.",
                task_id=task_id,
            ).to_sse()]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Runtime override
    if request.kubeconfig:
        settings.kubeconfig_path = request.kubeconfig
    if request.context:
        settings.kube_context = request.context

    # Build initial state — FaultSpec is the single source of truth.
    spec = FaultSpec.from_http_request(request)
    target_names = list(spec.names)
    initial_state = build_inject_initial_state(
        task_id=task_id,
        fault_spec=spec,
        needs_confirmation=request.confirm,
        kubeconfig=request.kubeconfig or settings.kubeconfig_path,
        kube_context=request.context or settings.kube_context,
        kubewiz_cluster_uuid=getattr(request, "cluster_uuid", "") or settings.kubewiz_cluster_uuid,
        kubewiz_profile=getattr(request, "profile", "") or settings.kubewiz_profile,
        direct=request.direct,
    )

    config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
    graph = agents["pipeline"]

    # Create session for recording
    session_store = agents.get("session_store")
    if session_store:
        session_store.create_session(task_id, operation="inject")

    async def event_generator():
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)

        # Register with task tracker for graceful shutdown
        stream_task = asyncio.current_task()
        task_tracker.register(task_id, stream_task)
        batcher = SSEBatcher(
            flush_interval_ms=settings.sse_batch_interval_ms,
            flush_chars=settings.sse_batch_chars,
        )
        try:
            _tsm.start_task_span(task_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(task_id)
            # Stream first invoke
            async for raw_event in graph.astream_events(initial_state, config, version="v2"):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected, aborting stream for task {task_id}")
                    break
                stream_evt = parse_stream_event(raw_event)
                if stream_evt is not None:
                    stream_evt.task_id = task_id
                    for sse in batcher.feed(stream_evt):
                        yield sse
            for sse in batcher.flush():
                yield sse

            # Check if paused at confirmation_gate
            current_state = await graph.aget_state(config)
            if current_state and current_state.next:
                next_nodes = list(current_state.next)
                if "confirmation_gate" in next_nodes:
                    plan_summary = ""
                    if current_state.values:
                        plan_summary = current_state.values.get("plan_summary", "")
                    yield StreamEvent(
                        type="confirm",
                        content=plan_summary,
                        node="confirmation_gate",
                        task_id=task_id,
                    ).to_sse()

                    if not request.confirm:
                        # Auto-approve
                        from langgraph.types import Command

                        async for raw_event in graph.astream_events(
                            Command(resume="approved"), config, version="v2"
                        ):
                            if await req.is_disconnected():
                                logger.info(f"Client disconnected during confirm flow for task {task_id}")
                                break
                            stream_evt = parse_stream_event(raw_event)
                            if stream_evt is not None:
                                stream_evt.task_id = task_id
                                for sse in batcher.feed(stream_evt):
                                    yield sse
                        for sse in batcher.flush():
                            yield sse

            # Extract final result
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values

                # Non-injection intent completed via intent_clarification (TUI mode)
                confirmed_intent = values.get("confirmed_intent")
                if confirmed_intent in ("chat", "recover"):
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(JSONEnvelope.ok(
                            data={
                                "task_id": task_id,
                                "result": "completed",
                                "confirmed_intent": confirmed_intent,
                            },
                            request_id=getattr(req.state, "request_id", ""),
                        ), ensure_ascii=False),
                        task_id=task_id,
                    ).to_sse()
                    return

                # Fault injection result
                from chaos_agent.server.routes.turn_result import build_inject_data_from_state
                _data = build_inject_data_from_state(values, task_id)
                _data["plan_summary"] = values.get("plan_summary", "")
                _data["needs_confirm"] = request.confirm
                _data["created_at"] = now_iso()

                yield StreamEvent(
                    type="result",
                    content=json.dumps(JSONEnvelope.ok(
                        data=_data,
                        request_id=getattr(req.state, "request_id", ""),
                    ), ensure_ascii=False),
                    task_id=task_id,
                ).to_sse()
            else:
                yield StreamEvent(
                    type="error",
                    content="Graph completed but no state available",
                    task_id=task_id,
                ).to_sse()

        except Exception as e:
            logger.exception(f"Stream inject failed for task {task_id}")

            # Auto-rollback
            rollback_info = ""
            try:
                current_state = await graph.aget_state(config)
                if current_state and current_state.values:
                    blade_uid = current_state.values.get("blade_uid", "")
                    kubeconfig = current_state.values.get("kubeconfig", "")
                    if blade_uid:
                        from chaos_agent.tools.blade import blade_destroy
                        await blade_destroy.ainvoke(
                            {"uid": blade_uid, "kubeconfig": kubeconfig}
                        )
                        rollback_info = f" (auto-rolled back blade_uid={blade_uid})"
            except Exception as rb_err:
                rollback_info = f" (rollback FAILED: {rb_err})"

            yield StreamEvent(
                type="error",
                content=f"Inject failed: {type(e).__name__}: {e}{rollback_info}",
                task_id=task_id,
            ).to_sse()
        finally:
            _tsm.end_task_span(task_id)
            # Finalize session: flush remaining messages from final graph state
            if session_store:
                try:
                    remaining = []
                    values_fin = {}
                    try:
                        final_state = await graph.aget_state(config)
                        if final_state and final_state.values:
                            values_fin = final_state.values
                            remaining = values_fin.get("messages", [])
                    except Exception:
                        pass
                    from chaos_agent.server.routes.turn_result import build_inject_data_from_state
                    _data = build_inject_data_from_state(values_fin, task_id) if values_fin else {
                        "task_id": task_id, "task_state": "unknown", "error": "",
                    }
                    session_store.finalize_session(
                        task_id,
                        remaining_messages=remaining,
                        result_summary=JSONEnvelope.ok(data=_data),
                        status="completed",
                    )
                except Exception:
                    logger.warning(f"Failed to finalize session for task {task_id}")
            task_tracker.unregister(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
