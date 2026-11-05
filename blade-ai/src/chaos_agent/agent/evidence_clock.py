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
import re
import shlex
import time
from datetime import datetime
from typing import Any, Iterator, Optional

from langchain_core.messages import AIMessage, ToolMessage

# Provenance vocabulary is single-sourced from the recovery-clock framework so
# the injection side and the recovery side describe "what the framework can and
# cannot prove about a moment" in ONE language. execution_artifacts does not
# import evidence_clock (nor does its target_guard/tool_verdicts chain), so this
# is a one-directional edge — no cycle. ``_tool_call_lookup`` is reused (not
# re-derived) so the tool_call_id → args mapping stays single-sourced; the
# host-receipt scan below correlates a receipt with the command that produced it
# through the SAME map the artifact ledger uses. ``host_call_is_registered_recovery``
# is reused for the SAME reason: it is the attributor's single-source verdict for
# "is this host-channel call a self-recovery timer", and routing through it keeps
# this GENERIC module off the provider ``declaration`` seam (the phase-11
# carrier-import boundary forbids a generic layer importing a provider subpackage;
# execution_artifacts is the allowlisted holder of that seam).
from chaos_agent.agent.execution_artifacts import (
    PROVENANCE_FRAMEWORK,
    PROVENANCE_UNOBSERVED,
    _tool_call_lookup,
    host_call_is_registered_recovery,
)
from chaos_agent.utils.time import BEIJING_TZ, parse_iso_timestamp

logger = logging.getLogger(__name__)


#: ``additional_kwargs`` keys the framework owns on a stamped receipt.
EVIDENCE_TS_KEY = "evidence_ts"
EVIDENCE_INTERVAL_KEY = "evidence_interval"

#: Window-position labels. Deliberately verbose: they are read by a model that
#: has no other way to know what the coordinate system is, and they are facts
#: about position only — none of them says what the evidence means.
#:
#: The vocabulary is ISSUE-window, NOT fault-onset. Every coordinate is measured
#: against ``command_issued`` — the write-once ISSUE-time stamp of the FIRST
#: native mutating command, which in a multi-step injection may be a SETUP
#: command minutes before the fault actually lands (see
#: :func:`injection_time_anchors`). Naming a band ``pre_onset`` /
#: ``in_fault_window`` would assert the framework knows when the fault took
#: effect; it does not — ``fault_onset`` is declared UNOBSERVED. So the bands say
#: where a receipt sits relative to the ISSUE window, and leave "did the fault
#: land, and when" to the model's reading of live evidence.
PRE_ISSUE = "before_first_command"
WITHIN_ISSUE_WINDOW = "within_issue_window"
POST_ISSUE_WINDOW_PRE_FIRE = "post_issue_window_pre_recovery_fire"
POST_ISSUE_WINDOW = "post_issue_window"
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

    ``t0_epoch`` is the ``command_issued`` anchor — the ISSUE-time stamp of the
    first native mutating command (see :func:`injection_time_anchors` for its
    provenance and why it is a lower bound on fault onset, not onset itself).
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


def _anchor_epoch(value: Any) -> Optional[float]:
    """Coerce a stored timestamp to ``float``, or ``None`` when there is none.

    Mirrors ``execution_artifacts._epoch``: ``bool`` is excluded explicitly
    because it is an ``int`` subclass, so a corrupted ``True`` would otherwise
    read as epoch 1 and anchor a timeline in 1970.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


#: The injection-clock anchors in causal order. Single source for the writer
#: (:func:`injection_time_anchors`) and every reader, mirroring the recovery
#: side's ``RECOVERY_TIME_ANCHOR_SLOTS`` so a new anchor cannot appear in one
#: view and not the other.
INJECTION_TIME_ANCHOR_SLOTS: tuple[str, ...] = (
    "command_issued",
    "fault_onset",
    "window_deadline",
)

#: Anchor pairs whose gap is worth reporting. Only pairs whose BOTH ends are
#: observed yield a number (:func:`injection_anchor_offsets`); the rest are
#: reported as named gaps. ``command_issued->fault_onset`` is the EFFECT LAG the
#: #75 verifier mis-derived from the bare T0 (it read the setup command's
#: issue-time as the fault action and called the gap to the first 403 a
#: "propagation delay"). Because no framework writer stamps ``fault_onset``, that
#: pair — and ``fault_onset->window_deadline`` — ALWAYS land in ``unobserved``.
#: Naming the effect lag as a GAP the model must fill from live evidence, rather
#: than a number the framework asserts, is the whole point.
INJECTION_ANCHOR_OFFSET_PAIRS: tuple[tuple[str, str], ...] = (
    ("command_issued", "fault_onset"),
    ("fault_onset", "window_deadline"),
    ("command_issued", "window_deadline"),
)


def injection_time_anchors(state: Any) -> dict:
    """The injection-clock anchors of the current drill, side by side.

    Pure derivation from state's own authoritative fields (``injection_start_time``
    and ``fault_spec.duration_seconds`` via :func:`read_window_facts`) — it stores
    nothing and cannot drift from them. Every slot is ALWAYS present with its
    provenance, mirroring :func:`recovery_time_anchors`, so a consumer iterates
    the schema without probing which fields this drill happens to carry.

    * ``command_issued`` — the write-once ISSUE-time stamp of the FIRST native
      mutating command. In a multi-step injection that may be a SETUP command
      (install, pod rebuild, mini-CA) minutes before the fault lands, so it is
      ``PROVENANCE_FRAMEWORK``: a framework wall-clock number usable as a
      conservative LOWER BOUND on onset, NOT evidence the fault landed.
    * ``fault_onset`` — declared ``PROVENANCE_UNOBSERVED`` with NO framework
      writer, exactly the recovery side's ``host_armed``/``host_fire`` precedent.
      When the fault actually took effect is a per-fault-type semantic judgement
      the framework cannot prove; declaring the slot UNOBSERVED makes that limit
      a machine-readable fact instead of letting ``command_issued`` masquerade
      as onset. A future evidence-derived writer fills this slot with no further
      schema or consumer change.
    * ``window_deadline`` — ``command_issued + duration``, the EARLIEST possible
      window close (``PROVENANCE_FRAMEWORK``, derived, conservative).
    """
    t0_epoch, duration, _fire = read_window_facts(state)

    def _slot(value: Any, provenance: str) -> dict:
        epoch = _anchor_epoch(value)
        if epoch is None:
            return {"epoch": None, "provenance": PROVENANCE_UNOBSERVED}
        return {"epoch": epoch, "provenance": provenance}

    deadline = (
        t0_epoch + duration
        if (t0_epoch is not None and duration > 0)
        else None
    )
    return {
        "command_issued": _slot(t0_epoch, PROVENANCE_FRAMEWORK),
        # No framework writer — see docstring. Declared so the rendering states
        # the gap instead of silently equating command_issued with onset.
        "fault_onset": _slot(None, PROVENANCE_UNOBSERVED),
        "window_deadline": _slot(deadline, PROVENANCE_FRAMEWORK),
    }


def injection_anchor_offsets(anchors: dict | None) -> dict:
    """Gap between each anchor pair, plus the gaps we cannot see.

    Mirrors :func:`recovery_anchor_offsets`: ``seconds`` holds only pairs whose
    BOTH ends are observed; ``unobserved`` names every pair missing an end and
    which end. On the injection side this is deliberately lopsided — because no
    framework writer stamps ``fault_onset``, the two onset-involving pairs always
    land in ``unobserved``, and only ``command_issued->window_deadline`` (the
    contract duration) is framework-observable. Reporting the effect lag as a
    NAMED GAP rather than a number is the fix: the framework stops asserting a
    landing time it cannot know, and the model reads the gap as "derive onset
    from live evidence", not "here is the propagation delay".
    """
    table = anchors if isinstance(anchors, dict) else {}
    seconds: dict[str, float] = {}
    unobserved: dict[str, list[str]] = {}
    for first, second in INJECTION_ANCHOR_OFFSET_PAIRS:
        label = f"{first}_to_{second}"
        a = table.get(first) if isinstance(table.get(first), dict) else {}
        b = table.get(second) if isinstance(table.get(second), dict) else {}
        start, end = _anchor_epoch(a.get("epoch")), _anchor_epoch(b.get("epoch"))
        if start is not None and end is not None:
            seconds[label] = round(end - start, 3)
            continue
        unobserved[label] = [
            name
            for name, value in ((first, start), (second, end))
            if value is None
        ]
    return {"seconds": seconds, "unobserved": unobserved}


def classify_evidence_interval(
    state: Any,
    ts_epoch: float,
    *,
    facts: tuple[Optional[float], int, Optional[float]] | None = None,
) -> str:
    """Place one moment on the ISSUE-window timeline.

    Pure arithmetic over state — the same two facts Case #58's clock already
    reads (``command_issued`` = ``injection_start_time``, the write-once ISSUE-time
    stamp; D = ``fault_spec.duration_seconds``) plus the armed recovery deadline.
    The bands are ISSUE-window coordinates, not fault-onset judgements: the
    framework cannot prove when the fault landed (``fault_onset`` is UNOBSERVED),
    so nothing here weighs evidence or claims the fault was present.
    """
    t0_epoch, duration, fire = facts if facts is not None else read_window_facts(state)
    if t0_epoch is None or duration <= 0:
        return WINDOW_UNKNOWN
    if ts_epoch < t0_epoch:
        return PRE_ISSUE
    if ts_epoch < t0_epoch + duration:
        return WITHIN_ISSUE_WINDOW
    if fire is None:
        return POST_ISSUE_WINDOW
    return POST_FIRE if ts_epoch >= fire else POST_ISSUE_WINDOW_PRE_FIRE


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


# ---------------------------------------------------------------------------
# Host-native self-recovery receipt scan (read-side only — see design D1/D2/D3)
#
# A host-domain native fault (raw-shell channel, no ChaosBlade UID) recovers
# through ONE handle: a systemd transient timer
# ``systemd-run --on-active=<N>s --unit=<U> <inverse>`` registered into the
# host's PID 1. It produces NO ``execution_artifacts`` entry (the ledger's
# collector is kubectl-gated) and its arm/fire facts are therefore invisible to
# ``_armed_fire_deadlines`` — the verifier historically read a bare "no armed
# recovery fire on record" and, in Case #13 run-5, invented a "primary effect
# must be live-observed" gate out of that silence.
#
# The receipts already exist in the persisted message stream: the arm command's
# merged stderr carries ``Running as unit: <U>`` and the verifier's own
# ``systemctl show <U>.timer -p Result`` carries ``Result=success``. This scan
# DERIVES those two facts from ``state["messages"]`` on every render. It writes
# nothing — no content, no ``additional_kwargs``, no artifact — and every
# failure path abstains to "unobserved" (fail-open toward not-knowing, NEVER
# toward a false CONFIRMED; a fabricated provenance is worse than a missing
# value). Tier-2 ``setsid`` (no systemd unit, no receipt) and Branch B (no
# carrier) yield an empty registered-unit set and stay unobserved by design.
# ---------------------------------------------------------------------------

#: A transient unit's role suffixes. ``systemd-run --unit=foo`` reports the
#: SERVICE (``Running as unit: foo.service``) while the timer it schedules is
#: ``foo.timer``; comparing BASES is what lets one armed unit match both its arm
#: receipt and its ``systemctl show foo.timer`` fire query.
_TRANSIENT_UNIT_SUFFIXES = (".service", ".timer", ".scope")


def _unit_base(unit: str) -> str:
    """Strip a transient-unit role suffix so ``foo`` / ``foo.timer`` correlate."""
    for suffix in _TRANSIENT_UNIT_SUFFIXES:
        if unit.endswith(suffix):
            return unit[: -len(suffix)]
    return unit


def _host_call_argv(args: Any) -> Optional[list[str]]:
    """Resolve a host tool_call's args to an argv, or ``None`` if not host-shaped.

    Two shapes, mirroring ``host_call_is_registered_recovery`` and the readonly
    layer's ``_host_native_call_is_readonly``: ``command`` is a raw string
    (shlex-split to argv); ``binary`` + ``args`` is already an argv. An
    unparseable payload yields ``None`` (abstain), never a partial argv.
    """
    if not isinstance(args, dict):
        return None
    command = args.get("command")
    if isinstance(command, str) and command.strip():
        try:
            return shlex.split(command)
        except ValueError:
            return None
    binary = args.get("binary")
    if isinstance(binary, str) and binary:
        extra = args.get("args") or []
        return [binary] + [str(a) for a in extra]
    return None


def _systemd_run_unit(argv: list[str]) -> Optional[str]:
    """The ``--unit=<U>`` (or ``--unit <U>``) a systemd-run argv names, else None."""
    for i, tok in enumerate(argv):
        if tok.startswith("--unit="):
            return tok.split("=", 1)[1] or None
        if tok == "--unit" and i + 1 < len(argv):
            return argv[i + 1] or None
    return None


def _registered_host_carrier_units(messages: list) -> set[str]:
    """Base unit names the framework ITSELF armed via a systemd-run timer.

    The host-channel + self-recovery-timer verdict is single-sourced through the
    attributor's own ``host_call_is_registered_recovery`` (which owns the
    provider ``declaration`` seam this generic module must not import), then
    narrowed to the Tier-1 ``systemd-run`` form via ``is_systemd_run_timer`` —
    the SAME primitive ToolGuard admits on — so only a guard-admitted systemd-run
    timer contributes a unit. A stray host ``systemd-run`` that is not a recovery
    carrier never enters the set, and Tier-2 ``setsid`` (no systemd unit) and
    Branch B (no carrier) yield an empty set — both stay unobserved by design.
    """
    from chaos_agent.tools.guard import is_systemd_run_timer

    units: set[str] = set()
    for message in messages:
        if not isinstance(message, AIMessage):
            continue
        for call in message.tool_calls or []:
            if not isinstance(call, dict):
                continue
            args = call.get("args")
            if not isinstance(args, dict):
                continue
            if not host_call_is_registered_recovery(call.get("name") or "", args):
                continue
            argv = _host_call_argv(args)
            if not argv or not is_systemd_run_timer(argv):
                continue
            unit = _systemd_run_unit(argv)
            if unit:
                units.add(_unit_base(unit))
    return units


def _property_selects_result(value: str) -> bool:
    """True when a ``systemctl show -p`` value selects the ``Result`` property.

    Handles both the bare form (``Result``) and the comma-separated batch form
    (``ActiveState,SubState,Result``) a verifier naturally issues when it reads
    a timer's state and its fire result in one query — Case #13's live retest
    used exactly ``-p ActiveState,SubState,Result``, which a bare ``== "Result"``
    test missed, silently dropping the CONFIRMED fire state to ARMED-only.
    ``Result`` is matched case-sensitively as ONE whole element, so ``Results``
    or ``ExecMainResult`` cannot falsely match.
    """
    return any(part.strip() == "Result" for part in value.split(","))


def _systemctl_show_result_unit(argv: Optional[list[str]]) -> Optional[str]:
    """The unit a ``systemctl show <U> … -p …Result…`` argv queries, else None.

    Requires BOTH the ``systemctl show <U>`` positional target AND a property
    selector that includes ``Result`` (alone or in a comma-separated list, via
    :func:`_property_selects_result`), so a generic ``systemctl show`` (all
    properties) or an unrelated command cannot be mistaken for the fire query.
    """
    if not argv or len(argv) < 2 or argv[0] != "systemctl" or argv[1] != "show":
        return None
    unit: Optional[str] = None
    has_result = False
    i = 2
    while i < len(argv):
        tok = argv[i]
        if tok in ("-p", "--property"):
            if i + 1 < len(argv) and _property_selects_result(argv[i + 1]):
                has_result = True
            i += 2
            continue
        if tok.startswith("--property="):
            if _property_selects_result(tok.split("=", 1)[1]):
                has_result = True
        elif tok.startswith("-p") and len(tok) > 2:
            if _property_selects_result(tok[2:]):
                has_result = True
        elif not tok.startswith("-") and unit is None:
            unit = tok
        i += 1
    return unit if (unit and has_result) else None


#: A transient ``.timer`` that has NOT yet fired reports ``SubState=waiting``
#: (armed, counting down). Matched as a whole ``systemctl show`` output line.
_TIMER_PENDING_RE = re.compile(r"^SubState=waiting\s*$", re.MULTILINE)


def _timer_still_pending(content: str) -> bool:
    """True when a ``systemctl show`` receipt shows a transient timer STILL armed.

    A ``systemd-run --on-active`` transient ``.timer`` reports ``Result=success``
    from the moment it is armed — that property reflects the timer unit's own
    activation job, NOT whether the timer has fired. Its pre-fire state is
    ``ActiveState=active`` / ``SubState=waiting``; only AFTER it fires does it
    transition to ``inactive`` / ``dead``. Reading ``Result=success`` alone as a
    confirmed fire therefore fabricates a fire out of the arm-state — exactly the
    false CONFIRMED this scan's contract forbids (module header: "fail-open
    toward not-knowing, NEVER toward a false CONFIRMED"). Bandwidth #25 caught
    this live: at 01:26:08 the tbf qdisc was STILL MOUNTED (fault live, carrier
    not yet fired) yet the timer read ``Result=success / ActiveState=active /
    SubState=waiting`` and the framework rendered "recovery fire CONFIRMED".
    ``SubState=waiting`` is the precise armed-not-fired signature, so a receipt
    carrying it vetoes the fire (the timer must LEAVE ``waiting`` to prove fire).
    """
    return bool(_TIMER_PENDING_RE.search(content))


def scan_host_recovery_receipts(state: Any) -> dict:
    """Derive a host-native carrier's arm/fire facts from the message stream.

    Read-side only: parses receipts the LLM/verifier ALREADY fetched into
    ``ToolMessage.content``, issues no new host query, and writes nothing. The
    two facts are dual-correlated to survive a stray match (design D3):

    * ``armed`` — a ``Running as unit: <U>`` receipt whose base unit is one the
      framework itself armed (``--unit`` in an admitted systemd-run timer).
    * ``fired`` — a ``systemctl show <U>.timer -p Result`` on an armed unit
      whose receipt reports ``Result=success`` (command AND output correlated,
      so a generic ``Result=success`` string cannot confirm a fire) AND does not
      show the timer still ``SubState=waiting`` — a transient timer's
      ``Result=success`` is its ARM-state (true from the moment it is armed), so
      the timer must have LEFT ``waiting`` for the receipt to evidence a fire
      (:func:`_timer_still_pending`; Bandwidth #25 false-CONFIRMED root fix).

    Returns all-unobserved on an empty stream, no registered carrier, or any
    exception. The wall-clock FIRE MOMENT is never claimed: a transient timer
    reports its ``Result``, not the instant it fired (and that instant would be
    skew-poisoned for a clock fault anyway) — so this records the EVENT, never a
    host time (design Non-Goal: event and moment stay separate).
    """
    unobserved = {
        "armed": {"confirmed": False, "unit_name": None, "receipt_source": None},
        "fired": {
            "confirmed": False, "result": None,
            "observed_via": None, "unit_name": None,
        },
    }
    messages = state.get("messages") if isinstance(state, dict) else None
    if not messages:
        return unobserved
    try:
        registered = _registered_host_carrier_units(messages)
        if not registered:
            return unobserved
        lookup = _tool_call_lookup(messages)
        armed = dict(unobserved["armed"])
        fired = dict(unobserved["fired"])
        arm_re = re.compile(r"Running (?:timer )?as unit:\s*(\S+)")
        for message in messages:
            if not isinstance(message, ToolMessage):
                continue
            content = message.content
            if not isinstance(content, str) or not content:
                continue
            # A JSON receipt is a provider-verdict channel, never a host shell
            # receipt — skip it (and never parse or touch it).
            if content.lstrip().startswith("{"):
                continue
            arm_match = arm_re.search(content)
            if arm_match and _unit_base(arm_match.group(1)) in registered:
                armed = {
                    "confirmed": True,
                    "unit_name": _unit_base(arm_match.group(1)),
                    "receipt_source": "systemd-run unit receipt",
                }
            call = lookup.get(getattr(message, "tool_call_id", "") or "")
            argv = _host_call_argv(call.get("args") if isinstance(call, dict) else None)
            fire_unit = _systemctl_show_result_unit(argv)
            if (
                fire_unit is not None
                and _unit_base(fire_unit) in registered
                and "Result=success" in content
                and not _timer_still_pending(content)
            ):
                fired = {
                    "confirmed": True,
                    "result": "success",
                    "observed_via": "systemctl show -p Result",
                    "unit_name": _unit_base(fire_unit),
                }
        return {"armed": armed, "fired": fired}
    except Exception:
        # A read-side derivation must never fail a render (or a drill): abstain
        # to all-unobserved, exactly like ``stamp_tool_results`` degrades.
        logger.debug("host recovery receipt scan failed (non-critical)", exc_info=True)
        return unobserved


def render_evidence_timeline(state: Any) -> str:
    """Render the stamped receipts as a dated timeline for the model.

    Same freshness contract as the two window reminders it sits beside:
    rendered on every builder call, never persisted, so the offsets cannot go
    stale and the section cannot accumulate. Returns ``""`` when nothing in
    history carries a stamp (pre-stamp ledgers, replan-cleared state), so
    callers can append unconditionally.

    The rendering is facts only — tool name, wall clock, offset from the
    ``command_issued`` anchor, ISSUE-window coordinate. Offsets are labelled
    against the ISSUE-time anchor (a framework-clock lower bound on fault onset),
    never as "since injection", so a receipt that set the anchor cannot read as
    "the fault landed here". What each coordinate implies about evidence is
    described where evidence semantics already live, not asserted here.
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
            if delta >= 0:
                offset = f", +{delta}s since command issued"
                if delta == 0:
                    # This receipt IS the command_issued anchor. In a multi-step
                    # injection the first mutating command may be SETUP, not the
                    # fault action — label it so a "+0s" entry cannot masquerade
                    # as "the injection landed here" (the #75 self-reinforcement:
                    # T0 always coincides with the receipt that set it).
                    offset += (
                        " ←anchor: first mutating command "
                        "(setup-or-fault not distinguished)"
                    )
            else:
                offset = f", {-delta}s before command issued"
        name = getattr(message, "name", "") or "tool"
        lines.append(f"  {clock} [{interval}{offset}] {name}")
    if not lines:
        return ""

    window = (
        f"issue window {duration}s (command-issued → earliest deadline)"
        if duration > 0 else "no fault duration on record"
    )
    # Four-state fire-note. The HOST facts (scan_host_recovery_receipts, from
    # the message stream) and the FRAMEWORK deadline (fire, from a k8s carrier
    # artifact) are strictly separate sources and never conflated: the old
    # single-line "recovery fire at <T>" rendered a framework-COMPUTED deadline
    # as if it were an observed fire — bookkeeping masquerading as physical
    # evidence. Precedence: a host-confirmed fire beats an armed-but-unobserved
    # carrier, which beats a framework-predicted deadline, which beats silence.
    receipts = scan_host_recovery_receipts(state)
    if receipts["fired"]["confirmed"]:
        unit = receipts["fired"]["unit_name"] or "the recovery carrier"
        fire_note = (
            f"; recovery fire CONFIRMED on host (Result=success on unit "
            f"{unit}.timer); the host wall-clock fire time is unavailable — a "
            f"transient timer reports its Result, not the moment it fired"
        )
    elif receipts["armed"]["confirmed"]:
        unit = receipts["armed"]["unit_name"] or "the recovery carrier"
        fire_note = (
            f"; recovery carrier ARMED on host (systemd-run unit {unit}); its "
            f"fire is NOT confirmed — either no `systemctl show {unit}.timer "
            f"-p Result` receipt is on record, or the latest receipt still shows "
            f"the timer armed (`SubState=waiting`), which reports `Result=success` "
            f"WITHOUT having fired; a transient timer's wall-clock fire time is "
            f"not retrievable, so treat the fault window as still OPEN unless a "
            f"live target-state probe proves otherwise"
        )
    elif fire is not None:
        fire_note = (
            f"; predicted recovery deadline at "
            f"{datetime.fromtimestamp(fire, BEIJING_TZ).strftime('%H:%M:%S')}"
            f" (framework-computed, not a host-confirmed fire)"
        )
    else:
        fire_note = "; no armed recovery fire on record"
    return (
        "**EVIDENCE TIMELINE (system-stamped, authoritative for WHEN each "
        "receipt landed)**: offsets are measured from ``command_issued`` — the "
        "ISSUE-time stamp of the FIRST mutating command (framework clock; a "
        "LOWER BOUND on fault onset, NOT onset itself). ``fault_onset`` is "
        "UNOBSERVED by the framework: derive when the fault actually took effect "
        "from the first receipt evidencing its signature, not from the anchor. "
        f"Window: {window}{fire_note}. Every receipt in your context is dated "
        f"here in order:\n" + "\n".join(lines)
    )


def render_time_facts(state: Any) -> str:
    """Render the ONE time fact every blind phase lacks: the current clock.

    The planning, execute and recover loops carried NO rendered time fact, so
    their LLM reverse-engineered today's date from tool-output archaeology —
    inject-14fdcd15: the executor inferred the date from the ChaosBlade binary's
    build year. Supplying the ``now`` anchor no message carries is exactly, and
    only, what cures that guessing.

    It deliberately renders NO fault-window position (T0 / elapsed / remaining /
    CLOSED) and NO armed-fire countdown. Those are judgements about the fault's
    lifecycle and belong to the verifier, which runs AFTER execute once landing
    is confirmed and already carries its own ``build_injection_window_clock``
    (Case #58), ``build_recovery_timer_reminder`` (Case #46) and dated
    ``render_evidence_timeline`` (Case #64). A blind phase that computed them
    would read ``injection_start_time`` — an ISSUE-time fail-safe marker stamped
    the moment an injection command goes out and never cleared when that command
    fails (execute_loop: "the attempt IS the injection", so a severed result
    cannot hide a real landing). When the first issued attempt fails and the
    real fault lands minutes later via a degraded path, that marker anchors a
    phantom window: the inject-14fdcd15 retest rendered "injection committed
    10:23:20 / window CLOSED ~438s ago" while the live fault had only just
    landed at 10:33:51, forcing the model to override its own "authoritative"
    clock five times and file the discrepancy as verdict Warning #1. Giving the
    blind phases the clock and leaving the window to the verifier removes that
    contradiction at the source.

    ``state`` is accepted for the uniform ``render_*(state)`` renderer signature
    but the clock is state-independent. Freshness contract identical to
    ``render_evidence_timeline``: recomputed on every builder call, never
    persisted; rides the volatile message TAIL, never the cacheable head.
    """
    now_iso = datetime.fromtimestamp(time.time(), BEIJING_TZ).isoformat(timespec="seconds")
    return f"**SYSTEM CLOCK (system-computed, authoritative)**: now {now_iso}."


def append_time_facts_reminder(messages: list, state: Any) -> None:
    """Append the fresh, transient time-fact reminder to a phase's message list.

    Single-source mount helper for every phase whose LLM would otherwise be
    time-blind (planning / execute / recover — the verifier already carries its
    own richer window renderers). The one reminder it appends carries ONLY the
    current system clock (``render_time_facts``): a blind phase gets the time
    fact it lacks, and the fault-window position stays the verifier's job (see
    ``render_time_facts`` for why a blind phase must not compute it — the
    issue-time ``injection_start_time`` marker survives a failed command and
    would anchor a phantom window). Each phase calls this once, right before its
    ``ainvoke``, passing the LOCAL transient ``messages`` copy it just built
    from state — NOT the list it persists back to state — so the reminder:

    * re-renders fresh every round (the clock never goes stale), and
    * never accumulates in history (evidence_clock freshness contract: rendered
      per call, never persisted).

    Keeping the mount in one helper is deliberate: the recurring defect this
    fixes was time facts rendered only in whichever phase a past case happened
    to fail in (#46 recover, #58 verify, #64 verify). A single helper a new
    phase adds one line to is what stops that phase-scoped point-fix pattern
    from recurring.
    """
    facts = render_time_facts(state)
    if not facts:
        return
    from langchain_core.messages import HumanMessage
    from chaos_agent.agent.prompts.reminder import wrap_system_reminder
    messages.append(HumanMessage(content=wrap_system_reminder(facts)))
