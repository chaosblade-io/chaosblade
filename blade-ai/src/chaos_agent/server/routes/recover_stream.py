"""POST /api/v1/recover-stream - SSE streaming recover endpoint."""

import asyncio
import json
import logging
import time
import uuid

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.server.routes import recover_router
from chaos_agent.server.routes.recover_common import (
    RecoverSetupError,
    build_recover_initial_state,
)
from chaos_agent.server.routes.turn_result import build_recover_result_payload
from chaos_agent.server.schemas import RecoverRequest

logger = logging.getLogger(__name__)


@recover_router.post("/recover-stream")
async def recover_stream(request: RecoverRequest, req: Request):
    """Recover a fault with real-time SSE streaming.

    Returns a Server-Sent Events stream with events:
    - token: LLM output tokens (recover verifier reasoning)
    - thinking: LLM thinking tokens
    - tool_start/tool_end: Tool invocations (kubectl, blade_status)
    - node_start/node_end: Graph node transitions
    - result: Final result envelope
    - error: Error message
    - done: Stream complete sentinel
    """
    inject_task_id = request.task_id
    record_task_id = f"task-{uuid.uuid4()}"
    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker
    req_id = getattr(req.state, "request_id", "")

    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([
                StreamEvent(type="error", content="Server is shutting down", task_id=record_task_id).to_sse(),
                StreamEvent(type="done", task_id=record_task_id).to_sse(),
            ]),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    if agents is None:
        return StreamingResponse(
            iter([
                StreamEvent(type="error", content="LLM config missing; run the setup wizard first.", task_id=record_task_id).to_sse(),
                StreamEvent(type="done", task_id=record_task_id).to_sse(),
            ]),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    async def event_generator():
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)

        stream_task = asyncio.current_task()
        task_tracker.register(record_task_id, stream_task)
        batcher = SSEBatcher(
            flush_interval_ms=settings.sse_batch_interval_ms,
            flush_chars=settings.sse_batch_chars,
        )
        session_store = None
        state_values = {}
        started_monotonic = time.monotonic()

        try:
            # 1. Build initial state from inject checkpoint
            try:
                initial_state, state_values = await build_recover_initial_state(
                    agents, inject_task_id, record_task_id, req_id,
                )
            except RecoverSetupError as e:
                yield StreamEvent(
                    type="error",
                    content=e.envelope.get("message", "Task not found"),
                    task_id=record_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                return

            inject_tui_session_id = initial_state.get("tui_session_id", "")

            # 2. Create session for recording
            session_store = agents.get("session_store")
            if session_store:
                inject_messages = state_values.get("messages", [])
                session_store.create_session(
                    record_task_id,
                    operation="recover",
                    tui_session_id=inject_tui_session_id,
                    parent_task_id=inject_task_id,
                    baseline_messages=inject_messages,
                )

            # 3. Stream recover graph
            recover_config = {
                "configurable": {"thread_id": record_task_id},
                "recursion_limit": settings.recursion_limit,
            }
            recover_graph = agents.get("recover")
            if recover_graph is None:
                yield StreamEvent(
                    type="error",
                    content="Recover graph not available",
                    task_id=record_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                return

            _tsm.start_task_span(record_task_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(record_task_id)

            async for raw_event in recover_graph.astream_events(
                initial_state, recover_config, version="v2"
            ):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected, aborting recover stream {record_task_id}")
                    break
                stream_evt = parse_stream_event(raw_event)
                if stream_evt is not None:
                    stream_evt.task_id = record_task_id
                    for sse in batcher.feed(stream_evt):
                        yield sse
            for sse in batcher.flush():
                yield sse

            # 4. Extract final result
            final_state = await recover_graph.aget_state(recover_config)
            if final_state and final_state.values:
                result = final_state.values
                remaining_messages = result.get("messages", [])

                is_recovered = False
                from chaos_agent.agent.operation_outcome import read_operation_outcome
                result_dict = read_operation_outcome(result).result
                if isinstance(result_dict, dict):
                    is_recovered = result_dict.get("recovered", False)

                result_payload = await build_recover_result_payload(
                    recover_graph,
                    recover_config,
                    record_task_id,
                    inject_task_id,
                    state_values,
                    started_monotonic,
                )
                if result_payload is None:
                    yield StreamEvent(
                        type="error",
                        content="Recover graph completed but no result payload was available",
                        task_id=record_task_id,
                    ).to_sse()
                    yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                    return

                try:
                    from langchain_core.messages import SystemMessage
                    from chaos_agent.memory.tui_session_store import get_global_tui_session_store
                    from chaos_agent.server.routes.sessions import get_store as get_tui_session_store
                    from chaos_agent.server.routes.turn_event_stream import _build_recover_summary_text

                    summary_text = _build_recover_summary_text(
                        result_payload,
                        inject_task_id,
                        state_values,
                    )
                    if summary_text:
                        summary_msg = SystemMessage(content=summary_text)
                        session_meta = (
                            get_tui_session_store().get(inject_tui_session_id)
                            if inject_tui_session_id
                            else None
                        )
                        thread_id = (
                            session_meta.get("conversation_thread_id")
                            if isinstance(session_meta, dict)
                            else ""
                        )
                        intent_graph = agents.get("intent") if isinstance(agents, dict) else None
                        if intent_graph is not None and thread_id:
                            try:
                                await intent_graph.aupdate_state(
                                    {
                                        "configurable": {"thread_id": thread_id},
                                        "recursion_limit": settings.recursion_limit,
                                    },
                                    {
                                        "messages": [summary_msg],
                                        "confirmed_intent": None,
                                        "recover_task_id": None,
                                        "pipeline_task_id": record_task_id,
                                    },
                                    as_node="save_dialogue",
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to write recover-stream summary to Intent Graph",
                                    exc_info=True,
                                )

                        tui_store = get_global_tui_session_store()
                        if tui_store is not None and inject_tui_session_id:
                            tui_store.append_dialogue(inject_tui_session_id, [summary_msg])
                            tui_store.add_task(inject_tui_session_id, record_task_id)
                        if inject_tui_session_id:
                            get_tui_session_store().add_task(inject_tui_session_id, record_task_id)
                except Exception:
                    logger.warning(
                        "Failed to persist recover-stream summary for %s",
                        record_task_id,
                        exc_info=True,
                    )

                if is_recovered:
                    if session_store:
                        try:
                            session_store.finalize_session(
                                record_task_id,
                                remaining_messages=remaining_messages,
                                result_summary=result_payload,
                                status="completed",
                            )
                        except Exception:
                            logger.warning(f"Failed to finalize recover session {record_task_id}")
                else:
                    if session_store:
                        try:
                            session_store.finalize_session(
                                record_task_id,
                                remaining_messages=remaining_messages,
                                result_summary=result_payload,
                                status="failed",
                            )
                        except Exception:
                            logger.warning(f"Failed to finalize recover session {record_task_id}")

                yield StreamEvent(
                    type="result",
                    content=json.dumps(result_payload, ensure_ascii=False),
                    task_id=record_task_id,
                ).to_sse()
            else:
                yield StreamEvent(
                    type="error",
                    content="Recover graph completed but no state available",
                    task_id=record_task_id,
                ).to_sse()

            yield StreamEvent(type="done", task_id=record_task_id).to_sse()

        except Exception as e:
            logger.exception(f"Recover stream failed for task {inject_task_id}")
            yield StreamEvent(
                type="error",
                content=f"Recovery failed: {type(e).__name__}: {e}",
                task_id=record_task_id,
            ).to_sse()
            yield StreamEvent(type="done", task_id=record_task_id).to_sse()
        finally:
            _tsm.end_task_span(record_task_id)
            if session_store and session_store.has_active(record_task_id):
                try:
                    session_store.finalize_session(
                        record_task_id, remaining_messages=[], status="failed",
                    )
                except Exception:
                    pass
            task_tracker.unregister(record_task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=sse_headers,
    )
