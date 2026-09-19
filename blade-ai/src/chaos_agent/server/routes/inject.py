"""POST /api/v1/inject - Fault injection endpoint."""

import asyncio
import logging

from fastapi import Request

from chaos_agent.agent.spec.fault_spec import DurationParamError, FaultSpec
from chaos_agent.agent.result.operation_result import (
    build_inject_status_data_from_state,
)
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_STATUS_ENVELOPE,
    finalize_inject_session,
)
from chaos_agent.persistence.task_identity import new_inject_task_id
from chaos_agent.models.schemas import JSONEnvelope, ResponseCode
from chaos_agent.server.routes import inject_router
from chaos_agent.server.schemas import InjectRequest

logger = logging.getLogger(__name__)


@inject_router.post("/inject")
async def inject_fault(request: InjectRequest, req: Request):
    """Inject a fault into a Kubernetes target."""
    task_id = new_inject_task_id()
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
    try:
        spec = FaultSpec.from_http_request(request)
    except DurationParamError as e:
        # Duration contract violation is a client input error — answer with
        # the standard envelope instead of an unhandled 500.
        return JSONEnvelope.fail(
            code=ResponseCode.INVALID_PARAMS,
            message=str(e),
            request_id=getattr(req.state, "request_id", ""),
        )
    initial_state = build_inject_initial_state(
        task_id=task_id,
        fault_spec=spec,
        needs_confirmation=request.confirm,
        kubeconfig=request.kubeconfig or settings.kubeconfig_path,
        kube_context=request.context or settings.kube_context,
        kubewiz_cluster_uuid=getattr(request, "cluster_uuid", "") or settings.kubewiz_cluster_uuid,
        kubewiz_profile=getattr(request, "profile", "") or settings.kubewiz_profile,
        kube_connection_mode=getattr(request, "kube_connection_mode", "") or settings.kube_connection_mode,
        host_name=getattr(request, "host_name", "") or getattr(settings, "host_name", ""),
        ssh_host=getattr(request, "ssh_host", "") or getattr(settings, "ssh_host", ""),
        ssh_user=getattr(request, "ssh_user", "") or getattr(settings, "ssh_user", ""),
        ssh_key_path=getattr(request, "ssh_key_path", "") or getattr(settings, "ssh_key_path", ""),
        ssh_port=getattr(request, "ssh_port", None) or getattr(settings, "ssh_port", None),
    )

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
            result = await agents["pipeline"].ainvoke(initial_state, config)

            # Unattended AUTO delegation (mirrors the CLI non-streaming
            # path in cli/runner.py struct-for-struct): with confirm=false
            # the graph pauses at confirmation_gate with no callback to
            # ask — the task would hang forever in a "planned but never
            # executed" limbo. Decide the resume through the shared
            # boundary helper: the manifest is the authority and the guard
            # enforces the per-name boundary, so the answer is always
            # "approved"; a WIDENED contract auto-approval additionally
            # lands in the audit log (no event stream on this route).
            # Like the CLI twin, the resume fires whenever the graph is
            # paused — the interrupt payload is read for the audit log,
            # never as a precondition (a pause without a payload must not
            # fall back into the limbo this block exists to close).
            # confirm=true is the API's client-controlled confirmation
            # contract — keep the pause and let POST /confirm/{task_id}
            # resume it.
            if not request.confirm:
                from langgraph.types import Command
                from chaos_agent.agent.nodes.gates._write_set_boundary import (
                    unattended_resume_value,
                    widened_auto_approval_payload,
                )

                paused = await agents["pipeline"].aget_state(config)
                if paused and paused.next:
                    interrupt_info = None
                    for t in (paused.tasks or []):
                        if getattr(t, "interrupts", None):
                            interrupt_info = t.interrupts[0].value
                            break
                    if widened_auto_approval_payload(interrupt_info) is not None:
                        logger.info(
                            "auto_approved: confirmation_gate delegated a "
                            "widened write-set contract (case manifest "
                            "mechanism_writes beyond victim coverage); "
                            "target_guard enforces the per-name boundary"
                        )
                    result = await agents["pipeline"].ainvoke(
                        Command(resume=unattended_resume_value(interrupt_info)),
                        config,
                    )
            return result
        except Exception as e:
            logger.exception(f"Inject failed for task {task_id}")

            # Auto-rollback: if an injection committed but the graph crashed
            # later, dispatch the fault handle's rollback (by kind, via the
            # provider registry) to avoid orphaned faults. Shared seam with
            # the CLI runner — the single tested implementation lives in
            # cli/session_finalize.py (no route-local twin copy).
            from chaos_agent.cli.session_finalize import auto_rollback

            # abort-safe: see invariants allowlist
            await auto_rollback(agents["pipeline"], config)

            return {"error": f"{type(e).__name__}: {e}"}
        finally:
            # Finalize session: flush remaining messages from final graph state
            # abort-safe: see invariants allowlist
            await finalize_inject_session(
                session_store,
                agents["pipeline"],
                config,
                task_id,
                result_summary_mode=RESULT_SUMMARY_STATUS_ENVELOPE,
            )
            _tsm.end_task_span(task_id)

    task = asyncio.create_task(_run_inject())
    task_tracker.register(task_id, task)

    def _on_task_done(t):
        task_tracker.unregister(task_id)

    task.add_done_callback(_on_task_done)

    # Return immediate response
    return JSONEnvelope.ok(
        data=build_inject_status_data_from_state(
            initial_state,
            task_id,
            result="pending",
            include_experiment_uid=False,
        ),
        request_id=getattr(req.state, "request_id", ""),
    )
