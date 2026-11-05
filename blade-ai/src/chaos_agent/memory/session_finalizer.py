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

Fail-closed floor (round-64 F1): "no classified word" and "the run
completed" are different facts, and the recover surface's default used
to conflate them — ``default_status`` defaulted to "completed", while
the CLI's expression could spell only two of the three terminal cases
(success / failure; an interrupt is a BaseException, invisible to the
boolean it keyed on). An interrupted ``blade-ai recover`` therefore
recorded its session as "completed" while the same record's envelope
said "recovering". The default is now None ("the caller has no word")
and a caller with nothing to say falls through to
``_recover_session_status``, whose floor is "failed": no evidence of
success must never upgrade to success — the same polarity
``inject_session_status`` and ``terminal_task_state`` already enforce
on the inject surface.
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
) -> str | None:
    """The session word the result payload itself spells, or None.

    ``None`` = no payload on record (every abort path and the whole CLI
    never build one) or a payload carrying no data dict — the caller
    then falls through to the graph state's own verdict instead of
    reading the absence as a completion.
    """

    if not isinstance(result_payload, dict):
        return None
    envelope_status = str(result_payload.get("status") or "").lower()
    if envelope_status in {"fail", "failed", "error"}:
        return "failed"
    data = result_payload.get("data")
    if not isinstance(data, dict):
        return None
    return "failed" if data.get("task_state") == "failed" else "completed"


def _recover_verdict_word(values_fin: dict[str, Any]) -> str | None:
    """The word a recover graph state's OWN verdict spells, or None.

    ``None`` means the state carries no verdict — still mid-flight.
    Both mid-flight markers are excluded, not just "recovering": a
    pre-dispatch recover state (operation not yet written) infers
    "injecting", which is a position on the way, not a verdict. The
    polarity is the payload path's own — only "failed" lands failed;
    unverified and partial_recovered are session-level completions.
    """

    if not values_fin:
        return None
    from chaos_agent.agent.state import infer_task_state

    state = infer_task_state(values_fin)
    if state in ("recovering", "injecting"):
        return None
    return "failed" if state == "failed" else "completed"


def _recover_session_status(
    values_fin: dict[str, Any],
    result_payload: dict[str, Any] | None,
    override: str | None,
) -> str:
    """The recover session word for a run with no classified word of its own.

    Priority: the caller's classified word (when it has one — the
    payload-mode callers and the CLI's own classified arms) > the result
    payload's envelope word > the graph state's own verdict > the
    fail-closed floor. The floor is "failed", NEVER "completed": a run
    that left no evidence of success must not be recorded as one. A
    mid-flight interrupt (Ctrl-C / SIGTERM / scope cancel) arrives here
    with no override, no payload and a "recovering" state, and the
    former "completed" floor recorded exactly that run as a success —
    while the same record's envelope said "recovering", the word split
    the row surface legislated away in rounds 55/56, session edition
    (round-64 F1).
    """

    if override:
        return override
    payload_word = _recover_status_from_payload(result_payload)
    if payload_word is not None:
        return payload_word
    verdict_word = _recover_verdict_word(values_fin)
    if verdict_word is not None:
        return verdict_word
    return "failed"


def build_recover_session_summary(
    recover_values: dict[str, Any],
    *,
    recover_task_id: str,
    inject_task_id: str,
    inject_state_values: dict[str, Any],
    result_payload: dict[str, Any] | None = None,
    mode: str = RESULT_SUMMARY_RECOVER_PAYLOAD,
    error_override: str | None = None,
) -> dict[str, Any] | str:
    """Build the persisted session ``result_summary`` for a recover run."""

    if mode == RESULT_SUMMARY_RECOVER_PAYLOAD:
        if result_payload is not None:
            return result_payload
        # a82984a3 root cure (PAYLOAD arm — the LAST recover persistence face
        # that bypassed the durable-evidence floor). PAYLOAD mode echoes a
        # caller-pre-built card, and the ONLY caller that builds one
        # (build_recover_result_payload, server NORMAL completion) never runs
        # on a crash — so the recover_stream / turn_event_stream abort arms and
        # every l4 fallback landed here with result_payload=None and persisted
        # "", discarding the recover_layer1_cache proof that the destroy DID
        # land. That is the same provenance inversion the inject arm already
        # cured: finalize_inject_session ALWAYS rebuilds ``data`` from values_fin
        # through build_inject_data_from_state → terminal_task_state (floor)
        # BEFORE mode dispatch, so no inject mode can bypass it. Mirror that
        # invariant here — derive the card from the DURABLE final state through
        # the SAME single-source renderer the normal path uses
        # (build_recover_data_from_state → recover_task_state_from_values →
        # Route B floor) — so the floor is structurally unavoidable for EVERY
        # PAYLOAD-mode caller, present and future, instead of resting on each
        # crash arm remembering to pre-build a payload (round-64 F1 boundary
        # preserved: empty values, i.e. aget_state failed, keep the honest "" —
        # no evidence, no upgrade; the floor never claims "recovered", a
        # verdict-less crash WITH Layer-1 proof spells "unverified", WITHOUT
        # proof "failed"). Fail-soft: finalize_recover_session's outer except
        # aborts the WHOLE persist, so a builder error must degrade to the old
        # "" rather than lose the session row entirely.
        if not recover_values:
            return ""
        try:
            from chaos_agent.agent.result.operation_result import (
                build_recover_data_from_state,
            )

            data = build_recover_data_from_state(
                recover_values, recover_task_id, inject_state_values,
            )
        except Exception:
            logger.debug(
                "Recover PAYLOAD summary floor-derive failed for %s",
                recover_task_id, exc_info=True,
            )
            return ""
        # Polarity matches build_inject_envelope / _recover_status_from_payload:
        # only a "failed" verdict lands fail; unverified / partial_recovered /
        # recovered are session-level completions (round-64 F1).
        return {
            "status": "fail" if data.get("task_state") == "failed" else "success",
            "data": data,
        }

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
        outcome = read_operation_outcome(recover_values)
        # Position face (round-64 F1) meets the durable-evidence floor
        # (a82984a3 root cure, recover arm). With no verdict infer_task_state
        # answers "recovering" — the run's POSITION. But a crash AFTER the
        # destroy landed leaves durable Layer-1 proof in the checkpoint, and the
        # honest position then is "unverified" (the run ended mid-adjudication
        # AFTER the recovery action, not mid-flight), not the optimistic
        # in-progress word. Lift ONLY on that durable proof; with no proof keep
        # "recovering" verbatim — round-64 F1's no-evidence-no-upgrade boundary
        # (session status "failed" vs envelope "recovering", the two faces not
        # contradicting) is untouched. The floor never claims "recovered".
        #
        # The lift fills ONLY the silence a CRASH leaves, so it is gated on the
        # ABSENCE of a deliberate terminal classification (``failure_reason``) —
        # the SAME gate ``recover_task_state_from_values`` (Route B) applies. A
        # budget-cut recovery writes RECOVERY_VERIFICATION_TIMEOUT /
        # WALL_CLOCK_TIMEOUT (W-56-8): the node has spoken "failure", and both
        # faces must honour it rather than one lifting to "unverified" while the
        # other stays "failed". A crashed node writes no reason at all.
        if (
            recover_values
            and inferred_state == TaskState.RECOVERING.value
            and not outcome.failure_reason
        ):
            from chaos_agent.agent.result.operation_outcome import (
                synthesize_recover_verification_floor,
            )

            if synthesize_recover_verification_floor(recover_values) is not None:
                inferred_state = TaskState.UNVERIFIED.value
        result_data = build_recover_cli_data_from_state(
            recover_values,
            inject_task_id,
            inject_state_values,
        )
        result_data["result"] = inferred_state
        result_data["error"] = outcome.error
        # Reason channel (recover edition of finalize_inject_session's
        # error_override): the recover graph is the single source of WHY the
        # recovery ended, but an uncaught exception kills the node before it
        # writes — the surviving CLI/server except arm deposits the cause it
        # already built. Graph state wins: fill only a reason-less projection.
        if error_override and not result_data["error"]:
            result_data["error"] = error_override
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
    error_override: str | None = None,
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

    Reason channel (``error_override``): the graph state is the single
    authoritative source of WHY a run ended — every normal terminal exit
    (reject, mark_loop_exhausted, mark_wall_clock_timeout, planning extract,
    safety_check victim-absent) writes failure_reason/error into it. An
    UNCAUGHT exception is the one ending the run cannot record itself: the
    node dies before writing, the checkpoint freezes at the prior superstep,
    and the projection carries an empty reason that build_inject_envelope's
    fail-closed branch collapses to the bare "Injection failed" (code 4002)
    — the durable record loses a cause the surviving orchestrator actually
    witnessed. ``error_override`` is that orchestrator's testimony: the
    CLI/server except arm deposits the reason it already built for the user.
    Graph state ALWAYS wins — the override fills ONLY a reason-less
    projection (same precedence as status_override: harness testimony fills
    the participant's silence, never overwrites its last words), so any run
    that recorded its own verdict is untouched.
    """

    _ = kwargs  # Kept for API compatibility with existing CLI callers.
    if not session_store:
        return

    try:
        remaining = []
        values_fin = {}
        snapshot = None

        # Snapshot read is best-effort and ISOLATED: a caller that already
        # holds the values (``precomputed_values`` — the turn route's finally
        # arm) must not lose them just because this second ``aget_state``
        # fails or the caller passed no graph at all. The snapshot is needed
        # only for the pause guard below; the values drive everything else.
        if graph_or_agent is not None:
            try:
                snapshot = await graph_or_agent.aget_state(config)
            except Exception:
                snapshot = None

        if precomputed_values:
            values_fin = precomputed_values
        elif snapshot and snapshot.values:
            values_fin = snapshot.values
        remaining = values_fin.get("messages", []) if values_fin else []

        # Round-64 F3: a run parked at the confirmation gate has NOT ended —
        # ``blade-ai confirm --task-id <id>`` / ``POST /confirm/{task_id}``
        # continues it. Finalizing here archived it as a finished failure
        # (``inject_session_status`` on a verdict-less projection is
        # fail-closed by design), released the in-memory session so no later
        # writer could correct the word, and blinded the turn route's
        # defensive arm (which gates on ``has_active``). Flush the dialogue
        # and keep the session OPEN: the resume path's own terminal point
        # (``save_memory``) is the closer.
        from chaos_agent.agent.state import resumable_pause

        if resumable_pause(snapshot):
            logger.info(
                "Session %s paused at an interrupt boundary; keeping it "
                "active for the resume path (no terminal word written)",
                session_id,
            )
            if remaining and session_store.has_active(session_id):
                try:
                    session_store.append_messages(session_id, remaining)
                except Exception:
                    logger.debug(
                        "Pre-resume message flush failed for %s",
                        session_id, exc_info=True,
                    )
            return

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
            build_inject_data_from_state(values_fin, session_id, snapshot=snapshot)
            if values_fin
            else build_unknown_inject_data(session_id)
        )
        # Reason channel (see the error_override contract above): fill the
        # participant's silence, never its last words — only a reason-less
        # projection takes the orchestrator's testimony. Every mode below
        # (inject_envelope / data_envelope / status_envelope) consumes
        # data["error"], and inject_session_status reads it too, so this one
        # point carries the cause into the message, the persisted data, and
        # the status word alike.
        if error_override and not data.get("error"):
            data["error"] = error_override

        # Structural terminal-row stamp (single un-bypassable chokepoint).
        # EVERY inject exit — clean finish, uncaught-exception crash,
        # cancel/disconnect abort — routes through THIS finally, and
        # ``data["task_state"]`` is already the floor-aware terminal word
        # (build_inject_data_from_state → terminal_task_state). Stamping the
        # durable TaskStore row HERE closes it by construction on every path,
        # so no exit has to remember its own row write. That per-path
        # hand-wiring was the defect: the CLI's generic-exception crash arm
        # and the server's non-stream inject route both forgot it, leaving a
        # zombie "injecting" row (the node-level upsert froze at the last
        # successful superstep; no later writer ever came). The word is the
        # SAME one the session record and the crash envelope already carry —
        # one event, one word, all surfaces (rounds 55-57 contract, now
        # structural instead of per-site). Ordering matches the aborted-row
        # rationale: the row is the user-facing fact (boot card / /tasks read
        # it), the session record is derived, so a failure in the latter must
        # not swallow the former.
        #
        # Guards, all pre-existing and preserved:
        #  • resumable_pause returned above → a run parked at the
        #    confirmation gate is never stamped (its row stays active for the
        #    resume path, round-64 F3).
        #  • status_override already yielded to a reached verdict above
        #    (round-62 P8), so an abort racing a finished run cannot rewrite
        #    its word; skip_if_terminal is the row surface's same rule
        #    (round-54 G6) and also makes the clean path an idempotent no-op
        #    (the final node's upsert already landed the terminal word).
        #  • a word outside the column domain ("unknown" — the graph state
        #    could not be read) is not a verdict: skip, never reject.
        from chaos_agent.agent.state import TASK_STATE_COLUMN_VALUES

        _row_word = status_override or str(data.get("task_state") or "")
        if _row_word in TASK_STATE_COLUMN_VALUES:
            from chaos_agent.server.routes.stream_abort import (
                write_aborted_task_row,
            )

            await write_aborted_task_row(session_id, _row_word)

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
    default_status: str | None = None,
    error_log_level: str = "warning",
    precomputed_values: dict[str, Any] | None = None,
    error_override: str | None = None,
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

    No-word contract (round-64 F1): ``default_status`` DEFAULT is None —
    "the caller has no classified word", never "the run completed". A
    caller with nothing to say falls through to ``_recover_session_status``,
    whose floor is "failed" (fail-closed): a run that left no evidence of
    success is never recorded as one. The former "completed" default made
    every caller that forgot to classify — the CLI recover paths on their
    interrupt exits, and any future entry point — record a mid-flight
    Ctrl-C/SIGTERM as a completed recovery, contradicting the same
    record's own envelope ("recovering").
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
            # normal path, "failed"/"cancelled" from the failure and
            # interrupt exits — the caller knows which one ran); the
            # verdict gate above still holds (a verdict reached before
            # the crash keeps its derived word). A caller with NO word
            # falls through to the evidence ladder instead of this leg
            # (round-64 F1): the interrupt exits that never classified
            # used to land right here on the old "completed" default and
            # recorded a mid-flight Ctrl-C/SIGTERM as a success.
            default_status
            if result_summary_mode == RESULT_SUMMARY_RECOVER_CLI_ENVELOPE
            and default_status
            else _recover_session_status(values_fin, result_payload, default_status)
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
                error_override=error_override,
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
