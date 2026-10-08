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

from chaos_agent.agent.execution_artifacts import make_teardown_matcher
from chaos_agent.agent.progress_ledger import merge_progress_ledger
from chaos_agent.agent.tool_verdicts import message_result_failed

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


# ---------------------------------------------------------------------------
# finish_execution soft gate — red-line visibility (生效确认硬门禁)
#
# SKILL.md legislates: 行为判据（探针探测故障效果）未采集即视为未生效确
# 认——不得进入等待、不得拆线、不得结束执行段；效果证据只能在故障存活期
# 内采集，恢复后/拆线后永久不可再采；机制证据（规则快照）不能替代效果证
# 据. The knowledge layer calls it a HARD gate; this tool used to enforce
# NOTHING (any finish got the same "do not call more tools" receipt). Live
# case inject-3dae7b4f is what that gap costs: the executor obeyed the old
# "then STOP" directive literally, finished 46s before the verifier's first
# behavioral probe, and the drill survived only because the fault window
# still had 77s of slack — luck, not mechanism. The gate below stays SOFT
# (the terminal ledger write proceeds either way): deciding "behavioral
# evidence exists" is semantic — kubectl exec reads both rules AND probe
# results — so one sample cannot justify hard-blocking the terminal write.
# What one sample CAN justify is making the receipt stop saying "do not
# call more tools" over the red line and say the reminder instead, so a
# model that genuinely forgot the probe can still take it next turn.
# ---------------------------------------------------------------------------

#: kubectl subcommands that WRITE cluster state — the injection anchors the
#: soft gate reasons from. Read subcommands (get/describe/explain) are
#: deliberately absent: those readbacks are mechanism evidence, not
#: injection. ``uncordon`` is absent by ruling (M3, r68 review): it is the
#: cordon drill's RECOVERY verb, never an injection — anchoring it reset
#: the evidence window after the executor's cleanup erased a probe that
#: HAD landed (measured in .b4tmp/r68_m3_probe.py case B).
#:
#: m4 (r68 review, measured by schema dump): kept exactly kubectl's own
#: docstring whitelist minus its read verbs — ``replace``/``edit`` are
#: doc-declared UNAVAILABLE and ``remove``/``rollout`` are not in the
#: whitelist at all (four dead entries removed), while ``debug`` (the
#: node-debugger carrier) and ``run`` (the recovery-carrier run shape)
#: ARE issuable writes and were missing. ``debug`` is dual-faced: an
#: anchor here for the FULL ``kubectl`` tool only — ``kubectl_read``'s
#: Literal surface is read-only, so its ``debug`` probe is behavioral
#: evidence (see ``_is_behavioral_read``), never an anchor.
#:
#: NAME DISAMBIGUATION (root cause II, proxy conflation): this is the
#: INJECTION-WINDOW ANCHOR vocabulary — "did this call open/extend the
#: fault-injection window?" — and is DELIBERATELY a DIFFERENT, WIDER set
#: from ``message_scanning.KUBECTL_WRITE_SUBCOMMANDS`` (8 members, the
#: OBJECT-WRITE ATTRIBUTION vocabulary). The two once shared the name
#: ``_KUBECTL_WRITE_SUBCOMMANDS`` (a single leading underscore apart),
#: which read as one concept with two spellings when they are two
#: concepts. This set ADDS ``apply``/``create``/``run`` because an
#: apply-native fault carrier DOES anchor the injection window, whereas
#: the attribution vocabulary EXCLUDES ``apply``/``create`` on purpose —
#: apply-native fault attribution is judged by content
#: (``is_apply_native_fault_injection``), never by the bare verb. Renamed
#: to a self-describing name so the two can never be mistaken for copies.
_KUBECTL_INJECTION_SUBCOMMANDS = frozenset({
    "patch", "create", "apply", "delete",
    "taint", "cordon", "drain", "scale",
    "annotate", "label", "set", "debug", "run",
})

#: kubectl subcommands whose output is BEHAVIORAL evidence — what the
#: workload DOES under the fault (its own logs, live resource consumption,
#: an in-container probe) — as opposed to mechanism evidence (what the
#: injected rule/spec says: get/describe readbacks). SKILL.md:
#: 机制证据不能替代效果证据.
#:
#: m4: ``events`` lives here through its only issuable form — ``get
#: events`` — matched in ``_is_behavioral_read`` (neither tool surface
#: has an ``events`` subcommand slot). ``debug`` is NOT in the shared
#: set: it is dual-faced (m4) — an anchor on the FULL ``kubectl`` tool
#: (its debug CREATES a debug pod), a behavioral read only on
#: ``kubectl_read``'s read-only Literal surface — separated in
#: ``_is_behavioral_read`` so the isolated predicate cannot disagree
#: with the anchor side (r68 self-review C14).
_BEHAVIORAL_READ_SUBCOMMANDS = frozenset({
    "logs", "top", "exec",
})

#: The receipt mark every red-line reminder carries — the SINGLE source
#: for both producing gates and the latch's retiring match (r68 review
#: F3: was three independent string literals whose agreement was only
#: test-anchored; now the producer and the matcher share the token).
REDLINE_REMINDER_MARK = "RED-LINE REMINDER"

#: Tools whose receipts may carry the red-line reminder — the latch's
#: retiring signature is judged over EXACTLY these paired tool names.
#: Adding a THIRD banned-move channel means adding it here AND mounting
#: the check in the tool (the AST reconciliation test
#: ``test_receipt_tools_mounted_and_latched`` goes red on a one-sided
#: change — a mounted-but-unlatched gate ping-pongs its reminder, a
#: latched-but-unmounted name is a dead entry).
REDLINE_RECEIPT_TOOLS = frozenset({"finish_execution", "time_wait"})


def _is_injection_call(tool_name: str, args: dict) -> bool:
    """True when the tool call writes fault state (the injection family)."""
    if tool_name in {
        "blade_create", "execute_skill_script",
        # M3 (r68 review): EXACT names, not prefixes — the prefix form
        # swallowed ``blade_python_revoke`` (the UNDO!) and would swallow
        # any future faultdrill teardown tool, resetting the window after
        # cleanup (measured in .b4tmp/r68_m3_probe.py case C). Single
        # source: the providers' own injection vocabularies —
        # python_provider.py ``inject_tool_names`` is exactly
        # ``{"blade_python_create"}``; faultdrill exposes exactly one
        # injection tool (the carrier assembler).
        "blade_python_create", "faultdrill_assemble_carrier",
    }:
        return True
    # M2 (r68 review): host_inject is host_read's SUPERSET — a read-only
    # diagnostic through it is a behavioral PROBE, not an injection. The
    # host-native attribution scan already judges this content-aware
    # (``_host_native_call_is_readonly``); without the mirror here, the
    # LAST successful diagnostic re-anchors the window and erases the
    # probe that landed (host twin of M3's teardown misread).
    if tool_name == "host_inject":
        from chaos_agent.agent.providers.message_scanning import (
            _host_native_call_is_readonly,
        )
        return not _host_native_call_is_readonly(args)
    if tool_name == "kubectl":
        # m4: the FULL kubectl surface — its docstring whitelist's write
        # verbs. ``kubectl_read`` is deliberately NOT here: its Literal
        # surface (schema-dumped) is read-only INCLUDING ``debug`` (the
        # in-container probe face), so a kubectl_read call is never an
        # injection anchor.
        sub = str((args or {}).get("subcommand") or "").strip().lower()
        return sub in _KUBECTL_INJECTION_SUBCOMMANDS
    return False


def _is_behavioral_read(tool_name: str, args: dict) -> bool:
    """True when the tool call reads behavioral evidence (see the set above)."""
    if tool_name in {"kubectl", "kubectl_read"}:
        sub = str((args or {}).get("subcommand") or "").strip().lower()
        if sub in _BEHAVIORAL_READ_SUBCOMMANDS:
            return True
        if sub == "debug":
            # m4 dual-face ruling: the FULL kubectl surface's ``debug``
            # CREATES a debug pod — an injection anchor
            # (``_is_injection_call``), never behavioral evidence; only
            # ``kubectl_read``'s read-only Literal surface runs it as the
            # in-container probe. Split here (r68 self-review C14) so the
            # isolated predicate agrees with the anchor side instead of
            # relying on the caller's if/elif order to mask the conflict.
            return tool_name == "kubectl_read"
        # ``events`` has no subcommand slot on EITHER tool surface (kubectl's
        # whitelist and kubectl_read's Literal both admit events only through
        # ``get``) — the issuable form of event evidence is ``get events ...``,
        # judged by the first positional token (measured by the failing
        # uncordon regression: a genuine ``get events`` probe nagged).
        if sub == "get":
            v_args = str((args or {}).get("v_args") or "").strip().split()
            return bool(v_args) and v_args[0].lower() == "events"
        return False
    # M2 (r68 review): the host profile's EXECUTE surface binds ONLY
    # ``host_inject`` (host_read is the read-only phases' tool) — yet the
    # soft gate's receipt prescribes a behavioral probe, and the host's
    # ONLY issuable probe form is host_inject's read-only-diagnostic
    # superset role (skip_guard). Without this arm the reminder is
    # UNCONDITIONAL on host (measured in .b4tmp/r68_m2_probe.py chain 2)
    # and its suggested logs/events/top/exec probes have no tool that can
    # run them. Judgement single-sourced from the host-native attribution
    # scan's own content-aware read-only classifier (message_scanning).
    if tool_name == "host_inject":
        from chaos_agent.agent.providers.message_scanning import (
            _host_native_call_is_readonly,
        )
        return _host_native_call_is_readonly(args)
    return False


def _last_evidence_window(messages, artifacts):
    """Scan the transcript for the last injection window.

    Returns ``(last_injection_idx, behavioral_seen)`` — the LAST
    successful non-teardown injection's message index, and whether a
    behavioral read landed after it. Single engine for the one-shot
    latch (:func:`behavioral_reminder_due`), so the reminder can never
    disagree with itself about where the window sits.

    Fail-open shapes (non-dict state, no messages, no successful
    injection): ``last_injection_idx = -1`` → no reminder is owed for a
    fault that never landed. Behavioural reads BEFORE the last injection
    (pre-arm state-file probes, SKILL.md's 先只读探针) do not count —
    their evidence predates the fault. Teardown of registered vehicles
    never counts as an injection (M3, r68 review): the compliant flow
    ends 「inject → probe → cleanup → finish」.
    """
    is_teardown = make_teardown_matcher(artifacts)
    call_args: dict[str, tuple[str, dict]] = {}
    last_injection_idx = -1
    behavioral_seen = False
    for idx, msg in enumerate(messages):
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                call_args[str(tc.get("id") or "")] = (
                    str(tc.get("name") or ""),
                    tc.get("args") or {},
                )
        if not isinstance(msg, ToolMessage):
            continue
        if message_result_failed(msg):
            continue
        name, args = call_args.get(
            str(getattr(msg, "tool_call_id", "") or ""), ("", {}),
        )
        if _is_injection_call(name, args):
            if is_teardown(name, args):
                # Registered-vehicle machinery (carrier/debug-pod cleanup,
                # the channel exec on the recovery carrier, the inline
                # blade destroy/revoke): neither an anchor nor evidence
                # erasure — it must not reset the window.
                continue
            # Only probes AFTER the last injection count: a re-injection
            # (retry, second fault) resets the evidence window.
            last_injection_idx = idx
            behavioral_seen = False
        elif _is_behavioral_read(name, args) and last_injection_idx >= 0:
            behavioral_seen = True
    return last_injection_idx, behavioral_seen


def behavioral_reminder_due(state) -> bool:
    """One-shot latch wrapper: is the red-line reminder STILL owed?

    m5 (r68 review, measured in .b4tmp/r68_m5_probe.py): without a latch
    the reminder re-fired on EVERY finish_execution while the model kept
    choosing mechanism readbacks — a reminder/readback/finish ping-pong
    whose every round consumed the fault window the reminder exists to
    protect. The latch is derived from the transcript itself (the
    channel-B lesson of R6-1: state stamped only at issue time leaves
    history re-scans without the facts): one reminder per injection
    window — a ``RED-LINE REMINDER`` receipt that already sits AFTER the
    last injection anchor retires the obligation; a re-injection opens a
    new window and re-arms it. After the one reminder the receipt falls
    back to the classic wording: the soft gate's whole act is receipt
    wording, and repeating that wording is how a soft gate turns into a
    window eater.

    Anchor detection is structural (tool name / kubectl subcommand) — an
    injection smuggled through a read-shaped exec is missed, the
    accepted miss rate of a soft gate that only ever adds a reminder to
    a receipt (r68 review F1: folded in from the retired
    ``behavioral_evidence_missing`` predicate, whose zero-consumer
    liveness made it a stale-API import hazard).
    """
    if not isinstance(state, dict):
        return False
    messages = state.get("messages")
    if not isinstance(messages, (list, tuple)):
        return False
    artifacts = state.get("execution_artifacts") or []
    last_injection_idx, behavioral_seen = _last_evidence_window(
        messages, artifacts,
    )
    if last_injection_idx < 0 or behavioral_seen:
        return False
    # The latch's retiring signature: a reminder receipt from EITHER
    # banned-move gate (finish_execution — the third banned move; time_wait
    # — the first, o10) sitting after the last injection anchor. Judged by
    # the paired tool name (REDLINE_RECEIPT_TOOLS), not the receipt text
    # alone, so a log line that merely quotes the phrase cannot retire
    # the obligation; the mark itself is the shared REDLINE_REMINDER_MARK
    # so the matcher cannot drift from the producers (F3).
    call_args: dict[str, tuple[str, dict]] = {}
    for msg in messages:
        tool_calls = getattr(msg, "tool_calls", None)
        if tool_calls:
            for tc in tool_calls:
                call_args[str(tc.get("id") or "")] = (
                    str(tc.get("name") or ""),
                    tc.get("args") or {},
                )
    for idx, msg in enumerate(messages):
        if idx <= last_injection_idx or not isinstance(msg, ToolMessage):
            continue
        name = call_args.get(
            str(getattr(msg, "tool_call_id", "") or ""), ("", {}),
        )[0]
        content = msg.content
        if (
            name in REDLINE_RECEIPT_TOOLS
            and isinstance(content, str)
            and REDLINE_REMINDER_MARK in content
        ):
            return False
    return True


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
      - the single behavioral probe of the fault's user-visible effect is
        in hand (red line: 行为判据未采集不得结束执行段 — effect evidence
        dies with the fault window; a mechanism readback does not
        substitute), AND
      - nothing useful remains within the approved boundaries.
      NOT ``request_replan`` (goal UNREACHABLE — a successful finish filed
      there would report a failure that never happened) and NOT
      ``update_progress`` (that records intermediate progress).

    Inputs:
      - summary: 1-3 sentences — what was executed and what the final
          cluster state is (the verifier reads the ledger log for context).

    Output: confirmation that execution is recorded complete. When the
    transcript shows an injection with no behavioral probe after it, the
    receipt carries a RED-LINE REMINDER (once per window) instead of
    "do not call more tools" — the ledger write itself still proceeds
    (soft gate).

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
    # Soft gate (SKILL.md 生效确认硬门禁) — see behavioral_reminder_due:
    # the terminal write PROCEEDS either way; only the closing instruction
    # differs, so a model that genuinely forgot the behavioral probe can
    # still take it in its next turn instead of being told "do not call
    # more tools" over the red line. ONE reminder per injection window
    # (m5): after it the receipt returns to the classic wording —
    # repeating the reminder while the model keeps choosing mechanism
    # readbacks is a ping-pong that eats the very window it protects.
    if behavioral_reminder_due(state):
        _receipt = (
            f"execution recorded complete (log={_n_log}). "
            f"{REDLINE_REMINDER_MARK} (soft gate): no behavioral evidence of "
            "the fault's user-visible effect (logs / events / top / an "
            "exec probe) followed the last injection step — only "
            "mechanism readbacks did, and a mechanism readback cannot "
            "substitute. Effect evidence dies with the fault window: if "
            "the verifier's first probe may land outside it, collect ONE "
            "behavioral probe now, before the window closes."
        )
    else:
        _receipt = (
            f"execution recorded complete (log={_n_log}). "
            "The system will verify the fault now — do not call more tools."
        )
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
            _receipt,
            tool_call_id=tool_call_id,
        )],
    })
