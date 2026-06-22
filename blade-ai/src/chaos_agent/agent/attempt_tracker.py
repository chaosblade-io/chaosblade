"""Patch E — pipeline-level attempt tracker.

Why this module exists:

    A single inject turn can pass through Phase 1 (planning),
    confirmation, baseline, and execute multiple times because:

      a) graph-level replan kicks in on tool error patterns,
      b) the LLM autonomously switches the inject target mid-turn
         (e.g. "node X has DiskPressure → let me try node Y"),
      c) the user re-runs via /retry.

    From the user's perspective these three look identical (the
    PhaseStepper restarts at "Intent → Plan → Safety → Inject"),
    but they have very different reasons. The user-reported task
    log showed the LLM doing (b) and the operator reading it as a
    confused retry-of-failure. We need to label each attempt so
    the TUI can render "attempt #2: LLM switched target to Y".

What this module owns:

    - The ``pipeline_attempt`` counter on AgentState
    - The ``pipeline_attempts_history`` audit log on AgentState
    - A single helper ``begin_attempt`` for graph nodes to call when
      they recognise the start of a new attempt
    - A small ``detect_target_switch`` heuristic so execute_loop can
      decide whether the LLM's new target qualifies as an attempt
      boundary

What this module does NOT own:

    - Where to call ``begin_attempt`` from — that's per-node policy
      (currently agent_loop initial entry + execute_loop on target
      switch + replan node). New call sites are added by editing
      those nodes.
    - Persistence — the history is part of state and gets persisted
      via the existing checkpoint / TaskStore machinery; no new
      storage layer.
    - UI rendering — TUI subscribes to the attempt-related fields
      via the existing state event channel.

Design constraints:

    - Pure functions (no I/O). All state mutation is expressed as a
      "delta" dict the caller merges into state. Mirrors the
      conventions used by other helpers in this package and keeps
      LangGraph's checkpointer happy with idempotent reducers.
    - ``begin_attempt`` always increments. There is no
      "edit-current-attempt" API by design — the contract is "tell
      me an attempt started", and the helper records it. Mid-attempt
      observations live elsewhere.
"""

from __future__ import annotations

import logging
import time
from copy import deepcopy

logger = logging.getLogger(__name__)


# Reason codes — keep these stable so logs / metrics / UI can key off
# them. Free-form strings are accepted (so plugins can add their own)
# but the canonical set is documented here.
REASON_INITIAL = "initial"
"""First attempt of the turn (called from agent_loop entry)."""

REASON_GRAPH_REPLAN = "graph_replan"
"""Graph-level replan kicked in (settings.replan_auto_trigger or
LLM emitted ``[REPLAN]``)."""

REASON_LLM_TARGET_SWITCH = "llm_target_switch"
"""LLM autonomously picked a different target inside execute_loop."""

REASON_USER_RERUN = "user_rerun"
"""Operator triggered a fresh attempt via /retry slash command."""


def begin_attempt(
    state: dict,
    *,
    target: dict | None,
    reason: str = REASON_INITIAL,
    notes: str = "",
) -> dict:
    """Record the start of a new pipeline attempt.

    Returns a delta dict the caller MERGES into state — does not
    mutate ``state`` in place. Caller is responsible for routing the
    delta through the LangGraph node return value or
    ``state.update(...)``.

    Args:
        state: Current AgentState (or dict). Read-only here.
        target: The target spec for this attempt (echoed into history
            for audit). Pass ``None`` if not yet known — the entry
            still records the timestamp.
        reason: One of the ``REASON_*`` constants (or a custom string
            for plugin scopes).
        notes: Optional human-readable note ("LLM said: target X has
            DiskPressure"). Surfaced in TUI / TaskStore.

    Returns:
        ``{"pipeline_attempt": int, "pipeline_attempts_history": list}``
        suitable for direct merge into state.
    """
    cur = int(state.get("pipeline_attempt", 0) or 0)
    new_attempt = cur + 1

    history = list(state.get("pipeline_attempts_history") or [])
    history.append(
        {
            "seq": new_attempt,
            "target": deepcopy(target) if target else None,
            "reason": reason,
            "notes": notes,
            "started_at": time.time(),
            "ended_at": None,
            "outcome": None,
        }
    )

    logger.info(
        "pipeline attempt #%d started (reason=%s, target=%s)",
        new_attempt,
        reason,
        _short_target(target),
    )

    return {
        "pipeline_attempt": new_attempt,
        "pipeline_attempts_history": history,
    }


def end_attempt(
    state: dict,
    *,
    outcome: str,
    notes: str = "",
) -> dict:
    """Mark the current (last) attempt as ended.

    Called from the graph's terminal node (save_memory) or from the
    rejection / failure paths. Idempotent: if no attempts were ever
    recorded, returns an empty delta.

    Args:
        outcome: ``"success"`` / ``"failed"`` / ``"aborted"`` /
            ``"superseded"`` (the latter when a follow-up
            ``begin_attempt`` is about to fire).
        notes: Optional reason string.

    Returns:
        ``{"pipeline_attempts_history": list}`` (only one field
        because ``pipeline_attempt`` doesn't change at end).
    """
    history = list(state.get("pipeline_attempts_history") or [])
    if not history:
        return {}
    last = dict(history[-1])
    last["ended_at"] = time.time()
    last["outcome"] = outcome
    if notes:
        last["notes"] = (last.get("notes") or "") + f" | {notes}" if last.get("notes") else notes
    history[-1] = last

    logger.info(
        "pipeline attempt #%d ended (outcome=%s)",
        last.get("seq", "?"),
        outcome,
    )
    return {"pipeline_attempts_history": history}


def detect_target_switch(
    prev_target: dict | None,
    new_target: dict | None,
) -> bool:
    """Heuristic: are these two targets meaningfully different?

    Used by execute_loop to decide whether the LLM's new
    ``submit_fault_intent`` invocation counts as a target switch
    (and therefore a new attempt boundary), versus a refinement of
    the same target (which should NOT trigger a new attempt).

    The heuristic is deliberately conservative — only treats a
    change as a switch if the *primary identifiers* differ:
      - ``names[0]`` (most common identifier across scopes)
      - ``namespace``
      - ``labels`` (pinned exact match)

    Other fields (params, intensity, duration) are considered
    refinements, not switches.

    Returns ``False`` when either side is None / empty (we don't
    know enough to call it a switch — caller should use other
    signals).
    """
    if not prev_target or not new_target:
        return False
    if not isinstance(prev_target, dict) or not isinstance(new_target, dict):
        return False

    prev_names = list(prev_target.get("names") or [])
    new_names = list(new_target.get("names") or [])
    if prev_names != new_names:
        return True

    if (prev_target.get("namespace") or "") != (new_target.get("namespace") or ""):
        return True

    if (prev_target.get("labels") or {}) != (new_target.get("labels") or {}):
        return True

    return False


def _short_target(target: dict | None) -> str:
    """Compact representation of a target for log lines."""
    if not target:
        return "(none)"
    if not isinstance(target, dict):
        return f"<{type(target).__name__}>"
    names = target.get("names") or []
    ns = target.get("namespace") or ""
    if names and ns:
        return f"{ns}/{names[0]}"
    if names:
        return str(names[0])
    if ns:
        return f"ns:{ns}"
    labels = target.get("labels") or {}
    if labels:
        return f"labels:{','.join(f'{k}={v}' for k, v in list(labels.items())[:2])}"
    return "(empty)"
