"""Shared session finalization helpers for inject-style graph runs.

API contract for abort-path status words (round-57): these finalize
functions NEVER classify causes themselves — a caller that knows the run
was aborted passes the word already classified through
``stream_abort.abort_row_word`` (interrupt causes → "cancelled", crash
and unknown → "failed", fail-closed) via ``status_override``
(finalize_inject_session) or ``default_status``
(finalize_recover_session). Every other caller passes nothing and the
status is inferred fail-closed from the final state. The drift this
contract closes: inline flag→word / cause→word conditionals at the call
sites — the same defect shape the TaskStore row surface legislated away
in rounds 55/56 (one abort event must carry ONE word onto ALL its
user-visible surfaces).

Verdict-yield contract (round-62 P8 inject / round-63 P8' recover):
the classified word classifies runs still MID-FLIGHT only — a graph
state carrying its own verdict keeps it, and the enforcement lives
INSIDE each finalize function so the guarantee is unconditional for
every caller (the row surface's ``skip_if_terminal`` is the same rule
on the row; both surfaces must hold or the word splits). The gate
spans EVERY ``result_summary_mode``: the recover CLI envelope's status
consumes ``default_status`` directly (round-63 R63-1 — its former
hard-coded "completed" recorded FAILED recoveries as completed,
contradicting the envelope's own ``data.result`` on the same record).
"""

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
        experiment_uid = data.get("experiment_uid") or ""
        return JSONEnvelope.ok(data={
            "task_id": data.get("task_id", ""),
            "result": task_state,
            "fault_type": data.get("fault_type", ""),
            "experiment_uid": experiment_uid,
            "fault_spec": data.get("fault_spec") or {},
            "targets": _targets_from_result_data(data),
            "verification": data.get("verification"),
            "error": data.get("error", ""),
        })
    raise ValueError(f"Unsupported inject session summary mode: {mode}")


def inject_session_status(data: dict[str, Any]) -> str:
    """Map the canonical result projection to the persisted session status.

    ``task_state == "unknown"`` is not a verdict — it is the absence of one
    (``build_unknown_inject_data``: the graph state could not be read at
    finalize time). Mapping it to "completed" (the pre-round-53 behavior)
    recorded the worst unknown as the best known: an interrupted inject
    whose aget_state failed under the scope-cancel path finalized its
    session row as a successful run. Fail-closed, the same rule
    ``terminal_task_state`` applies ("without a verdict the run is failed"
    — no evidence of success must never upgrade to success), and the same
    direction memory_nodes' own comment legislates ("never upgrade a run
    without a verdict to 'completed'", task-ff057e7f). Callers that DO
    know the run was interrupted pass ``status_override`` (the streaming
    routes' cancel exits) — "cancelled" is a KNOWN terminal state, not an
    unknown one, and does not flow through this map.
    """
    task_state = str(data.get("task_state") or "unknown")
    if task_state in {"failed", "rejected", "unknown"} or data.get("error"):
        return "failed"
    return "completed"


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
        from chaos_agent.agent.result.operation_outcome import read_operation_outcome
        from chaos_agent.agent.result.operation_result import build_recover_cli_data_from_state
        from chaos_agent.agent.state import TaskState, infer_task_state

        # Empty values (aget_state failure — finalize_recover_session's
        # except swallows it — or a genuinely empty final state) must NOT
        # fabricate a success claim: "recovered" is a positive verdict,
        # "unverified" is the honest spelling of "no evidence" (round-16
        # S2, same family as the D4/D5 honest-ignorance rulings).
        inferred_state = (
            infer_task_state(recover_values)
            if recover_values
            else TaskState.UNVERIFIED.value
        )
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
    error_log_level: str = "warning",
    precomputed_values: dict | None = None,
    result_summary_mode: str = RESULT_SUMMARY_INJECT_ENVELOPE,
    status_override: str | None = None,
) -> None:
    """Finalize an inject-type session by reading final graph state.

    The state-reading and message-flushing mechanics are shared across CLI
    and server routes. Callers choose the persisted result_summary shape
    through ``result_summary_mode`` to preserve their external compatibility.

    The former ``is_open_conversation`` / ``tui_session_store`` pair (skip
    finalize, route dialogue to the TUI display store) was retired with the
    local converse_stream twin on 2026-09-01: conversations are the server
    /turn route's business and every remaining caller is a blocking
    one-shot — always finalize.

    Override contract (round-62 P8): ``status_override`` classifies runs
    still MID-FLIGHT only — a graph state carrying its own verdict
    (``infer_task_state != "injecting"``) keeps it, regardless of the
    override passed. Callers may pre-gate (the turn stream does, because
    its row-write arm needs the same predicate); this function enforces
    the rule for every caller unconditionally.
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

        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
            build_unknown_inject_data,
        )

        # G6 on the session surface (round-62 R62-1 / P8): the abort word
        # only classifies runs still MID-FLIGHT. A run whose graph already
        # reached its own verdict (verification on record → infer !=
        # "injecting") keeps that verdict — an interrupt racing in during
        # result extraction must not rewrite "injected" to "cancelled".
        # This is the single-source counterpart of the row surface's
        # ``skip_if_terminal`` guard (round-54): same event, same rule,
        # both surfaces. The turn stream gates the override BEFORE the
        # call (its row-write gate needs the same predicate); this internal
        # check makes the guarantee unconditional for every caller —
        # inject_stream's flag arm passes the override unconditionally and
        # is protected here. Empty values (aget_state failed) do NOT
        # suppress the override: the interrupt itself is a KNOWN terminal
        # fact (round-53 ruling), and with no state on record there is no
        # evidence any verdict was reached.
        if status_override and values_fin:
            from chaos_agent.agent.state import infer_task_state

            if infer_task_state(values_fin) != "injecting":
                logger.info(
                    "Session %s reached its own verdict; abort override %r "
                    "yields to inference",
                    session_id, status_override,
                )
                status_override = None

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
            status=status_override or inject_session_status(data),
            progress_ledger=values_fin.get("progress_ledger") if values_fin else None,
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
    """Finalize a recover session and preserve caller-specific summary shape.

    Verdict-yield contract (round-63 P8', the recover-side counterpart of
    the inject surface's round-62 P8): ``default_status`` classifies runs
    still MID-FLIGHT only. A recover graph carrying its own verdict
    (recover_verification on record) keeps it regardless of the
    default_status passed — the G5 fallback and the recover-stream
    disconnect arm pass the abort word unconditionally, and an interrupt
    racing in during result extraction must not rewrite "recovered" to
    "cancelled"/"failed" while the row's skip_if_terminal guard keeps the
    row's verdict (the r55 F2 word split, recover edition). Empty values
    (aget_state failed) do NOT suppress the default: the interrupt itself
    is a KNOWN terminal fact (round-53 ruling).
    """

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

        # G6 on the recover session surface (round-63 P8'): the classified
        # default_status classifies runs still MID-FLIGHT only. A recover
        # graph carrying its own verdict (recover_verification on record)
        # keeps it — the G5 fallback and the recover-stream disconnect arm
        # pass the abort word unconditionally, and an interrupt racing in
        # during result extraction must not rewrite "recovered" to
        # "cancelled"/"failed" (the row's skip_if_terminal already holds
        # the row's verdict; without this gate the session splits from it).
        # The predicate cannot be just "!= 'recovering'": a pre-dispatch
        # recover state (operation not yet written) infers "injecting" —
        # a mid-flight position, not a verdict. The confirmed_intent
        # bridge mapping ("recover" → "completed") cannot fire here: the
        # recover graph's reset policy clears the field (recover_default
        # is None). The verdict-derived word follows the payload path's
        # own polarity (only "failed" lands failed; unverified and
        # partial_recovered are session-level completions, same mapping
        # the turn stream's defensive finalize applies). Double call is
        # deliberate: the pure predicate keeps the I11(p) tooth shape
        # aligned with its inject twin (I11(o)). The gate spans EVERY
        # result_summary_mode — the CLI envelope branch below consumes
        # default_status directly, so excluding it would leave the CLI's
        # failure exits (RECOVERY_FAILED / except in cli/runner.py) free
        # to record a failed recovery as "completed" (round-63 R63-1).
        if default_status and values_fin:
            from chaos_agent.agent.state import infer_task_state

            if infer_task_state(values_fin) not in ("recovering", "injecting"):
                logger.info(
                    "Recover session %s reached its own verdict; "
                    "default_status %r yields to inference",
                    recover_task_id, default_status,
                )
                default_status = (
                    "failed"
                    if infer_task_state(values_fin) == "failed"
                    else "completed"
                )

        status = (
            # CLI envelope: the caller classifies ("completed" on the
            # normal path, "failed" from the failure exits — the caller
            # knows which one ran); the verdict gate above still holds
            # (a verdict reached before the crash keeps its derived word).
            default_status
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
            progress_ledger=values_fin.get("progress_ledger") if values_fin else None,
            # Task-chain persistence: the recover record must point back at
            # its inject task (state carries it via recovery_state; the
            # inject_task_id argument is the fallback). Without this the
            # task json loses the link and post-hoc analysis cannot pair
            # recovery with its fault.
            parent_task_id=str(
                (values_fin.get("parent_task_id") if values_fin else "")
                or inject_task_id
                or ""
            ),
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
