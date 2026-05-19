"""POST /api/v1/recover - Fault recovery endpoint."""

import logging
import uuid

from fastapi import Request

from chaos_agent.config.settings import settings
from chaos_agent.models.schemas import ResponseCode
from chaos_agent.server.routes import recover_router
from chaos_agent.server.schemas import RecoverRequest

logger = logging.getLogger(__name__)


@recover_router.post("/recover")
async def recover_fault(request: RecoverRequest, req: Request):
    """Recover a fault injection by task ID.

    Uses the recover graph which includes two-layer verification:
    - Layer 1: blade_destroy + blade_status confirmation
    - Layer 2: LLM reads skill's recovery verification instructions
    """
    agents = req.app.state.agents
    inject_task_id = request.task_id
    # recover gets its own record file; parent_task_id cross-refs the inject
    record_task_id = f"task-{uuid.uuid4()}"
    req_id = getattr(req.state, "request_id", "")

    config = {"configurable": {"thread_id": inject_task_id}, "recursion_limit": settings.recursion_limit}

    try:
        # Get current state from inject graph checkpoint
        current_state = await agents["inject"].aget_state(config)
        if not current_state or not current_state.values:
            return JSONEnvelope.fail(code=ResponseCode.TASK_NOT_FOUND, message=f"Task not found: {inject_task_id}", request_id=req_id)

        state_values = current_state.values
        blade_uid = state_values.get("blade_uid", "")
        target = state_values.get("target", {}) or {}
        skill_name = state_values.get("skill_name", "")
        kubeconfig = state_values.get("kubeconfig", "")
        inject_tui_session_id = state_values.get("tui_session_id", "") or ""

        # Build inject context from inject-phase messages for recover LLM
        # Reformatted: raw kubectl outputs are abstracted to prevent
        # "causal chain illusion" (LLM reusing stale inject-phase data as
        # current post-recovery evidence instead of calling kubectl).
        # See utils/inject_context.py for rationale and implementation.
        from chaos_agent.utils.inject_context import build_inject_context
        inject_msgs = state_values.get("messages", [])
        inject_context = build_inject_context(inject_msgs)

        # Build initial state for recover graph
        # Explicitly clear verification/messages to prevent inject graph
        # checkpoint state from leaking into the recover verifier loop.
        initial_state = {
            "task_id": record_task_id,
            "tui_session_id": inject_tui_session_id,
            "parent_task_id": inject_task_id,
            "operation": "recover",
            "blade_uid": blade_uid,
            "skill_name": skill_name,
            "skill_case_content": state_values.get("skill_case_content", ""),
            "inject_verification_summary": state_values.get("inject_verification_summary", ""),
            "inject_context": inject_context,
            "target": target,
            "kubeconfig": kubeconfig,
            "injection_method": state_values.get("injection_method"),
            "kubectl_exec_pod_name": state_values.get("kubectl_exec_pod_name"),
            "created_at": state_values.get("created_at", ""),  # Preserve inject's created_at
            "verifier_loop_count": 0,
            "verification": None,       # Clear inject graph's verification
            "recover_verification": None,  # Clear stale recover verification
            "messages": [],             # Clear inject graph's conversation
            "inject_layer1_cache": None,   # Clear inject layer1 cache
            "recover_layer1_cache": None,  # Clear stale recover layer1 cache
        }

        # Create session for recording, passing inject messages as baseline
        # so that inherited inject messages are excluded from the recover session
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

        # Execute recover graph (includes two-layer verification)
        result = await agents["recover"].ainvoke(initial_state, config)

        # Extract remaining messages from final graph state for session flush
        remaining_messages = []
        if isinstance(result, dict):
            remaining_messages = result.get("messages", [])

        # Extract verification results
        is_recovered = False
        recovery_level = "recovered"
        verification = None
        if isinstance(result, dict):
            is_recovered = result.get("result", {}).get("recovered", False)
            recovery_level = result.get("result", {}).get("recovery_level", "recovered")
            verification = result.get("recover_verification")

        # Build targets info (fallback to inject graph params for namespace)
        names = target.get("names", []) if target else []
        ns = target.get("namespace", "") if target else ""
        if not ns:
            inject_params = state_values.get("params") or {}
            ns = inject_params.get("namespace", "")

        from chaos_agent.memory.session_store import build_verification_simple

        if not is_recovered:
            # Extract failure_reason from graph result
            failure_reason = result.get("failure_reason") if isinstance(result, dict) else None
            merged_error = failure_reason or result.get("error") or "Recovery verification failed" if isinstance(result, dict) else "Recovery verification failed"
            if session_store:
                try:
                    from chaos_agent.models.schemas import JSONEnvelope
                    from chaos_agent.memory.session_store import build_verification_simple
                    session_store.finalize_session(
                        record_task_id,
                        remaining_messages=remaining_messages,
                        result_summary=JSONEnvelope.fail(
                            code=ResponseCode.RECOVERY_FAILED,
                            message=merged_error,
                            data={
                                "task_id": inject_task_id,
                                "result": "failed",
                                "blade_uid": blade_uid,
                                "targets": [{"name": name, "namespace": ns} for name in names],
                                "verification": build_verification_simple(verification),
                                "error": merged_error,
                            },
                        ),
                        status="failed",
                    )
                except Exception:
                    logger.warning(f"Failed to finalize recover session {record_task_id}")
            recover_fail_data = {
                "task_id": inject_task_id,
                "result": "failed",
                "blade_uid": blade_uid,
                "targets": [{"name": name, "namespace": ns} for name in names],
                "error": failure_reason or "Recovery verification failed",
            }
            return JSONEnvelope.fail(
                code=ResponseCode.NO_BLADE_UID,
                message="Recovery verification failed",
                request_id=req_id,
                data=recover_fail_data,
            )

        if session_store:
            try:
                from chaos_agent.models.schemas import JSONEnvelope
                from chaos_agent.memory.session_store import build_verification_simple
                session_store.finalize_session(
                    record_task_id,
                    remaining_messages=remaining_messages,
                    result_summary=JSONEnvelope.ok(data={
                        "task_id": inject_task_id,
                        "result": recovery_level,
                        "blade_uid": blade_uid,
                        "targets": [{"name": name, "namespace": ns} for name in names],
                        "verification": build_verification_simple(verification),
                        "error": "",
                    }),
                    status="completed",
                )
            except Exception:
                logger.warning(f"Failed to finalize recover session {record_task_id}")
        return JSONEnvelope.ok(
            data={
                "task_id": inject_task_id,
                "result": recovery_level,
                "blade_uid": blade_uid,
                "targets": [{"name": name, "namespace": ns} for name in names],
                "verification": build_verification_simple(verification),
            },
            request_id=req_id,
        )

    except Exception as e:
        logger.exception(f"Recover failed for task {inject_task_id}")
        if session_store:
            try:
                session_store.finalize_session(record_task_id, remaining_messages=[], status="failed")
            except Exception:
                logger.warning(f"Failed to finalize recover session {record_task_id}")
        return JSONEnvelope.fail(
            code=ResponseCode.RECOVERY_FAILED,
            message=f"Recovery failed: {type(e).__name__}: {e}",
            request_id=req_id,
            data={
                "task_id": inject_task_id,
                "result": "failed",
                "blade_uid": blade_uid or "",
                "targets": [],
                "error": f"internal_error: Recovery failed: {type(e).__name__}: {e}",
            },
        )
