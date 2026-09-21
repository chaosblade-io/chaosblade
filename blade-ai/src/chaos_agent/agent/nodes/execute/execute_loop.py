"""Execute loop node: Phase 2 ReAct execution (follow skill instructions to call blade)."""

import json
import logging
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
    ToolMessage,
)

from chaos_agent.agent.node_names import EXECUTE_LOOP
from chaos_agent.agent.execution_artifacts import (
    cleanup_debug_pod_artifacts,
    collect_execution_artifacts,
)
from chaos_agent.agent.capabilities import (
    build_capability_context,
    filter_tools_for_context,
)
from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
    inject_task_id_into_tool_calls,
    sync_kubewiz_runtime,
)
# Baseline-pair lifecycle (change stale-baseline-pair-seam-cleanup): the
# replan seam must remove the synthetic pair by its stable ids — imported
# from the construction source instead of copying literals, so an id rename
# cannot silently disarm the cleanup. Verify's package __init__ is a pure
# docstring (no transitive imports), keeping this cross-node import acyclic.
from chaos_agent.agent.nodes.verify._verifier_messages import (
    BASELINE_PAIR_MESSAGE_IDS,
)
from chaos_agent.agent.nodes.store._store_sync import sync_to_store
from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    build_stagnation_hint,
    persist_corrective_hint,
    persist_replaceable_hint,
    filter_stagnant_tool,
    post_invoke_debug,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
    detect_transient_retry_exhaustion,
    emit_debug_tool_messages,
    extract_rejected_params,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    handle_truncated_response,
)
from chaos_agent.agent.replan import (
    REQUEST_REPLAN_TOOL_NAME,
    ReplanRequest,
    parse_replan_request,
)
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.target_guard.types import GuardVerdict
from chaos_agent.agent.tool_verdicts import tool_result_failed
from chaos_agent.agent.state import (
    AgentState,
    has_active_fault,
    has_live_fault,
    live_liability_uids,
)
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.errors import ErrorAction, classify_error
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

MAX_EXECUTE_LOOP = settings.max_execute_loop

# Phase 1 → Phase 2 seam detection: the anchor ``finish_planning`` returns on
# success (factory.py) and the marker identifying the kickoff message this
# module emits right after a fresh finalization (see _maybe_build_phase2_kickoff).
_PLANNING_FINALIZED_PREFIX = "Planning finalized"
_PHASE2_KICKOFF_MARKER = "**PHASE 2 — EXECUTE NOW**"
# The stall-guard nudge (_detect_terminal_conclusion) already announces the
# phase transition; a kickoff after it would be a duplicate signal.
_EXECUTION_REQUIRED_MARKER = "**EXECUTION REQUIRED**"


# The ledger terminal-phase vocabulary and merge seam (C1): the module
# needs them at import time (reset_attribution_state reads the frozen set
# at call time, but importing names here keeps the single-source import
# discipline — progress.py owns the vocabulary, this module only borrows).
from chaos_agent.tools.progress import EXECUTION_COMPLETE_PHASES  # noqa: E402
from chaos_agent.agent.progress_ledger import merge_progress_ledger  # noqa: E402


def _ledger_declares_execution_complete(state) -> bool:
    """Thin wrapper so the tail gates read one line, import-free.

    Delegates to :func:`chaos_agent.tools.progress.ledger_declares_execution_complete`
    (imported lazily — the tools package imports cleanly standalone, but a
    module-level import here would drag the tool surface into every
    consumer that only wants the router's routing helpers)."""
    from chaos_agent.tools.progress import ledger_declares_execution_complete
    return ledger_declares_execution_complete(state if isinstance(state, dict) else {})


def _issue_call_is_registered_teardown(
    tool_name: str, tool_args: Any, state: AgentState,
) -> bool:
    """True when a freshly-issued kubectl call is a registered-vehicle TEARDOWN.

    Thin state-reading wrapper (P3) over the single-source call-level
    predicate :func:`execution_artifacts.issue_call_is_registered_teardown`
    — the teardown≠mutation judgement (classifier + vehicle registry)
    lives there, exactly once; this wrapper only adapts the agent-side
    ``state`` shape (``state["execution_artifacts"]``) the two remaining
    call-granular consumers use: the channel-A issue-time skip (R6-1)
    and the replan-attempt skip (R10-1). Teardown is not a fault
    mutation, so neither may weigh it as evidence. Registry-matched
    ONLY: a delete of an UNREGISTERED object (the delete-pod-to-restart
    fault forms) is a real native mutation and stays attributed.
    """
    from chaos_agent.agent.execution_artifacts import (
        issue_call_is_registered_teardown,
    )

    return issue_call_is_registered_teardown(
        tool_name, tool_args, state.get("execution_artifacts") or [],
    )


def _extract_original_replicas_from_messages(messages: list, resource_name: str) -> int | None:
    """Extract the original replica count for a resource from message history.

    Scans ToolMessages from kubectl get calls (JSON output) that were made
    BEFORE any scale operation, to find the pre-injection replica count.
    """
    import re as _re
    for msg in messages:
        if not isinstance(msg, ToolMessage):
            continue
        name = getattr(msg, "name", "") or ""
        if name != "kubectl":
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        # Look for "replicas": N in JSON output
        if resource_name in content and '"replicas"' in content:
            match = _re.search(r'"replicas"\s*:\s*(\d+)', content)
            if match:
                count = int(match.group(1))
                # Sanity check: replicas should be > 0 and reasonable
                if 0 < count <= 1000:
                    return count
    return None


def _collect_guard_rejections(messages: list, limit: int = 5) -> list[dict]:
    """Collect target_guard FORM-LEVEL rejection receipts as replan constraints.

    A guard rejection (``[target_guard] <VERDICT> — …``) is NOT a failed
    attempt the planner may re-weigh as evidence — it is a boundary the
    guard will not relax (B76: a replan round re-wrote a plan whose
    addressing form had already been rejected, burning a full planning
    cycle before the terminal rejection). ``_build_replan_context`` funnels
    these into ``guard_rejections`` so ``get_replan_section`` can present
    them as hard constraints, distinct from the "evidence, not verdict"
    failure chain.

    FORM-LEVEL only — ``GuardVerdict.is_form_level_rejection`` is the
    single source of truth (B76 review P1-1): REJECT_STAGNANT alternates
    by design and its receipt tells the LLM to retry a reshaped call,
    so freezing it as a never-relaxing boundary would contradict the
    guard's own semantics; it stays in the failure chain's evidence
    semantics via the ordinary failed-call scan. Unknown verdict strings
    (pre-upgrade checkpoints, fabricated text) are likewise left to the
    failure chain rather than guessed at.

    Receipts are CONTRACT-RELATIVE: each was judged against the approved
    target frozen at the time. A plan-change approval REPLACES that
    contract, so receipts older than the newest ``[PLAN CHANGE APPROVED``
    notice are constraints of a contract that no longer exists — the B76
    canonical continuation even re-approves the very form an old receipt
    rejected, and freezing that receipt would outlaw the new contract's
    own target. Scanning newest-first, the first approval notice seen is
    the boundary: everything older is stale and the scan stops there
    (measured on the production chain: guard → renderer →
    plan_change_confirm → this collector). REJECTED/RETRY notices replace
    nothing, so they are not boundaries.

    Newest-first like the failed-call scan; verdict parsed from the fixed
    ``[target_guard] VERDICT — reason`` prefix the guard emits
    (tool_screener._format_rejection_for_llm).
    """
    import re as _re

    pattern = _re.compile(r"^\[target_guard\]\s+([A-Z_]+)\s+—\s*(.*)", _re.DOTALL)
    rejections: list[dict] = []
    for msg in reversed(messages):
        if isinstance(msg, HumanMessage):
            if "[PLAN CHANGE APPROVED" in (
                msg.content if isinstance(msg.content, str) else str(msg.content)
            ):
                break
            continue
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else str(msg.content)
        match = pattern.match(content)
        if not match:
            continue
        try:
            verdict = GuardVerdict[match.group(1)]
        except KeyError:
            continue
        if not verdict.is_form_level_rejection:
            continue
        rejections.append({
            "tool": getattr(msg, "name", "") or "",
            "verdict": match.group(1),
            "message": content[:500],
        })
        if len(rejections) >= limit:
            break
    return rejections


def _build_replan_context(state: AgentState, request: ReplanRequest) -> dict:
    """Extract structured error context from conversation history for Phase 1 replan.

    NOTE: live-experiment uids are deliberately NOT collected here. This
    scan stops after 5 failed messages, so a successful ``blade_create``
    buried deeper in history goes unseen (task-349ccf5d lost uid
    ``5aaa51dbcb78a25d`` exactly this way). ``_fire_replan_seam`` fills
    ``existing_experiment_uids`` from the canonical extractor instead.
    """
    messages = state.get("messages", [])
    failed_calls = []

    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or ""
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            tool_call_id = getattr(msg, "tool_call_id", "")

            # Single-source failure verdict (agent/tool_verdicts.py): the
            # generic renderings plus whatever shape the owning provider
            # declares. This used to be an ``if name == "blade_create"``
            # branch with its own json.loads — a per-tool special case in a
            # generic node, which every further JSON-shaped tool (the
            # faultdrill assembler receipt) would have had to duplicate.
            #
            # One deliberate behaviour change: the old branch's non-JSON
            # fallback matched ``"error" in content.lower()``, which swept in
            # blade_create's ``Warning: ... outcome uncertain`` rendering.
            # That shape is explicitly UNKNOWN, not failed (cli.py's transport
            # exception path), and its own text instructs the agent to POLL
            # rather than replan — so it no longer lands in failed_calls. Its
            # UID is not lost: ``_fire_replan_seam`` fills
            # ``existing_experiment_uids`` from the canonical extractor.
            if tool_result_failed(
                name, content, status=getattr(msg, "status", None)
            ):
                failed_calls.append({
                    "name": name,
                    "tool_call_id": tool_call_id,
                    "error": content[:500],
                })

            if len(failed_calls) >= 5:
                break

    # Extract rejected params from all error sources
    all_rejected: list[str] = extract_rejected_params(request.invalidated_assumption)
    failed_tool_names: set[str] = set()
    for fc in failed_calls:
        all_rejected.extend(extract_rejected_params(fc.get("error", "")))
        if fc.get("name"):
            failed_tool_names.add(fc["name"])

    runtime_evidence_refs = list(dict.fromkeys(
        call["tool_call_id"]
        for call in failed_calls
        if call.get("tool_call_id")
    ))

    return {
        "error_summary": request.invalidated_assumption,
        **request.as_context(),
        # Runtime, not the model, owns opaque tool-call identifiers. Keep any
        # model-provided semantic references separately for audit context.
        "model_evidence_refs": list(request.evidence_refs),
        "evidence_refs": runtime_evidence_refs,
        "failed_tool_calls": failed_calls,
        # Filled canonically by _fire_replan_seam (see module note above).
        "existing_experiment_uids": [],
        "iteration_at_failure": state.get("execute_loop_count", 0),
        "rejected_params": list(dict.fromkeys(all_rejected)),
        "failed_tool_names": sorted(failed_tool_names),
        "guard_rejections": _collect_guard_rejections(messages),
    }


def _detect_consecutive_idle_turns(
    messages: list,
    replan_exhausted: bool = False,
) -> str | None:
    """Detect when the LLM is stuck producing text-only responses with no tools.

    Scans the most recent AI messages. If >= 3 consecutive AI messages have no
    tool_calls, the LLM is likely stuck in a "can't execute" loop and should
    either try a new tool or make a definitive conclusion.

    Early-exit on duplication: if just 2 consecutive idle AI messages have
    substantially similar content (first 50 chars match), the hint fires
    immediately — prevents the user from seeing the same text 3× before
    intervention.

    The hint adapts to ``replan_exhausted``: when ``replan_count >=
    max_replan_count`` the system can no longer route a replan request back
    to Phase 1, so suggesting it would invite an infinite loop where
    the LLM keeps requesting replan and the router keeps falling
    through to "continue" (the exact stuck-loop the user reports).
    With ``replan_exhausted=True`` the hint drops the replan
    option entirely and asks the LLM for a final conclusion.

    Returns a convergence hint if a stuck loop is detected, or None.
    """
    threshold = settings.idle_turn_threshold
    # Collect the last N AI messages (skipping non-AI messages)
    recent_ai = []
    for msg in reversed(messages):
        if hasattr(msg, "type") and msg.type == "ai":
            recent_ai.append(msg)
            if len(recent_ai) >= threshold:
                break

    # Early-exit on content duplication: if the last 2 AI messages are
    # both text-only AND have substantially similar content (first 50
    # chars match), fire the hint immediately — prevents the user from
    # seeing the same output repeated before the count-based threshold.
    content_dup = False
    if len(recent_ai) >= 2:
        m0, m1 = recent_ai[0], recent_ai[1]
        idle0 = not (hasattr(m0, "tool_calls") and m0.tool_calls)
        idle1 = not (hasattr(m1, "tool_calls") and m1.tool_calls)
        if idle0 and idle1:
            c0 = (getattr(m0, "content", "") or "").strip()
            c1 = (getattr(m1, "content", "") or "").strip()
            if c0 and c1 and c0[:50] == c1[:50]:
                content_dup = True

    if not content_dup:
        # Original path: need threshold consecutive idle turns
        if len(recent_ai) < threshold:
            return None
        all_idle = all(
            not (hasattr(m, "tool_calls") and m.tool_calls)
            for m in recent_ai
        )
        if not all_idle:
            return None

    if replan_exhausted:
        return (
            f"**EXECUTION CONVERGENCE NOTICE**: {threshold} consecutive responses "
            f"contained no tool calls, and the replan budget is exhausted. "
            f"A replan request can no longer change the graph state. Choose a response "
            f"that is grounded in the current evidence: take a safe, meaningful "
            f"available action if one remains; otherwise provide a concise final "
            f"conclusion without repeating prior text."
        )
    return (
        f"**EXECUTION CONVERGENCE NOTICE**: {threshold} consecutive responses "
        f"contained no tool calls and no new execution evidence. Reassess the "
        f"current hypothesis before continuing. Select a non-redundant safe action, "
        f"provide an evidence-based conclusion, or emit the structured replan "
        f"request when the approved plan itself requires reconsideration."
    )


def _detect_injection_method(
    messages: list, *, is_host: bool = False, is_teardown=None,
) -> str | None:
    """Detect the injection method used based on conversation history.

    Determines how the fault was actually injected so the verifier can
    choose the correct Layer 1 verification strategy.

    Args:
        messages: The conversation history to scan.
        is_host: Whether the resolved transport channel targets a host
            (ssh / kubewiz_host). Enables the ``host_native`` branch.
        is_teardown: The teardown≠mutation matcher
            (``execution_artifacts.make_teardown_matcher``), threaded into
            every vocabulary-carrier scan (P3): a registered-vehicle
            teardown delete is asset removal, never mutation evidence.
            ``None`` (the default) is RAW evidence (test fixtures).

    Returns:
        "host_blade" | "kubectl_exec" | "kubectl_native" | "host_native" | None
    """
    # Delegated to the FaultProvider registry, the single dispatch point that
    # replaced the former inline fall-through. The registry first scopes
    # candidates by CHANNEL (``is_host`` → profile; a k8s channel never probes
    # the host backend, and vice versa), then arbitrates the survivors by
    # RECENCY of each provider's injection evidence. Attribution keys on the
    # injection ATTEMPT (AIMessage tool_calls), not on command text or tool
    # result; every carrier scans for its own evidence inside its provider.
    from chaos_agent.agent.providers import FaultProviderRegistry

    return FaultProviderRegistry.detect_method(
        messages, is_host=is_host, is_teardown=is_teardown,
    )


def _should_redetect_injection_method(
    current_injection_method: str | None, experiment_uid: str | None
) -> bool:
    """Gate the per-iteration history re-scan (channel B) to its real jobs.

    Direction B records ``injection_method`` at ISSUE time (channel A), so the
    reverse history scan is only needed to:

    - RESUME: recover attribution when nothing is recorded yet (``current`` is
      empty) — e.g. after a restart where the injection lives in history, or on
      the first injection turn before channel A commits it. The caller bounds
      this scan to the CURRENT attribution epoch
      (:func:`_epoch_bounded_messages`), so after a replan seam the scan cannot
      resurrect PRE-seam attempts (task-5193538b).
    - UPGRADE: promote the provisional multi-step native attribution to the
      experiment backend once a live experiment UID appears (the UID lives in
      the tool RESULT, which channel A cannot see). The trigger is the
      registry-extracted experiment id — carrier-neutral, so any UID-bearing
      backend arms the upgrade, not just ChaosBlade.
    - DOWNGRADE (task-51193464): an experiment-method attribution whose UID
      never materialised is UNFULFILLED — the method promises a live
      experiment, and ``experiment_uid`` is that promise's proof. While the
      promise is outstanding the scan stays armed so the registry's RECENCY
      arbitration can correct a mis-attribution (a k8s OBJECT uid once
      mis-read as blade evidence) to the native backend whose evidence is
      more recent. The arbitration is idempotent — a genuine experiment
      still bootstrapping keeps winning on its own recency — and the moment
      its UID appears the attribution is fulfilled and this arm closes.

    In steady state (a non-multi-step method already set with no outstanding
    experiment promise, or no new UID) the scan would only re-derive the same
    answer, so we skip it.
    """
    if not current_injection_method:
        return True
    from chaos_agent.agent.providers import FaultProviderRegistry

    provider = FaultProviderRegistry.resolve_by_method(current_injection_method)
    if not experiment_uid:
        # An experiment-method attribution without its UID proof is
        # outstanding: arm the scan for the DOWNGRADE correction above. A
        # UID-less native method is its own proof (the attempt IS the
        # injection) — only the multi-step UPGRADE probe re-scans it, below.
        return provider is not None and provider.has_experiment_uid
    return provider is not None and provider.is_multi_step


def _issue_disproven_in_epoch(state: AgentState, messages: list, provider) -> bool:
    """True when ``provider``'s ``issue_disproven`` finds EXPLICIT
    counter-evidence inside the CURRENT attribution epoch.

    The provider hook reports a TRUSTWORTHY result proving an issue-time
    attempt never committed (a failed object-write); carriers whose results
    are untrustworthy by nature — the forensic paradox, where the fault
    severs its own feedback channel — return False, as do experiment
    carriers whose attribution is result-born in the first place. Shared by
    the three result-awareness seams below (revocation, RESUME veto,
    upgrade-without-combo) so they can never drift apart.
    """
    if provider is None or getattr(provider, "has_experiment_uid", False):
        return False
    disprove = getattr(provider, "issue_disproven", None)
    if disprove is None:
        return False
    # Teardown ≠ mutation, in the confirmation guard too (O-2): the
    # registered-vehicle teardown delete's SUCCESS receipt would otherwise
    # be counted by the pre-pass as "a write landed — attribution
    # confirmed", masking the revocation a genuinely failed injection
    # earned. P3 threads the ``is_teardown`` matcher INTO the scan — the
    # call-level skip applies at the vocabulary layer, mixed batches
    # included (the retired message-level window kept a mixed batch's
    # teardown receipt readable). A real write's success/failure verdict
    # is untouched (three shapes re-pinned by TestIssueTimeTeardownAttribution).
    from chaos_agent.agent.execution_artifacts import make_teardown_matcher

    return bool(disprove(
        _epoch_bounded_messages(messages, state),
        is_teardown=make_teardown_matcher(
            state.get("execution_artifacts") or []
        ),
    ))


def _maybe_revoke_issue_time_attribution(
    state: AgentState,
    result: dict,
    messages: list,
    current_method: str | None,
) -> bool:
    """Revoke an issue-time (channel A) attribution its result disproves.

    Issue-time attribution records the injection the moment the tool call is
    issued — before the result exists — so a UID-less native method can be
    committed for an attempt that provably failed. When the carrier's result
    channel carries explicit counter-evidence (see
    :func:`_issue_disproven_in_epoch`), the attribution facts are cleared so
    downstream gates — REPLAN_EXHAUSTED context, verifier routing, the
    tail projection — never treat a FAILED attempt as a live fault. The
    attempt itself remains replan evidence via the epoch scan in
    ``_injection_attempted_this_contract``; only the "committed fault"
    reading is withdrawn. Returns True when the attribution was revoked.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    provider = FaultProviderRegistry.resolve_by_method(current_method)
    if not _issue_disproven_in_epoch(state, messages, provider):
        return False
    result["injection_method"] = None
    result["fault_handle"] = None
    result["injection_start_time"] = None
    logger.info(
        "Revoked issue-time attribution %s: the result disproves the commit "
        "(failed attempt stays replan evidence)",
        current_method,
    )
    return True


def _project_fault_handle(state: AgentState, result: dict) -> None:
    """Fault-handle sync — the single projection point.

    Runs AFTER every attribution write of a turn (UID extraction block,
    ISSUE-time native commit inside ``_process_response_tool_calls``,
    replan-seam clears, issue-time revocation) and projects the final
    attribution facts into the carrier-agnostic handle. Projection, not a
    message re-scan: an attribution cleared at a seam stays cleared —
    re-scanning history would resurrect exactly what the seam invalidated.
    Writes ``result["fault_handle"]`` only on change so untouched turns add
    no redundant state update. Extracted as a pure helper so the projection
    contract is unit-testable without a full execute-loop fixture.
    """
    from chaos_agent.agent.providers import FaultProviderRegistry

    next_handle = FaultProviderRegistry.derive_handle_from_legacy(
        {**state, **result}
    )
    prev_handle = (
        result["fault_handle"] if "fault_handle" in result
        else state.get("fault_handle")
    )
    if next_handle != prev_handle:
        result["fault_handle"] = next_handle


def _epoch_bounded_messages(messages: list, state: AgentState) -> list:
    """Messages belonging to the CURRENT attribution epoch.

    ``reset_attribution_state`` records ``attribution_epoch_index`` at every
    replan seam — the message count at the moment the seam landed in state.
    The RESUME re-detection scan must read only messages AFTER that boundary:
    pre-seam attempts belong to the fault contract the replan just invalidated,
    and re-attributing them is exactly how a stale diagnostic exec surfaced as
    the new fault's injection, letting the executor conclude "already
    injected" and exit Phase 2 without issuing anything (task-5193538b).

    No boundary (first epoch, or a task restored without one) → full history,
    preserving restart-recovery semantics. If trimming shifted the list so the
    boundary exceeds its length, fall back to full history too — an over-scan
    keeps the pre-fix attribution behaviour instead of fabricating a smaller
    window that hides the real current-epoch attempt (never under-attribute).
    """
    boundary = state.get("attribution_epoch_index")
    if not boundary:
        return messages
    try:
        start = int(boundary)
    except (TypeError, ValueError):
        return messages
    if start > len(messages):
        return messages
    return messages[start:]


def _merged_message_count(state_messages: list, result_messages: list) -> int:
    """Length of ``state_messages`` once ``add_messages`` merges ``result_messages``.

    Replicates the reducer's LENGTH accounting without copying messages: a
    ``RemoveMessage`` deletes its in-place id, a message whose id already
    exists replaces it (length unchanged), everything else appends. Used by
    ``reset_attribution_state`` so the recorded epoch boundary equals the real
    post-merge length even when the seam's result already carries
    replace-type messages — or a compaction hook's RemoveMessages + summary
    merged ahead of it (``merge_hook_updates`` runs before the replan seam,
    so the seam's accounting must simulate the MIXED batch).

    Assumes at most one message per id in ``state_messages`` — the
    ``add_messages`` replace-by-id invariant. The real reducer deletes EVERY
    copy of a duplicated id, which this accounting would under-count; that
    state is unreachable through the reducer itself (appending a duplicate
    id replaces, it never stacks), so the invariant is not defended here.
    """
    alive = {m.id for m in state_messages if getattr(m, "id", None)}
    total = len(state_messages)
    for msg in result_messages:
        mid = getattr(msg, "id", None)
        if isinstance(msg, RemoveMessage):
            if mid in alive:
                alive.discard(mid)
                total -= 1
        elif mid is not None and mid in alive:
            continue  # in-place replace: length unchanged
        else:
            total += 1
            if mid is not None:
                alive.add(mid)
    return total


def reset_attribution_state(
    result: dict,
    *,
    keep_experiment_uid: bool = False,
    state_messages: list | None = None,
    state: AgentState | None = None,
) -> None:
    """Reset injection-attribution fields at a replan seam.

    Every replan invalidates "who injected the current fault": the next
    execution round may switch carriers (blade -> kubectl-native fallback),
    so the recorded method / carrier pod / Layer-1 cache / start time must
    not leak into the next attempt. Clearing ``injection_method`` re-arms
    ``_should_redetect_injection_method`` (empty current -> RESUME), letting
    the registry re-attribute by RECENCY on the new messages — without this
    reset the narrow gate never re-opens and a stale attribution survives
    the method switch (task-29848471).

    ``keep_experiment_uid``: an execute-time replan may fire while an experiment
    is STILL ACTIVE (``existing_experiment_uids`` non-empty) — dropping that UID
    would orphan a live experiment for recovery. Verify-replan destroys its
    residue first (and retires the UID separately), so it always passes the
    default ``keep_experiment_uid=False``.

    ``state``: the pre-seam state, for the ledger plan-scoped-field
    reset (cascade review C1 knife-2; O2 extension). The ledger's
    ``execution-complete`` is a fact about THE PLAN THAT JUST RAN — a
    replan retires that plan, so the fact must not survive the seam: a
    stale terminal phase would satisfy the stall gate and the router's
    text-only branch for the NEXT plan, letting a fresh plan reach the
    verifier with zero steps executed (the same
    stale-fact-across-a-seam family as task-5193538b's attribution, in a
    new carrier). ``current_step`` joins the reset for the same reason:
    the retired plan's step pointer is meaningless for the next plan.
    Cleared: ``state.phase`` (terminal marker) and
    ``state.current_step``. Kept: facts, log and anchor — they describe
    history, which the next plan may legitimately build on. Omit
    ``state`` at call sites that have no ledger (or no seam semantics).

    ``state_messages``: the state's message list at the seam. Two effects:
    (1) the synthetic baseline pair persisted by the previous verification
    cycle is removed — replan invalidates the epoch's baseline evidence along
    with its attribution, and a surviving pair (stable ids outlive handoff
    stripping) would be diagnosed INTACT by the next cycle's pair gate and
    skip the rebuild, anchoring the model on pre-replan numbers while
    ``baseline_data`` already holds the new ones (E1, change
    ``stale-baseline-pair-seam-cleanup``); (2) the attribution epoch boundary
    is recorded as the POST-MERGE list length (pair deletions accounted) so
    the RESUME re-detection scan only reads CURRENT-epoch messages: clearing
    ``injection_method`` re-arms the scan, and an unbounded scan would
    re-derive the PRE-replan attribution — counting stale diagnostic execs as
    the new fault's injection and letting the executor conclude "already
    injected" without issuing anything (task-5193538b). Omit it for a pure
    field reset with no boundary and no pair cleanup.
    """
    if not keep_experiment_uid:
        result["experiment_uid"] = None
        # The fault handle mirrors the UID's fate: the committed fault it
        # describes was invalidated with it (keep_experiment_uid keeps both — a
        # live experiment keeps its handle for the recover graph).
        result["fault_handle"] = None
        # The combo marker belongs to the same attribution: the UID's
        # native companion is invalidated together with it (keep_experiment_uid
        # keeps both — a live experiment keeps its native component).
        result["combo_native_issued"] = None
    result["injection_method"] = None
    result["kubectl_exec_pod_name"] = None
    result["inject_layer1_cache"] = None
    result["injection_start_time"] = None
    # Fault-window hold origin: retired with the same attribution — the
    # replanned attempt's window must re-anchor at ITS execute-loop end
    # (the next verifier entry re-stamps), never inherit this attempt's
    # verifier-entry stamp (a stale origin would silently erode the new
    # attempt's window by the replan cycle's entire duration).
    result["injection_window_start_time"] = None
    # Ledger plan-scoped-field reset (C1 knife-2 + O2): retire the previous
    # plan's execution facts together with its attribution. Two fields are
    # plan-scoped — they describe THE PLAN THAT JUST RAN and must not leak
    # into the replanned one: ``phase`` (terminal marker — a stale
    # execution-complete satisfies the stall gate and the router's
    # text-only branch for the NEXT plan, letting it reach the verifier
    # with zero steps executed) and ``current_step`` (the retired plan's
    # step pointer — meaningless for the next plan; display-layer only,
    # no gate consumes it, but a stale "step s3" rendered as the new
    # plan's current step is a lie to the model). The reset writes a
    # MERGE patch (None values) rather than the whole ledger:
    # progress_ledger's state is a shallow-merge channel
    # (merge_progress_ledger state_update semantics), so None lands as
    # "absent" in the merged view — facts/log/anchor ride through
    # untouched (established_facts are environment facts, the log is
    # history: both legitimately survive a replan). No ledger in state →
    # nothing to retire (the normal first-attempt shape).
    if state is not None:
        _ledger = state.get("progress_ledger")
        if isinstance(_ledger, dict) and isinstance(_ledger.get("state"), dict):
            _phase = str(_ledger["state"].get("phase") or "").strip().lower()
            _step = _ledger["state"].get("current_step")
            _retired: dict = {}
            if _phase in EXECUTION_COMPLETE_PHASES:
                _retired["phase"] = None
            if _step is not None:
                _retired["current_step"] = None
            if _retired:
                _merged = merge_progress_ledger(
                    _ledger, state_update=_retired,
                )
                result["progress_ledger"] = _merged
                logger.info(
                    "replan seam: retired plan-scoped ledger fields %s "
                    "(belonged to the retired plan)",
                    sorted(_retired),
                )
    if state_messages is None:
        return
    # Baseline-pair lifecycle: see docstring. SCAN-then-delete — only ids
    # PRESENT in state get a RemoveMessage (``add_messages`` raises on absent
    # ids; "pair not yet injected" is the norm at execute-time seams, not an
    # edge). Sorted for deterministic emission; the deletions are a set
    # operation, order carries no semantics.
    present_ids = [
        mid for mid in sorted(BASELINE_PAIR_MESSAGE_IDS)
        if any(getattr(m, "id", None) == mid for m in state_messages)
    ]
    result_messages = list(result.get("messages") or []) + [
        RemoveMessage(id=mid) for mid in present_ids
    ]
    result["messages"] = result_messages
    # Epoch boundary = post-merge length. The old tail count
    # (``len(state) + len(result)``) would overshoot by the deleted pair
    # count: every epoch consumer's overshoot defence degrades to FULL
    # history, re-arming exactly what the boundary exists to prevent
    # (task-5193538b misattribution; stale context-HM suppression on the
    # verifier side).
    result["attribution_epoch_index"] = _merged_message_count(
        state_messages, result_messages,
    )


def _build_execution_hints(
    messages: list,
    state: AgentState,
    persist_into: list | None = None,
    counts_out: dict | None = None,
) -> tuple[list[HumanMessage], str | None]:
    """Build all execution-phase hints to inject before the LLM call.

    Returns (hints, stagnant_tool) where stagnant_tool is the tool name
    that should be filtered from bindings, or None.

    ``persist_into`` collects the copies that must reach ``result["messages"]``.
    Corrective hints (loop / stagnation / tool-error / idle) are re-derived every
    iteration, so a turn-local copy reads as a first-time warning forever and the
    model never learns it has already been told — measured in task-e9ee12d6,
    where the notice fired from turn 11 and the same call was issued 31 more
    times. The per-iteration convergence hints deliberately do NOT persist: their
    text names the current iteration number, so a stale copy would tell a later
    turn it has more budget than it does.
    """
    hints: list[HumanMessage] = []
    _persist = persist_into if persist_into is not None else []
    _history = state.get("messages", [])
    # Counts live on state so compaction cannot reset them; the caller folds
    # ``counts_out`` into its state update.
    _counts = counts_out if counts_out is not None else {}
    _counts.update(state.get("hint_repeat_counts") or {})

    loop_hint = detect_repeated_tool_calls(messages, phase="execute")
    if loop_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "loop", "execute", loop_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    _, stagnant_tool = detect_action_stagnation(messages, phase="execute")
    if stagnant_tool:
        exec_hint = build_stagnation_hint(
            stagnant_tool,
            colon_suffix="to complete remaining injection steps",
            else_actions=[
                "Use a DIFFERENT tool to achieve the injection goal.",
                "Output your conclusion if injection already succeeded "
                "(include the injection UID if available).",
                "Emit a structured replan request only if the approved plan itself requires reconsideration.",
            ],
        )
        hints.append(persist_corrective_hint(
            _persist, _history, "stagnation", stagnant_tool, exec_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    error_hint = detect_tool_error_hint(messages)
    if error_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "tool_error", "execute", error_hint,
            counts=_counts, counts_out=_counts,
        ))

    transient_hint = detect_transient_retry_exhaustion(messages)
    if transient_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "transient_exhaustion", "execute", transient_hint,
            counts=_counts, counts_out=_counts,
        ))

    try:
        _max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan = 2
    replan_exhausted = state.get("replan_count", 0) >= _max_replan
    idle_hint = _detect_consecutive_idle_turns(
        messages, replan_exhausted=replan_exhausted
    )
    if idle_hint:
        hints.append(persist_corrective_hint(
            _persist, _history, "idle", "execute", idle_hint,
            escalate_after=settings.hint_escalate_after,
            counts=_counts, counts_out=_counts,
        ))

    # conflict_uids: already handled by the pre-execution conflict gate.
    # Residual experiments are NOT the executor's concern — do NOT inject
    # hints about them, as they cause the LLM to investigate/verify instead
    # of focusing on its injection task.

    return hints, stagnant_tool


def _process_response_tool_calls(
    response,
    state: AgentState,
    result: dict,
    tracker,
    count: int,
) -> None:
    """Process tool_calls from the LLM response: blade params, FCAT, scale tracking."""
    tool_calls = getattr(response, "tool_calls", None) or []
    # A productive turn (issued tool calls) breaks any text-only stall streak:
    # reset the consecutive-stall counter so a later, unrelated stall gets a
    # fresh nudge budget instead of inheriting an old strike.
    if tool_calls and state.get("_execute_text_stall_count"):
        result["_execute_text_stall_count"] = 0
    for tc in tool_calls:
        tc_name, tc_args = extract_tool_call_fields(tc)

        # Phase-7 T3: issue-time structured-parameter extraction dispatched
        # through the registry — each backend recognises its own tool-call
        # forms (the blade_create flags and the kubectl-exec embedded blade
        # create both live in the ChaosBlade provider now).
        from chaos_agent.agent.providers import FaultProviderRegistry

        parsed_params = FaultProviderRegistry.parse_injection_params(
            tc_name, tc_args
        )
        if parsed_params:
            result["injection_parsed_params"] = parsed_params

        if tc_name == "blade_create":
            logger.info(f"Blade create params: {tc_args}")

            target_metadata = state.get("target_metadata") or {}
            from chaos_agent.utils.fault_context import (
                lookup_adaptations, compute_safe_burn_size,
            )
            from chaos_agent.agent.spec.fault_spec import read_fault_spec as _rfs
            _spec_for_fcat = _rfs(state)
            _scope = (_spec_for_fcat.scope if _spec_for_fcat else "") or tc_args.get("scope", "")
            _target = (_spec_for_fcat.fault_target if _spec_for_fcat else "") or tc_args.get("target", "")
            _action = (_spec_for_fcat.fault_action if _spec_for_fcat else "") or tc_args.get("action", "")
            adaptations = lookup_adaptations(
                _scope, _target, _action, target_metadata,
                rule_type="param_override",
            )
            for adj in adaptations:
                if adj.mode in ("llm", "both") and "param_overrides" in adj.action:
                    for key, val in adj.action["param_overrides"].items():
                        if key == "size" and val == "auto":
                            safe_size = compute_safe_burn_size(
                                target_metadata.get("pod_memory_limit_mb")
                            )
                            tc_args[key] = str(safe_size)
                        else:
                            tc_args[key] = val
                    logger.info(
                        "FCAT: %s applied, params adjusted: %s",
                        adj.id, adj.action["param_overrides"],
                    )
                    from chaos_agent.memory.session_store import get_global_session_store
                    _fcat_store = get_global_session_store()
                    _fcat_tid = state.get("task_id", "")
                    if _fcat_store and _fcat_tid:
                        _mem_str = (
                            "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                            else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                        )
                        _fcat_msg = f"[FCAT P0] {adj.id}: size adjusted to {tc_args.get(key, safe_size)}MB (pod_memory_limit={_mem_str})"
                        _fcat_store.append_messages(_fcat_tid, [HumanMessage(content=_fcat_msg)], node_name=EXECUTE_LOOP)
                    if settings.is_debug and tracker:
                        _mem_str_dbg = (
                            "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                            else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                        )
                        tracker.update(
                            f"[FCAT P0] {adj.id}: size→{tc_args.get(key, safe_size)}MB (pod_mem={_mem_str_dbg})"[:200],
                            {"debug": True, "fcat": True},
                        )

        if tc_name == "kubectl" and tc_args.get("subcommand") == "scale":
            v_args = tc_args.get("v_args", "")
            import re as _re
            replicas_match = _re.search(r"--replicas=(\d+)", v_args)
            resource_match = _re.search(
                r"(?:deployment|statefulset)\s+(\S+)", v_args
            )
            if replicas_match and resource_match:
                new_replicas = int(replicas_match.group(1))
                resource_name = resource_match.group(1)
                existing = state.get("original_replicas") or {}
                if resource_name not in existing:
                    orig_count = _extract_original_replicas_from_messages(
                        state.get("messages", []), resource_name
                    )
                    if orig_count is not None and orig_count != new_replicas:
                        existing[resource_name] = orig_count
                        result["original_replicas"] = existing
                        logger.info(
                            f"Recorded original_replicas: {resource_name}={orig_count}"
                        )

    # Direction B: record injection_method at ISSUE time from the freshly
    # issued tool_calls, rather than reverse-scanning history later. Only the
    # UID-less providers (has_experiment_uid=False — the native backends) are
    # committed here — the attempt IS the injection, so a severed exec result
    # cannot hide it. The experiment-UID providers are deferred to the
    # experiment_uid path (proof the experiment actually succeeded), so a
    # failed carrier attempt that falls back to a native method is not
    # mis-recorded. Never overrides an already-set method (monotonic); the
    # experiment_uid upgrade stays in the caller's re-detect block.
    from chaos_agent.agent.nodes.execute._injection_detection import (
        classify_issue_time_method,
    )
    from chaos_agent.agent.providers.registry import FaultProviderRegistry
    from chaos_agent.transports.registry import is_host_scope_channel

    _is_host = is_host_scope_channel(state)
    _current_method = state.get("injection_method") or result.get("injection_method")
    for tc in tool_calls:
        tc_name, tc_args = extract_tool_call_fields(tc)
        _issued = classify_issue_time_method(tc_name, tc_args, is_host=_is_host)
        # Registry dispatch (phase-9 T4): skip everything that is not a live
        # UID-less backend — unknown/None methods (``_issuer is None``) and
        # experiment-UID backends both defer to the experiment_uid path.
        # Six-value equivalence vs the retired hard-coded set is pinned by
        # ``TestPhase9IssueTimeSixValues`` (test_phase9_rename_guards.py).
        _issuer = FaultProviderRegistry.resolve_by_method(_issued)
        if _issuer is None or _issuer.has_experiment_uid:
            continue
        # Teardown ≠ fault mutation (R6-1): a registry-matched vehicle
        # delete skips BOTH the method commit and the combo evaluation —
        # see :func:`_issue_call_is_registered_teardown` for the ghost-row
        # chain this skip closes. The loop then keeps scanning: teardown
        # must not consume the first-native-call slot either (an iteration
        # carrying [teardown delete, real patch] still attributes the
        # patch).
        if _issue_call_is_registered_teardown(tc_name, tc_args, state):
            continue
        if not _current_method:
            result["injection_method"] = _issued
            logger.info("Recorded injection_method at issue time: %s", _issued)
            if (
                not state.get("injection_start_time")
                and "injection_start_time" not in result
            ):
                result["injection_start_time"] = now_iso()
                logger.info("Set injection_start_time (%s issued)", _issued)
            # A carried-over live fault with no attributed method means a LIVE
            # experiment survived an execute-time replan seam (keep_experiment_uid
            # keeps the UID but clears the method for re-detection). Native
            # work issued in that epoch is still a combo — the experiment is
            # alive even though nothing is attributed yet. The epoch-bounded
            # re-detect scan cannot see the pre-seam blade_create, so this
            # issue-time check is the only coverage for that ordering.
            # The LIVE predicate (round-27 R2): the committed twin licensed a
            # combo on a DESTROYED experiment's kept UID — a corpse parked by
            # the seam mis-marked the next native issue as combo and durably
            # routed recovery to the LLM path for a native-only task
            # (the task-51193464 lesson, re-opened through the live door).
            #
            # Judge on the carried-over STATE only (never ``{**state, **result}``):
            # ``result`` may already hold the native method recorded a few lines
            # above for THIS very tool call, and a native provider would claim
            # it to build a handle — mis-marking a plain native fallback
            # (blade failed with no UID) as a combo.
            _experiment_live = has_live_fault(state)
        else:
            # COMBO (blade-first order): a native mutating injection was
            # issued while an experiment method is already attributed — both
            # vehicles mutated the target. Record it durably (messages may be
            # compacted before recovery) so the recover graph routes to the
            # LLM-driven Layer-1 flow: deterministic recovery can ONLY destroy
            # the blade experiment and would leak the native mutation.
            from chaos_agent.agent.providers import FaultProviderRegistry

            _cur_combo_provider = FaultProviderRegistry.resolve_by_method(
                _current_method
            )
            _experiment_live = (
                _cur_combo_provider is not None
                and _cur_combo_provider.has_experiment_uid
                # The experiment must actually be attested live — its UID is
                # the proof. A UID-less experiment-method attribution is
                # UNFULFILLED (task-51193464: one was a k8s object uid
                # mis-read as blade evidence), and marking a combo on top
                # of it would durably route recovery down the LLM path for
                # a task whose only real mutation was the native one.
                # LIVE axis (round-28, the R2 twin): the state-side UID
                # PRESENCE used to license a combo on a DESTROYED
                # experiment's corpse slot (destroy clears no slot); the
                # liability oracle judges the slot instead (owned −
                # retired − message-proven destroy — the durable-record
                # evidence source keeps a compacted-away live UID owned,
                # so the protected shape stays True). The result-side UID
                # keeps presence semantics: it is THIS iteration's fresh
                # birth, whose create receipt has not merged into the
                # state's message face yet — born-live by construction.
                and bool(
                    result.get("experiment_uid")
                    or live_liability_uids(state)
                )
            )
        if _experiment_live and not (
            state.get("combo_native_issued")
            or result.get("combo_native_issued")
        ):
            result["combo_native_issued"] = True
            logger.info(
                "Combo injection: native issued alongside a live experiment "
                "(method=%s, experiment_uid=%s) — recovery will use the LLM route",
                _current_method,
                state.get("experiment_uid")
                or result.get("experiment_uid"),
            )
        break

    post_invoke_debug(tracker, response, count, "Iteration")


def _phase2_kickoff_needed(messages: list) -> bool:
    """True when the newest plan finalization has no phase-transition signal after it.

    Reverse-scans for the LAST ToolMessage whose content starts with
    ``Planning finalized`` (the ``finish_planning`` success answer). The
    transition has already been announced if anything AFTER that message is a
    kickoff marker, an ``EXECUTION REQUIRED`` stall-nudge, or a productive
    AIMessage (one carrying tool_calls). Otherwise the model enters Phase 2
    staring at a plan summary as the newest signal — measured in
    task-3198b391 / task-ccfadf7d as a text-only "report to the user" turn
    that only the stall guard corrected, burning one LLM round-trip per task.

    The check is POSITIONAL, which makes replan support automatic: a replan
    produces a NEW ``Planning finalized`` ToolMessage after the old kickoff,
    so the scan re-arms and emits a fresh kickoff for the new plan.
    """
    finalized_idx = None
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if not isinstance(msg, ToolMessage):
            continue
        content = msg.content if isinstance(msg.content, str) else ""
        if content.startswith(_PLANNING_FINALIZED_PREFIX):
            finalized_idx = i
            break
    if finalized_idx is None:
        return False
    for msg in messages[finalized_idx + 1:]:
        if isinstance(msg, AIMessage):
            if getattr(msg, "tool_calls", None):
                return False
        elif isinstance(msg, HumanMessage):
            content = msg.content if isinstance(msg.content, str) else ""
            if _PHASE2_KICKOFF_MARKER in content:
                return False
            if _EXECUTION_REQUIRED_MARKER in content:
                return False
    return True


def _maybe_build_phase2_kickoff(messages: list) -> HumanMessage | None:
    """Build the Phase 1 → Phase 2 kickoff message, or None when not needed.

    The wording says "the next unexecuted step" (not "start the injection")
    so a mid-execution resume through this seam still reads correctly.

    The message carries an id at CONSTRUCTION time (B78): this object is
    immediate-written to the session store before ``add_messages`` merges
    it into state, and the PreReasoningHook flush writes it again after the
    merge. An id-less construction made those two serializations fall under
    two different dedup keys (composite vs ``id:``) — one logical message,
    two audit records, drifting timestamps. With a construction-time id the
    store's ID-first dedup collapses the deliberate double write. The store
    itself now stamps uuids on id-less state messages as the general guard;
    this is the belt-and-suspenders layer (identity from birth).
    """
    if not _phase2_kickoff_needed(messages):
        return None
    return HumanMessage(
        content=wrap_system_reminder(
            f"{_PHASE2_KICKOFF_MARKER} Phase 1 (planning) is OVER and the plan "
            "is approved — you are now in Phase 2 (execution). Immediately call "
            "the tool that performs the next unexecuted step of the approved "
            "plan. Do NOT restate the plan, do NOT output a summary, do NOT "
            "wait for confirmation. Execute now."
        ),
        id=f"phase2-kickoff:{uuid4()}",
    )


def _detect_terminal_conclusion(
    response,
    state: AgentState,
    result: dict,
) -> None:
    """Detect when LLM gives a text-only terminal conclusion in Phase 2.

    The executor's job is ONLY injection. When the LLM outputs text (no
    tool_calls), the exit is permitted ONLY on an attributed
    ``injection_method`` — the system's record of who injected the CURRENT
    fault. ``experiment_uid`` alone is deliberately NOT an exit ticket: after an
    execute-time replan the UID may survive the seam
    (``keep_experiment_uid=True``, kept so recovery still reaches the live
    experiment), and letting it license a text-only exit re-opens the
    task-5193538b empty spin under the NEW contract — the executor would
    conclude "already injected" having issued nothing. The UID is not lost:
    the same-iteration RESUME scan turns current-epoch blade evidence into
    the method attribution that licenses the exit, and a bare UID kept for
    recovery continues to serve recover graphs regardless.

    EXCEPTION: multi-step injections (kubectl_native / host_shell) often span
    several actions (e.g., ``patch`` to add a finalizer, then ``delete`` to
    trigger termination). Setting the injection_method after the first step and
    then allowing a text-only exit could silently skip remaining steps. Before
    letting a multi-step backend exit, we offer a one-shot soft step self-check
    via ``build_injection_step_selfcheck`` (LLM decides completeness).
    """
    _has_tool_calls = bool(getattr(response, "tool_calls", None))
    _injection_method = result.get("injection_method") or state.get("injection_method")
    _resp_content = (getattr(response, "content", "") or "").strip()

    # A multi-step backend (kubectl_native / host_shell) may span several
    # injection steps. Before allowing a text-only exit, offer a SOFT one-shot
    # self-check when the skill case is multi-step. The backend declares its
    # eligibility via ``is_multi_step``; whether THIS scenario is multi-step is
    # read from the skill case. We do NOT verify each step programmatically
    # (brittle) — the LLM self-verifies and may exit if it judges the injection
    # complete.
    from chaos_agent.agent.providers import FaultProviderRegistry

    _method_provider = FaultProviderRegistry.resolve_by_method(_injection_method)
    if (
        not _has_tool_calls
        and _method_provider is not None
        and _method_provider.is_multi_step
        and not state.get("_injection_selfcheck_nudged")
        # A ledger-declared completion supersedes the step self-check:
        # the model already asserted every mutation step ran (and the
        # assertion is recorded fact, not prose) — re-questioning it
        # re-opens the tail tension the declaration exists to close.
        and not _ledger_declares_execution_complete(state)
    ):
        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )
        _skill_case = (
            result.get("skill_case_content")
            or state.get("skill_case_content", "")
        )
        _all_msgs = state.get("messages", []) + result.get("messages", [])
        # O-3 (teardown ≠ step-credit): the step self-check counts EXECUTED
        # verbs off the message history; a registered-vehicle teardown
        # delete's success receipt used to credit the documented ``delete``
        # step, silencing the "step not yet performed" soft reminder for a
        # step the model never performed against the fault target. P3
        # threads the matcher INTO the executed-side scan (call-level, mixed
        # batches included); deliberately NO epoch bound here — the
        # self-check is high-tolerance by design (under-reporting on
        # purpose), so cross-epoch credit for a GENUINE step verb stays
        # and only teardown-for-credit is removed.
        from chaos_agent.agent.execution_artifacts import make_teardown_matcher

        _selfcheck = build_injection_step_selfcheck(
            _skill_case, _all_msgs, _injection_method,
            is_teardown=make_teardown_matcher(
                state.get("execution_artifacts") or []
            ),
        )
        if _selfcheck:
            logger.info(
                "multi-step injection: emitting one-shot step self-check "
                "before allowing text-only exit"
            )
            result.setdefault("messages", []).append(
                HumanMessage(content=wrap_system_reminder(_selfcheck))
            )
            # Give the LLM one more turn to act on the self-check (route
            # "continue" via should_continue_execute_loop:336). The one-shot
            # guard ensures any later text-only exit passes through freely —
            # the LLM may conclude if it judges the injection complete.
            result["injection_method"] = None
            result["_injection_selfcheck_nudged"] = True
            return  # Don't fall through to generic nudge below

    # Non-kubectl_native injection method (host_blade, kubectl_exec)
    # or kubectl_native with all steps complete → exit is correct — EXCEPT
    # an experiment-method attribution that has gone unfulfilled (its UID
    # never materialised). The method promises a live experiment whose proof
    # is the UID; without it no fault handle can be built, so the router's
    # exit gate (has_active_fault) can never open and a text-only conclusion
    # would spin the loop until the budget dies (task-51193464: the model
    # concluded "execution complete" for six minutes while the router kept
    # returning "continue"). Error is a signal, not a verdict — fail into
    # the verifier, which checks whether the fault actually took effect.
    if _injection_method:
        if (
            not _has_tool_calls
            and _method_provider is not None
            and _method_provider.has_experiment_uid
            and not state.get("experiment_uid")
            and not result.get("experiment_uid")
        ):
            result.update(fail_state(
                FailureCategory.EXECUTION_FAILED,
                "text conclusion with unfulfilled experiment attribution "
                f"({_injection_method} recorded but no experiment UID)",
                state.get("messages", []) + result.get("messages", []),
            ))
        return

    # No injection method at all — text-only without any injection action.
    if (
        not _has_tool_calls
        and not result.get("error")
        and _resp_content
        and parse_replan_request(_resp_content) is None
    ):
        # Ledger-declared completion (#39 third-retest tail-tension root
        # fix): the model recorded ``execution-complete`` via
        # ``finish_execution`` — every planned mutation step has run, and
        # the nudge's premise ("call the injection tool NOW") is false.
        # Issuing EXECUTION REQUIRED at a concluded plan burns rounds on
        # redundant probes and pressures the model toward out-of-authority
        # actions. Respect the recorded fact: let the text conclusion
        # through to the router, which routes to the verifier (the fault's
        # truth is checked there, never here).
        if _ledger_declares_execution_complete(state):
            logger.info(
                "executor concluded with ledger phase execution-complete "
                "— skipping stall nudge, routing to verifier",
            )
            return
        # Consecutive text-only stall: no tool call, no injection recorded, and
        # no parseable replan. A productive turn (tool_calls issued) resets this
        # counter in ``_process_response_tool_calls``, so only a genuine STREAK
        # of stalls accumulates — unrelated stalls separated by real work each
        # get their own nudge budget. Nudge until the budget is spent, then fail
        # fast so a stuck executor cannot burn the whole loop budget.
        try:
            max_stalls = int(settings.max_execute_text_stalls)
        except (TypeError, ValueError):
            max_stalls = 3
        if max_stalls < 1:
            max_stalls = 1
        stall_count = state.get("_execute_text_stall_count", 0) + 1
        if stall_count < max_stalls:
            result.setdefault("messages", []).append(
                HumanMessage(content=wrap_system_reminder(
                    "**EXECUTION REQUIRED**: You output text instead of "
                    "calling a tool. You are in Phase 2 (execution) — "
                    "the plan is already approved. Call the injection "
                    "tool your approved plan requires (from your bound "
                    "tools) NOW to carry out the fault. Do NOT output "
                    "plans, summaries, or wait for confirmation. Execute "
                    "immediately."
                ))
            )
            result["_execute_text_stall_count"] = stall_count
        else:
            result.update(fail_state(
                FailureCategory.EXECUTION_FAILED,
                "LLM concluded without tool use",
                state.get("messages", []) + result.get("messages", []),
            ))


def _parse_replan_tool_call(response):
    """Extract a ReplanRequest from a ``request_replan`` tool call on *response*.

    Returns ``(replan_request, replan_tc_id, tool_calls)`` where ``tool_calls`` is
    the response's full tool_call list (used to answer every call once we route
    away from phase2_tools). Returns ``(None, None, [])`` when no valid
    request_replan call is present (caller then falls back to the free-text
    marker). We only read the CURRENT response's tool_calls (never an old
    ToolMessage — see ``_handle_replan`` docstring).
    """
    tool_calls = getattr(response, "tool_calls", None) or []
    if not tool_calls:
        return None, None, []

    for tc in tool_calls:
        name, args = extract_tool_call_fields(tc)
        if name != REQUEST_REPLAN_TOOL_NAME:
            continue
        # Drop keys whose value is None before validating. The tool signature
        # declares the optional list fields as ``list = None``, so its JSON
        # schema invites the model to emit an explicit ``null`` (e.g.
        # observed_evidence=null). ReplanRequest types those as ``list[str]``
        # (non-nullable), so a raw None would raise a validation error and be
        # misread as "malformed" — silently dropping a real plan_invalid replan.
        # Stripping None lets ReplanRequest's own defaults ([]) apply.
        raw_args = args if isinstance(args, dict) else {}
        clean_args = {k: v for k, v in raw_args.items() if v is not None}
        try:
            replan_request = ReplanRequest.model_validate(clean_args)
        except ValueError:
            # Genuinely malformed args (e.g. missing a required field). Don't
            # fire replan; leave the tool_call for the ToolNode to answer with
            # an error so the model can correct itself.
            logger.warning("request_replan tool call had invalid args: %s", args)
            return None, None, []
        replan_tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        return replan_request, replan_tc_id, tool_calls

    return None, None, []


def _answer_replan_tool_calls(
    result: dict,
    tool_calls: list,
    replan_tc_id,
    replan_content: str,
) -> None:
    """Synthesize a ToolMessage for every tool_call in a request_replan turn.

    A fired/terminal replan routes to Phase 1 (or end) instead of phase2_tools,
    so these tool_calls never reach the ToolNode. Answering each one keeps the
    AIMessage's tool_calls all resolved so the next LLM call sees well-formed
    history. The synthesized ToolMessage is appended as the LAST message, which
    also keeps the ReAct shape normal for any continue routing.
    """
    if not tool_calls:
        return
    synthesized = []
    for tc in tool_calls:
        name, _ = extract_tool_call_fields(tc)
        tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if not tc_id:
            continue
        if tc_id == replan_tc_id:
            content = replan_content
        else:
            content = (
                "Not executed: a replan was requested in the same turn, so the "
                "current plan is being abandoned before this call ran."
            )
        synthesized.append(ToolMessage(content=content, tool_call_id=tc_id, name=name or "tool"))
    if synthesized:
        result.setdefault("messages", []).extend(synthesized)


def _fire_replan_seam(
    state: AgentState,
    result: dict,
    replan_request: ReplanRequest,
    replan_context: dict,
) -> None:
    """Write the replan seam transition into ``result``.

    Single seam writer for EVERY trigger source (LLM tool call, free-text
    marker, system auto-trigger): budget, context, attribution reset, history
    and attempt tracking must never differ by how the replan was initiated.
    Callers gate on budget and structural review BEFORE calling; this function
    only performs the transition.
    """
    current_replan_count = state.get("replan_count", 0)
    result["replan_requested"] = True
    result["replan_context"] = replan_context
    result["replan_request"] = replan_request.model_dump()
    result["replan_count"] = current_replan_count + 1
    if settings.replan_reset_execute_count:
        result["execute_loop_count"] = 0
    # Clear the WHOLE terminal-error triple, not just ``error``:
    # ``read_merged_error`` falls back to ``failure_detail`` (via
    # ``read_failure_reason``), so a lingering detail dict would keep
    # ``outcome.error`` non-empty and ``should_continue_agent_loop``
    # would reject the re-planned run on its very first evaluation.
    result["error"] = None
    result["failure_detail"] = None
    result["failure_reason"] = None
    result["approved_target"] = None
    if replan_request.changes_target_or_risk:
        # The structured request is the authoritative declaration that the next
        # plan alters a confirmation boundary. Re-entering planning alone is
        # insufficient; the new boundary must be shown to the user even if
        # later discovery happens to look similar.
        result["needs_confirmation"] = True
    # Attribution reset at the replan seam: the next attempt may switch
    # carriers, so method/carrier-pod/cache must not leak across.
    # experiment_uid survives ONLY while an experiment is still active.
    # task-349ccf5d: the keep decision used to trust the truncated raw
    # scan in replan_context, which cannot see a successful create once
    # 5 failed messages sit closer to the seam — the live experiment was
    # orphaned. Use the canonical extractor instead (full history,
    # destroyed/retired uids filtered out — same contract as the
    # verifier and the per-iteration re-extraction), and fall back to
    # the persisted ``state.experiment_uid`` for the memory-compression
    # boundary where the create ToolMessage may have been summarized
    # away. Worst case of the fallback (uid already dead) is bounded:
    # recover hits the designed "experiment lost -> alert" branch.
    state_msgs = list(state.get("messages") or [])
    all_messages = state_msgs + list(result.get("messages") or [])
    retired_uids = state.get("retired_experiment_uids")
    from chaos_agent.agent.providers import FaultProviderRegistry
    from chaos_agent.transports.registry import is_host_scope_channel

    live_uid = FaultProviderRegistry.extract_experiment_uid(
        all_messages,
        retired=retired_uids,
        is_host=is_host_scope_channel(state),
    )
    # Compression-boundary fallback: the persisted uid may be the only
    # evidence left once the create ToolMessage is summarized away. It
    # must still pass the SAME death filters as the extractor — nothing
    # clears ``state.experiment_uid`` when the LLM issues ``blade_destroy``,
    # so an unfiltered fallback would resurrect a destroyed experiment
    # into ``existing_experiment_uids`` (the Phase-1 replan prompt) and the
    # keep decision.
    fallback_uid = state.get("experiment_uid") or None
    if fallback_uid:
        # Registry seam (union over every UID-bearing provider's destroy
        # scan, channel-unfiltered), same contract the canonical extractor
        # applies internally.
        dead_uids = FaultProviderRegistry.destroyed_experiment_ids(
            all_messages
        ) | set(retired_uids or [])
        if fallback_uid in dead_uids:
            fallback_uid = None
    experiment_uid_at_seam = live_uid or fallback_uid or None
    replan_context["existing_experiment_uids"] = [experiment_uid_at_seam] if experiment_uid_at_seam else []
    reset_attribution_state(
        result,
        keep_experiment_uid=bool(experiment_uid_at_seam),
        state_messages=state_msgs,
        state=state,
    )
    history = list(state.get("replan_history") or [])
    history.append({
        "attempt": result["replan_count"],
        "original_error": replan_context.get("error_summary", ""),
        "action_taken": "(pending Phase 1 analysis)",
        # Audit trail: record the experiment handle observed at the seam
        # regardless of the keep decision, so a lost uid stays traceable.
        "experiment_uid_at_seam": experiment_uid_at_seam,
    })
    result["replan_history"] = history
    from chaos_agent.agent.attempt_tracker import (
        REASON_GRAPH_REPLAN,
        begin_attempt,
    )
    attempt_delta = begin_attempt(
        {**state, **result},
        target=state.get("fault_spec"),
        reason=REASON_GRAPH_REPLAN,
        notes=replan_context.get("error_summary", "")[:200],
    )
    result.update(attempt_delta)


def _handle_replan(
    response,
    state: AgentState,
    result: dict,
) -> None:
    """Apply state transitions for an explicit Phase 2 replan request.

    Tool errors remain inside the executor's ReAct loop. Looking backwards for
    an old ToolMessage here can pre-empt a newer model action, so graph re-entry
    is intentionally driven only by the model's current response.
    """
    replan_requested = False
    replan_context = None
    replan_request = None
    replan_tc_id = None
    replan_tool_calls = []

    if response is not None:
        # Preferred channel: a structured ``request_replan`` tool call. Tool-calling
        # models emit control signals as tool calls, not free text, so this is the
        # primary path; the ``<replan_request>{...}</replan_request>`` free-text
        # marker below stays as a backward-compatible fallback for text models.
        # We only read the CURRENT response's tool_calls (never an old ToolMessage
        # — see docstring). Because a fired/terminal replan routes to Phase 1 (or
        # end) instead of phase2_tools, every tool_call in this turn is answered
        # with a synthetic ToolMessage (per-outcome, below) to keep history
        # well-formed.
        replan_request, replan_tc_id, replan_tool_calls = _parse_replan_tool_call(response)
        if replan_request is None:
            content = getattr(response, "content", "") or ""
            replan_request = parse_replan_request(content)
        if replan_request is not None:
            replan_requested = True
            replan_context = _build_replan_context(state, replan_request)
            logger.info("Phase 2 LLM requested replan for step=%s", replan_request.affected_step)

    if not replan_requested:
        return

    review_reason = _review_replan_request(state, replan_request)
    if review_reason:
        # Reviewed rejection: keep executing, do NOT fire a Phase-1 replan.
        if replan_tool_calls:
            # Tool channel: leave the request_replan tool_call UNANSWERED here so
            # it flows once through phase2_tools (routing is "continue" ->
            # phase2_tools, which re-parses the latest AIMessage's tool_calls).
            # We must NOT synthesize a ToolMessage (phase2_tools would still
            # re-execute the call -> a duplicate answer for the same
            # tool_call_id) and must NOT interleave a HumanMessage between the
            # AIMessage and its ToolMessage (breaks tool-response adjacency).
            # The tool's own "Replan request recorded." result + the tool
            # docstring ("needs_investigation ... does NOT replan") carry the
            # semantics; the ReAct loop continues naturally.
            if replan_request.decision == "plan_invalid":
                # Deferred LIFECYCLE REVIEW: the rejection reason cannot be
                # appended now (adjacency above); the next execute_loop
                # iteration emits it once, then clears the flag.
                result["_replan_review_rejection"] = review_reason
            return
        result.setdefault("messages", []).append(HumanMessage(content=wrap_system_reminder(
            f"[LIFECYCLE REVIEW] Continue execution: {review_reason} A tool result "
            "is evidence about that call, not by itself a conclusion that the "
            "approved plan is infeasible."
        )))
        return

    try:
        _max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan = 2
    current_replan_count = state.get("replan_count", 0)
    replan_can_fire = current_replan_count < _max_replan

    if replan_can_fire:
        _answer_replan_tool_calls(
            result, replan_tool_calls, replan_tc_id,
            "Replan request recorded; returning to planning.",
        )
        _fire_replan_seam(state, result, replan_request, replan_context)
    else:
        _answer_replan_tool_calls(
            result, replan_tool_calls, replan_tc_id,
            "Replan requested but the replan budget is exhausted; converting to "
            "terminal failure.",
        )
        _fs = fail_state(
            FailureCategory.REPLAN_EXHAUSTED,
            f"attempts={current_replan_count}, last_error={(replan_context or {}).get('error_summary', '')[:200]}",
            state.get("messages", []) + result.get("messages", []),
        )
        result.update(_fs)
        result["replan_requested"] = False
        logger.warning(
            "Replan exhausted: LLM emitted a replan request but "
            "replan_count=%d already at max=%d; converting to "
            "terminal failure",
            current_replan_count, _max_replan,
        )


def _injection_attempted_this_contract(state: AgentState) -> bool:
    """Structural proof that the CURRENT contract attempted an injection.

    The review rule must rest on state facts the model cannot talk around,
    never on the free text of a replan request (task-71fa78b6 hallucinated
    its evidence wholesale). Two proofs, strongest first:

    1. Attribution present (``injection_method`` / ``experiment_uid``) — an
       injection was recorded under this contract.
    2. An injection tool call was ISSUED in the current attribution epoch —
       attempts count even when they failed; a failed attempt is exactly the
       evidence a legitimate replan is built on. WHAT counts as an injection
       call is judged by ``classify_issue_time_method`` — the SAME canonical
       issue-time classifier the attribution path commits to, so every carrier
       (blade, python-agent, kubectl object-write / exec-blade / command-mode
       mutation, host-native shell) is covered by one vocabulary and this rule
       can never drift away from attribution. A registered-vehicle TEARDOWN
       delete is skipped at CALL granularity before classification (R10-1):
       cleanup proves nothing about feasibility, and message-level filtering
       would not cover the mixed batch (teardown alongside read-only calls —
       no genuine attempt in the batch, yet the message survives filtering
       and its teardown call would classify as kubectl_native).
    """
    # NOTE: this is an ATTRIBUTION-presence check, deliberately NOT
    # ``has_active_fault``: a blade attribution without a UID means the
    # create FAILED — still an attempt (the very evidence a legitimate
    # replan is built on), but not a committed fault.
    if state.get("injection_method") or state.get("experiment_uid"):
        return True
    epoch_msgs = _epoch_bounded_messages(state.get("messages") or [], state)
    if not epoch_msgs:
        return False
    from chaos_agent.agent.nodes.execute._injection_detection import (
        classify_issue_time_method,
    )
    from chaos_agent.transports.registry import is_host_scope_channel
    _is_host = is_host_scope_channel(state)
    for msg in epoch_msgs:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name, args = extract_tool_call_fields(tc)
            # Teardown ≠ attempt (R10-1): a registered-vehicle teardown
            # delete must not masquerade as the attempt evidence that gets
            # an otherwise-refused replan granted — the review rule's whole
            # point is state facts the model cannot fake, and cleanup is
            # faked evidence (it proves nothing about feasibility). Call-
            # level skip so the mixed-batch form (teardown + read-only
            # calls, no genuine attempt anywhere in the batch) stays honest.
            if _issue_call_is_registered_teardown(name, args, state):
                continue
            if classify_issue_time_method(name, args, is_host=_is_host):
                return True
    return False


#: The CLOSED flag grammar an honest absence probe may carry. Flipping
#: the enumeration: three blacklist passes each missed a form (template
#: ``-file`` variants, ``poddisruptionbudgets`` under a "pod" prefix,
#: ``--server``/``--as``/``--token`` global flags), and membership
#: scanning misread a flag VALUE as a flag (``-n -A``: pflag consumes
#: the next token as ``-n``'s value UNCONDITIONALLY). Only whitelisted
#: flags parse; any other dash token — cluster, identity, or endpoint
#: switch — refuses the proof outright.
_PROBE_VALUE_FLAGS = frozenset({
    "-n", "--namespace",
    "-l", "--selector",
    "-o", "--output",
    "--kubeconfig",
})
_PROBE_BOOL_FLAGS = frozenset({
    "-A", "--all-namespaces",
    "--show-labels",
    "--no-headers",
})


def _scan_probe_flags(tokens: list[str]) -> dict | None:
    """Position-aware whitelist parse of a probe's flag tokens.

    Mirrors kubectl's pflag consumption so the guard reads EXACTLY the
    query kubectl ran: a value flag consumes the next token as its value
    unconditionally — even one starting with ``-`` (``-n -A`` sets
    namespace ``-A``; ``-A`` is NOT a flag there) — and a repeated flag
    takes its LAST occurrence. Bool flags accept the bare form or
    ``=true``; ``=false`` parses as the flag's explicit negation (same
    effect as absence), anything else refuses. One matching pair of
    surrounding quotes is stripped from values (shell semantics).

    Returns the effective ``namespace``/``selector``/``output`` values
    and the ``all_namespaces`` bool, or ``None`` when ANY token falls
    outside the probe grammar: an unlisted flag (``--field-selector``,
    ``--context``, ``--server``, ``--as``, ``--token``…) can change what
    the query matched, and a positional argument (``pods web-1``)
    narrows it to one named resource — for both, an empty result proves
    nothing about the selector-defined set. ``-A`` alongside ``-n`` is
    refused too: the local flag is then dead text and the receipt's
    effective scope cannot be read off the command line.
    """
    namespace: str | None = None
    selector: str | None = None
    output: str | None = None
    all_namespaces = False
    i = 1  # tokens[0] is the resource kind, validated by the caller
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("-"):
            return None  # positional resource name narrows the query
        flag, eq, inline = tok.partition("=")
        if flag in _PROBE_BOOL_FLAGS:
            if eq and inline != "true":
                if inline != "false":
                    return None  # not a pflag-legal bool form
                # ``=false``: explicit negation, same effect as absence.
            elif flag in ("-A", "--all-namespaces"):
                all_namespaces = True
            i += 1
            continue
        if flag not in _PROBE_VALUE_FLAGS:
            return None
        if eq:
            value = inline
            i += 1
        elif i + 1 < len(tokens):
            value = tokens[i + 1]
            i += 2
        else:
            return None  # value flag with no value: kubectl errors out
        if (
            len(value) >= 2
            and value[0] in ("\"", "'")
            and value[-1] == value[0]
        ):
            value = value[1:-1]
        if flag in ("-n", "--namespace"):
            namespace = value
        elif flag in ("-l", "--selector"):
            selector = value
        elif flag in ("-o", "--output"):
            output = value
        # --kubeconfig: consumed and discarded — it needs no anchoring
    if all_namespaces and namespace is not None:
        return None  # ambiguous scope: which of the two did kubectl run?
    return {
        "namespace": namespace,
        "selector": selector,
        "output": output,
        "all_namespaces": all_namespaces,
    }


#: Output formats whose emptiness is a faithful empty MATCH SET.
#: Table views (``wide``, default) list matching rows, ``name`` prints
#: one line per match; ``json``/``yaml`` always emit the List envelope.
#: Everything else — jsonpath/go-template (file variants included),
#: custom-columns — can render EXISTING resources to nothing and is
#: rejected. A positive allowlist, because enumerating dangerous
#: spellings always misses one (poddisruptionbudget under a "pod"
#: prefix, jsonpath-file without "jsonpath=").
_SAFE_OUTPUT_FORMATS = frozenset({"wide", "name", "json", "yaml"})


def _target_absence_proven_in_epoch(state: AgentState) -> bool:
    """Structural proof that the approved target set is EMPTY.

    The attempt proof above deadlocks when the target is physically gone:
    an injection call can never be issued against pods that do not exist,
    so "prove infeasibility by attempting" has no terminating path and the
    executor is condemned to run the approved plan against an empty set
    (task inject-65bbf344: 600s of no-op deletes, then a dead verify).
    This second structural key unlocks exactly that case.

    The proof is two halves paired by ``tool_call_id``:

    1. A framework-generated EMPTY-SET RECEIPT — the ``EMPTY_SELECTOR_HINT``
       that ``tools/kubectl_cli.py`` appends only when a ``get`` carrying a
       label selector returns NOTHING. The model cannot write ToolMessages,
       so this half cannot be fabricated (the 71fa78b6 failure mode).
    2. The issuing call ANCHORS the approved target: a read-only
       ``kubectl``/``kubectl_read`` ``get`` whose effective flags match
       the CURRENT contract. Namespace comes from the LAST ``-n``/
       ``--namespace`` (kubectl/pflag: last wins) and must EQUAL the
       approved one. The label selector comes from the LAST ``-l``/
       ``--selector`` and must constrain a SUBSET of the approved pairs
       — a WIDER probe (pairs dropped) is sound (its match set contains
       the approved one), while a NARROWER probe (pairs added or
       altered) can be empty while the approved target thrives, so its
       emptiness proves nothing. For a pod-scope spec the probed
       resource must be pods: an empty events list proves nothing about
       pods. Flag reading is POSITION-AWARE over a closed whitelist
       (:func:`_scan_probe_flags`), because pflag consumes a value
       flag's next token unconditionally (``-n -A`` → namespace ``-A``,
       NOT the all-namespaces flag) and membership scanning misread
       exactly that.

    Receipt soundness — an empty OUTPUT is only an empty MATCH SET when
    nothing could have suppressed rows: the output format must be a
    row-faithful one (``_SAFE_OUTPUT_FORMATS`` allowlist — templates
    render existing resources to nothing, including their ``-file``
    variants), and EVERY flag must belong to the probe grammar
    (``_PROBE_VALUE_FLAGS``/``_PROBE_BOOL_FLAGS``) — any other flag can
    change what the query matched: ``--field-selector`` makes the
    emptiness unattributable, ``--context``/``--cluster``/``--server``/
    ``--as``/``--token`` point it at another cluster or identity, a
    positional resource name narrows it to one object. Cluster switches
    are checked on BOTH entry points: the v_args strings AND the
    structured tool args that ``_build_kubectl_global_args`` injects as
    the same global flags (emptiness is about another cluster).
    ``--kubeconfig`` is deliberately ALLOWED: skill recipes mandate an
    explicit kubeconfig, the value points at the task's cluster in
    practice, and the bounded cost of a forged path is one wasted
    replan that Phase 1 re-checks under the user confirmation gate. An
    all-namespaces probe (``-A``/``--all-namespaces``) is a SUPERSET
    probe — its match set contains the approved namespace's — so its
    emptiness is sound without a ``-n`` equality; combining ``-A`` WITH
    ``-n`` is refused, since the local flag becomes dead text and the
    receipt's effective scope can no longer be read off the command.

    The receipt id must be UNAMBIGUOUS: exactly ONE ToolMessage answers
    the call. A duplicated ``tool_call_id`` (adversarial reuse across
    turns) could pair the anchor call with a receipt from a DIFFERENT
    invocation — rejected outright rather than risk mis-pairing.

    Scope soundness: the probed resource KIND must be EXACTLY the
    target's (``pod`` scope → ``pod``/``pods``; ``node`` scope →
    ``node``/``nodes`` — a prefix match would let an empty
    ``poddisruptionbudget`` list pose as pod absence); any other or
    empty scope cannot be kind-verified and stays out. A pod-scope spec
    additionally REQUIRES a namespace: without one the probe may have
    queried only the default namespace, and a default-namespace empty
    set proves nothing about the approved target. A node-scope spec
    normally carries no namespace (nodes are cluster-scoped) and
    anchors on the selector alone.

    Bounded by the same attribution epoch as the attempt proof, so probes
    issued under an OLD contract cannot unlock a replan for the current
    one. Specs carrying explicit ``names`` (alone or mixed with labels)
    stay out of scope: their absence evidence is per-name NotFound, a
    different receipt shape, and the names/labels combination semantics
    make a selector-only empty set unprovable for the whole target.
    """
    spec = read_fault_spec(state)
    if spec is None or spec.names or not spec.labels:
        return False
    kind = spec.scope.strip().lower().rstrip("s") if spec.scope else ""
    if kind not in ("pod", "node"):
        return False
    kind_names = {kind, kind + "s"}
    if kind == "pod" and not spec.namespace:
        return False
    epoch_msgs = _epoch_bounded_messages(state.get("messages") or [], state)
    if not epoch_msgs:
        return False
    from chaos_agent.tools.kubectl_cli import EMPTY_SELECTOR_HINT

    receipts_by_id: dict[str, list[ToolMessage]] = {}
    for msg in epoch_msgs:
        if isinstance(msg, ToolMessage):
            receipts_by_id.setdefault(msg.tool_call_id, []).append(msg)
    empty_receipt_ids = {
        tc_id
        for tc_id, receipts in receipts_by_id.items()
        if len(receipts) == 1
        and EMPTY_SELECTOR_HINT in str(receipts[0].content)
    }
    if not empty_receipt_ids:
        return False
    approved_kv = {f"{k}={v}" for k, v in spec.labels.items()}
    for msg in epoch_msgs:
        if not isinstance(msg, AIMessage):
            continue
        for tc in getattr(msg, "tool_calls", None) or []:
            name, args = extract_tool_call_fields(tc)
            if name not in ("kubectl", "kubectl_read"):
                continue
            if not isinstance(args, dict):
                continue
            if str(args.get("subcommand") or "").lower() != "get":
                continue
            # Cluster-switch rejection has TWO entry points: the v_args
            # ``--context``/``--cluster`` strings checked below, and the
            # STRUCTURED ``context``/``cluster`` tool args that
            # ``_build_kubectl_global_args`` injects as the very same global
            # flags. An emptiness observed in another cluster proves nothing
            # here — reject the structured form with the same rule.
            if args.get("context") or args.get("cluster"):
                continue
            v_args = str(args.get("v_args") or "")
            tokens = v_args.split()
            if not tokens or tokens[0].lower() not in kind_names:
                continue
            flags = _scan_probe_flags(tokens)
            if flags is None:
                continue  # outside the probe grammar: refusal, not luck
            # Receipt soundness: empty output must mean an empty MATCH SET.
            output_format = flags["output"]
            if output_format is not None and output_format not in _SAFE_OUTPUT_FORMATS:
                continue  # non-row-faithful output can render resources to nothing
            # All-namespaces probes (``-A``/``--all-namespaces``) cover
            # every namespace, so their match set CONTAINS the approved
            # one — the same superset soundness as dropping selector
            # pairs. pflag forms: bare ``-A``/``--all-namespaces`` or
            # ``--all-namespaces=true``; ``=false`` negates (needs the
            # namespace equality), anything else fails closed in the
            # scanner.
            if (
                spec.namespace
                and not flags["all_namespaces"]
                and flags["namespace"] != spec.namespace
            ):
                continue
            selector = flags["selector"]
            if selector is None:
                continue
            # Subset soundness: probe pairs ⊆ approved pairs ⇒ probe match
            # set ⊇ approved set ⇒ empty probe result proves absence.
            terms = [t for t in selector.split(",") if t]
            if not terms or not all(t in approved_kv for t in terms):
                continue
            tc_id = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
            if tc_id and tc_id in empty_receipt_ids:
                return True
    return False


def _banned_rejection_on_record(state: AgentState) -> bool:
    """A REJECT_BANNED receipt exists under the current contract.

    Reuses :func:`_collect_guard_rejections` (same contract boundary: a
    plan-change approval notice ends the scan), so the receipts counted
    here are always judged against the target set this contract froze.
    """
    messages = state.get("messages", [])
    return any(
        r.get("verdict") == "REJECT_BANNED"
        for r in _collect_guard_rejections(messages)
    )


def _review_replan_request(
    state: AgentState,
    request: ReplanRequest,
) -> str | None:
    """Keep explicit investigation in ReAct; plan-invalid requests replan.

    Structural review rule: a contract that has not attempted its injection
    cannot be declared infeasible. Infeasibility is proven by a real attempt
    (or its recorded attribution), never by anticipation — so the check keys
    on state facts only (see :func:`_injection_attempted_this_contract`) and
    ignores the request's free text entirely.

    Two structural exceptions:
      - The approved target set is provably EMPTY
        (:func:`_target_absence_proven_in_epoch`): an attempt is physically
        impossible, and demanding one would deadlock the contract — the
        plan replans on the framework's own empty-set receipt instead.
      - A safety-kind request with a REJECT_BANNED receipt on record
        (:func:`_banned_rejection_on_record`): the infeasibility is already
        framework-proven, not anticipated — the guard refused the very
        call the plan needs, and "issue the injection call first" would
        order the model to bypass the safety boundary the refusal marks
        (inject-cc2d5080: that review push landed a fault with no recovery
        timer armed, because the refused call was the carrier that arms
        it). The request's free text is still ignored — the kind label
        and the receipt are both structured facts.
    """
    if request.decision == "needs_investigation":
        return "The request says more investigation is needed."
    if not _injection_attempted_this_contract(state):
        if _target_absence_proven_in_epoch(state):
            return None
        if request.kind == "safety" and _banned_rejection_on_record(state):
            return None
        return (
            "No injection attempt is recorded under the current contract. "
            "A plan is proven infeasible by attempting it, not by "
            "anticipation — issue the injection call first, or probe the "
            "approved target with a read-only kubectl get whose empty "
            "result proves the target is gone."
        )
    return None


async def execute_loop(state: AgentState) -> dict:
    """Phase 2: ReAct loop for execution.

    The LLM follows skill instructions to call blade/kubectl tools.

    Returns updated state fields.
    """
    # Write-set boundary consistency assertion (D4): a still-pending
    # snapshot here means no approval path ran — audit loudly, then
    # proceed (the target_guard enforces the manifest boundary).
    from chaos_agent.agent.nodes.gates._write_set_boundary import (
        execute_loop_entry_sentinel,
    )
    execute_loop_entry_sentinel(state)

    task_id = state.get("task_id", "") or ""
    skill_name = read_active_skill_name(state)
    count = state.get("execute_loop_count", 0) + 1

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "execute_loop",
        f"Execute loop iteration {count}: executing skill '{skill_name}'",
        {"iteration": count, "skill_name": skill_name},
    )

    if count > MAX_EXECUTE_LOOP:
        logger.warning(
            f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP}) for task "
            f"{task_id}"
        )
        tracker.fail(f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP})")
        return fail_state(
            FailureCategory.EXECUTION_TIMEOUT,
            f"max_iterations={MAX_EXECUTE_LOOP}",
        )

    tracker.complete(f"Execute loop iteration {count} done")
    return {"execute_loop_count": count}


async def _check_execute_loop_limits(
    state: AgentState, count: int, task_id: str, tracker,
) -> dict | None:
    """Phase 1 early exits: max-iteration budget and zombie-replan detection.

    Returns a short-circuit result dict (already ``sync_to_store``'d) when the
    loop must terminate, else ``None`` to continue. Pure extraction from
    ``_execute_loop_with_llm`` — behaviour unchanged.
    """
    if count > MAX_EXECUTE_LOOP:
        logger.warning(
            f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP}) for task "
            f"{task_id}"
        )
        tracker.fail(f"Execute loop exceeded max iterations ({MAX_EXECUTE_LOOP})")
        result = fail_state(
            FailureCategory.EXECUTION_TIMEOUT,
            f"max_iterations={MAX_EXECUTE_LOOP}",
            state.get("messages", []),
        )
        await sync_to_store(state, result)
        return result

    # --- Zombie-replan early exit ---------------------------------
    # If the previous iteration's replan request pushed ``replan_count``
    # to ``max_replan_count`` AND the router refused to take the
    # "replan" branch (it returns False from ``_should_replan``
    # once the count cap is hit), ``state.replan_requested=True``
    # is sticky and unrelated subsequent iterations cannot escape
    # via any normal path:
    #
    #   * the gate's else branch only fires when THIS iteration's
    #     LLM emits another replan request (with the new no-replan
    #     hint, a well-behaved LLM stops emitting it)
    #   * ``state.error`` was cleared by the prior fire so the
    #     router's error branch can't end the turn
    #   * no active fault so the router's verifier branch can't end either
    #
    # The router falls through to "continue" and execute_loop
    # spins until ``max_execute_loop`` is hit (default 50) — up
    # to ~47 wasted LLM calls between the cap and the budget. We
    # short-circuit that here: detect the stuck state, terminate
    # cleanly with REPLAN_EXHAUSTED, and let the router's
    # ``state.error`` branch take "end".
    try:
        _max_replan_zombie = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan_zombie = 2
    zombie_replan = (
        state.get("replan_requested")
        and state.get("replan_count", 0) >= _max_replan_zombie
        # LIVE predicate (round-27 R3): the committed twin is True for every
        # task that ever injected, so on a destroyed experiment this guard
        # NEVER fired and replan kept burning budget until MAX_EXECUTE_LOOP.
        # "No further injection paths" is only worth terminating for when
        # nothing live remains — a live experiment (or an attributed native
        # mutation, which has no death oracle and stays committed-True)
        # still needs the loop to converge.
        and not has_live_fault(state)
    )
    if zombie_replan:
        stuck_error = (
            f"Replan exhausted after {state.get('replan_count', 0)} "
            f"attempt(s); no further injection paths available."
        )
        logger.warning(
            "Zombie replan detected on task %s: count=%d max=%d; "
            "terminating early to avoid burning execute_loop budget",
            task_id,
            state.get("replan_count", 0),
            _max_replan_zombie,
        )
        tracker.fail(stuck_error)
        result = {
            **fail_state(
                FailureCategory.REPLAN_EXHAUSTED,
                f"attempts={state.get('replan_count', 0)}",
                state.get("messages", []),
            ),
            "replan_requested": False,
            "execute_loop_count": count,
        }
        await sync_to_store(state, result)
        return result
    return None


def _maybe_auto_trigger_replan(state: AgentState, result: dict) -> None:
    """Convert this turn's terminal error into a real replan seam.

    The router's error branch auto-routes REPLAN-classified errors to
    ``agent_loop``, but that path writes NO replan bookkeeping: no
    ``replan_context`` (Phase 1 re-enters blind), no ``replan_count``
    increment (the budget gate never engages), and the error stays set —
    so ``should_continue_agent_loop`` rejects on its very next evaluation.
    The "replan" was a one-iteration detour straight to failure.

    The fix converges the system auto-trigger with the two LLM channels on
    the SAME review and seam writer: a synthesized request passes the
    structural review (:func:`_review_replan_request`) and is fired through
    :func:`_fire_replan_seam`. The trigger criterion is the canonical
    classifier — ``classify_error(...).action == REPLAN`` — matching exactly
    what the router's auto-detect consults, so this function and the router
    can never disagree about WHICH errors qualify.

    Reads only the error THIS turn stamped into ``result``: a stale error
    from a previous iteration must not re-fire the conversion mid-loop.
    """
    if result.get("replan_requested") or state.get("replan_requested"):
        return  # this turn already fired (or a sticky flag is pending)
    if not settings.replan_auto_trigger:
        return
    error = str(result.get("error") or "")
    if not error:
        return
    if classify_error(error).action is not ErrorAction.REPLAN:
        return
    try:
        _max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        _max_replan = 2
    if state.get("replan_count", 0) >= _max_replan:
        return  # budget exhausted — the error proceeds to its normal verdict

    request = ReplanRequest(
        kind="feasibility",
        decision="plan_invalid",
        invalidated_assumption=(
            "Execution terminated with a replan-classified runtime error: "
            f"{error[:1800]}"
        ),
        observed_evidence=[error[:500]],
        affected_step="Phase 2 execution terminated with a replan-classified error",
        changes_target_or_risk=False,
    )
    review_reason = _review_replan_request(state, request)
    if review_reason:
        # Same outcome as a reviewed rejection of an LLM request: keep
        # executing. Clear the terminal error or the router's error branch
        # would bounce the run back to agent_loop and reject instantly.
        result["error"] = None
        result["failure_detail"] = None
        result.setdefault("messages", []).append(HumanMessage(content=wrap_system_reminder(
            f"[LIFECYCLE REVIEW] Continue execution: {review_reason} A terminal "
            "error describing a problem is evidence about that call, not by "
            "itself a conclusion that the approved plan is infeasible."
        )))
        return
    replan_context = _build_replan_context(state, request)
    _fire_replan_seam(state, result, request, replan_context)
    logger.info(
        "System auto-triggered replan seam from terminal error: %s", error[:200]
    )


def _build_convergence_hints(
    count: int, persist_into: list | None = None,
) -> list[HumanMessage]:
    """Phase 3 convergence hints: tiered last-iteration conclusion prompts.

    Returns 0 or 1 ``HumanMessage`` nudging the LLM to conclude / replan as the
    iteration budget runs low.

    Persisted through ``persist_replaceable_hint`` — a STABLE id, so each tier
    replaces the previous notice instead of stacking. Turn-local injection alone
    meant the model saw the countdown once and the next turn had no idea how much
    budget was left; a plain append would be worse, leaving "iteration 3 of 15"
    in history for turn 12 to read. Replacement gives history exactly one entry
    that always states the current number.
    """
    remaining = MAX_EXECUTE_LOOP - count
    hints: list[HumanMessage] = []
    _persist = persist_into if persist_into is not None else []

    def _emit(text: str) -> None:
        hints.append(persist_replaceable_hint(
            _persist, "budget", "execute", text,
        ))

    if MAX_EXECUTE_LOOP - 5 <= count < MAX_EXECUTE_LOOP - 1:
        # Tier 1: Soft warning — iterations running low
        _emit(
            f"**Iteration Progress**: You are on iteration {count} of max {MAX_EXECUTE_LOOP} "
            f"({remaining} remaining). "
            f"Think the next step through BEFORE acting: state to yourself what "
            f"you already know, what is still genuinely unknown, and what the next "
            f"call would add that the last one did not. "
            f"Use the remaining budget only for safe, meaningful actions that advance "
            f"the approved objective or resolve relevant uncertainty. Avoid spending it "
            f"on unchanged repetition. If the execution evidence is sufficient, conclude; "
            f"if the plan itself requires reconsideration, emit the structured replan request."
        )
    elif count == MAX_EXECUTE_LOOP - 1:
        # Tier 2: Urgent warning — second-to-last iteration
        _emit(
            f"**CRITICAL WARNING**: This is iteration {count} of max {MAX_EXECUTE_LOOP} — "
            f"your SECOND-TO-LAST iteration. Reason it through before you act: "
            f"with one action left, name the single unknown that action would "
            f"resolve. If you cannot name one, there is nothing left to gather. "
            f"Choose the next action only if it is safe, "
            f"meaningful, and justified by a changed hypothesis or new evidence. Otherwise "
            f"produce an evidence-based conclusion, or emit the structured replan request when the plan needs "
            f"reconsideration."
        )
    elif count >= MAX_EXECUTE_LOOP:
        # Tier 3: Final conclusion — tools unbound, must provide conclusion
        _emit(
            f"**FINAL ITERATION**: This is iteration {count} of max {MAX_EXECUTE_LOOP}. "
            f"No further tool calls are available. Think through what the evidence "
            f"actually supports, then provide a concise, evidence-based "
            f"conclusion that distinguishes observed facts from remaining uncertainty. "
            f"If the available evidence shows that a different plan is required, output "
            f"the structured replan request with the plan assumption that needs reconsideration."
        )
    return hints


async def _build_execute_system_prompt(
    state: AgentState, task_id: str, skill_name: str, tools,
    skill_catalog: str, env_info, registry,
) -> tuple[str, object]:
    """Phase 3: build the execute-phase system prompt + capability context.

    Returns ``(execute_prompt, capability_context)``. The progress ledger is NO
    LONGER injected here (context-cache-prefix-stability Unit A / task 2.1): it
    rides the message TAIL via the append-only channel in
    ``_execute_loop_with_llm`` so this head stays byte-stable across rounds.
    """
    from chaos_agent.agent.prompts import build_system_prompt, PromptMode
    from chaos_agent.agent.env_info import compute_env_info
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    plan = state.get("plan")
    plan_path = state.get("plan_path")
    # Build structured_params_hint from FaultSpec. Duration rides along:
    # the execute prompt (MINIMAL mode) has no Reviewed FaultSpec section,
    # so this hint is the executor's ONLY view of the contracted injection
    # window — command-level timers (sleep N / --timeout N) anchor on it.
    _spec_for_hint = read_fault_spec(state)
    structured_params_hint = ""
    if _spec_for_hint and _spec_for_hint.is_complete:
        structured_params_hint = (
            f"scope={_spec_for_hint.scope}, "
            f"target={_spec_for_hint.fault_target}, "
            f"action={_spec_for_hint.fault_action}, "
            f"duration={_spec_for_hint.duration_seconds}s"
        )
    # Build user_params_hint from FaultSpec.params so user-specified
    # values (e.g. finalizer=...) take priority over skill template
    # placeholders during Phase 2 execution.
    user_params_hint = ""
    if _spec_for_hint and _spec_for_hint.params:
        user_params_hint = json.dumps(dict(_spec_for_hint.params), ensure_ascii=False)
    # Resolve env_info: prefer constructor arg, fallback to dynamic computation
    resolved_env_info = env_info or await compute_env_info(task_id)
    capability_context = build_capability_context(state, "execute", tools)
    execute_prompt = build_system_prompt(
        PromptMode.MINIMAL,
        skill_catalog=registry.build_catalog_prompt() if registry else skill_catalog,
        skill_name=skill_name,
        plan=plan or "",
        plan_path=plan_path or "",
        structured_params_hint=structured_params_hint,
        user_params_hint=user_params_hint,
        env_info=resolved_env_info,
        profile=capability_context.profile,
    )
    return execute_prompt, capability_context


def make_execute_loop(hook=None, llm=None, tools=None, skill_catalog="", env_info=None, registry=None):
    """Create an execute_loop node with optional PreReasoningHook and LLM.

    When llm is provided, the node performs actual LLM reasoning
    (calling the model with bound tools, returning the response as a message).
    When llm is None, behaves identically to the plain execute_loop
    (only tracks iteration count, for test compatibility).
    """
    if llm is None and hook is None:
        return execute_loop

    async def _execute_loop_with_llm(state: AgentState) -> dict:
        # Write-set boundary consistency assertion (D4): a still-pending
        # snapshot here means no approval path ran — audit loudly, then
        # proceed (the target_guard enforces the manifest boundary).
        # Runs BEFORE the first LLM call so the assertion costs zero
        # tokens.
        from chaos_agent.agent.nodes.gates._write_set_boundary import (
            execute_loop_entry_sentinel,
        )
        execute_loop_entry_sentinel(state)

        # 0. Reset time_wait consecutive-call guard if last round included
        # any non-wait tool (allows time_wait to be called again after a
        # real tool like kubectl ran).  Scans the entire most-recent
        # ToolMessage batch to handle parallel tool calls correctly.
        from chaos_agent.tools.wait import check_and_reset_wait_guard
        check_and_reset_wait_guard(state.get("messages", []))

        # 0b. Create-reconcile three-state scan (blade-create-reconcile-
        # before-retry D6): classify the most recent gate-armed create
        # ToolMessage (uncertain / gate-blocked-or-fabricated / plain;
        # which tools are gate-armed is declared provider-side and
        # consumed through the registry seam) and update the
        # create_reconcile flag BEFORE this iteration's LLM call. The
        # interception gate judges the response AFTER it — same
        # iteration, strictly ordered, so the FIRST blind retry after a
        # result-uncertain create already meets a registered flag. The
        # scan derives everything from the message history, so a lost
        # update (early-exit paths return before result-building) simply
        # re-derives the same outcome next iteration.
        from chaos_agent.agent.nodes.execute._reconcile_gate import (
            NO_CHANGE as _RECONCILE_NO_CHANGE,
            scan_create_reconcile,
        )
        _reconcile_update = scan_create_reconcile(
            state.get("messages", []),
            state.get("create_reconcile"),
        )

        # 1. Iteration count + limit check
        task_id = state.get("task_id", "") or ""
        skill_name = read_active_skill_name(state)
        count = state.get("execute_loop_count", 0) + 1

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "execute_loop",
            f"Execute loop iteration {count}: executing skill '{skill_name}'",
            {"iteration": count, "skill_name": skill_name},
        )

        # Phase 1 early exits (max-iteration + zombie-replan) extracted.
        early_exit = await _check_execute_loop_limits(state, count, task_id, tracker)
        if early_exit is not None:
            return early_exit

        # Freeze the progress-ledger anchor on the first execute iteration.
        # Lazy (execute_loop runs AFTER confirmation_gate, so fault_spec here is
        # the APPROVED goal, not a pre-confirmation draft) and idempotent (only
        # when the anchor is absent, so the tool-maintained ledger on later
        # turns is never clobbered). ``_ledger_seeded`` persists the seeded
        # ledger in ``result``.
        #
        # "Absent" means the ANCHOR, not the whole ledger (tier1-speedup): the
        # cross-graph bridge can deliver an intent-stage ledger here — facts
        # the model recorded during clarification — which by design carries no
        # anchor (``update_progress`` never writes one). Freezing only on an
        # empty ledger would let that truthy-but-anchorless ledger silently
        # disable anchor freezing for the whole execute/verify/recover run. So:
        # seed the anchor while PRESERVING the intent-time state/log.
        from chaos_agent.agent.progress_ledger import ANCHOR, LOG, STATE, freeze_anchor
        _ledger = state.get("progress_ledger")
        _ledger_seeded = False
        # Non-dict corruption is treated as empty, matching merge/render's
        # ``isinstance(Mapping)`` guards.
        if not isinstance(_ledger, dict) or not _ledger:
            _spec_for_anchor = read_fault_spec(state)
            _ledger = freeze_anchor(
                _spec_for_anchor.to_dict() if _spec_for_anchor else None,
                goal=str(state.get("input") or ""),
            )
            _ledger_seeded = True
        elif not (_ledger.get(ANCHOR) or {}):
            _spec_for_anchor = read_fault_spec(state)
            _frozen = freeze_anchor(
                _spec_for_anchor.to_dict() if _spec_for_anchor else None,
                goal=str(state.get("input") or ""),
            )
            _ledger = {
                ANCHOR: _frozen[ANCHOR],
                STATE: _ledger.get(STATE) or {},
                LOG: _ledger.get(LOG) or [],
            }
            _ledger_seeded = True
        else:
            # Anchor present — reconcile its spec side against the CURRENT
            # contract (cascade review C2, plan-A convergence point). The
            # spec's legitimate change channels are structurally scattered
            # (plan_change_confirm approval, tool_screener drift correction,
            # agent_loop lazy derivation after a replan re-entry); patching
            # each seam proved inexhaustible (O1 covered one of three).
            # execute re-entry is the ONE point every changed spec must pass
            # through, so the anchor realigns here before being rendered onto
            # the execute/verify/recover message tails (context-cache-prefix-
            # stability Unit A moved the ledger off the prompt heads; the
            # reconciled anchor still reaches all three via state.progress_ledger,
            # which each loop renders onto its tail). Goal/state/log untouched;
            # no-drift → no write (None), keeping the result free of a
            # needless override racing the model's update_progress.
            from chaos_agent.agent.progress_ledger import reconcile_anchor_spec
            _spec_now = read_fault_spec(state)
            _reconciled = reconcile_anchor_spec(
                _ledger, _spec_now.to_dict() if _spec_now else None,
            )
            if _reconciled is not None:
                _ledger = dict(_reconciled)
                _ledger_seeded = True  # persist via the same first-iteration channel below
                logger.info(
                    "anchor spec reconciled to current contract (drifted "
                    "across a spec-change seam)",
                )

        # 2. Call pre_reason_hook (memory compaction)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # 2b. Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state)

        # 3. Call LLM with bound tools
        # Declared OUTSIDE the ``llm`` branch, matching agent_loop's
        # ``_injections_for_state``. The result-building code below reads it from
        # a SIBLING branch (``if response is not None``), not a nested one, so a
        # declaration inside would only be safe as long as ``llm is None`` stays
        # unreachable — which is true today but is not a property this line
        # should depend on.
        _hints_for_state: list = []
        _rejection_consumed = False
        if llm is not None:
            messages = list(state.get("messages", []))

            # --- Execution hints (stagnation, idle, conflict, errors) ---
            _hint_counts: dict = {}
            hints, stagnant_tool = _build_execution_hints(
                messages, state, persist_into=_hints_for_state,
                counts_out=_hint_counts,
            )
            messages.extend(hints)

            # --- Phase 1 → Phase 2 kickoff (planning → execution seam) ---
            # A fresh "Planning finalized" with no transition signal after it
            # makes the model treat this turn as the wrap-up of planning and
            # output prose instead of calling the injection tool (the stall
            # guard then spent a whole round-trip correcting it). Emit one
            # explicit kickoff per finalization — positional detection also
            # re-arms after a replan produces a new finalization.
            # Persistence: ``_hints_for_state`` carries it into
            # ``result["messages"]`` (LangGraph state), and the direct write
            # below lands it in the task JSON immediately (the hook flush at
            # the next pre_reason_hook sees the same message again). This
            # double write is INTENTIONAL (crash-safe archival before the
            # node returns); it is idempotent ONLY because the message has
            # an id from construction (B78) — see the session store's own
            # id-stamping guard — so both serializations dedup to one entry.
            _kickoff = _maybe_build_phase2_kickoff(messages)
            if _kickoff is not None:
                messages.append(_kickoff)
                _hints_for_state.append(_kickoff)
                if hook and getattr(hook, "session_store", None) and task_id:
                    _kickoff.additional_kwargs.setdefault("_node", EXECUTE_LOOP)
                    hook.session_store.append_messages(task_id, [_kickoff])
                logger.info("Phase 2 kickoff emitted (planning → execution seam)")

            # --- Deferred replan-review rejection (tool-channel adjacency) ---
            # A plan_invalid replan rejected by _review_replan_request reaches
            # the model one iteration later: by now phase2_tools has created
            # the ToolMessage, so this review no longer breaks tool-response
            # adjacency. Emitted once, then the flag is cleared below.
            if state.get("_replan_review_rejection"):
                _rejection_msg = HumanMessage(content=wrap_system_reminder(
                    f"[LIFECYCLE REVIEW] Replan rejected: {state['_replan_review_rejection']} "
                    "The approved plan remains authoritative — continue executing it."
                ))
                messages.append(_rejection_msg)
                _hints_for_state.append(_rejection_msg)
                _rejection_consumed = True

            # --- Progress ledger (drift anchor) — TAIL append, not the head ---
            # context-cache-prefix-stability Unit A (tasks 2.2/2.3, design D1/D2):
            # the ledger used to ride build_execute_system_prompt's head, so its
            # per-round rewrite broke the provider cache prefix from the first
            # changed byte. It now rides the message tail through the SAME
            # append-only channel as the hints above (``messages.append`` +
            # ``_hints_for_state``). A FRESH snapshot rides the tail every round
            # (max recency for the anchor); ``build_ledger_tail_content`` prefixes
            # it with a "supersedes earlier snapshots" marker so the stale copies
            # append-only leaves in history can't mislead the model. NO stable id:
            # a stable id would make add_messages replace the copy IN PLACE, pinning
            # it at its first position — dragging it out of the recency tail AND
            # reintroducing an early volatile byte that re-bills the whole suffix
            # every round (measured: stable-prefix share decays 50%→41% as the loop
            # grows, vs append's 63%→82%). Placed before the convergence hints so
            # the terminal nudge stays outermost on the final iterations.
            from chaos_agent.agent.progress_ledger import build_ledger_tail_content
            _ledger_tail = build_ledger_tail_content(_ledger)
            if _ledger_tail:
                _ledger_msg = HumanMessage(content=wrap_system_reminder(_ledger_tail))
                messages.append(_ledger_msg)
                _hints_for_state.append(_ledger_msg)

            # --- Convergence hints (last-iteration conclusion prompts) ---
            messages.extend(_build_convergence_hints(
                count, persist_into=_hints_for_state,
            ))

            # Build execution prompt using the modular prompt system (Phase 3
            # prompt build extracted to _build_execute_system_prompt).
            execute_prompt, capability_context = await _build_execute_system_prompt(
                state, task_id, skill_name, tools, skill_catalog, env_info, registry,
            )
            # On last iteration, unbind tools to force text conclusion
            if count >= MAX_EXECUTE_LOOP:
                llm_to_call = llm
            else:
                visible_tools = filter_tools_for_context(tools, capability_context)
                # An empty GATE result must not degrade to an unbound LLM:
                # the model would still emit calls from the prompt's tool list.
                # Bind nothing instead. Only when a NON-EMPTY set was gated away
                # though — with no static tools at all (or after the benign
                # stagnant filter) the unbound LLM is the intended prose path,
                # and it is the only usable one, since a provider rejects a
                # request carrying an empty ``tools`` array.
                if tools and not visible_tools:
                    logger.warning(
                        "execute_loop: capability gate left no visible tools "
                        "(profile=%s) — binding an empty tool set",
                        capability_context.profile,
                    )
                    llm_to_call = llm.bind_tools([])
                else:
                    tools_this_iter = filter_stagnant_tool(visible_tools, stagnant_tool)
                    llm_to_call = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

            # Record system prompt to session store (dedup handles repeated prompts)
            record_system_prompt(hook, state, execute_prompt, node_name=EXECUTE_LOOP)

            response = await llm_to_call.ainvoke(
                [SystemMessage(content=execute_prompt)] + messages
            )
        else:
            response = None

        # 4. Build result
        result = {"execute_loop_count": count}
        # Scan outcome → state. ``None`` is meaningful (clear the cycle);
        # NO_CHANGE writes nothing (flag keeps its value, merge diff stays
        # minimal). Written into ``result`` (not ``state``) so the merge
        # publishes it AND the same iteration's interception gate — which
        # reads the merged result view — sees the freshly registered flag.
        if _reconcile_update is not _RECONCILE_NO_CHANGE:
            result["create_reconcile"] = _reconcile_update
        if _rejection_consumed:
            result["_replan_review_rejection"] = None

        # Persist the freshly-frozen ledger anchor (first iteration only). Later
        # turns leave progress_ledger to the update_progress tool, so this never
        # clobbers model-recorded content.
        if _ledger_seeded:
            result["progress_ledger"] = _ledger

        # Extract experiment_uid from ToolMessages (blade_create results)
        messages = state.get("messages", [])
        fault_spec = read_fault_spec(state)
        artifacts = collect_execution_artifacts(
            messages,
            state.get("execution_artifacts"),
            task_id=task_id,
            operation_family=(fault_spec.fault_target if fault_spec else ""),
        )
        if artifacts != (state.get("execution_artifacts") or []):
            result["execution_artifacts"] = artifacts

        # Extract the live experiment UID through the registry: each
        # UID-bearing provider scans for its own experiment id, so this loop
        # never names a carrier-specific extractor.
        from chaos_agent.agent.providers import FaultProviderRegistry
        from chaos_agent.transports.registry import is_host_scope_channel

        experiment_uid = FaultProviderRegistry.extract_experiment_uid(
            messages,
            retired=state.get("retired_experiment_uids"),
            is_host=is_host_scope_channel(state),
        )
        if experiment_uid and experiment_uid != state.get("experiment_uid"):
            result["experiment_uid"] = experiment_uid
            logger.info(f"Extracted experiment UID from ToolMessage: {experiment_uid}")
            # Birth registry (B76 review G): record ownership the moment a UID
            # is first seen — the same event that (legitimately) overwrites the
            # single attribution slot. The slot is last-write-wins, so without
            # this append a superseded experiment's liability claim is erased
            # by the next create and becomes structurally un-recoverable (the
            # message scan returns only the newest UID; the whitelist's durable
            # source is that same slot). Append-only for the task lifetime.
            _owned = list(state.get("owned_experiment_uids") or [])
            if experiment_uid not in _owned:
                _owned.append(experiment_uid)
                result["owned_experiment_uids"] = _owned
                # Round-32b — persist the combo discriminator at birth:
                # False asserts "experiments born, no native companion
                # (yet)" and lets a later BALANCED wing settle the row dead
                # (may_carry_live_fault A2 — the C2 ghost fix). A combo
                # mark overwrites it in either order (blade-first marks a
                # later iteration; native-first's upgrade seam below in
                # this same result dict writes True and the later write
                # wins), and the store's upsert latch keeps a subsequent
                # None flush from erasing either leg.
                if not (
                    state.get("combo_native_issued")
                    or result.get("combo_native_issued")
                ):
                    result["combo_native_issued"] = False
            if not state.get("injection_start_time"):
                result["injection_start_time"] = now_iso()
                logger.info("Set injection_start_time (experiment UID first seen)")

        # Birth registry plural face (round-26): the single-slot seam above
        # registers ownership only for the uid that CHANGED the slot — a
        # composite inline create (``blade create A && blade create B``)
        # proves TWO births in one call and the single-slot scan surfaces
        # only the first, so the second was born an orphan: never in
        # ``owned_experiment_uids``, invisible to ``live_liability_uids``,
        # unrecoverable by any sweep. Scan the plural face and append every
        # un-owned birth. Idempotent against the ledger view (the single-slot
        # append above may have already registered one of them — read the
        # result's view first, it supersedes state).
        _born = FaultProviderRegistry.extract_experiment_uids(
            messages,
            retired=state.get("retired_experiment_uids"),
            is_host=is_host_scope_channel(state),
        )
        if _born:
            _owned = list(
                result.get("owned_experiment_uids")
                or state.get("owned_experiment_uids")
                or []
            )
            _unowned = sorted(uid for uid in _born if uid not in _owned)
            if _unowned:
                _owned.extend(_unowned)
                result["owned_experiment_uids"] = _owned
                # Round-32b — same birth assertion as the single-slot seam
                # above: the plural face births UIDs the single face never
                # fired on (the slot already held one of them).
                if not (
                    state.get("combo_native_issued")
                    or result.get("combo_native_issued")
                ):
                    result["combo_native_issued"] = False
                logger.info(
                    "Birth registry (plural): %s proven born",
                    ", ".join(_unowned),
                )

        # Death registration (B76 review I1): mirror twin of the birth
        # registry above. An LLM-issued destroy's death proof lives ONLY in
        # messages (the AIMessage tool_call + its paired ToolMessage), which
        # compaction summarises away and the recover bridge resets — so the
        # moment a PROVEN kill is visible in this turn's history, it must
        # land in the durable retired ledger (LLM-side blade_destroy has no
        # framework-side ToolMessage; only this seam records it). Idempotent:
        # the append is filtered against the current ledger view, and
        # ``live_liability_uids`` already subtracts the ledger.
        _proven_dead = FaultProviderRegistry.destroyed_proven_experiment_ids(messages)
        if _proven_dead:
            _retired = list(state.get("retired_experiment_uids") or [])
            _unrecorded = [
                uid for uid in _proven_dead if uid not in _retired
            ]
            if _unrecorded:
                _retired.extend(_unrecorded)
                result["retired_experiment_uids"] = _retired
                logger.info(
                    "Death registry: %s proven destroyed (paired tool output)",
                    ", ".join(_unrecorded),
                )

        # Detect injection method for verifier Layer 1 strategy selection.
        # Direction B: injection_method is recorded at ISSUE time (channel A) by
        # ``_process_response_tool_calls`` from the fresh ``response.tool_calls``.
        # This history re-scan is the NARROW fallback, gated by ``need_redetect``
        # so we do not re-derive the same answer every iteration:
        #   - no method yet          → RESUME fallback: recover attribution after
        #     a restart, where the injection lives in history (not this turn's
        #     response, which channel A can no longer see); also covers the
        #     first injection turn until channel A sets it below.
        #   - experiment_uid appeared AND current method is the provisional multi-step
        #     kubectl_native → UPGRADE it to the experiment backend (host_blade /
        #     kubectl_exec). This is the one thing channel A structurally cannot
        #     do: the UID only exists in the tool RESULT, not the issue-time call.
        # Steady state (method already set, no upgrade trigger) skips the scan.
        current_injection_method = state.get("injection_method") or result.get("injection_method")
        _cur_provider = FaultProviderRegistry.resolve_by_method(current_injection_method)

        # REVOCATION (channel B result-awareness): issue-time attribution was
        # recorded BEFORE the tool result existed, so a UID-less native method
        # can be committed for an attempt that provably failed. A proven-failed
        # attribution is cleared so downstream gates (REPLAN_EXHAUSTED context,
        # verifier routing, the tail projection) never treat a failed attempt
        # as a live fault. The attempt itself remains replan evidence via the
        # epoch scan in ``_injection_attempted_this_contract``.
        _revoked = _maybe_revoke_issue_time_attribution(
            state, result, messages, current_injection_method
        )

        if not _revoked and _should_redetect_injection_method(current_injection_method, experiment_uid):
            # Teardown ≠ fault mutation, on the history side too (R8-1):
            # P3 threads the ``is_teardown`` matcher into the re-scan — a
            # registered-vehicle cleanup delete in the epoch must not
            # resurrect the attribution channel A correctly skipped
            # (call-level skip, mixed batches included; the epoch bound
            # still scopes out pre-seam attempts).
            from chaos_agent.agent.execution_artifacts import make_teardown_matcher

            detected_method = _detect_injection_method(
                _epoch_bounded_messages(messages, state),
                is_host=is_host_scope_channel(state),
                is_teardown=make_teardown_matcher(
                    state.get("execution_artifacts") or []
                ),
            )
            if detected_method and detected_method != current_injection_method:
                _new_provider = FaultProviderRegistry.resolve_by_method(detected_method)
                _committed_this_scan = False
                # Experiment-carrying method wins over a provisional multi-step one:
                # if a experiment_uid appeared, upgrade the multi-step backend (kubectl_native)
                # to the experiment backend (host_blade / kubectl_exec).
                if (
                    _cur_provider is not None
                    and _cur_provider.is_multi_step
                    and _new_provider is not None
                    and _new_provider.has_experiment_uid
                ):
                    result["injection_method"] = detected_method
                    _committed_this_scan = True
                    # COMBO (native-first order): the provisional native
                    # injection committed at issue time DID mutate the target,
                    # and the blade experiment succeeded too — both vehicles
                    # acted. Mark the combo durably so recovery routes to the
                    # LLM-driven Layer-1 flow (deterministic destroy would
                    # leak the native mutation — see combo_native_issued).
                    # EXCEPT when the native component is PROVEN failed: then
                    # only the experiment acted — upgrade WITHOUT the combo
                    # mark so recovery stays deterministic.
                    _native_disproven = _issue_disproven_in_epoch(
                        state, messages, _cur_provider
                    )
                    if _native_disproven:
                        logger.info(
                            f"Upgraded injection_method: {current_injection_method} → {detected_method} "
                            f"(native component proven failed — no combo mark)"
                        )
                    else:
                        result["combo_native_issued"] = True
                        logger.info(
                            f"Upgraded injection_method: {current_injection_method} → {detected_method} "
                            f"(combo marked — native component needs LLM undo)"
                        )
                elif not current_injection_method:
                    # RESUME re-attribution must not resurrect a PROVEN-FAILED
                    # attempt: the same counter-evidence that revokes it vetoes
                    # the restore — otherwise revocation and RESUME would
                    # oscillate every iteration (RESUME's detect keys on the
                    # attempt, which a failed attempt still satisfies).
                    _resume_ok = not _issue_disproven_in_epoch(
                        state, messages, _new_provider
                    )
                    if _resume_ok:
                        result["injection_method"] = detected_method
                        _committed_this_scan = True
                        logger.info(f"Detected injection_method: {detected_method}")
                elif (
                    _cur_provider is not None
                    and _cur_provider.has_experiment_uid
                    and not experiment_uid
                    and not state.get("experiment_uid")
                    and _new_provider is not None
                    and not _new_provider.has_experiment_uid
                ):
                    # DOWNGRADE (task-51193464): the current experiment-method
                    # attribution has gone unfulfilled (no UID ever appeared)
                    # while the registry's RECENCY arbitration now recognises
                    # a MORE-RECENT UID-less native injection — the original
                    # attribution was a mis-read (e.g. a k8s object uid taken
                    # for blade evidence). Correct it to the native backend so
                    # the fault-handle projection can claim the fault and the
                    # verifier gate opens. If a genuine blade experiment later
                    # lands its UID, the UPGRADE branch above re-promotes the
                    # attribution and marks the combo — no recovery coverage
                    # is lost by this correction.
                    result["injection_method"] = detected_method
                    _committed_this_scan = True
                    logger.info(
                        f"Downgraded injection_method: {current_injection_method} → {detected_method} "
                        "(experiment attribution unfulfilled — no UID; native "
                        "evidence is more recent)"
                    )
                # Set injection_start_time for non-ChaosBlade methods too —
                # only when this scan actually committed an attribution.
                if _committed_this_scan and not state.get("injection_start_time") and "injection_start_time" not in result:
                    result["injection_start_time"] = now_iso()
                    logger.info("Set injection_start_time (%s detected)", detected_method or current_injection_method)

        # Post-landing readback guard + session-reconciler arming: retired
        # with the CR channel (M2 task 2.4) — the programmatic carrier
        # assembler verifies its own landing inline, and the migration-
        # window CR applies recover from the task ledger (task 2.3).

        # Extract kubectl exec injection pod name for verifier preference
        current_pod_name = state.get("kubectl_exec_pod_name")
        if not current_pod_name:
            from chaos_agent.agent.providers import FaultProviderRegistry

            pod_name = FaultProviderRegistry.extract_kubectl_exec_pod_name(
                messages
            )
            if pod_name:
                result["kubectl_exec_pod_name"] = pod_name
                logger.info(f"Recorded kubectl exec pod name: {pod_name}")

        # Extract skill use-case content from read_skill_resource ToolMessages
        # (used by Layer 2 verification as PRIMARY AUTHORITY)
        current_skill_case = state.get("skill_case_content")
        if not current_skill_case:
            for msg in reversed(messages):
                if not isinstance(msg, ToolMessage):
                    continue
                if getattr(msg, "name", "") != "read_skill_resource":
                    continue
                content = msg.content if isinstance(msg.content, str) else ""
                # Detect catalogue use-case files by key section markers
                if content and ("**故障现象**" in content or "**注入验证**" in content or "**恢复验证**" in content):
                    result["skill_case_content"] = content
                    logger.info("Extracted skill_case_content from read_skill_resource ToolMessage")
                    break

        if response is not None:
            # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
            # has the correct kubeconfig, even if the LLM forgot to include it.
            kubeconfig = _resolve_kubeconfig(state)
            inject_kubeconfig_into_tool_calls(response, kubeconfig)
            inject_task_id_into_tool_calls(response, task_id)
            sync_kubewiz_runtime(state)

            # Output-limit truncation: the tool calls in this response may carry
            # silently incomplete arguments, so none of them may run against the
            # cluster. Parseable calls get a synthetic error answer; calls whose
            # JSON is broken are stripped from the message instead (answering
            # those makes the provider parse the args and reject the request).
            # Either way the screener is flagged to route back here rather than
            # forwarding the batch to the ToolNode.
            truncated = handle_truncated_response(response)
            if truncated is not None:
                safe_message, truncated_results = truncated
                logger.warning(
                    "execute_loop: response truncated by output token limit — "
                    "%d tool call(s) failed unexecuted, %d unparseable call(s) dropped",
                    len(truncated_results),
                    len(getattr(response, "invalid_tool_calls", None) or []),
                )
                result["messages"] = _hints_for_state + [safe_message] + truncated_results
                if _hint_counts and _hint_counts != (state.get("hint_repeat_counts") or {}):
                    result["hint_repeat_counts"] = _hint_counts
                result["truncated_tool_calls"] = True
                record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)
                log_reasoning_content(response, "Execute loop", count)
            else:
                # Create-reconcile gate (blade-create-reconcile-before-retry
                # D6): judge the batch's gate-armed create calls against the
                # registered flag BEFORE the response is published. Merged
                # view — the same iteration's top-of-loop scan may have
                # just registered the flag into ``result``.
                if "create_reconcile" in result:
                    _gate_flag = result["create_reconcile"]
                else:
                    _gate_flag = state.get("create_reconcile")
                from chaos_agent.agent.nodes.execute._reconcile_gate import (
                    apply_reconcile_gate,
                )
                _gate_outcome = await apply_reconcile_gate(
                    response, state.get("messages", []), _gate_flag,
                    tracker=tracker,
                    kubeconfig=kubeconfig, task_id=task_id,
                )
                if _gate_outcome is not None:
                    _gate_answers, _gate_flag_after = _gate_outcome
                    # Whole batch held: fabricated answers replace
                    # execution, the screener routes back here (never the
                    # ToolNode), and the issue-time bookkeeping below is
                    # skipped — none of these calls will run.
                    result["messages"] = _hints_for_state + [response] + _gate_answers
                    if _hint_counts and _hint_counts != (state.get("hint_repeat_counts") or {}):
                        result["hint_repeat_counts"] = _hint_counts
                    result["truncated_tool_calls"] = False
                    result["_reconcile_gate_blocked"] = True
                    result["create_reconcile"] = _gate_flag_after
                    record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)
                    log_reasoning_content(response, "Execute loop", count)
                    logger.info(
                        "create_reconcile gate: held a same-fingerprint "
                        "create batch (blocked_count=%d)",
                        _gate_flag_after.get("blocked_count"),
                    )
                else:
                    result["messages"] = _hints_for_state + [response]
                    if _hint_counts and _hint_counts != (state.get("hint_repeat_counts") or {}):
                        result["hint_repeat_counts"] = _hint_counts
                    # Clear at the source every non-truncated turn: a flag left set
                    # by a turn that exited via replan/end (bypassing the screener
                    # that consumes it) must not reach a later, healthy batch.
                    result["truncated_tool_calls"] = False
                    result["_reconcile_gate_blocked"] = False

                    # Immediately save AI message (including reasoning_content) to session
                    record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)

                    # Diagnostic log for reasoning_content presence
                    log_reasoning_content(response, "Execute loop", count)

                    _process_response_tool_calls(response, state, result, tracker, count)

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result, hook_updates)

        # A truncated turn is not a conclusion: it was cut off mid-emission, so
        # its (possibly empty) tool_calls say nothing about whether the executor
        # is done. Running the terminal-conclusion detector here would also let
        # it append a nudge AFTER the synthetic tool results, breaking the
        # "answers are the last messages" precondition the screener checks
        # before diverting the batch away from the ToolNode.
        if response is not None and not result.get("truncated_tool_calls"):
            _detect_terminal_conclusion(response, state, result)

        # --- Last-iteration failure attribution ---
        if count >= MAX_EXECUTE_LOOP:
            if not has_active_fault({**state, **result}):
                _fs = fail_state(
                    FailureCategory.EXECUTION_TIMEOUT,
                    f"max_iterations={MAX_EXECUTE_LOOP}",
                    state.get("messages", []) + result.get("messages", []),
                )
                result.update(_fs)

        _handle_replan(response, state, result)

        # System auto-trigger channel: converge this turn's terminal error
        # with the LLM channels on the same review + seam writer (see the
        # function docstring for the broken path this replaces).
        _maybe_auto_trigger_replan(state, result)

        # Fault-handle sync — the single projection point (see the helper
        # docstring for the ordering contract it enforces).
        _project_fault_handle(state, result)

        # Replan must not carry helper pods from the failed execution attempt
        # into a newly approved plan. This is artifact cleanup, not fault
        # recovery; actual fault compensations remain the recover graph's job.
        if result.get("replan_requested"):
            merged_artifacts = result.get("execution_artifacts")
            if merged_artifacts is None:
                merged_artifacts = state.get("execution_artifacts")
            cleaned_artifacts, _ = await cleanup_debug_pod_artifacts(
                merged_artifacts,
                kubeconfig=_resolve_kubeconfig(state),
                task_id=task_id,
            )
            if cleaned_artifacts != (merged_artifacts or []):
                result["execution_artifacts"] = cleaned_artifacts

        tracker.complete(f"Execute loop iteration {count} done")
        await sync_to_store(state, result)
        # Patch C — wall-clock cause labelling. If the router is about
        # to terminate this loop due to ``settings.max_inject_seconds``,
        # stamp ``failure_reason = WALL_CLOCK_TIMEOUT`` so the result
        # envelope is honest. Only fires when budget > 0 and started.
        from chaos_agent.agent.router import (
            mark_loop_exhausted,
            mark_wall_clock_timeout,
        )
        result = mark_wall_clock_timeout(state, result)
        # Same idea for the iteration budget. The router stops on
        # ``count >= max_loop`` while the early-exit check above only fires on
        # ``count > MAX_EXECUTE_LOOP``, so a run that uses its budget EXACTLY
        # was ending with no recorded cause at all (task-ff057e7f: 100/100
        # iterations, ``failure_reason=""``, envelope said success).
        return mark_loop_exhausted(result, count, MAX_EXECUTE_LOOP)

    return _execute_loop_with_llm
