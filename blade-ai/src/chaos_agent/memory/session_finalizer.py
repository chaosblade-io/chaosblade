"""Shared session finalization helpers for inject-style graph runs."""

from __future__ import annotations

import logging
from typing import Any

from chaos_agent.models.schemas import JSONEnvelope, build_inject_envelope

logger = logging.getLogger(__name__)

RESULT_SUMMARY_INJECT_ENVELOPE = "inject_envelope"
RESULT_SUMMARY_STATUS_ENVELOPE = "status_envelope"
RESULT_SUMMARY_DATA_ENVELOPE = "data_envelope"
RESULT_SUMMARY_RECOVER_PAYLOAD = "recover_payload"
RESULT_SUMMARY_RECOVER_CLI_ENVELOPE = "recover_cli_envelope"


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
            "fault_spec": data.get("fault_spec") or {},
            "targets": _targets_from_result_data(data),
            "verification": data.get("verification"),
            "error": data.get("error", ""),
        })
    raise ValueError(f"Unsupported inject session summary mode: {mode}")


def _recover_status_from_payload(
    result_payload: dict[str, Any] | None,
    *,
    default_status: str = "completed",
) -> str:
    if not isinstance(result_payload, dict):
        return default_status
    envelope_status = str(result_payload.get("status") or "").lower()
    if envelope_status in {"fail", "failed", "error"}:
        return "failed"
    data = result_payload.get("data")
    if not isinstance(data, dict):
        return default_status
    return "failed" if data.get("task_state") == "failed" else "completed"


def build_recover_session_summary(
    recover_values: dict[str, Any],
    *,
    recover_task_id: str,
    inject_task_id: str,
    inject_state_values: dict[str, Any],
    result_payload: dict[str, Any] | None = None,
    mode: str = RESULT_SUMMARY_RECOVER_PAYLOAD,
) -> dict[str, Any] | str:
    """Build the persisted session ``result_summary`` for a recover run."""

    if mode == RESULT_SUMMARY_RECOVER_PAYLOAD:
        if result_payload is not None:
            return result_payload
        return ""

    if mode == RESULT_SUMMARY_RECOVER_CLI_ENVELOPE:
        from chaos_agent.agent.operation_outcome import read_operation_outcome
        from chaos_agent.agent.operation_result import build_recover_cli_data_from_state
        from chaos_agent.agent.state import infer_task_state

        inferred_state = infer_task_state(recover_values) if recover_values else "recovered"
        result_data = build_recover_cli_data_from_state(
            recover_values,
            inject_task_id,
            inject_state_values,
        )
        result_data["result"] = inferred_state
        result_data["error"] = read_operation_outcome(recover_values).error
        return build_inject_envelope(
            result_data,
            inferred_state,
            result_data.get("error", ""),
        )

    raise ValueError(f"Unsupported recover session summary mode: {mode}")


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


async def finalize_recover_session(
    session_store,
    recover_graph,
    recover_config: dict | None,
    recover_task_id: str,
    inject_task_id: str,
    inject_state_values: dict[str, Any] | None = None,
    *,
    result_payload: dict[str, Any] | None = None,
    result_summary_mode: str = RESULT_SUMMARY_RECOVER_PAYLOAD,
    default_status: str = "completed",
    error_log_level: str = "warning",
    precomputed_values: dict[str, Any] | None = None,
) -> None:
    """Finalize a recover session and preserve caller-specific summary shape."""

    if not session_store:
        return

    try:
        remaining = []
        values_fin: dict[str, Any] = {}

        try:
            if precomputed_values is not None:
                values_fin = precomputed_values
            elif recover_graph is not None and recover_config is not None:
                final_graph_state = await recover_graph.aget_state(recover_config)
                if final_graph_state and final_graph_state.values:
                    values_fin = final_graph_state.values
            remaining = values_fin.get("messages", []) if values_fin else []
        except Exception:
            pass

        status = (
            "completed"
            if result_summary_mode == RESULT_SUMMARY_RECOVER_CLI_ENVELOPE
            else _recover_status_from_payload(
                result_payload,
                default_status=default_status,
            )
        )

        session_store.finalize_session(
            recover_task_id,
            remaining_messages=remaining,
            result_summary=build_recover_session_summary(
                values_fin,
                recover_task_id=recover_task_id,
                inject_task_id=inject_task_id,
                inject_state_values=dict(inject_state_values or {}),
                result_payload=result_payload,
                mode=result_summary_mode,
            ),
            status=status,
        )
    except Exception:
        log = logger.warning if error_log_level == "warning" else logger.debug
        log("Failed to finalize recover session %s", recover_task_id, exc_info=True)


__all__ = [
    "RESULT_SUMMARY_DATA_ENVELOPE",
    "RESULT_SUMMARY_INJECT_ENVELOPE",
    "RESULT_SUMMARY_RECOVER_CLI_ENVELOPE",
    "RESULT_SUMMARY_RECOVER_PAYLOAD",
    "RESULT_SUMMARY_STATUS_ENVELOPE",
    "build_inject_session_summary",
    "build_recover_session_summary",
    "finalize_inject_session",
    "finalize_recover_session",
]
