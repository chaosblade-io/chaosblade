"""Shared session finalization helpers for inject-style graph runs."""

from __future__ import annotations

import logging
from typing import Any

from chaos_agent.models.schemas import JSONEnvelope, build_inject_envelope

logger = logging.getLogger(__name__)

RESULT_SUMMARY_INJECT_ENVELOPE = "inject_envelope"
RESULT_SUMMARY_STATUS_ENVELOPE = "status_envelope"
RESULT_SUMMARY_DATA_ENVELOPE = "data_envelope"


def _targets_from_result_data(data: dict[str, Any]) -> list[dict[str, str]]:
    target = data.get("target") if isinstance(data.get("target"), dict) else {}
    names = target.get("names", []) if target else []
    namespace = target.get("namespace", "") if target else ""
    return [{"name": str(name), "namespace": str(namespace)} for name in names]


def build_inject_session_summary(
    data: dict[str, Any],
    *,
    mode: str = RESULT_SUMMARY_INJECT_ENVELOPE,
) -> dict[str, Any]:
    """Build the persisted session ``result_summary`` for an inject run."""

    task_state = str(data.get("task_state") or "unknown")
    if mode == RESULT_SUMMARY_INJECT_ENVELOPE:
        return build_inject_envelope(
            data,
            task_state,
            str(data.get("error") or ""),
        )
    if mode == RESULT_SUMMARY_DATA_ENVELOPE:
        return JSONEnvelope.ok(data=data)
    if mode == RESULT_SUMMARY_STATUS_ENVELOPE:
        return JSONEnvelope.ok(data={
            "task_id": data.get("task_id", ""),
            "result": task_state,
            "fault_type": data.get("fault_type", ""),
            "blade_uid": data.get("blade_uid", ""),
            "targets": _targets_from_result_data(data),
            "verification": data.get("verification"),
            "error": data.get("error", ""),
        })
    raise ValueError(f"Unsupported inject session summary mode: {mode}")


async def finalize_inject_session(
    session_store,
    graph_or_agent,
    config,
    session_id: str,
    kwargs: dict | None = None,
    is_open_conversation: bool | None = None,
    error_log_level: str = "warning",
    precomputed_values: dict | None = None,
    tui_session_store=None,
    result_summary_mode: str = RESULT_SUMMARY_INJECT_ENVELOPE,
) -> None:
    """Finalize an inject-type session by reading final graph state.

    The state-reading and message-flushing mechanics are shared across CLI,
    TUI, and server routes. Callers choose the persisted result_summary shape
    through ``result_summary_mode`` to preserve their external compatibility.
    """

    _ = kwargs  # Kept for API compatibility with existing CLI callers.
    if not session_store:
        return

    try:
        remaining = []
        values_fin = {}

        try:
            if precomputed_values:
                values_fin = precomputed_values
            else:
                final_graph_state = await graph_or_agent.aget_state(config)
                if final_graph_state and final_graph_state.values:
                    values_fin = final_graph_state.values
            remaining = values_fin.get("messages", []) if values_fin else []
        except Exception:
            pass

        if is_open_conversation is True:
            try:
                tui_ses_id = values_fin.get("tui_session_id", "") if values_fin else ""
                if tui_session_store and tui_ses_id:
                    tui_session_store.append_dialogue(tui_ses_id, remaining)
                else:
                    session_store.append_messages(session_id, remaining)
            except Exception:
                logger.debug(
                    "Mid-conversation append failed for %s",
                    session_id,
                    exc_info=True,
                )
            return

        from chaos_agent.agent.operation_result import (
            build_inject_data_from_state,
            build_unknown_inject_data,
        )

        data = (
            build_inject_data_from_state(values_fin, session_id)
            if values_fin
            else build_unknown_inject_data(session_id)
        )
        session_store.finalize_session(
            session_id,
            remaining_messages=remaining,
            result_summary=build_inject_session_summary(
                data,
                mode=result_summary_mode,
            ),
            status="completed",
        )
    except Exception:
        log = logger.warning if error_log_level == "warning" else logger.debug
        log("Failed to finalize session for %s", session_id, exc_info=True)


__all__ = [
    "RESULT_SUMMARY_DATA_ENVELOPE",
    "RESULT_SUMMARY_INJECT_ENVELOPE",
    "RESULT_SUMMARY_STATUS_ENVELOPE",
    "build_inject_session_summary",
    "finalize_inject_session",
]
