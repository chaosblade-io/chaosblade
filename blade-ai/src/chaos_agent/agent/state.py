"""AgentState definition for LangGraph StateGraph."""

from enum import StrEnum
from typing import Annotated, Optional

from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages

from chaos_agent.agent.progress_ledger import merge_ledger_channel
from chaos_agent.agent.result.operation_outcome import (
    read_failure_reason,
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
)
from chaos_agent.agent.result.verdict import (
    RECOVER_SUCCESS_VALUES,
    Layer1Status,
    RecoverVerdict,
)
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.utils.time import now_iso, parse_iso_timestamp


def _ts_add_messages(left, right):
    """Wrap add_messages to stamp Beijing wall-clock on every incoming message."""
    ts = now_iso()
    if isinstance(right, list):
        for msg in right:
            kwargs = getattr(msg, "additional_kwargs", None)
            if isinstance(kwargs, dict):
                kwargs.setdefault("_ts", ts)
    return add_messages(left, right)


def materialize_fault_handle(values: dict) -> Optional[dict]:
    """Return the committed fault handle for *values*, deriving it when absent.

    The handle is the carrier-agnostic identity of a committed fault (who
    injected, what to recover). It is written by the execute loop's
    attribution sync and inherited by the recover graph; when it is missing
    the registry derives it from the legacy attribution facts each provider
    claims (experiment_uid / injection_method). Generic consumers must go
    through this helper (or :func:`has_active_fault`) instead of reading
    carrier fields directly.

    COMMITTED semantics, not live (round-25 R1/R2): the handle has no death
    axis — the projection mirrors the attribution slots, and no destroy or
    recovery path clears those slots (the retired ledger is a separate
    durable field). A destroyed experiment therefore keeps materializing
    here while :func:`live_liability_uids` correctly convicts it dead. A
    consumer that needs the LIVE question ("does the environment still
    carry this fault?") must gate on the liability primitive for
    experiment carriers — never on this predicate alone.
    """
    handle = values.get("fault_handle")
    if isinstance(handle, dict) and handle:
        return handle
    from chaos_agent.agent.providers import FaultProviderRegistry

    return FaultProviderRegistry.derive_handle_from_legacy(values)


def has_active_fault(values: dict) -> bool:
    """True when *values* describe a committed fault injection.

    The single generic predicate replacing every carrier-specific presence
    check (historically ``bool(experiment_uid)``). A native injection with no
    experiment UID satisfies it exactly like a ChaosBlade experiment.

    COMMITTED semantics, not live (round-25 R1/R2): the underlying handle
    has no death axis — the slot-derived projection stays live-shaped after
    a proven destroy (see :func:`materialize_fault_handle`). Consumers
    answering the LIVE question ("does the environment still carry this
    fault?") must gate on :func:`live_liability_uids` for experiment
    carriers — this predicate answers "did this task ever commit a fault
    that something (recovery, postmortem, summary) may still need to know
    about".
    """
    return materialize_fault_handle(values) is not None


def live_liability_uids(values: dict) -> list[str]:
    """Experiments this task still owes a destroy — the liability axis.

    Root-cause companion of the single ``experiment_uid`` slot (B76 review G):
    the slot answers the ATTRIBUTION question ("what did the CURRENT contract
    just inject") and is legitimately last-write-wins, but that same
    overwrite silently erased the LIABILITY record of every earlier
    experiment — with four persistent consumers standing on the
    one-live-experiment assumption (recover identity, destroy whitelist,
    replan visibility), a superseded experiment became structurally
    un-recoverable. Liability is a SET, monotonic by birth, and only ever
    reduced by PROVEN death: output-proven destroys (the paired ToolMessage
    confirms the kill — B76 review J1) plus the retired registry
    (framework-side cleanup leaves no ToolMessage). Never reduced by contract
    replacement, batch advance, or attribution resets — replacing the intent
    does not dissolve the side effects already running in the cluster.

    The death filter is deliberately the PROVEN dialect, not the issued one
    (:meth:`FaultProviderRegistry.destroyed_experiment_ids` — a destroy CALL
    is terminal "whether the destroy succeeded or failed"). Attribution
    must stay conservative (an attempted destroy stops the UID being
    re-claimed as the live fault), but liability must stay honest: a FAILED
    destroy leaves the experiment possibly-alive and still owed — doubt
    keeps the UID here until the sweep retries it or the convergence valve
    proves the death (an issued-side filter would silently orphan exactly
    the experiments the sweep exists to catch, and disagree with itself
    across the recover bridge — probe_b76_round10.py J1).
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    owned = values.get("owned_experiment_uids") or []
    if not owned:
        # Hydration fallback (mirrors ``materialize_fault_handle``): legacy
        # checkpoints predate the birth registry, and DB-only recovery paths
        # never saw it — rebuild ownership from the provenance scan each
        # UID-bearing provider already implements. Compacted-away create
        # messages simply yield an empty set (no net, same as before the
        # fix); no false positives — provenance only ever names UIDs this
        # task's own creates proved.
        owned = sorted(FaultProviderRegistry.created_experiment_ids(
            values.get("messages") or [], values,
        ))
    if not owned:
        return []
    dead = FaultProviderRegistry.destroyed_proven_experiment_ids(
        values.get("messages") or []
    ) | set(values.get("retired_experiment_uids") or [])
    return [uid for uid in dict.fromkeys(owned) if uid and uid not in dead]


def has_live_fault(values: dict) -> bool:
    """True when *values* describe a fault the environment STILL carries.

    The LIVE twin of :func:`has_active_fault` (round-27). Round-25
    legislated the contract split — the committed predicate stays True
    forever after any injection (the handle projection has no death
    axis, and recovery targeting / postmortem / summary legitimately
    need the identity to survive the death) — but back then the live
    judgment existed only inline inside ``_emergency_recover``. Every
    other consumer that NEEDED the live answer (keep-across-seam,
    combo marking, zombie guard, graft source, auto-rollback) read the
    committed predicate because it was the only public one, each
    comment claiming "ALIVE"/"LIVE" while the code answered
    "ever-committed" (round-27 R1-R6). This predicate is that gate
    promoted to the single source:

    * experiment-carrier handle (kind ``experiment_uid``):
      :func:`live_liability_uids` decides (owned − retired −
      message-proven destroy — the UID axis's legislation, zero new
      assembly).
    * every other carrier (native mutations — no death oracle
      exists): the committed predicate. A missed live fault (residual
      environment damage) is strictly worse than a redundant check —
      the asymmetry round-25 accepted for the emergency gate, kept
      here verbatim.

    Degenerate shapes stay correct: a LIVE experiment that survived
    the replan seam (``keep_experiment_uid`` kept the UID, the seam
    cleared the method) hydrates through
    :func:`materialize_fault_handle` to an experiment-kind handle (the
    provider claims the bare UID slot), so the split still reaches the
    liability oracle — the seam's protected aftermath keeps its live
    verdict. Never-injected values return False (no provider claims
    the facts). COMBO caveat (inherited from round-25, on file): a
    stored experiment-kind handle whose experiment died while a
    native mutation stays live answers False — the native leg has no
    oracle, and the handle records only one carrier.
    """
    handle = materialize_fault_handle(values)
    if handle is None:
        return False
    if handle.get("kind") == "experiment_uid":
        return bool(live_liability_uids(values))
    return True


# ---------------------------------------------------------------------------
# TaskState — the task lifecycle vocabulary (B76 round-15 legislation)
#
# Round-14 legislated the verdict domains in verdict.py but left this
# domain as prose: the 10-word closed set existed only in the
# infer_task_state docstring while ~10 files hand-copied subsets of it
# (terminal frozensets, session-close tuples, status-map keys). The enum
# is the de-facto set — it predates this legislation unchanged.
# ---------------------------------------------------------------------------


class TaskState(StrEnum):
    """Task lifecycle states across injection + recovery."""

    INJECTING = "injecting"
    INJECTED = "injected"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    PARTIAL_RECOVERED = "partial_recovered"
    UNVERIFIED = "unverified"
    FAILED = "failed"
    REJECTED = "rejected"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


TASK_STATE_VALUES = frozenset(member.value for member in TaskState)

# Lifecycle states that carry a final verdict — the transient pair
# (injecting / recovering) excluded. Persisted-store flush guards and
# session-close gates derive from this, never hand-copy it.
TASK_STATE_TERMINAL_VALUES = frozenset(
    member.value for member in TaskState
    if member not in (TaskState.INJECTING, TaskState.RECOVERING)
)

# RETIRED from runtime duty (round-32): this set was the word-guessing
# form of the recoverability question — "which lifecycle words sound
# unfinished" — and every consumer has migrated to the evidence-backed
# ``tasks.liability_live`` verdict (rendered by
# ``persistence.task_store.may_carry_live_fault``; see the CLEARED mirror
# below). Kept as a vocabulary-archive constant for the reconciliation
# tests' display-coverage assertions and as the historical record of
# the round-16 S1 legislation (the TUI hand-copy that lost 'unverified'
# and silently hid live-fault tasks). Do NOT wire new runtime predicates
# to it — that re-opens the word-vs-liability confusion.
#
# Historical semantics (pre-round-32): task rows that may still carry a
# live fault — the "recoverable / pending" query set, with ``unverified``
# as the fail-closed member.
TASK_STATE_ACTIVE_VALUES = frozenset({
    TaskState.INJECTING.value,
    TaskState.INJECTED.value,
    TaskState.UNVERIFIED.value,
})


# Task rows whose task_state word itself PROVES the liability cleared (or
# never born) — the negative mirror of TASK_STATE_ACTIVE_VALUES. Its live
# consumer is the one-shot BACKFILL migration (round-32: the SQLite / PG
# "UPDATE tasks SET liability_live = 1 … AND task_state NOT IN (<cleared>)"
# pass that restores recovery entrances to already-blinded legacy rows; the
# migration reads this set so the words stay single-sourced, never
# hand-copied). The write-side predicate ``may_carry_live_fault`` does NOT
# consume this set — its clearing proof is VERDICT evidence
# (``_recovery_fully_cleared``: recover_verification.level /
# result.recovered pair), the round-33 legislation: the release channel
# for liability_live is the recover flow's own verdict, never a lifecycle
# word. Deliberately a strict subset of TERMINAL:
# ``failed`` / ``partial_recovered`` / ``unverified`` are verdict-terminal
# but NOT liability-clearing — a failed verification (L1 passed + L2
# failed) can sit on a live experiment (round-32 K2), a partial recovery
# means at least one fault may survive, and unverified is the fail-closed
# member. ``recovering`` is mid-flight by definition. Recovering those
# rows from the discoverable set because their VERDICT word sounds
# terminal is exactly the word-vs-liability confusion this axis's
# materialised column (tasks.liability_live) exists to retire.
TASK_STATE_CLEARED_VALUES = frozenset({
    TaskState.RECOVERED.value,
    TaskState.REJECTED.value,
    TaskState.COMPLETED.value,
    TaskState.CANCELLED.value,
})


def liability_group_for(task_state: str | None) -> str:
    """Three-group display split for liability-live rows (round-32b P3).

    The boot card's pending list groups its liability-live rows by what
    the ROW ITSELF claims vs what the ledger says — the group answers
    "why is this row here?" for a user scanning boot output:

    - ``in_flight`` — the lifecycle word is not terminal (injecting /
      recovering / waiting_input / pending / unknown / legacy SQLite
      words like ``running`` / ``interrupted``): the drill is still
      running, nothing to do yet.
    - ``needs_recovery`` — verdict-terminal but NOT liability-clearing
      (injected / unverified / failed / partial_recovered): the run
      stopped with the fault still on the books — the recover entry.
    - ``uncleared`` — a CLEARED word (recovered / rejected / completed
      / cancelled) over a live ledger: the row CLAIMS settlement while
      the wings stay unbalanced — the ghost family (round-32b C1's
      completed-haunt, sweep-failed residuals); the loudest group.

    Legislates the split off the SAME word tables the write-side
    predicate trusts (TERMINAL / CLEARED, single-sourced above) so the
    display layer never re-derives group membership from words — the
    TS side carries no word copy (the PENDING_STATES drift family
    stays retired; round-16 S1 / round-32 BC).
    """
    ts = task_state or ""
    if ts in TASK_STATE_CLEARED_VALUES:
        return "uncleared"
    if ts in TASK_STATE_TERMINAL_VALUES:
        return "needs_recovery"
    return "in_flight"


class TaskStateOverlay(StrEnum):
    """Persistence-layer session-overlay values in the task_state COLUMN.

    NOT agent state machine states: the graph never enters these, the
    persistence layer DERIVES them when materializing a row (round-17 D4
    legislation — until now the column's real value domain was two words
    wider than the TaskState legislation, and the round-16 S5 write gate
    would have rejected the very words the upsert write path emits).

    ``waiting_input`` — a session paused at an interrupt boundary
    (confirmation gate / ask_human); the TUI crash-recovery detector
    keys on it. ``pending`` — newborn anchor: a row with zero lifecycle
    evidence has not entered its pipeline yet (homonym warning: also a
    coarse infer_status domain member — different domain, same spelling).
    """

    WAITING_INPUT = "waiting_input"
    PENDING = "pending"


# The persistence-layer overlay slice (round-17 D4).
TASK_STATE_OVERLAY_VALUES = frozenset(
    member.value for member in TaskStateOverlay
)

# The task_state COLUMN's actual value domain: the lifecycle closed set
# plus the persistence overlay words. Write gates face this domain, not
# the bare TaskState set — the column legitimately holds both layers
# (two write paths: update_task_state direct writes, upsert's
# _infer_fields overlay derivation).
TASK_STATE_COLUMN_VALUES = TASK_STATE_VALUES | TASK_STATE_OVERLAY_VALUES


def recovery_task_state_from_level(
    level: str,
    *,
    recovered: bool,
    layer1_status: str = "",
) -> str:
    """Single-source recover verdict → task_state mapping (B76 round-15 D3/D4).

    Round-15 found three parallel hand implementations (infer_task_state's
    recover branch, recover_task_state_from_values, _recover_finalize's
    ternary) disagreeing on exactly one input combination. Every writer
    and reader now routes through this truth table:

      recovered=True              → partial_recovered iff level == "partial",
                                    else recovered
      level == "unverified"       → unverified (honest ignorance ≠ failure)
      layer1 skipped + level ∈
      RECOVER_SUCCESS_VALUES      → recovered / partial_recovered
                                    (non-ChaosBlade faults: Layer 2 is the
                                    only verification layer, so its verdict
                                    stands even without the result boolean)
      anything else               → failed

    Divergence-combo ruling (design D4, A semantics — verification
    authoritative): ``recovered=False + level="recovered" +
    layer1="skipped"`` → ``recovered``. Unreachable on the write path
    (recover finalize derives both the boolean and the level from the same
    verification dict), but hydrated legacy states can carry the pair and
    every reader must resolve it identically.
    """
    if recovered:
        return (
            TaskState.PARTIAL_RECOVERED.value
            if level == RecoverVerdict.PARTIAL.value
            else TaskState.RECOVERED.value
        )
    if level == RecoverVerdict.UNVERIFIED.value:
        return TaskState.UNVERIFIED.value
    if (
        layer1_status == Layer1Status.SKIPPED.value
        and level in RECOVER_SUCCESS_VALUES
    ):
        return (
            TaskState.PARTIAL_RECOVERED.value
            if level == RecoverVerdict.PARTIAL.value
            else TaskState.RECOVERED.value
        )
    return TaskState.FAILED.value


def infer_task_state(values: dict) -> str:
    """Infer the overall task_state from AgentState values.

    The closed set is :class:`TaskState` above (10 words — the docstring
    prose list predating the legislation lives there now). Lifecycle
    states reflecting the two major stages (injection + recovery):
      - injecting: fault injection in progress
      - injected: injection completed, fault is active, awaiting recovery
      - recovering: fault recovery in progress
      - recovered: fault has been fully recovered
      - partial_recovered: fault partially recovered
      - failed: injection or recovery failed
      - rejected: safety check rejected the injection
      - completed: non-injection intents (chat/recover-bridge)
    """
    # Non-injection intents — no fault lifecycle (must check FIRST)
    # "recover" intent in inject_graph is a bridge state (confirmed but
    # actual recovery happens in recover_graph), so it's also "completed".
    if values.get("confirmed_intent") in ("chat", "recover"):
        return "completed"

    operation = values.get("operation", "")
    safety_status = values.get("safety_status", "pending")
    has_fault = has_active_fault(values)
    verification = read_inject_verification(values)
    outcome = read_operation_outcome(values)
    error = outcome.error
    result = outcome.result or {}
    if not isinstance(result, dict):
        result = {}

    # Safety rejection. The word means the SAFETY GATE rejected this attempt —
    # not "any terminal rejection": the reject node deliberately leaves
    # ``safety_status`` alone (W-56-6 defect d), so a planning-timeout run that
    # died in the loop keeps whatever the gate last said ("pending" = it never
    # ran, "retry" = it sent the plan back) and lands on "failed" below.
    if safety_status == "rejected":
        return "rejected"

    # Dry-run terminal shape (round-61 R61-4/R61-4b): the /plan preview
    # terminates at route_after_confirmation ("end" for dry_run) BEFORE
    # any verification exists — the run's own deliverable IS the plan.
    # Without this branch the preview infers "injecting" → terminal
    # "failed", so a SUCCESSFUL preview recorded "failed" on its session
    # record AND stalled its TaskStore row at "injecting" (the upsert
    # path infers from this same function — one branch, both surfaces).
    # ``values["dry_run"]`` is the authoritative discriminator: the
    # pipeline input carries it straight from the TUI /plan context (and
    # the CLI dry-run thread before lift_dry_run_and_run flips it), so
    # the branch cannot collide with a REAL run — planning writes
    # ``plan_summary`` for confirmed runs too (extract_planning_metadata
    # feeds the confirm card), and once the gate sets
    # needs_confirmation=False a real run mid-flight carries the same
    # plan_summary shape. A dry_run=True thread with the gate closed and
    # no verification and no live fault has exactly one meaning: the
    # preview finished producing its plan.
    if (
        values.get("dry_run")
        and values.get("plan_summary")
        and values.get("needs_confirmation") is False
        and not verification
        and not has_fault
    ):
        return "completed"

    # Error — but replan in progress is not a failure, and if the
    # verifier already checked the actual fault state, defer to its
    # verdict instead of short-circuiting to "failed".
    if error:
        if (values.get("replan_count", 0) > 0 or values.get("verify_replan_count", 0) > 0) and values.get("replan_context"):
            pass  # Replan in progress, continue to normal state inference
        elif verification:
            # Error from execute_loop, but verifier checked the actual
            # state — fall through to verification-based logic below.
            pass
        else:
            return "failed"

    # Replan exhaustion: replan was attempted but graph completed without success.
    if (values.get("replan_count", 0) > 0 or values.get("verify_replan_count", 0) > 0) and values.get("replan_context"):
        if not has_fault and not verification:
            return "failed"

    # Recovery operation — the verdict→state truth table is single-sourced
    # in recovery_task_state_from_level (round-15 D3); this branch was
    # implementation A of three parallel copies. Level comes from the
    # verification dict (D4: verification authoritative over the result
    # mirror — the write path guarantees both carry the same value).
    if operation == "recover":
        recover_verification = read_recover_verification(values)
        if recover_verification:
            rv = recover_verification if isinstance(recover_verification, dict) else {}
            rl1 = rv.get("layer1") or {}
            return recovery_task_state_from_level(
                rv.get("level", ""),
                recovered=bool(result.get("recovered")),
                layer1_status=rl1.get("status", "") if isinstance(rl1, dict) else "",
            )
        return "recovering"

    # Injection lifecycle
    if not verification:
        # Still in injection process — the lifecycle position comes from the
        # verification verdict, not from handle presence (a handle proves a
        # creation/issue happened, not where the run is).
        return "injecting"

    # Verification done for injection
    layer1 = verification.get("layer1", {}) if isinstance(verification, dict) else {}
    layer2 = verification.get("layer2", {}) if isinstance(verification, dict) else {}
    l1_status = layer1.get("status")
    l1_pass = l1_status == "passed"
    l1_skip_no_chaos = l1_status == "skipped"  # non-ChaosBlade fault, Layer 1 not applicable
    l2_status = layer2.get("status", "unknown") if isinstance(layer2, dict) else "unknown"
    # ChaosBlade (L1 passed):
    if l1_pass and l2_status in ("passed", "skipped"):
        return "injected"
    # L2 "unknown": the verifier ran but produced no conclusion. Combined with
    # level, this is the "honest ignorance" zone — the LLM checked what it
    # could and reports it cannot tell. That is a knowledge claim of its own
    # ("unverified"), not evidence of failure: L1 shows the experiment Running.
    if l1_pass and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("unverified", "unknown"):
            return "unverified"
        return "injected"
    # L2 "partial": the LLM's Overall field is the authority
    # on whether the partial result is acceptable (e.g., timing delays) or a real failure.
    if l1_pass and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "injected"
        return "failed"
    # Non-ChaosBlade (L1 skipped): Layer 2 is the ONLY verification layer,
    # so "unknown" is NOT passing — the injection cannot be confirmed.
    if l1_skip_no_chaos and l2_status == "passed":
        return "injected"
    # Non-CB path L2=partial: defer to level (mirrors CB path logic)
    if l1_skip_no_chaos and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "injected"
        return "failed"
    # Non-CB path L2=unknown: Layer 2 is the only verification layer, so an
    # "unknown" is NOT passing — but honest ignorance ("unverified" level) is
    # still distinct from counter-evidence: it maps to "unverified", while any
    # other level without an L2 verdict stays failed (fail-closed).
    if l1_skip_no_chaos and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("unverified", "unknown"):
            return "unverified"
        return "failed"
    # L1 Warning (e.g., CLI timeout but fault may have taken effect):
    # L2 is the deciding factor — mirrors infer_phase() logic.
    if l1_status == "warning" and l2_status == "passed":
        return "injected"
    if l1_status == "warning" and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("unverified", "unknown"):
            return "unverified"
        return "injected"
    if l1_status == "warning" and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "injected"
        return "failed"
    # Side-effect confirmation: L1 passed + evidence destroyed by side-effect
    # → not a failure, but a valid drill finding (e.g., burn → OOMKill → restart)
    if l1_pass and l2_status == "recovered_before_observation":
        side_effects = verification.get("side_effects") if isinstance(verification, dict) else None
        if side_effects and any(v for v in side_effects.values() if v):
            return "injected"
        return "failed"
    return "failed"


def terminal_task_state(values: dict) -> str:
    """``infer_task_state`` for a run that has ENDED — no verdict means failed.

    ``infer_task_state`` answers "where is this run", so with no verification on
    record it answers ``injecting``: still in progress. At a terminal point that
    answer is not available — the run stopped, and what it describes is a run
    that ended without a verdict.

    The policy is that task state comes from verification: without one there is
    no basis to call the fault injected, no matter what handles exist. An
    experiment UID proves a creation request was accepted, not that the fault took
    effect. task-ff057e7f shipped ``status=success / task_state=injected`` with
    ``verification=null`` on a run whose own postmortem said it stalled without
    injecting. A verdict of "unverified" (verification ran, evidence unavailable)
    is passed through unchanged — it is a terminal knowledge claim, not
    "injecting".

    Shared by every terminal consumer (single-injection result builder, batch
    per-fault record) so the rule cannot be applied in one place and forgotten in
    the other — which is exactly how those two drifted apart.
    """
    state = infer_task_state(values)
    return "failed" if state == "injecting" else state


def graph_is_paused(snapshot) -> bool:
    """True when a graph snapshot sits at an interrupt boundary.

    The ENGINE is the authority on "has this run stopped or is it waiting
    for someone": a static ``interrupt()`` leaves the checkpoint with a
    non-empty ``next`` and returns the in-flight values to ``ainvoke``
    without raising. Reading the values alone cannot answer the question
    — the same dict describes a run that ended without a verdict and a
    run that has not ended yet.

    Single source for the pause question (round-64 R4). Before this the
    answer was re-invented at six consumption sites in five different
    shapes (``if final_state.next`` in turn_result, a ``paused_at_interrupt``
    flag in turn_event_stream, ``has_active_fault`` in the CLI lift path,
    ``result="pending"`` in the HTTP route, a hand-set ``needs_confirm`` in
    the SSE route, ``_compute_inferred`` in the persistence layer) and
    omitted at four more — every omission translated "waiting for a human"
    into "the injection failed".
    """
    return bool(snapshot is not None and getattr(snapshot, "next", None))


def paused_task_state(values: dict) -> str | None:
    """``waiting_input`` when *values* describe a run parked at a gate, else None.

    The VALUES-side counterpart of :func:`graph_is_paused`, for callers
    that hold a state dict and no snapshot (the persistence row writer,
    result builders fed by ``ainvoke``'s return). Derivation is the one
    ``TaskStore._compute_inferred`` has shipped since round-17 D4:
    mid-flight word + a confirmation still owed + no committed fault.
    A fault already on the books means the gate ran and the run moved
    past it — the pause is history, not the current situation.
    """
    state = infer_task_state(values)
    if state not in ("injecting", "cancelled"):
        return None
    if not values.get("needs_confirmation"):
        return None
    if has_active_fault(values):
        return None
    return TaskStateOverlay.WAITING_INPUT.value


def resumable_pause(snapshot) -> bool:
    """True when *snapshot* is an inject pause a resume command can continue.

    Stricter than :func:`graph_is_paused` on purpose, because the session
    finalizer must tell two paused graphs apart:

    * the PIPELINE parked at ``confirmation_gate`` — the run is not over,
      ``blade-ai confirm`` / ``POST /confirm/{task_id}`` continues it, so
      closing the session would archive a live drill as a finished failure;
    * the INTENT / dialogue graph parked at ``intent_confirm`` — every
      consumer of that pause finalizes on purpose (the round-60 F4'''
      ruling: the turn route's finally reads the intent graph, whose pause
      is not a resumable inject).

    The discriminator is the confirmation contract itself: engine says
    paused AND a confirmation is still owed AND no fault committed yet —
    the same three facts :func:`paused_task_state` derives from values
    alone, so the snapshot-side and values-side answers cannot drift.
    """
    if not graph_is_paused(snapshot):
        return False
    values = getattr(snapshot, "values", None) or {}
    return paused_task_state(dict(values)) is not None


def infer_stage(values: dict) -> Optional[str]:
    """Infer the current major stage.

    Returns: injection / recovery / None
    - injection: fault injection in progress or completed (awaiting recovery)
    - recovery: fault recovery in progress or completed
    - None: non-injection intents (chat/recover-bridge) have no fault stage
    """
    # Non-injection intents — no fault stage (must check BEFORE operation)
    # "recover" intent in inject_graph is a bridge state (actual recovery
    # happens in recover_graph), so it has no injection/recovery stage here.
    if values.get("confirmed_intent") in ("chat", "recover"):
        return None

    operation = values.get("operation", "")

    if operation == "recover":
        return "recovery"

    return "injection"


def infer_phase(values: dict) -> str:
    """Infer the current phase within the major stage.

    Injection phases:  planning → safety_check → confirming → executing → verifying → verification_passed / verification_failed / replanning
    Recovery phases:   recovering → verifying → recovered / partial_recovered / verification_failed
    Non-injection intents (chat/recover-bridge): return None (no phase applicable)
    """
    # Non-injection intents — no fault phase applicable in inject_graph context.
    # "recover" intent in inject_graph is a bridge state (actual recovery
    # happens in recover_graph), so it has no injection phase either.
    intent = values.get("confirmed_intent")
    if intent in ("chat", "recover"):
        return None

    operation = values.get("operation", "")
    safety_status = values.get("safety_status", "pending")
    active_skill_name = read_active_skill_name(values)
    has_fault = has_active_fault(values)
    verification = read_inject_verification(values)
    outcome = read_operation_outcome(values)
    error = outcome.error
    needs_confirmation = values.get("needs_confirmation", False)

    if error:
        return "failed"
    if safety_status == "rejected":
        return "rejected"

    # Dry-Run preview (TUI `/plan`): once a plan_summary has been generated and
    # dry_run is still True, surface the dedicated phase so reviewers and the
    # status bar can tell this is a preview, not a real injection.
    if values.get("dry_run") and values.get("plan_summary"):
        return "dry_run_planned"

    # Replan in progress (Phase 2 errored, routed back to Phase 1)
    if values.get("replan_context") and (values.get("replan_count", 0) > 0 or values.get("verify_replan_count", 0) > 0):
        if not has_fault:
            return "replanning"

    # --- Recovery phases ---
    if operation == "recover":
        recover_verification = read_recover_verification(values)
        if recover_verification:
            result = outcome.result or {}
            if not isinstance(result, dict):
                result = {}
            rv = recover_verification if isinstance(recover_verification, dict) else {}
            rl1 = rv.get("layer1") or {}
            # Single-source mapping (round-16 S3): this branch was the
            # FOURTH parallel copy of the recover verdict → lifecycle
            # truth table — it read the result mirror while every
            # task_state reader honours the verification dict (D4), so a
            # mirror/verification divergence split phase from task_state
            # for the SAME state. Derive through the single source, then
            # render the phase word form.
            task_state = recovery_task_state_from_level(
                rv.get("level", ""),
                recovered=bool(result.get("recovered")),
                layer1_status=rl1.get("status", "") if isinstance(rl1, dict) else "",
            )
            # Phase-domain word forms: the phase vocabulary spells the
            # honest-ignorance and fail outcomes both as
            # "verification_failed" (P7 word-form split — a phase-domain
            # naming question deliberately left for a later round; this
            # round only removes the source divergence).
            if task_state == TaskState.RECOVERED.value:
                return "recovered"
            if task_state == TaskState.PARTIAL_RECOVERED.value:
                return "partial_recovered"
            return "verification_failed"
        return "recovering"

    # --- Injection phases ---
    if not active_skill_name and not has_fault:
        return "planning"
    if not active_skill_name and has_fault:
        # fault committed but skill not yet identified (edge case)
        return "executing"
    if needs_confirmation:
        return "confirming"
    if safety_status in ("safe", "warning") and not has_fault:
        return "safety_check"
    if not has_fault:
        return "planning"
    if not verification:
        return "executing"
    # Verification result
    layer1 = verification.get("layer1", {}) if isinstance(verification, dict) else {}
    layer2 = verification.get("layer2", {}) if isinstance(verification, dict) else {}
    l1_status = layer1.get("status")
    l1_pass = l1_status == "passed"
    l1_skip_no_chaos = l1_status == "skipped"
    l2_status = layer2.get("status", "unknown") if isinstance(layer2, dict) else "unknown"
    # ChaosBlade (L1 passed):
    if l1_pass and l2_status in ("passed", "skipped"):
        return "verification_passed"
    # L2 "unknown": LLM didn't produce a clear conclusion — check level
    if l1_pass and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level == "unverified":
            return "verification_failed"
        return "verification_passed"
    # L2 "partial": defer to LLM's Overall field
    if l1_pass and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "verification_passed"
        return "verification_failed"
    # Non-ChaosBlade (L1 skipped): L2 is the ONLY verification, "unknown" is NOT passing
    if l1_skip_no_chaos and l2_status in ("passed", "skipped"):
        return "verification_passed"
    # Non-CB path L2=partial: defer to verification level (mirrors CB path logic)
    if l1_skip_no_chaos and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "verification_passed"
        return "verification_failed"
    # Side-effect confirmation: L1 passed + container restart destroyed evidence
    # → not a failure, but a valid drill finding (e.g., burn → OOMKill → restart)
    if l1_pass and l2_status == "recovered_before_observation":
        side_effects = verification.get("side_effects") if isinstance(verification, dict) else None
        if side_effects and side_effects.get("container_restarts"):
            return "verification_passed"
        return "verification_failed"
    # L1 Warning (e.g., CLI timeout but CRD may exist): L2 is the deciding factor
    if l1_status == "warning" and l2_status == "passed":
        return "verification_passed"
    if l1_status == "warning" and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level == "unverified":
            return "verification_failed"
        return "verification_passed"
    if l1_status == "warning" and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "verification_passed"
        return "verification_failed"
    return "verification_failed"


def infer_inject_status(task_state: str, operation: str = "") -> str:
    """Infer the injection phase result from task state and operation.

    Returns: success / failed / in_progress / pending
    """
    # Recovery operation means injection already succeeded
    if operation == "recover":
        return "success"

    if task_state in ("injected", "recovering", "recovered", "partial_recovered"):
        return "success"
    if task_state == "injecting":
        return "in_progress"
    # "unverified" is a terminal knowledge claim (verification ran, evidence
    # unavailable). The coarse four-value domain has no "unknown": reporting
    # "pending" would mislead (the run has ENDED), so it falls to "failed" —
    # the fine-grained distinction lives in task_state / verification fields.
    if task_state in ("failed", "rejected", "unverified"):
        return "failed"
    return "pending"


def infer_recover_status(task_state: str, operation: str = "") -> str:
    """Infer the recovery phase result from task state and operation.

    Returns: success / failed / in_progress / pending
    """
    if task_state in ("recovered", "partial_recovered"):
        return "success"
    if task_state == "recovering":
        return "in_progress"
    # "unverified" mirrors infer_inject_status: a terminal knowledge claim
    # (recovery verification ran, observation channel unavailable). The coarse
    # four-value domain has no "unknown" and the run has ENDED, so "pending"
    # would mislead; it falls to "failed" — the fine-grained distinction
    # lives in task_state / verification fields.
    if task_state in ("failed", "unverified") and operation == "recover":
        return "failed"
    return "pending"


def infer_status(stage: Optional[str], task_state: str, operation: str = "") -> Optional[str]:
    """Infer the current stage's status (unified for injection/recovery).

    Returns: success / failed / in_progress / pending / None
    - injection stage: delegates to infer_inject_status()
    - recovery stage: delegates to infer_recover_status()
    - None (chat intent): returns None (no fault status applicable)
    """
    if stage == "injection":
        return infer_inject_status(task_state, operation)
    elif stage == "recovery":
        return infer_recover_status(task_state, operation)
    return None


def strip_side_effects(verification: dict | None) -> dict | None:
    """Remove internal-only side_effects field from verification dict.

    side_effects is used by infer_phase for result mapping. We strip it
    from the *verification* subdict so older API consumers don't see an
    unexpected nested field, but ``build_status_data`` re-exposes it at
    the top level (``data["side_effects"]``) — UIs need it to surface
    "your fault caused a real container restart" signal to operators.
    """
    if not verification or not isinstance(verification, dict):
        return verification
    v = dict(verification)
    v.pop("side_effects", None)
    return v


def _extract_side_effects(verification: dict | None) -> dict:
    """Pull ``side_effects`` out of verification before it gets stripped.

    Returns a plain dict (possibly empty) so callers can branch on truthiness
    without re-coalescing None. The two known signal shapes are:
      - ``container_restarts``: list of ``{pod, restart_count, reason, note}``
      - any future signal we add (kept generic on purpose)
    """
    if not isinstance(verification, dict):
        return {}
    raw = verification.get("side_effects")
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _build_side_effects_summary(verification: dict | None, profile: str | None = None) -> str:
    """Build a one-line summary of the run's side-effect detection results.

    Enumerates every detector category with its count so the TUI can
    display "what was checked" regardless of whether issues were found.
    The summary is assembled here (backend) so new detectors automatically
    appear without a TUI release.

    Scoped to the run's ``profile`` (k8s/host): only that profile's detectors
    actually run (see ``run_all_detectors``), so listing the cross-profile
    union would pad the breakdown with foreign categories stuck at ``: 0``
    (host labels on a k8s run and vice versa). ``profile=None`` falls back to
    the k8s group.
    """
    from chaos_agent.agent.nodes.side_effect._side_effect_detectors import detectors_for

    if not isinstance(verification, dict):
        return ""
    raw = verification.get("side_effects")
    detected = dict(raw) if isinstance(raw, dict) else {}

    _KEY_LABELS = {
        "container_restarts": "ContainerRestarts",
        "evicted_pods": "EvictedPods",
        "oom_killed_pods": "OOMKill",
        "crash_loop_pods": "CrashLoop",
        "endpoint_removals": "EndpointRemovals",
        "hpa_scaling": "HPAScaling",
        "probe_failures": "ProbeFailures",
        "dependency_errors": "DependencyErrors",
        "process_deaths": "ProcessDeaths",
        "filesystem_full": "FilesystemFull",
        "dmesg_oom": "KernelOOM",
        "service_down": "ServiceDown",
    }

    parts = []
    for d in detectors_for(profile):
        items = detected.get(d.key, [])
        count = len(items) if isinstance(items, list) else 0
        label = _KEY_LABELS.get(d.key, d.key)
        parts.append(f"{label}: {count}")

    total = sum(len(v) for v in detected.values() if isinstance(v, list))
    if total == 0:
        return f"No collateral impact detected ({', '.join(parts)})"
    return f"{total} collateral impact(s) detected ({', '.join(parts)})"


def _derive_failure_reason(values: dict) -> str:
    """Derive a failure_reason string from state, preferring failure_detail."""
    return read_failure_reason(values)


def extract_ui_diagnostics(values: dict) -> dict:
    """Return the UI-visible diagnostic fields for a result envelope payload.

    Centralizes which fields propagate from AgentState into stream events,
    so all production sites (cli/runner.py, server/routes/inject_stream.py)
    surface the same set without recopying boilerplate. Anything that flows
    through here becomes visible in `render_result` via `_read_diagnostic`.
    """
    outcome = read_operation_outcome(values)
    failure_detail = outcome.failure_detail
    failure_reason = outcome.failure_reason

    verification = read_inject_verification(values)
    # Capability profile of the run (k8s/host) — mirrors se_detect's derivation
    # so the side-effect summary lists only the profile that actually ran.
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    from chaos_agent.agent.spec.feasibility import profile_for_spec

    spec = read_fault_spec(values)
    profile = profile_for_spec(spec) if spec else None
    return {
        "failure_reason": failure_reason,
        "failure_detail": failure_detail,
        "replan_count": int(values.get("replan_count") or 0),
        "verify_replan_count": int(values.get("verify_replan_count") or 0),
        "replan_history": list(values.get("replan_history") or []),
        "side_effects": _extract_side_effects(verification),
        "side_effects_summary": _build_side_effects_summary(verification, profile),
    }


def duration_ms_from_timestamps(created_at: str, finished_at: str) -> int:
    """Derive wall-clock duration (ms) from ISO timestamps; 0 when underivable.

    Single source for the created_at→finished_at derivation shared by
    ``build_status_data`` (read path) and ``sync_to_store`` (write path,
    W-55-11: the tasks.duration_ms column used to stay 0 for every
    inject row because no writer ever computed it — the only fallback
    was the read-side recompute in ``get_metric``).
    """
    if not created_at or not finished_at:
        return 0
    try:
        ct = parse_iso_timestamp(created_at)
        ft = parse_iso_timestamp(finished_at)
        return int((ft - ct).total_seconds() * 1000)
    except (ValueError, TypeError):
        return 0


def build_status_data(task_id: str, values: dict) -> dict:
    """Build a complete status data dict from LangGraph checkpoint values.

    Used by both CLI AgentRunner.status() and Server status route.
    """

    from chaos_agent.agent.spec.fault_spec import (
        fault_type_from_state,
        legacy_params_dict,
        legacy_target_dict,
    )

    active_skill_name = read_active_skill_name(values)
    experiment_uid = values.get("experiment_uid") or ""
    blade_params = legacy_params_dict(values)
    target = legacy_target_dict(values)
    verification = read_inject_verification(values)
    safety_reason = values.get("safety_reason") or ""

    fault_type = fault_type_from_state(values)

    # Timestamps
    created_at = values.get("created_at") or ""
    finished_at = values.get("finished_at") or ""

    # Calculate duration (single-sourced derivation, W-55-11)
    duration_ms = duration_ms_from_timestamps(created_at, finished_at)

    outcome = read_operation_outcome(values)
    failure_detail = outcome.failure_detail
    failure_reason_raw = outcome.failure_reason
    merged_error = outcome.error

    task_state = infer_task_state(values)
    stage = infer_stage(values)
    status = infer_status(stage, task_state, values.get("operation", ""))

    data = {
        "task_id": task_id,
        "stage": stage,
        "status": status,
        "phase": infer_phase(values),
        "fault_type": fault_type,
        "skill_name": active_skill_name,
        "active_skill_name": active_skill_name,
        "target": target,
        "params": blade_params or None,
        "experiment_uid": experiment_uid,
        "safety_status": values.get("safety_status", "pending"),
        "safety_reason": safety_reason,
        "needs_confirm": values.get("needs_confirmation", False),
        "verification": strip_side_effects(verification),
        "recover_verification": strip_side_effects(read_recover_verification(values)),
        "side_effects": _extract_side_effects(verification),
        "plan_summary": values.get("plan_summary", ""),
        "error": merged_error,
        "failure_reason": failure_reason_raw,
        "failure_detail": failure_detail,
        "intent_confidence": float(values.get("intent_confidence") or 0.0),
        "replan_count": int(values.get("replan_count") or 0),
        "verify_replan_count": int(values.get("verify_replan_count") or 0),
        "replan_history": list(values.get("replan_history") or []),
        "created_at": created_at,
        "updated_at": now_iso(),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }
    if values.get("baseline_data"):
        data["baseline_data"] = values["baseline_data"]

    return data



class AgentState(MessagesState):
    """State for the Chaos Engineering Agent inject/recover graphs.

    Fields are grouped by lifecycle phase. Within each group, fields
    appear in the order they are typically populated.
    """

    # ── Core Identity ──────────────────────────────────────────────
    messages: Annotated[list, _ts_add_messages]
    task_id: str = ""
    tui_session_id: str = ""             # Owning TUI session (empty for non-TUI callers)
    parent_task_id: str = ""             # For recover: the inject task_id being recovered
    operation: str = ""                  # inject / recover / chat
    tenant_id: str = ""                  # Multi-tenant isolation key (SDK platform mode)
    workspace_id: str = ""               # Workspace isolation key (platform mode; empty = unfiltered, same contract as tenant_id)

    # ── Intent & Input ─────────────────────────────────────────────
    input: Optional[str] = None          # NL description (entry-point routing only)
    confirmed_intent: Optional[str] = None   # "inject" | "recover" | "chat" | None
    interaction_mode: str = "cli"        # "cli" / "tui"
    intent_context: Optional[str] = None     # Intent description text (passed to planning node)
    intent_confidence: float = 0.0       # Confidence score 0.0-1.0
    clarification_round: int = 0         # User turns spent clarifying the intent before submission (opening turn excluded; a turn that only replayed a reviewed contract is refunded, one that bootstrapped it is not)
    dialogue_round: int = 0              # Overall dialogue round tracking (chat + clarification)
    intent_reasoning: Optional[str] = None   # LLM classification reasoning (audit trail)
    needs_task_selection: bool = False    # RECOVER intent needs user to pick a task
    recover_task_id: Optional[str] = None    # task_id of the inject experiment to recover
    dry_run: bool = False                # TUI /plan dry-run mode

    # ── Planning ───────────────────────────────────────────────────
    skill_name: Optional[str] = None
    fault_spec: Optional[dict] = None    # FaultSpec dict (see chaos_agent.agent.spec.fault_spec)
    skill_case_content: Optional[str] = None     # Full content of the matched skill use-case file
    plan: Optional[str] = None
    plan_summary: str = ""               # Human-facing execution preview / dry-run summary
    plan_path: Optional[str] = None      # saved plan file path (memory/plan/{task_id}.md)
    is_complex: Optional[bool] = None    # True if task requires a formal plan document
    plan_verification: Optional[str] = None  # plan's Verification Methods + Expected Impact slices for the verifier
    planning_rejected: bool = False      # Planning exit rejected or nudged (several writers); edge routes back
    _planning_rejection_reason: Optional[str] = None  # LLM rejection_reason for fail diagnosis
    _planning_alternatives: str = ""     # LLM-proposed alternatives after planning rejection
    _catalogue_rejection_nudged: bool = False  # Guard: nudge only once before accepting rejection
    _identity_declaration_nudged: bool = False  # Guard: nudge once for an undeclared fault identity (B83)
    _identity_split_nudged: bool = False        # Guard: nudge once for a declaration/reviewed-identity split (B84)
    _plan_text_stall_count: int = 0      # Guard: consecutive Phase-1 text-only stalls (no tool/skill); reset on progress, fail at max_plan_text_stalls
    plan_builder_round: int = 0          # Dialogue round counter within plan_builder
    planning_mode: Optional[str] = None  # None=auto; "guided" (options) | "expert" (direct structured plan)
    plan_builder_prefetch_done: bool = False  # Guided orchestration read-only discovery sent
    plan_confirmed: bool = False         # submit_plan completed; /run routes to safety_check

    # ── Safety ─────────────────────────────────────────────────────
    # ``safety_status`` is the run's last-safety-verdict word and drives
    # task_state / status payloads / operator reports. Two writer domains
    # hold authority for the terminal value "rejected" (pinned in
    # tests/test_agent/test_write_contracts.py):
    #   * the safety gates (safety_check / confirmation_gate /
    #     _write_set_boundary) — a safety verdict, always with a fresh
    #     ``safety_reason`` beside it;
    #   * agent_loop's transport exit — a configuration failure, written
    #     together with ``planning_rejected`` (the node's other entry, its
    #     count-cap branch, is unreachable: the router rejects at
    #     ``count >= settings.max_agent_loop`` — the value ``MAX_AGENT_LOOP``
    #     snapshots — before a larger count can reach the node).
    # Terminal nodes only: stamping "rejected" mid-loop flips the
    # task_state derivation (failed → rejected), so every writer above is a
    # terminal exit by construction.
    safety_status: str = "pending"       # pending / safe / unsafe / warning / rejected / retry
    safety_reason: Optional[str] = None
    safety_checked_detail: Optional[str] = None
    conflict_uids: Optional[list[str]] = None    # UIDs of existing active experiments
    # Create-reconcile gate state (outcome-uncertain gate-armed create):
    # registered by the execute-loop three-state scan when the latest
    # gate-armed create ToolMessage carries the uncertain marker (which
    # creates are gate-armed is declared provider-side, consumed through
    # the registry seam), consumed by the retry gate.
    # Shape: {"fingerprint": {namespace, labels, target_names,
    # scope_target_action}, "uncertain_call_id": <tool_call_id of the
    # uncertain return>, "blocked_count": int, "gate_reconciled": bool}.
    # Per-fault lifecycle (batch reset), never durable.
    create_reconcile: Optional[dict] = None
    safety_score: Optional[dict] = None          # Multi-dimensional numeric safety score (dict form)
    blast_radius_scope: Optional[str] = None     # "target-only" | "namespace-wide" | "cluster-wide"
    blast_radius_detail: Optional[str] = None
    target_health_report: Optional[dict] = None  # HealthReport dict (see chaos_agent.agent.target_health)
    feasibility_report: Optional[dict] = None    # FeasibilityReport dict (see chaos_agent.agent.spec.feasibility)

    # ── Confirmation ───────────────────────────────────────────────
    needs_confirmation: bool = False
    approved_target: Optional[dict] = None   # ApprovedTarget dict (see chaos_agent.agent.target_guard)
    drift_reject_count: int = 0          # Target-change rejection counter
    plan_change_reject_count: int = 0    # Replan fault type switch rejection counter
    # Task-lifetime budget of CLI auto-approvals (B76 review E1): monotonic,
    # never reset by an approval itself — each auto-approval spends one unit
    # and the seam also resets replan/execute budgets, so an unbounded number
    # of them would reset agent_loop_count every round and disarm the
    # MAX_AGENT_LOOP unbounded-loop defence (agent_loop L376).
    plan_change_auto_approve_count: int = 0
    screener_route: Optional[str] = None # Transient routing hint: "pass" / "retry" / "replan"
    # Set by agent_loop / execute_loop when the LLM response was cut off by the
    # output token limit: its tool calls were answered with synthetic errors and
    # must NOT reach the ToolNode (truncated args can parse yet be incomplete).
    # The screener consumes and clears it, routing back to the loop instead.
    truncated_tool_calls: bool = False
    # Set by execute_loop's create-reconcile gate when it HELD a batch (a
    # same-fingerprint gate-armed create retry after a result-uncertain
    # create): the whole batch was answered with fabricated ToolMessages
    # and must not reach the ToolNode. Consumed and cleared by the
    # screener, same transient lifecycle as truncated_tool_calls.
    _reconcile_gate_blocked: bool = False
    # Unattended write-set boundary exit payload (confirmation_gate's
    # WRITE_SET_BOUNDARY terminal): the case manifest's entries beyond the
    # victim coverage + interactive re-run guidance, machine-readable for
    # pipeline consumption. None for every other outcome.
    write_set_boundary: Optional[dict] = None

    # ── Execution ──────────────────────────────────────────────────
    experiment_uid: Optional[str] = None   # experiment UID attribution field (renamed from experiment_uid in phase-9; legacy checkpoints hydrate via the read fallback). Generic consumers use fault_handle.
    # Carrier-agnostic handle of the live fault, written by the execute loop's
    # attribution sync and consumed by every generic decision point via
    # ``has_active_fault`` / ``materialize_fault_handle``. Shape is owned by
    # the provider that built it: ``{"kind": "experiment_uid", "value": <uid>,
    # "method": <method>}`` for experiment carriers (kind renamed off
    # ``"blade_uid"`` in phase-14 G7), ``{"kind": "native",
    # "method": "kubectl_native"|"host_native"}`` for UID-less carriers.
    # None = no committed fault (or the attribution was cleared at a seam).
    fault_handle: Optional[dict] = None
    # UIDs retired by FRAMEWORK-side cleanup (verify-replan residual destroy).
    # Such destroys run in code, so they leave NO blade_destroy ToolMessage in
    # history and _collect_destroyed_uids cannot see them; without this list a
    # stale UID gets re-extracted into experiment_uid and misroutes the verifier
    # Layer-1 onto a destroyed experiment (task-29848471). Append-only for the
    # whole task lifetime — the death wing of the liability ledger
    # (owned_experiment_uids is the birth wing): it must survive batch advance
    # (whose message wipe would otherwise erase the only death proof) and cross
    # into the recover graph (whose final sweep would otherwise repeat-destroy
    # already-retired experiments — B76 review H).
    retired_experiment_uids: Optional[list[str]] = None
    # Birth registry (B76 review G): every experiment UID this task ever saw
    # extracted, appended at the execute-loop detection seam the moment it is
    # first seen. This is the ownership record the single ``experiment_uid``
    # slot structurally cannot hold — the slot is last-write-wins (correct for
    # attribution) so a superseded experiment's recovery claim vanishes with
    # the overwrite, leaving a live-orphan nothing can re-discover: the
    # message scan returns only the newest UID, and the destroy whitelist's
    # durable source is that same slot. Append-only for the whole task
    # lifetime: contract replacement, batch advance and attribution resets
    # never touch it (intent changes do not dissolve cluster side effects);
    # death is proven elsewhere (destroy scan + retired registry) and read
    # through :func:`live_liability_uids`.
    owned_experiment_uids: Optional[list[str]] = None
    injection_method: Optional[str] = None   # "host_blade" | "kubectl_exec" | "kubectl_native" | "host_native" | "python_agent"
    # Combo injection marker (durable, both orders): a kubectl-native mutating
    # injection was issued ALONGSIDE an experiment-carrying method — either
    # native-first (UPGRADE records it) or blade-first (issue-time recording).
    # Recovery routes such tasks to the LLM-driven Layer-1 flow: deterministic
    # recovery can ONLY destroy the blade experiment and would leak the native
    # mutation, while the LLM route is the superset executor (it covers the
    # blade part deterministically first, then undoes the native component).
    combo_native_issued: Optional[bool] = None
    # Attribution epoch boundary: message count recorded at each replan seam
    # (``reset_attribution_state``). The RESUME injection re-detection scan
    # reads only messages after this index, so pre-seam attempts of the
    # invalidated contract cannot be re-attributed as the new fault's
    # injection (task-5193538b). None = first epoch → scan full history.
    attribution_epoch_index: Optional[int] = None
    execution_artifacts: Optional[list[dict]] = None  # Durable created/modified resources for guard/recover
    kubectl_exec_pod_name: Optional[str] = None  # Tool pod used during kubectl exec injection
    # Injection vehicles confirmed by LIVE cluster discovery (label-selector
    # tool-pod lookup, same mechanism baseline/conflict checks use). The
    # screener consults it — alongside task-registered artifacts — before
    # reading an exec into such a pod as identity drift. Cluster fact, not
    # a naming convention.
    known_vehicle_pods: Optional[tuple[str, ...]] = None
    # Pod names probed by that same discovery and proven NOT vehicles. A
    # bounded negative cache: without it a genuine drift would re-probe the
    # cluster on every screener iteration — in-band kubectl on the very API
    # path a network fault may be severing (self-poisoning).
    vehicle_probe_misses: Optional[tuple[str, ...]] = None
    # Pod → nodeName bindings resolved by the screener for exec-vehicle
    # node binding (host-level ``blade create`` inside a tool pod carries
    # no selector; the fault lands on the pod's host node). Cached so the
    # binding is probed once per pod per task, never per screener round.
    # Entries are (pod_name, node_name) pairs; an empty node string means
    # the probe failed (negative entry, keeps fail-closed review).
    exec_pod_node_bindings: Optional[tuple[tuple[str, str], ...]] = None
    # Label selector → current pod names, resolved by the screener for the
    # selector cross-shape comparison (labels-vs-names): the guard policy
    # compares selectors statically and cannot see that an approved name set
    # and an executed label selector pick the same pods. Cached so each
    # (namespace, selector) is probed once per task, never per screener
    # round. Entries are (probe_key, names_tuple) pairs; an empty tuple
    # means the probe failed or matched nothing (negative entry, keeps the
    # fail-closed review).
    selector_name_probes: Optional[tuple[tuple[str, tuple[str, ...]], ...]] = None
    injection_parsed_params: Optional[dict] = None  # issue-time parsed injection parameters, e.g. {"path": "/tmp", "percent": "85"}
    original_replicas: Optional[dict] = None     # kubectl scale-based faults: {resource -> count}
    kubeconfig: Optional[str] = None
    kube_context: Optional[str] = None
    kubewiz_cluster_uuid: Optional[str] = None
    kubewiz_profile: Optional[str] = None
    # Explicit channel override (per-session); empty = field-based inference.
    kube_connection_mode: Optional[str] = None
    # Host transport parameters (per-session override)
    host_name: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key_path: Optional[str] = None
    ssh_port: Optional[int] = None
    inject_context: Optional[str] = None     # Inject-phase context for recover LLM
    # Recover-only: collateral side effects recorded by the inject task
    # (from its persisted result data). Layer 1 must undo/reconcile them;
    # Layer 2 must verify each one. Distinct from verification-internal
    # side effects, which are stripped before persistence.
    side_effects: Optional[dict] = None
    baseline_data: Optional[dict] = None     # Pre-injection baseline (from baseline_capture node)
    target_metadata: Optional[dict] = None   # {pod_memory_limit_mb, active_same_action_experiments, ...}
    disk_burn_post_check: Optional[dict] = None   # Post-injection I/O throughput verification
    disk_fill_post_check: Optional[dict] = None   # Post-injection fill file verification
    se_snapshot: Optional[dict] = None       # Pre-injection side-effect snapshot
    force_override: bool = False             # CLI --force-override flag
    _execute_text_stall_count: int = 0       # Guard: consecutive text-only stalls (no tool/injection/replan); reset on any tool call, fail at max_execute_text_stalls
    _injection_selfcheck_nudged: bool = False  # Guard: emit multi-step injection step self-check only once
    batch_submit_args: Optional[dict] = None     # Multi-fault submit_plan args
    current_fault_index: int = 0
    batch_results: Optional[list] = None

    # ── Verification ───────────────────────────────────────────────
    verification: Optional[dict] = None          # Two-layer: layer1=blade_status, layer2=fault-specific
    recover_verification: Optional[dict] = None
    inject_layer1_cache: Optional[dict] = None   # Persisted across ReAct iterations
    recover_layer1_cache: Optional[dict] = None
    # Combo recovery: the deterministic blade-destroy verdict, captured before
    # the LLM-driven Layer-1 flow undoes the native component. Merged into the
    # final Layer-1 verdict when the LLM concludes (either part failing fails
    # the composite — a partially undone fault is still active).
    combo_blade_part: Optional[dict] = None
    metric_observations: Optional[list[dict]] = None  # Structured observation timeline (all nodes)
    inject_verification_summary: Optional[str] = None  # Layer 2 observations for recover baseline
    reverify_count: int = 0              # Re-verification attempt count
    reverify_gaps: Optional[list[str]] = None    # Gap types that triggered re-verification
    cleaned_debug_pods: Optional[list[str]] = None   # Debug pods already cleaned up (exactly-once)

    # ── Recovery ───────────────────────────────────────────────────
    recover_phase: str = "layer1_recovery"   # "layer1_recovery" | "layer2_verification"
    # "deterministic" | "llm_driven". Written by the flow that runs Layer 1:
    # the LLM flow at its layer transitions ("llm_driven"), the simple
    # entry / the first Layer-2 iteration after a deterministic destroy.
    # None (legacy checkpoints) falls back at each reader to the dispatched
    # provider's deterministic-recover capability.
    recover_layer1_type: Optional[str] = None
    layer1_iteration_count: int = 0
    layer2_context_added: bool = False       # Non-ChaosBlade Layer 2 may start at count > 1
    recover_layer2_first: bool = False       # Verdict on first Layer 2 turn (anti-laziness guard)
    # B51 (case #33): verifier_loop_count pinned at Layer 2's first iteration,
    # so the convergence hint can gate on the Layer-2-LOCAL count. MUST be
    # declared here: LangGraph silently drops node updates for keys absent
    # from the state schema — an undeclared pin never persists across real
    # graph iterations (hand-built-state unit tests would stay green while
    # the gate silently reverts to the shared counter; verified empirically).
    layer2_start_count: int = 0

    # ── Loop Control ───────────────────────────────────────────────
    agent_loop_count: int = 0
    execute_loop_count: int = 0
    verifier_loop_count: int = 0
    # How many times each corrective hint has been issued, keyed by
    # ``"<kind>:<key>"``. Lives on state rather than being counted from the hint
    # MESSAGES because compaction rewrites ``messages`` and leaves other fields
    # alone: a hint is recorded at its first occurrence, which is early in the
    # history, so once the drill outgrows ``reserve_tokens`` that message lands in
    # the summarised half and is removed. Counting from messages then resets to
    # zero and the model is told "reminder #1" after twenty real occurrences —
    # exactly the amnesia the persistence was added to fix.
    hint_repeat_counts: dict[str, int] = {}
    pipeline_started_at: float = 0.0     # Wall-clock guard (0.0 = not yet stamped)
    transient_retry_count: int = 0       # INFRA_TRANSIENT short-retry budget
    # Deferred LIFECYCLE REVIEW text for a REJECTED plan_invalid replan arriving
    # over the tool channel. It cannot be appended at rejection time: the
    # request_replan ToolMessage does not exist yet (phase2_tools produces it),
    # and a HumanMessage between the AIMessage and its ToolMessage would break
    # tool-response adjacency. The next execute_loop iteration emits it once,
    # then clears the flag.
    _replan_review_rejection: str | None = None
    pipeline_attempt: int = 0            # Attempt tracking (incremented by begin_attempt)
    pipeline_attempts_history: Optional[list] = None
    replan_requested: bool = False
    replan_count: int = 0
    replan_request: Optional[dict] = None       # Structured Phase 2 -> Phase 1 request
    replan_context: Optional[dict] = None
    replan_history: Optional[list] = None
    replan_context_injected_attempt: Optional[int] = None  # One persisted handoff per replan attempt
    _replan_loop_reset: Optional[int] = None  # Tracks total replan count (execute + verify) that has been loop-reset
    verify_replan_count: int = 0              # Independent counter for verify-triggered replan

    # ── Results ────────────────────────────────────────────────────
    result: Optional[dict] = None
    error: Optional[str] = None
    failure_reason: Optional[str] = None
    failure_detail: Optional[dict] = None    # FailureDetail dict (category + context + llm_analysis)
    postmortem: Optional[dict] = None        # {"path": str, "markdown": str, "summary": str}
    issue_report: Optional[dict] = None      # {"status": str, "issue_url"?, "error"?, "message"?, "archive_path"?}
    created_at: Optional[str] = None         # ISO 8601
    finished_at: Optional[str] = None        # ISO 8601
    injection_start_time: Optional[str] = None   # ISO 8601, set when blade_create succeeds
    # ISO 8601, stamped at the VERIFIER entry — i.e. the moment the
    # execute-loop concluded and the fault is present with execution
    # wrapped up. This is the fault-window hold's window ORIGIN
    # (``turn_hold_fault_window``): the contract window covers
    # "fault present, execute phase over", so verification time and hold
    # time both count against it. Distinct from ``injection_start_time``
    # (the blade_create moment): execute-loop work after the create
    # (UID reconcile, follow-up probes) must not erode the window.
    # Write-once per attempt — verifier self-loop re-entries keep the
    # first stamp. Cleared at every replan seam
    # (``reset_attribution_state``) and re-stamped on the next verifier
    # entry of the replanned attempt.
    injection_window_start_time: Optional[str] = None

    # ── Memory ─────────────────────────────────────────────────────
    compressed_summary: Optional[str] = None
    experiment_history: Optional[list] = None
    operational_notes: Optional[str] = None

    # ── Progress ledger ────────────────────────────────────────────
    # Model-maintained three-layer working note (anchor / state / log), written
    # via the ``update_progress`` tool and re-injected into the system prompt
    # each round. Lives OUTSIDE ``messages`` so it survives compaction untouched,
    # and is mirrored to the intent graph at any turn exit. See
    # ``chaos_agent.agent.progress_ledger``.
    # Annotated reducer (NOT a bare LastValue field — Case #46,
    # task inject-357401b8): ``update_progress`` and ``finish_execution`` both
    # write this channel via ``Command(update=...)``; a model batching both in
    # one turn hit "Can receive only one value per step", the graph died
    # mid-execute and the rollback failed the same way — the cleanup chain
    # never ran. The reducer merges same-base writes; see
    # ``merge_ledger_channel`` for the seam semantics.
    # Type is ``dict`` (NOT ``Optional[dict]``): BinaryOperatorAggregate seeds
    # its initial value by instantiating the annotated type — ``dict()`` gives
    # an empty dict (every write, including the FIRST one, runs the reducer),
    # while an uninstantiable ``Optional[dict]`` leaves the channel MISSING and
    # the first write bypasses the reducer entirely (direct store).
    progress_ledger: Annotated[dict, merge_ledger_channel]

    # ── Probe snapshot ────────────────────────────────────────────
    # Frozen record of what intent clarification established about the target
    # environment, harvested at ``intent_confirm`` approval time (before the
    # clarification history is trimmed away). Each entry is
    # ``{fact, source_tool, probed_at}`` — the per-fact timestamps are exactly
    # what ``progress_ledger.established_facts`` (a bare string list) cannot
    # carry. Rendered into the ``[FAULT INTENT]`` anchored message so the
    # original intent-time evidence stays visible through execute/verify,
    # after the ledger itself has been rewritten by later phases.
    probe_snapshot: Optional[dict] = None


class IntentState(MessagesState):
    """State for the Intent Graph (conversation layer).

    Contains only dialogue-level fields. Execution-level fields
    (experiment_uid, verification, safety_status, skill_name, etc.)
    live on AgentState in the Pipeline Graph.
    """

    messages: Annotated[list, _ts_add_messages]

    tui_session_id: str = ""
    interaction_mode: str = "tui"

    # Corrective-hint repeat counts; see AgentState.hint_repeat_counts.
    hint_repeat_counts: dict[str, int] = {}

    # Intent recognition
    confirmed_intent: Optional[str] = None
    intent_confidence: float = 0.0
    clarification_round: int = 0
    dialogue_round: int = 0
    intent_reasoning: Optional[str] = None

    # Recover target
    needs_task_selection: bool = False
    recover_task_id: Optional[str] = None

    # Pipeline dispatch
    pipeline_task_id: Optional[str] = None
    pipeline_result_summary: Optional[str] = None
    handoff_summary: Optional[str] = None

    # FaultSpec (converged from intent_clarification)
    fault_spec: Optional[dict] = None
    input: Optional[str] = None

    # Cross-graph bridge fields (tier1-speedup). These live on AgentState in the
    # Pipeline Graph where they are actually consumed (ledger re-injection into
    # planning prompts; probe-snapshot rendering into the [FAULT INTENT] anchor).
    # They exist HERE only so the writes made inside THIS graph are not silently
    # dropped by langgraph (unknown-channel updates vanish without error):
    # ``update_progress`` (bound to clarification tools) writes ``progress_ledger``;
    # ``intent_confirm``'s handoff commit writes ``probe_snapshot``. The dispatch
    # path then copies both into the pipeline's initial state (see
    # ``intent_handoff.PipelineHandoff`` / ``state_builders.build_inject_initial_state``).
    # Same Annotated reducer as AgentState.progress_ledger: this graph binds
    # ``update_progress`` to its clarification tools, so a model batching two
    # ledger writes in one turn would hit the same Case #46 crash here. Type
    # ``dict`` (not Optional) so the channel seeds empty and the first write
    # runs the reducer too (see AgentState's note).
    progress_ledger: Annotated[dict, merge_ledger_channel]
    probe_snapshot: Optional[dict] = None

    # Batch fault injection (from submit_batch_intent)
    batch_submit_args: Optional[dict] = None

    # Session-level
    kubeconfig: Optional[str] = None
    # Isolation axes ride the session (platform injects both via
    # blade_ai_context → interaction graph_input). They MUST be declared
    # here: langgraph silently DROPS unknown-channel inputs (verified —
    # see progress_ledger's precedent above), and without the declaration
    # load_memory / recover_handler in THIS graph read state.get(...)
    # as "" and query_active falls back to unfiltered — both a latent
    # tenant_id gap (pre-existing, fixed here) and the exact workspace
    # leak this column exists to close.
    tenant_id: str = ""
    workspace_id: str = ""
    kube_context: Optional[str] = None
    kubewiz_cluster_uuid: Optional[str] = None
    kubewiz_profile: Optional[str] = None
    # Explicit channel override (per-session); empty = field-based inference.
    kube_connection_mode: Optional[str] = None
    # Host transport parameters (per-session override)
    host_name: Optional[str] = None
    ssh_host: Optional[str] = None
    ssh_user: Optional[str] = None
    ssh_key_path: Optional[str] = None
    ssh_port: Optional[int] = None
    needs_confirmation: bool = False
    dry_run: bool = False
    # Planning style request from the TUI turn (guided/expert; None = auto —
    # AgentState.planning_mode documents the semantics). Must be DECLARED
    # here or langgraph silently drops the key from the route's input dict
    # (unknown-channel updates vanish without error, and inputs are filtered
    # by the same channel set): the /turn route writes it per turn, and the
    # pipeline lift in turn_event_stream.py reads it back via
    # ``iv.get("planning_mode")`` before build_inject_initial_state persists
    # the choice onto the pipeline graph. Undeclared, the value never left
    # the request body (found in review 2026-09-01: the field was written
    # from the route's first commit yet was the ONE key of eighteen with no
    # channel here).
    planning_mode: Optional[str] = None
    compressed_summary: Optional[str] = None
    operational_notes: Optional[str] = None

    # Task ID (allocated by _allocate_operation_task_id in intent_clarification)
    task_id: str = ""
