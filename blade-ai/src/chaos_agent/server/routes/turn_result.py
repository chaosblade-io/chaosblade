"""Result payload builders for the /turn SSE endpoint."""

from __future__ import annotations

import logging
import time

from chaos_agent.agent.operation_result import (
    build_inject_data_from_state,
    build_recover_data_from_state,
)

logger = logging.getLogger(__name__)


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

    return {
        "status": "success",
        "data": build_recover_data_from_state(
            values,
            recover_task_id,
            inject_state_values,
            elapsed_ms=elapsed_ms,
        ),
    }
