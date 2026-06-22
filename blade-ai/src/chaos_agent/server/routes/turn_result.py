"""Result payload builders for the /turn SSE endpoint."""

from __future__ import annotations

import logging
import time

from chaos_agent.agent.state import (
    extract_ui_diagnostics,
    infer_task_state,
    strip_side_effects,
)
from chaos_agent.agent.operation_outcome import (
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
    read_verification_side_effects,
)

logger = logging.getLogger(__name__)


def build_inject_data_from_state(
    values: dict,
    task_id: str,
    *,
    elapsed_ms: int = 0,
) -> dict:
    """Build a unified inject result data dict from graph state values.

    All result construction paths (CLI runner, SSE stream, TUI turn)
    should call this to avoid field set divergence.
    """
    from chaos_agent.agent.fault_spec import (
        fault_type_from_state,
        legacy_params_dict,
        legacy_target_dict,
    )

    task_state = infer_task_state(values)
    if task_state == "injecting":
        task_state = "injected" if values.get("blade_uid") else "failed"

    fault_type = fault_type_from_state(values)

    blade_uid = values.get("blade_uid", "") or ""
    target = legacy_target_dict(values)
    params = legacy_params_dict(values)
    verification = read_inject_verification(values)
    outcome = read_operation_outcome(values)

    diagnostics: dict = {}
    try:
        diagnostics = extract_ui_diagnostics(values) or {}
    except Exception:
        pass

    return {
        "task_id": task_id,
        "task_state": task_state,
        "fault_type": fault_type,
        "blade_uid": blade_uid,
        "duration_ms": elapsed_ms,
        "target": target,
        "params": params,
        "verification": strip_side_effects(verification),
        "side_effects": read_verification_side_effects(verification),
        "postmortem": outcome.postmortem,
        "error": outcome.error,
        **diagnostics,
    }


async def build_recover_initial_from_store(
    task_id: str,
    rec_task_id: str,
    tui_session_id: str,
    agents: dict,
) -> dict | None:
    """Build recover_initial from task_store, bypassing LangGraph checkpoint.

    Used for cross-session TUI recovery where the checkpoint is stored
    under conversation_thread_id (not task_id) and can't be looked up.
    """
    from chaos_agent.agent.task_snapshot import resolve_recover_initial_state

    resolution = await resolve_recover_initial_state(
        task_id,
        record_task_id=rec_task_id,
        agents=agents,
        tui_session_id=tui_session_id,
    )
    if resolution is None:
        return None
    return resolution.initial_state


async def build_result_payload(
    graph,
    config: dict,
    task_id: str,
    started_monotonic: float,
) -> dict | None:
    """Read final graph state and shape it into a ResultCard envelope.

    Returns ``None`` when nothing operational happened (chat /
    capability Q&A / ambiguous turns with no plan and no blade_uid).
    """
    try:
        final_state = await graph.aget_state(config)
    except Exception:
        logger.debug("aget_state failed during result extraction", exc_info=True)
        return None
    if not final_state or not final_state.values:
        return None

    if final_state.next:
        logger.debug(
            "graph still paused at %s; suppressing result envelope",
            list(final_state.next),
        )
        return None

    values = final_state.values

    if values.get("confirmed_intent") != "inject":
        return None

    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)
    state_task_id = values.get("task_id") or ""
    real_task_id = state_task_id if isinstance(state_task_id, str) and state_task_id else task_id

    data = build_inject_data_from_state(values, real_task_id, elapsed_ms=elapsed_ms)
    return {"status": "success", "data": data}


async def build_recover_result_payload(
    recover_graph,
    recover_config: dict,
    recover_task_id: str,
    inject_task_id: str,
    inject_state_values: dict,
    started_monotonic: float,
) -> dict | None:
    """Build a result card payload for recover_graph completion."""
    try:
        final = await recover_graph.aget_state(recover_config)
    except Exception:
        return None
    if not final or not final.values:
        return None

    values = final.values
    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)

    is_recovered = False
    recovery_level = "failed"
    outcome = read_operation_outcome(values)
    result_dict = outcome.result
    if isinstance(result_dict, dict):
        is_recovered = result_dict.get("recovered", False)
        recovery_level = result_dict.get("recovery_level", "recovered" if is_recovered else "failed")

    if not is_recovered:
        task_state = "failed"
    elif recovery_level == "partial":
        task_state = "partial_recovered"
    else:
        task_state = "recovered"

    blade_uid = inject_state_values.get("blade_uid", "")
    from chaos_agent.agent.fault_spec import (
        fault_type_from_state,
        legacy_params_dict,
        legacy_target_dict,
    )
    target = legacy_target_dict(inject_state_values)
    params = legacy_params_dict(inject_state_values)

    return {
        "status": "success",
        "data": {
            "task_id": recover_task_id,
            "operation": "recover",
            "task_state": task_state,
            "fault_type": fault_type_from_state(inject_state_values),
            "blade_uid": blade_uid,
            "duration_ms": elapsed_ms,
            "target": target,
            "params": params,
            "verification": strip_side_effects(read_recover_verification(values)),
        },
    }
