"""Shared abort-event semantics for the SSE StreamingResponse modules.

Round-54 legislation: ONE abort event — detected by whichever mechanism
wins the race (scope-cancel, ``is_disconnected`` poll, internal error),
in whichever module it lands — must converge to the SAME terminal
semantics. Before this module each stream answered the three questions
independently, and the race decided the record:

  * r48 live probe: the scope cancel almost always WINS the
    is_disconnected poll race on a real client disconnect.
  * r54 G1/G2: but the poll does win sometimes (server-side cancel,
    fast disconnects, proxies), and the poll path in inject_stream /
    recover_stream was a silent ``break`` into the NORMAL completion
    path — no cancelled flag, no TaskStore row write, no
    status_override, and (inject) it kept driving the unattended
    auto-approve resume with a dead client.
  * r54 G3: the turn twin's own poll exit ran the abort chain but
    never set the flag its finally-block terminal triad keys on.

The shared vocabulary:

  ``ClientDisconnected`` — the poll-side abort signal. Raising it from
      the streaming loop converts the poll into a first-class abort
      exit with a handler of its own, exactly symmetric to the
      scope-cancel exit (turn_event_stream has raised it since r48;
      its class definition now lives here, re-exported there).

  ``ABORT_SQLITE_CEILING_S`` — every abort-path shield is bounded (the
      r49 ruling, generalised r54/F5): the "millisecond local-SQLite"
      no-ceiling exemption was checkpointer-shaped (AsyncSqliteSaver,
      verified r53) and the shields carry aget_state — a checkpointer
      READ — so a future remote checkpointer re-opens the r49 hang
      surface. The SQLite-class bound is generous (30s ≫ any realistic
      local write) because hitting it means the environment is already
      pathological; the loss at the ceiling is bounded abandon, the
      same trade the vehicle-class ceiling (90s) legislated.

  ``ABORT_INTERRUPT_CAUSES`` / ``abort_row_word`` — the terminal-word
      taxonomy (round-55 F1/F2; jurisdiction moved here round-56): the
      interrupt causes (user-side termination) map to "cancelled",
      crash causes and anything unknown to "failed". Declared beside
      the row writer because a taxonomy declared in any ONE stream
      module leaves the other two free to grow private mappings —
      which is exactly what the round-56 census found: recover_stream
      carried an inline cause→word conditional with the OPPOSITE
      unknown-cause polarity (unknown → "cancelled", fail-open — an
      unknown cause understated as a user cancel), and inject_stream
      carried an inline flag→word conditional at the write site (the
      r54 G4 defect shape, flag edition).

  ``write_aborted_task_row`` — the guarded terminal write for the
      TaskStore row, shared by all three streams' abort paths (r54
      G4/G5: the r53 triad was wired for the cancel exits only — a
      crashed or poll-aborted run left its row a zombie at the last
      mid-graph upsert, "injecting" forever in /tasks). Guarded:
      refuses to regress a row that already reached its OWN terminal
      word (r54 G6 — a cancel landing during result extraction, after
      the pipeline completed, must not overwrite "completed" with
      "cancelled"), and retries on the SQLite lock-contention family
      before giving up with a loud warning.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class ClientDisconnected(Exception):
    """Raised when the SSE client disconnects mid-stream.

    The poll-side twin of the scope-level cancellation a real client
    disconnect delivers inside the Starlette task group. Both exits
    must land in the same terminal semantics — before round-54 the
    inject/recover polls silently ``break``-ed into the normal
    completion path, and which semantics a disconnect got was decided
    by the poll-vs-cancel RACE (r48: the cancel usually wins, but "the
    race picks the record" is a defect, not a coin flip).
    """


# Ceiling for abort-path shields whose awaits are local-SQLite class
# (checkpointer aget_state/aupdate_state, TaskStore upserts). Generous:
# any realistic local write is milliseconds; hitting this bound means
# the environment is pathological and the remaining cleanup is
# abandoned (bounded loss) rather than holding the task group — and a
# shutdown-waiting uvicorn — hostage. See the module docstring for why
# the former "no ceiling" ruling (r49) was checkpointer-shaped.
ABORT_SQLITE_CEILING_S = 30.0

# SQLite writes under abort conditions can transiently collide with the
# graph's own writes (the lock-contention family round-51's dispatched
# clear already armours against). A handful of fast retries absorbs the
# collision; persistent failure gives up with a loud warning — the
# recover graph's evidence-based discovery (may_carry_live_fault) does
# not depend on any of these words.
_SQLITE_RETRY_ATTEMPTS = 6
_SQLITE_RETRY_DELAY_S = 0.05


# Causes that terminate a run from the USER'S side: the run stopped
# because a person walked away — cancelled it, dropped the connection,
# or never answered the confirmation gate. Orthogonal to the rollback
# question each module answers separately (turn_event_stream's
# _ABORT_ROLLBACK_CAUSES: fork the intent thread or keep its
# Command(resume=) path — confirm_timeout KEEPS it): this set answers
# what the TaskStore row's terminal WORD should be. Round-55 F1 found
# the word mapping had dropped confirm_timeout onto "failed" while the
# turn module called that cause "a DESIGNED pause" three lines away;
# round-56 moved the taxonomy here — next to the row writer every
# stream already routes through — after finding the sibling streams
# carrying private mappings of their own.
ABORT_INTERRUPT_CAUSES = frozenset({
    "user_cancel", "disconnected", "confirm_timeout",
})


def abort_row_word(cause: str) -> str:
    """Terminal word for an aborted run's TaskStore row.

    Interrupt causes (user-side termination) get "cancelled"; crash
    causes (internal_error) get "failed". The empty string — the turn
    finally's fallback reads its cause memo with a defensive default —
    and any unknown cause classify as "failed": fail-closed, because
    the fallback exists precisely for runs that did not close
    themselves, and an unknown cause must not understate a crash as a
    user cancel (round-56 F2: recover_stream's former inline mapping
    defaulted exactly the other way).
    """
    return "cancelled" if cause in ABORT_INTERRUPT_CAUSES else "failed"


async def write_aborted_task_row(task_id: str, task_state: str) -> None:
    """Write the terminal TaskStore word for an aborted run, guarded.

    Shared by the abort paths of all three stream modules (the r53
    triad's row write, generalised off the cancel exits): a run that
    ended in a disconnect or an internal error must not leave its row
    at the last mid-graph upsert — a zombie "injecting" in /tasks that
    inference can never fix (no later writer comes on these paths).

    The guard (r54 G6): ``update_task_state(skip_if_terminal=True)``
    refuses to move a row that already reached its OWN terminal word —
    a cancel landing during result extraction, after the pipeline
    completed, must not rewrite "completed" into "cancelled". The
    abort word is for runs whose graph NEVER got to finish; the run
    that finished keeps its own verdict.

    Fail-soft by design (an abort path must not raise past the exit
    that is already unwinding), but LOUD: the row word is the
    user-facing fact, and its loss was invisible at debug level.
    """
    from chaos_agent.persistence.task_store import get_task_store

    try:
        store = await get_task_store()
        if store is None:
            return
        last_error: Exception | None = None
        for attempt in range(_SQLITE_RETRY_ATTEMPTS):
            try:
                await store.update_task_state(
                    task_id, task_state, skip_if_terminal=True,
                )
                if attempt:
                    logger.info(
                        "Aborted task row write for %s landed on retry %d",
                        task_id, attempt,
                    )
                return
            except Exception as e:  # noqa: BLE001 — retried below, warned after
                last_error = e
                # abort-safe: retry backoff — losing it to cancellation
                # forfeits nothing durable (allowlisted, invariants test).
                await asyncio.sleep(_SQLITE_RETRY_DELAY_S)
        logger.warning(
            "Failed to write aborted task row %s=%s after %d attempts "
            "(zombie-row risk; recover graph discovery is unaffected)",
            task_id, task_state, _SQLITE_RETRY_ATTEMPTS,
            exc_info=last_error,
        )
    except Exception:
        logger.warning(
            "Failed to write aborted task row %s=%s",
            task_id, task_state, exc_info=True,
        )
