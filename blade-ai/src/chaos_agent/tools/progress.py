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
  * Reads the current ledger via ``InjectedState`` and returns the fully merged
    ledger via ``Command(update=...)`` — the default (replace) reducer then
    stores it. All merge rules (anchor frozen, state overwritten, log appended
    and capped) live in the pure ``merge_progress_ledger`` helper, not here.
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
    return _coerce_json_arg(v, dict, "state_update")


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
      - log_append: milestones to append, each ``{"event": str, "status":
          "observed"|"verified"|"assumed"}``. Mark ``verified`` ONLY for what you
          actually checked — an unverified finding must not reach the user as
          established fact.

    Output: confirmation with the ledger's fact/log counts.

    Side effects: None. Touches no cluster resource; the approved goal cannot be
    rewritten here.
    """
    current = state.get("progress_ledger") if isinstance(state, dict) else None
    merged = merge_progress_ledger(
        current, state_update=state_update, log_append=log_append,
    )
    _n_facts = len((merged.get("state") or {}).get("established_facts") or [])
    _n_log = len(merged.get("log") or [])
    return Command(update={
        "progress_ledger": merged,
        "messages": [ToolMessage(
            f"progress recorded (facts={_n_facts}, log={_n_log})",
            tool_call_id=tool_call_id,
        )],
    })
