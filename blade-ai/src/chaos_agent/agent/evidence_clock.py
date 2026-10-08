"""Evidence clock — a framework-stamped wall clock on every tool receipt.

The LLM has no wall clock, and until this module existed **none of its context
channels carried a timestamp either**: not the message history, not the
progress ledger, not the system prompt. Two rounds of fixes worked around that
by rendering the window position afresh on every builder call — Case #46's
``build_recovery_timer_reminder`` and Case #58's
``build_injection_window_clock``. Both tell the model where *now* sits. Neither
can tell it where a *past observation* sat, because the moment an observation
was taken was never recorded anywhere.

Case #64 (Service_调用失败_ReadinessProbe配置不一致) is what that costs. The
verifier's own probes came back as a flat, undated list — three Services
refusing connections and one pod IP answering 200 — and it had to
reverse-engineer the ordering from message sequence. It reasoned its way to
"ClusterIP egress is broken environment-wide" and downgraded a verified
injection to ``partial``. The run log held the disproof: the recovery carrier,
~26s from one of those failing probes, had successfully curled
``kubernetes.default.svc`` — a ClusterIP — to restore the target. Two
contradictory ClusterIP results separated by a known interval is evidence about
a transition; the same two results with no interval at all is noise the model
has to explain away. The fact existed. It was never written down.

Design, and the line this module stays on:

* **The program supplies facts, the model draws conclusions.** This module
  records *when* a receipt landed and *where that moment sits* relative to the
  fault window — both pure arithmetic over values already in state. It never
  decides what a receipt means, never filters one out, and never gates a
  verdict on the interval. An interval label is a coordinate, not a judgement:
  a fault signature appearing in ``post_recovery_fire`` is exactly the evidence
  that recovery has not converged, so "discard anything after the fire" would
  destroy a finding rather than protect one.
* **Stamps go in ``additional_kwargs``, never in ``content``.** Tool receipts
  are parsed: ``tool_verdicts.loads_dict`` requires ``content`` to start with
  ``{`` and abstains otherwise, so a single prepended line would silently turn
  every JSON receipt into an unreadable one and the provider verdicts with it.
  ``additional_kwargs`` is already the codebase's channel for framework-owned
  message tags (see ``_VERIFIER_CONTEXT_KWARGS_KEY``).
* **One chokepoint.** Every tool in every phase runs inside a prebuilt
  ``ToolNode`` wrapped by ``dispatch.with_tool_span``, so stamping there covers
  the execute loop, the verifier and the recover verifier alike — no per-tool
  and per-node enumeration to fall out of date when a new tool or phase
  appears.
* **No cap on the rendered timeline.** Truncating it would be the program
  choosing which facts deserve to be seen. The entry count is already bounded
  by the loop budgets, which are flow control rather than evidence selection.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Iterator, Optional

from langchain_core.messages import ToolMessage

from chaos_agent.utils.time import BEIJING_TZ, parse_iso_timestamp

logger = logging.getLogger(__name__)


#: ``additional_kwargs`` keys the framework owns on a stamped receipt.
EVIDENCE_TS_KEY = "evidence_ts"
EVIDENCE_INTERVAL_KEY = "evidence_interval"

#: Window-position labels. Deliberately verbose: they are read by a model that
#: has no other way to know what the coordinate system is, and they are facts
#: about position only — none of them says what the evidence means.
PRE_ONSET = "pre_onset"
IN_FAULT_WINDOW = "in_fault_window"
POST_WINDOW_PRE_FIRE = "post_fault_window_pre_recovery_fire"
POST_WINDOW = "post_fault_window"
POST_FIRE = "post_recovery_fire"
WINDOW_UNKNOWN = "window_unknown"


def _armed_fire_deadlines(state: Any) -> list[float]:
    """Every numeric self-recovery fire deadline on record, ascending.

    Same source ``build_recovery_timer_reminder`` reads: artifacts armed with a
    ``recovery_deadline_epoch``. The ChaosBlade path times out inside the
    experiment and never arms one, so an empty list is normal.
    """
    artifacts = state.get("execution_artifacts") if isinstance(state, dict) else None
    deadlines: list[float] = []
    for artifact in artifacts or []:
        if not isinstance(artifact, dict) or artifact.get("status") != "recovery_armed":
            continue
        deadline = artifact.get("recovery_deadline_epoch")
        if isinstance(deadline, (int, float)) and not isinstance(deadline, bool):
            deadlines.append(float(deadline))
    return sorted(deadlines)


def read_window_facts(state: Any) -> tuple[Optional[float], int, Optional[float]]:
    """Return ``(t0_epoch, duration_seconds, fire_deadline_epoch)`` from state.

    Each element is ``None``/``0`` when that fact is simply not on record — the
    caller renders what it has instead of guessing. ``fire_deadline`` is the
    EARLIEST armed deadline: a first fire is what ends the "nothing has been
    reversed yet" reading of the window.
    """
    t0_epoch: Optional[float] = None
    t0 = state.get("injection_start_time") if isinstance(state, dict) else None
    if isinstance(t0, str) and t0:
        try:
            t0_epoch = parse_iso_timestamp(t0).timestamp()
        except (ValueError, TypeError):
            # A replan seam clears injection_start_time until the next
            # injection re-stamps it; an unparseable leftover must read as
            # absent rather than anchor the timeline on a bogus T0.
            t0_epoch = None

    duration = 0
    try:
        from chaos_agent.agent.spec.fault_spec import read_fault_spec
        spec = read_fault_spec(state) if isinstance(state, dict) else None
        duration = int(getattr(spec, "duration_seconds", 0) or 0)
    except Exception:  # pragma: no cover - defensive, spec reader is total
        duration = 0

    deadlines = _armed_fire_deadlines(state)
    return t0_epoch, duration, (deadlines[0] if deadlines else None)


def classify_evidence_interval(
    state: Any,
    ts_epoch: float,
    *,
    facts: tuple[Optional[float], int, Optional[float]] | None = None,
) -> str:
    """Place one moment on the fault-window timeline.

    Pure arithmetic over state — the same two facts Case #58's clock already
    reads (T0 = ``injection_start_time``, D = ``fault_spec.duration_seconds``)
    plus the armed recovery deadline. Nothing here weighs evidence.
    """
    t0_epoch, duration, fire = facts if facts is not None else read_window_facts(state)
    if t0_epoch is None or duration <= 0:
        return WINDOW_UNKNOWN
    if ts_epoch < t0_epoch:
        return PRE_ONSET
    if ts_epoch < t0_epoch + duration:
        return IN_FAULT_WINDOW
    if fire is None:
        return POST_WINDOW
    return POST_FIRE if ts_epoch >= fire else POST_WINDOW_PRE_FIRE


def _iter_tool_messages(result: Any) -> Iterator[ToolMessage]:
    """Yield every ``ToolMessage`` reachable from a ``ToolNode`` return value.

    The shape is not fixed: a plain batch returns ``{"messages": [...]}``, a
    tool that answers with ``Command(update=...)`` (``update_progress``,
    ``finish_execution``) makes the node return a ``Command``, and mixed
    batches nest both. Walking structurally rather than by expected key is
    what keeps the stamp from silently skipping a shape nobody enumerated.
    """
    seen: set[int] = set()
    stack: list[Any] = [result]
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, ToolMessage):
            yield node
        elif isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
        else:
            update = getattr(node, "update", None)
            if isinstance(update, dict):
                stack.append(update)


def stamp_tool_results(result: Any, state: Any, *, now: float | None = None) -> Any:
    """Stamp every receipt in a ``ToolNode`` result with its own wall clock.

    Mutates the messages' ``additional_kwargs`` in place and returns ``result``
    unchanged, so it composes as a pass-through on the dispatch path. The stamp
    is the moment the batch landed — the closest honest anchor to when the
    observation was true, and the granularity Case #64 needed (its two
    contradictory ClusterIP probes were ~26s apart).

    ``setdefault`` makes this idempotent: a receipt already stamped (a rebuild,
    a retry, a re-entered node) keeps its first time instead of being silently
    re-dated to the later pass.

    Degrades to "no stamp" on any failure. This sits on the return path of
    every tool node in the graph, so a raising stamp would turn an
    observability gap into a failed mutation that has ALREADY been dispatched
    — the one outcome strictly worse than an undated receipt.
    """
    ts_epoch = time.time() if now is None else float(now)
    try:
        iso = datetime.fromtimestamp(ts_epoch, BEIJING_TZ).isoformat()
        interval = classify_evidence_interval(state, ts_epoch)
        for message in _iter_tool_messages(result):
            kwargs = getattr(message, "additional_kwargs", None)
            if not isinstance(kwargs, dict):
                continue
            kwargs.setdefault(EVIDENCE_TS_KEY, iso)
            kwargs.setdefault(EVIDENCE_INTERVAL_KEY, interval)
    except Exception:
        # A stamp must never fail a drill: see the docstring.
        logger.debug("evidence stamp failed (non-critical)", exc_info=True)
    return result


def render_evidence_timeline(state: Any) -> str:
    """Render the stamped receipts as a dated timeline for the model.

    Same freshness contract as the two window reminders it sits beside:
    rendered on every builder call, never persisted, so the offsets cannot go
    stale and the section cannot accumulate. Returns ``""`` when nothing in
    history carries a stamp (pre-stamp ledgers, replan-cleared state), so
    callers can append unconditionally.

    The rendering is facts only — tool name, wall clock, offset from T0,
    window coordinate. What each coordinate implies about evidence is described
    where evidence semantics already live, not asserted here.
    """
    messages = state.get("messages") if isinstance(state, dict) else None
    if not messages:
        return ""
    t0_epoch, duration, fire = read_window_facts(state)

    lines: list[str] = []
    for message in messages:
        if not isinstance(message, ToolMessage):
            continue
        kwargs = getattr(message, "additional_kwargs", None)
        if not isinstance(kwargs, dict):
            continue
        iso = kwargs.get(EVIDENCE_TS_KEY)
        if not isinstance(iso, str) or not iso:
            continue
        interval = kwargs.get(EVIDENCE_INTERVAL_KEY) or WINDOW_UNKNOWN
        try:
            when = parse_iso_timestamp(iso).astimezone(BEIJING_TZ)
        except (ValueError, TypeError):
            continue
        clock = when.strftime("%H:%M:%S")
        offset = ""
        if t0_epoch is not None:
            delta = int(when.timestamp() - t0_epoch)
            offset = (
                f", +{delta}s since injection" if delta >= 0
                else f", {-delta}s before injection"
            )
        name = getattr(message, "name", "") or "tool"
        lines.append(f"  {clock} [{interval}{offset}] {name}")
    if not lines:
        return ""

    window = (
        f"fault window {duration}s" if duration > 0 else "no fault duration on record"
    )
    fire_note = (
        f"; recovery fire at "
        f"{datetime.fromtimestamp(fire, BEIJING_TZ).strftime('%H:%M:%S')}"
        if fire is not None else "; no armed recovery fire on record"
    )
    return (
        "**EVIDENCE TIMELINE (system-stamped, authoritative)**: when each tool "
        f"receipt landed, and where that moment sits relative to the fault "
        f"window ({window}{fire_note}). Every receipt in your context is dated "
        f"here in order:\n" + "\n".join(lines)
    )
