"""POST /api/v1/inject-stream - SSE streaming inject endpoint."""

import asyncio
import json
import logging
import uuid

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.agent.state import extract_ui_diagnostics, strip_side_effects
from chaos_agent.agent.streaming import StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
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

    # Runtime override
    if request.kubeconfig:
        settings.kubeconfig_path = request.kubeconfig
    if request.context:
        settings.kube_context = request.context

    # Build initial state
    if request.input and not all([request.fault_type, request.target_type, request.target_name, request.namespace]):
        initial_state = {
            "task_id": task_id,
            "tui_session_id": "",
            "operation": "inject",
            "target": None,
            "params": request.params or {},
            "needs_confirmation": request.confirm,
            "safety_status": "pending",
            "input": request.input,
            "kubeconfig": request.kubeconfig or settings.kubeconfig_path,
            "kube_context": request.context or settings.kube_context,
        }
        target_names = []
    else:
        target_names = [n.strip() for n in request.target_name.split(",")]
        initial_state = {
            "task_id": task_id,
            "tui_session_id": "",
            "operation": "inject",
            "target": {
                "namespace": request.namespace,
                "resource_type": request.target_type,
                "names": target_names,
            },
            "params": request.params or {},
            "needs_confirmation": request.confirm,
            "safety_status": "pending",
            "kubeconfig": request.kubeconfig or settings.kubeconfig_path,
            "kube_context": request.context or settings.kube_context,
            "created_at": now_iso(),
            "blade_scope": request.scope,
            "blade_target": request.target,
            "blade_action": request.action,
            "direct": request.direct,
            "params_flags": request.params_flags or [],
        }

    config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
    graph = agents["inject"]

    # Create session for recording
    session_store = agents.get("session_store")
    if session_store:
        session_store.create_session(task_id, operation="inject")

    async def event_generator():
        # Register with task tracker for graceful shutdown
        stream_task = asyncio.current_task()
        task_tracker.register(task_id, stream_task)
        try:
            # Stream first invoke
            async for raw_event in graph.astream_events(initial_state, config, version="v2"):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected, aborting stream for task {task_id}")
                    break
                stream_evt = parse_stream_event(raw_event)
                if stream_evt is not None:
                    stream_evt.task_id = task_id
                    yield stream_evt.to_sse()

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
                                yield stream_evt.to_sse()

            # Extract final result
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values
                skill_name = values.get("skill_name", "")
                blade_uid = values.get("blade_uid", "")

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
                from chaos_agent.agent.state import infer_task_state
                safety_status = values.get("safety_status", "unknown")
                result_target = values.get("target", {}) or {}
                blade_params = values.get("params") or {}
                ns = result_target.get("namespace", "") or request.namespace or blade_params.get("namespace", "") or ""
                res_type = result_target.get("resource_type", "") or request.target_type or ""
                names = result_target.get("names", []) or target_names or [request.target_name or ""]

                # Infer correct task_state from full graph state
                task_state = infer_task_state(values)
                if task_state == "injecting":
                    task_state = "injected" if blade_uid else "failed"

                yield StreamEvent(
                    type="result",
                    content=json.dumps(JSONEnvelope.ok(
                        data={
                            "task_id": task_id,
                            "fault_type": request.fault_type or skill_name or "",
                            "targets": [
                                {
                                    "target_type": res_type,
                                    "target_name": name,
                                    "namespace": ns,
                                    "state": task_state,
                                    "blade_uid": blade_uid,
                                }
                                for name in names
                            ],
                            "params": request.params,
                            "plan_summary": values.get("plan_summary", ""),
                            "needs_confirm": request.confirm,
                            "verification": strip_side_effects(values.get("verification")),
                            "created_at": now_iso(),
                            "estimated_duration_ms": 0,
                            **extract_ui_diagnostics(values),
                        },
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
            # Finalize session: flush remaining messages from final graph state
            if session_store:
                try:
                    remaining = []
                    verification = None
                    blade_uid = ""
                    target = None
                    skill_name_fin = ""
                    error_fin = ""
                    failure_reason_fin = ""
                    blade_params = {}
                    values_fin = {}
                    try:
                        final_state = await graph.aget_state(config)
                        if final_state and final_state.values:
                            values_fin = final_state.values
                            remaining = values_fin.get("messages", [])
                            verification = values_fin.get("verification")
                            blade_uid = values_fin.get("blade_uid", "")
                            target = values_fin.get("target")
                            skill_name_fin = values_fin.get("skill_name", "")
                            error_fin = values_fin.get("error") or ""
                            failure_reason_fin = values_fin.get("failure_reason") or ""
                            blade_params = values_fin.get("params") or {}
                    except Exception:
                        pass
                    from chaos_agent.models.schemas import JSONEnvelope
                    from chaos_agent.memory.session_store import build_verification_simple
                    from chaos_agent.agent.state import infer_task_state

                    inferred_state = infer_task_state(values_fin) if values_fin else "unknown"
                    if inferred_state == "injecting":
                        inferred_state = "injected" if blade_uid else "failed"

                    fault_type_fin = ""
                    if blade_params:
                        _s = blade_params.get("scope", "")
                        _a = blade_params.get("action", "")
                        _t = blade_params.get("target", "")
                        if _s and _t and _a:
                            fault_type_fin = f"{_s}-{_t}-{_a}"
                    if not fault_type_fin:
                        fault_type_fin = skill_name_fin

                    merged_error_fin = failure_reason_fin or error_fin or ""
                    names = target.get("names", []) if target else []
                    ns = target.get("namespace", "") if target else ""
                    ns = ns or blade_params.get("namespace", "")
                    session_store.finalize_session(
                        task_id,
                        remaining_messages=remaining,
                        result_summary=JSONEnvelope.ok(data={
                            "task_id": task_id,
                            "result": inferred_state,
                            "fault_type": fault_type_fin,
                            "blade_uid": blade_uid,
                            "targets": [{"name": n, "namespace": ns} for n in names],
                            "verification": build_verification_simple(verification),
                            "error": merged_error_fin,
                        }),
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
