"""POST /api/v1/inject - Fault injection endpoint."""

import asyncio
import logging
import uuid

from fastapi import Request

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import inject_router
from chaos_agent.server.schemas import InjectRequest

logger = logging.getLogger(__name__)


@inject_router.post("/inject")
async def inject_fault(request: InjectRequest, req: Request):
    """Inject a fault into a Kubernetes target."""
    task_id = f"task-{uuid.uuid4()}"
    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker

    # Check if server is shutting down
    if task_tracker.is_shutting_down:
        return JSONEnvelope.fail(code=ResponseCode.SERVER_SHUTTING_DOWN, message="Server is shutting down", request_id=getattr(req.state, "request_id", ""))

    # Lifespan deferred ``create_agent`` because LLM config wasn't
    # set yet — the TUI should redirect to the setup wizard rather
    # than receive a 500 from the OpenAIError we'd raise downstream.
    if agents is None:
        return JSONEnvelope.fail(
            code=ResponseCode.NEEDS_SETUP,
            message="LLM config missing; run the setup wizard first.",
            request_id=getattr(req.state, "request_id", ""),
        )

    # Runtime override: kubeconfig/context from request
    if request.kubeconfig:
        settings.kubeconfig_path = request.kubeconfig
    if request.context:
        settings.kube_context = request.context

    # Build initial state. FaultSpec is the single source of truth for
    # fault identity + tuning; consumers read via ``read_fault_spec``.
    spec = FaultSpec.from_http_request(request)
    target_names = list(spec.names)
    initial_state = {
        "task_id": task_id,
        "tui_session_id": "",
        "operation": "inject",
        "fault_spec": spec.to_dict(),
        "needs_confirmation": request.confirm,
        "safety_status": "pending",
        "direct": request.direct,
        "kubeconfig": request.kubeconfig or settings.kubeconfig_path,
        "kube_context": request.context or settings.kube_context,
    }

    # Execute inject graph asynchronously
    config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}

    # Create session for recording
    session_store = agents.get("session_store")
    if session_store:
        session_store.create_session(task_id, operation="inject")

    async def _run_inject():
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)
        try:
            _tsm.start_task_span(task_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(task_id)
            result = await agents["inject"].ainvoke(initial_state, config)
            return result
        except Exception as e:
            logger.exception(f"Inject failed for task {task_id}")

            # Auto-rollback: if blade_create succeeded but graph crashed later,
            # we must destroy the experiment to avoid orphaned faults.
            try:
                current_state = await agents["inject"].aget_state(config)
                if current_state and current_state.values:
                    blade_uid = current_state.values.get("blade_uid", "")
                    kubeconfig = current_state.values.get("kubeconfig", "")
                    if blade_uid:
                        logger.warning(
                            f"Auto-rollback: destroying blade experiment {blade_uid} "
                            f"after inject failure"
                        )
                        from chaos_agent.tools.blade import blade_destroy
                        destroy_result = await blade_destroy.ainvoke(
                            {"uid": blade_uid, "kubeconfig": kubeconfig}
                        )
                        logger.info(f"Auto-rollback result: {destroy_result}")
            except Exception as rb_err:
                logger.error(f"Auto-rollback failed for task {task_id}: {rb_err}")

            return {"error": f"{type(e).__name__}: {e}"}
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
                        final_state = await agents["inject"].aget_state(config)
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
            _tsm.end_task_span(task_id)

    task = asyncio.create_task(_run_inject())
    task_tracker.register(task_id, task)

    def _on_task_done(t):
        task_tracker.unregister(task_id)

    task.add_done_callback(_on_task_done)

    # Build fault_type from structured params
    fault_type = ""
    if request.scope and request.target and request.action:
        fault_type = f"{request.scope}-{request.target}-{request.action}"

    # Return immediate response
    return JSONEnvelope.ok(
        data={
            "task_id": task_id,
            "result": "pending",
            "fault_type": fault_type,
            "targets": [
                {"name": name, "namespace": request.namespace or ""}
                for name in (target_names or [request.target_name or ""])
            ],
        },
        request_id=getattr(req.state, "request_id", ""),
    )
