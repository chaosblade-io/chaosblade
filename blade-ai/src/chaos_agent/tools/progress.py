"""``update_progress`` — the tool the executor calls to maintain its ledger.

This is the write path of the progress ledger (see
``chaos_agent.agent.progress_ledger`` for the schema and merge semantics, and
the prompt section that re-injects it each round). The executor calls this
proactively — like Claude Code's TodoWrite — to record what it has established
and where it is, so it stays anchored to the original goal and so any consumer
(the intent graph, an interrupted turn's mirror) can see progress.

Design notes:
  * Pure state write, ZERO cluster side effects. It touches no real resource, so
    it is safe on both tool surfaces and must be waved through the guards.
  * Returns a DELTA (``{"state_update": …, "log_append": …}``) via
    ``Command(update=...)`` — the ``progress_ledger`` channel's reducer
    (``merge_ledger_channel``) applies it with ``merge_progress_ledger``.
    Submitting the delta (NOT a pre-merged snapshot) is what makes a model
    BATCHING ``update_progress`` + ``finish_execution`` in one turn fold as
    two sequential applications instead of crashing the super-step (Case #46:
    task inject-357401b8 died with "Can receive only one value per step",
    rollback failed the same way, and the cleanup chain never ran).
  * ``InjectedState`` is read ONLY for the confirmation echo's counts — the
    merge itself lives in the channel reducer, so the tool stays a thin
    pass-through and concurrent writes cannot race a stale snapshot.
  * The anchor is never taken from tool arguments: the executor cannot rewrite
    the goal it is being measured against.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Optional

from langchain_core.messages import ToolMessage
from langchain_core.tools import InjectedToolCallId, tool
from langgraph.prebuilt import InjectedState
from langgraph.types import Command
from pydantic import BeforeValidator

from chaos_agent.agent.progress_ledger import merge_progress_ledger

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Argument coercion
#
# Some models JSON-stringify structured tool arguments before serialising the
# tool_call — a known qwen-class quirk, already handled the same way for
# ``submit_fault_intent`` (see the coercion helpers in
# ``nodes/planning/intent_clarification``). Without it the ``dict`` / ``list``
# annotations reject the call at the ``@tool`` boundary with "Input should be a
# valid dictionary", and the ledger write is simply lost.
#
# task-fc64c982 is what that costs: the executor confirmed the node had gone
# Ready→NotReady, called ``update_progress`` with both arguments
# JSON-stringified, was rejected, retried with the identical payload, was
# rejected again, and gave up. The drill's ledger stayed empty — for the one run
# that was then reported as failed, i.e. exactly when the record matters most.
# ---------------------------------------------------------------------------


def _coerce_json_arg(raw, kind: type, field: str):
    """Parse a JSON-stringified ``dict`` / ``list`` argument into the real type.

    Anything already of the right type, or that cannot be parsed into it, is
    returned untouched so Pydantic still reports a genuine type error rather
    than this helper masking one.
    """
    if not isinstance(raw, str):
        return raw
    s = raw.strip()
    if not s:
        return None
    try:
        parsed = json.loads(s)
    except (ValueError, TypeError):
        logger.debug("update_progress: %s is not parseable JSON: %r", field, s[:120])
        return raw
    if isinstance(parsed, kind):
        return parsed
    logger.debug(
        "update_progress: %s parsed to %s, expected %s",
        field, type(parsed).__name__, kind.__name__,
    )
    return raw


def _validate_state_update(v):
    coerced = _coerce_json_arg(v, dict, "state_update")
    if isinstance(coerced, dict):
        # Single-writer discipline for the terminal phase (cascade review
        # C1, knife-1): ``execution-complete`` is the ledger fact the
        # harness gates on (stall nudge + router text-only branch).
        # update_progress is bound in ALL five ReAct phases — a verifier
        # or clarification model writing a generic ``phase=complete`` /
        # ``execution-complete`` (meaning ITS phase is done) would poison
        # the execute-side gates for the rest of the task. The terminal
        # phase has exactly one legal writer: ``finish_execution``
        # (phase2-only binding, prompt-taught). A rejected write falls
        # back to the plain type error the harness already handles.
        phase = coerced.get("phase")
        if isinstance(phase, str) and phase.strip().lower() in (
            "execution-complete", "execution_complete",
        ):
            raise ValueError(
                "execution-complete is finish_execution's terminal marker — "
                "update_progress cannot write it (use finish_execution to "
                "declare Phase 2 complete)"
            )
    return coerced


def _validate_log_append(v):
    return _coerce_json_arg(v, list, "log_append")


@tool
def update_progress(
    state_update: Annotated[Optional[dict], BeforeValidator(_validate_state_update)] = None,
    log_append: Annotated[Optional[list], BeforeValidator(_validate_log_append)] = None,
    *,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Record progress into your working ledger, shown back to you every round.

    Keeping it current is how you stay on the approved goal instead of
    re-deriving it, and it is the only record a later dialogue turn sees if this
    operation is interrupted.

    When to use:
      - A fact is established (target confirmed, precondition met).
      - A milestone is reached (injected, verified, recovered).
      - You move to a new phase or step.
      At real state changes only, not every turn.

    Inputs:
      - state_update: what is true NOW, merged over current state. Keys:
          ``phase``, ``current_step``, ``established_facts`` (list).
          Reserved: ``execution-complete`` is finish_execution's terminal
          marker — rejected here; finish with finish_execution.
      - log_append: milestones to append, each ``{"event": str, "status":
          "observed"|"verified"|"assumed"}``. Mark ``verified`` ONLY for what you
          actually checked — an unverified finding must not reach the user as
          established fact.

    Output: confirmation with the ledger's fact/log counts.

    Side effects: None. Touches no cluster resource; the approved goal cannot be
    rewritten here.
    """
    current = state.get("progress_ledger") if isinstance(state, dict) else None
    # Echo counts from a preview merge (the real merge happens in the channel
    # reducer when the delta is applied — the preview only feeds this receipt).
    merged = merge_progress_ledger(
        current, state_update=state_update, log_append=log_append,
    )
    _n_facts = len((merged.get("state") or {}).get("established_facts") or [])
    _n_log = len(merged.get("log") or [])
    return Command(update={
        # DELTA form: merge_ledger_channel applies it — a concurrent
        # finish_execution in the same turn folds AFTER this patch instead of
        # racing it (Case #46).
        "progress_ledger": {
            "state_update": state_update,
            "log_append": log_append,
        },
        "messages": [ToolMessage(
            f"progress recorded (facts={_n_facts}, log={_n_log})",
            tool_call_id=tool_call_id,
        )],
    })


#: Ledger ``state.phase`` values that mean "the executor has DECLARED
#: Phase 2 finished" — the harness-side gate reads this to stop nudging
#: a concluded plan (the #39 third-retest tail-tension: 12 rounds of
#: EXECUTION REQUIRED fired at a model whose every remaining tool call
#: was either redundant, out of authority, or destructive).
#:
#: Canonical spellings ONLY — the bare word ``complete`` was deliberately
#: REMOVED (cascade review C1, knife-1): update_progress is bound in all
#: five ReAct phases, and a verifier/clarification model writing a generic
#: ``phase=complete`` ("my phase is done") is a different fact that must
#: never satisfy the execute-side gates. The single legal writer is
#: finish_execution; the validator above rejects the canonical spellings
#: through update_progress so this set can stay exact without tolerance.
EXECUTION_COMPLETE_PHASES = frozenset({
    "execution-complete", "execution_complete",
})


def ledger_declares_execution_complete(values: dict) -> bool:
    """True when the progress ledger's ``state.phase`` says execution ended.

    Read by BOTH the execute_loop's stall-nudge gate and the router's
    text-only fallback: a conclusion the model already RECORDED via
    ``finish_execution`` is task fact, and the harness must respect it
    instead of re-issuing EXECUTION REQUIRED — the nudge's own premise
    ("the plan is already approved, call the injection tool NOW") is
    false once every planned step has run. Absent/unknown phases read
    as False (fail back to the old nudge behaviour, not into a silent
    exit).

    The predicate recognises ONLY finish_execution's canonical
    spellings — the bare ``complete`` is rejected because the fact has
    one writer (single-writer discipline, cascade review C1).
    """
    if not isinstance(values, dict):
        return False
    ledger = values.get("progress_ledger")
    if not isinstance(ledger, dict):
        return False
    state = ledger.get("state")
    if not isinstance(state, dict):
        return False
    phase = str(state.get("phase") or "").strip().lower()
    return phase in EXECUTION_COMPLETE_PHASES


@tool
def finish_execution(
    summary: str,
    *,
    state: Annotated[dict, InjectedState],
    tool_call_id: Annotated[str, InjectedToolCallId],
) -> Command:
    """Declare execution (Phase 2) COMPLETE — every planned mutation step has run.

    The clean exit for a finished execution: verification is the SYSTEM's
    next move, so call nothing further here.

    When to use:
      - Every planned mutation step has executed (including required waits), AND
      - nothing useful remains within the approved boundaries.
      NOT ``request_replan`` (goal UNREACHABLE — a successful finish filed
      there would report a failure that never happened) and NOT
      ``update_progress`` (that records intermediate progress).

    Inputs:
      - summary: 1-3 sentences — what was executed and what the final
          cluster state is (the verifier reads the ledger log for context).

    Output: confirmation that execution is recorded complete.

    Side effects: None. Touches no cluster resource; submits the terminal
    ledger delta the channel reducer applies (phase marker the stall guard
    reads to stop demanding more tool calls).
    """
    current = state.get("progress_ledger") if isinstance(state, dict) else None
    merged = merge_progress_ledger(
        current,
        state_update={"phase": "execution-complete"},
        log_append=[{
            "event": f"execution declared complete: {summary}",
            "status": "observed",
        }],
    )
    _n_log = len(merged.get("log") or [])
    return Command(update={
        # DELTA form (same channel protocol as update_progress): a model
        # batching update_progress + finish_execution in ONE turn folds the
        # two patches sequentially instead of crashing the super-step
        # (Case #46, task inject-357401b8).
        "progress_ledger": {
            "state_update": {"phase": "execution-complete"},
            "log_append": [{
                "event": f"execution declared complete: {summary}",
                "status": "observed",
            }],
        },
        "messages": [ToolMessage(
            f"execution recorded complete (log={_n_log}). "
            "The system will verify the fault now — do not call more tools.",
            tool_call_id=tool_call_id,
        )],
    })
