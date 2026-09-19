"""Verifier node: two-layer post-injection verification.

Layer 1 (General): Programmatically call blade_status + blade_query_k8s
    to check experiment state and per-resource success.
Layer 2 (Specific): LLM reads skill's "注入验证" section and uses
    kubectl/blade tools to verify the actual fault effect.
    If no injection verification instructions are found, skips with a warning.

The verifier operates as a ReAct loop:
    verifier_loop ⇄ verifier_tools
When the LLM outputs a final text (no tool_calls), the loop ends.
"""

import logging

from langchain_core.messages import SystemMessage, HumanMessage

from chaos_agent.agent.node_names import VERIFIER
from chaos_agent.agent.capabilities import (
    build_capability_context,
    filter_tools_for_context,
)
from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
    inject_task_id_into_tool_calls,
    sync_kubewiz_runtime,
)
from chaos_agent.agent.nodes.store._store_sync import sync_to_store
from chaos_agent.agent.nodes.verify._verifier_layer1 import (
    run_layer1_for_state,
    _restore_layer1_from_state,
)
from chaos_agent.agent.nodes.verify._verifier_layer2_parse import (  # noqa: F401 — re-exports for tests
    _count_verification_steps_in_skill_case,  # noqa: F401
    _validate_step_number_coverage,  # noqa: F401
    _try_parse_json,  # noqa: F401
    _parse_verification_result,  # noqa: F401
    _parse_checklist_items,  # noqa: F401
    _has_checklist,  # noqa: F401
    _detect_checklist_conclusion_inconsistency,  # noqa: F401
    _has_injection_verification_section,  # noqa: F401
    _extract_verification_step_descriptions,  # noqa: F401
    _split_candidates,  # noqa: F401
    cross_check_evidence,  # noqa: F401
    dict_to_verification_result,  # noqa: F401
)
from chaos_agent.agent.nodes.verify._verifier_messages import (
    _SYNTHETIC_TOOL_CALL_IDS,
    _VERIFIER_CONTEXT_KWARGS_KEY,
    # noqa: F401  re-export for tests
    # noqa: F401  re-export for tests
    _build_layer2_messages,
    # noqa: F401
    _verification_cycle_needs_context,
)
from chaos_agent.agent.nodes.verify._verifier_shared import (
    _compute_baseline_confidence,
    # noqa: F401
    # noqa: F401
)
from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    build_stagnation_hint,
    persist_corrective_hint,
    filter_stagnant_tool,
    post_invoke_debug,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
    emit_debug_tool_messages,
    extract_persistent_hm,
    extract_synthetic_messages,
    extract_tool_call_fields,
    record_system_prompt,
)
from chaos_agent.agent.result.operation_outcome import write_inject_verification
from chaos_agent.agent.result.verdict import layer1_to_dict
from chaos_agent.agent.prompts import build_system_prompt, PromptMode
from chaos_agent.agent.prompts.reminder import wrap_system_reminder
from chaos_agent.agent.spec.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState, materialize_fault_handle
from chaos_agent.config.settings import settings
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import (
    ChecklistItemStatus,
    FailureCategory,
    InjectVerdict,
    Layer2Status,
)
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso
from chaos_agent.agent.dispatch import dispatch_node_message
from chaos_agent.agent.providers import FaultProviderRegistry

# JSON-mode schema vocabulary derives from the legislation enums
# (B76 round-14): the reminder can never teach a word set the boundary
# clamps would reject. The layer1 line stays a deliberate teaching
# subset (passed/failed/skipped are the only terminal Layer1 outcomes
# an LLM may claim — warning/error/in_progress are internal states and
# the claim is overwritten by the real Layer1 result anyway).
_JSON_OVERALL_VOCAB = "|".join(m.value for m in InjectVerdict)
_JSON_LAYER2_VOCAB = "|".join(m.value for m in Layer2Status)
_JSON_ITEM_STATUS_VOCAB = "|".join(m.value for m in ChecklistItemStatus)


def _experiment_uid_of(handle) -> str:
    """Experiment UID rendered from the handle (protocol field ``value``);
    empty for UID-less native handles. Mirrors the recover chain's helper
    (:func:`_recover_verifier_loop._experiment_uid_of`) and feeds tracker
    copy plus the ``experiment_uid`` result key — an L4/Web/DB-facing contract
    field, so this render seam is permanent, not transitional."""
    return str((handle or {}).get("value") or "")


def _resolve_fault_dispatch(state):
    """Registry fault dispatch as a module seam: returns ``(provider,
    identity_handle)`` through the same four-level identity resolution the
    recover chain uses (experiment claim → message-history claim →
    attribution handle → method)."""
    return FaultProviderRegistry.resolve_fault_dispatch(state)


def _stamp_window_start(state: AgentState, result: dict) -> None:
    """Stamp ``injection_window_start_time`` at the verifier entry.

    The verifier is the first node after the execute-loop concludes, so
    this moment IS the execute-loop end for window purposes — the
    fault-window hold (``turn_hold_fault_window``) anchors its contract
    window here, and verification time counts against that window (an
    execute-loop that kept probing after blade_create succeeded must not
    have eroded it first).

    Write-once per attempt: verifier self-loop re-entries (L2 needing
    more tool evidence) keep the FIRST stamp — the window origin is a
    fact about the execute-loop boundary, not about any given verify
    iteration. Re-arming happens exclusively at replan seams, where
    ``reset_attribution_state`` clears the field and the replanned
    attempt's verifier entry stamps a fresh origin.

    Guarded on ``injection_start_time`` (the blade_create-moment
    attribution evidence): a turn that never committed an injection has
    no window to anchor, and stamping anyway would let the hold flip a
    recover dispatch for a turn with no experiment in flight.
    """
    if state.get("injection_window_start_time"):
        return
    if not state.get("injection_start_time"):
        return
    result["injection_window_start_time"] = now_iso()


def _recovery_vehicle_of(state: dict) -> str | None:
    """Recovery vehicle (e.g. the kubectl-exec tool pod) rendered via the
    dispatched provider's ``recovery_vehicle`` hook — the verifier never
    names a carrier-specific state field. Dispatched through the same fault
    identity resolution as both verifier entries (phase-4 T5): a
    message-history experiment claim resolves to the experiment carrier,
    whose vehicle record then renders."""
    provider, _identity = _resolve_fault_dispatch(state)
    return (provider.recovery_vehicle(state) or "") or None


def _layer1_session_content(layer1) -> str:
    """Session-record content line for a Layer-1 result (round-29).

    The anchor's status/details stay the mainline; the plural face adds
    one bounded sibling digest (``uid=status`` per sibling) so the L4
    replay reader sees every polled experiment without parsing — the
    r28 string-append's session-side replacement (the append lived in
    ``details``, which this face renders untruncated but the Layer-2
    prompt starved; structure beats suffixes everywhere).
    """
    base = f"[Verifier Layer 1] status={layer1.status}, details={layer1.details}"
    siblings = [e for e in layer1.experiments if not e.is_anchor]
    if siblings:
        base += "; siblings: " + ", ".join(
            f"{e.uid}={e.status}" for e in siblings
        )
    return base


def _layer1_session_detail(layer1) -> dict:
    """Session-record detail dict for a Layer-1 result (round-29).

    Identical to the pre-round-29 shape, plus the structured plural face
    (``experiments`` — full :class:`ExperimentEvidence` dicts) whenever
    the poll produced one. Absent on the single-experiment mainline —
    old replay consumers keep their exact shape.
    """
    detail = {
        "layer": 1,
        "status": layer1.status,
        "details": layer1.details,
        "raw_output": (layer1.raw_output or "")[:500],
    }
    if layer1.experiments:
        detail["experiments"] = [
            e.model_dump(mode="json") for e in layer1.experiments
        ]
    return detail


logger = logging.getLogger(__name__)

# Loop budget: settings.max_verifier_loop (default 60, env BLADE_AI_MAX_VERIFIER_LOOP)


# Layer-1 split homes: Layer1Result + layer1_to_dict in result/verdict.py
# (phase-7 T1 unified address); the execution domain (parse helpers, the
# kubectl-exec and host-blade runners) in providers/chaosblade/verify.py
# (phase-4 T4); the state orchestration helpers (run_layer1_for_state,
# _restore_layer1_from_state) in _verifier_layer1.py.




# Messages domain moved to _verifier_messages.py

# ---------------------------------------------------------------------------
# Entry point 1: Simple verifier (no LLM, Layer 1 only)
# ---------------------------------------------------------------------------

# _cleanup_debug_pods moved to _verifier_finalize.py (Scheme B). Re-exported
# for back-compat with callers/tests importing it from this module.
from chaos_agent.agent.nodes.verify._verifier_finalize import _cleanup_debug_pods  # noqa: E402,F401


async def verifier(state: AgentState) -> dict:
    """Simple verifier without LLM: only Layer 1 (blade_status + blade_query_k8s)."""
    task_id = state.get("task_id", "")
    skill_name = read_active_skill_name(state)
    kubeconfig = _resolve_kubeconfig(state)

    # Attribution handle for logging/echo — ``materialize_fault_handle``
    # hydrates checkpoints that predate the field from the legacy facts.
    handle = materialize_fault_handle(state)

    # Route through the registry's fault dispatch (experiment claim →
    # message-history claim → attribution handle → method) — the same
    # four-level identity resolution the recover chain uses, replacing the
    # legacy direct state read plus the execute-loop message
    # fallback. The message-history claim lives INSIDE the dispatch (retired
    # UIDs stay dead there — task-29848471), so both verifier entries and
    # the recover entries agree on the same identity.
    provider, identity = _resolve_fault_dispatch(state)
    # Experiment UID for tracker/log context: dispatch identity first
    # (combo-safe — a live experiment claim outranks the native
    # attribution), the materialized attribution handle as fallback.
    experiment_uid = _experiment_uid_of(identity) or _experiment_uid_of(handle)
    if experiment_uid:
        logger.info(f"verifier: identity handle={identity or handle}")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "verifier",
        f"Verifying fault injection (uid={experiment_uid or 'N/A'})",
        {"experiment_uid": experiment_uid, "skill_name": skill_name},
    )

    # Save tracker state before Layer 1 sub-operations (defensive —
    # run_command now uses emit() so this protects against future sub-ops)
    _saved_tracker_state = tracker.save_state()
    layer1 = await run_layer1_for_state(
        state, experiment_uid, kubeconfig, task_id=task_id,
    )
    tracker.restore_state(_saved_tracker_state)
    detail_msg = f"Layer 1: {layer1.status}"
    if layer1.details:
        detail_msg += f" - {layer1.details}"
    tracker.update(detail_msg, {"layer1_status": layer1.status})

    # Record Layer 1 result to session (programmatic operations
    # are not captured by PreReasoningHook since they bypass LLM)
    _task_id_local = state.get("task_id", "")
    _session_store = state.get("_session_store")
    if _task_id_local and _session_store:
        try:
            _session_store.append_raw_message(_task_id_local, {
                "type": "system",
                "content": _layer1_session_content(layer1),
                "detail": _layer1_session_detail(layer1),
                "node": VERIFIER,
            })
        except Exception:
            pass  # Session persistence is best-effort

    # Status-reverse inference (skipped ⇒ non-experiment carrier), NOT an
    # identity check — phase-4 explicit Non-Goal: splitting the skipped
    # double meaning ("carrier not applicable" vs "infrastructure failure")
    # is a behaviour change deferred to its own task. Kept as-is on purpose.
    _is_non_chaosblade = layer1.status == "skipped"
    _is_expired = layer1.expired
    if _is_non_chaosblade:
        # Non-ChaosBlade fault: cannot verify without LLM (Layer 2)
        # Layer 1 is not applicable, and without LLM there's no Layer 2 check
        _verification_level = "unverified"
        _verified = False
    elif _is_expired:
        # Experiment expired before verification — known cause
        _verification_level = "partial"
        _verified = False
    else:
        # Layer 1 passed but Layer 2 not performed (no LLM).
        # "partial" level means we cannot confirm the fault effect is actually observable.
        _verification_level = "partial" if layer1.is_passed() else "unverified"
        _verified = False  # Cannot confirm fault effect without Layer 2
    verification = {
        "level": _verification_level,
        "layer1": layer1_to_dict(layer1),
        "layer2": {
            "status": "recovered_before_observation" if _is_expired else "skipped",
            "details": (
                "Fault expired before Layer 2 observation — "
                "recovered_before_observation (no LLM available for specific verification)"
                if _is_expired
                else "No LLM available for specific verification"
            ),
        },
        "baseline_confidence": _compute_baseline_confidence(state),
        "warnings": (
            [
                "Layer 2 (fault-specific) verification was skipped. "
                "Only Layer 1 (programmatic) verification was performed."
            ]
            if layer1.is_passed()
            else (
                [
                    "Fault experiment record expired (Destroyed/Revoked) before or during "
                    "verification, so live fault effects could not be observed by Layer 1. "
                    "Layer 2 was unavailable (no LLM) to judge cluster-level evidence."
                ]
                if _is_expired
                else (
                    [
                        "Native fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
                        "Fault injection could NOT be verified — the fault may not have been injected."
                    ]
                    if _is_non_chaosblade
                    else []
                )
            )
        ),
    }

    result = {
        "task_id": task_id,
        "skill": skill_name,
        "experiment_uid": experiment_uid,
        "verified": _verified,
    }

    if _verified:
        tracker.complete(f"Verification result: {layer1.status} (uid={experiment_uid or 'N/A'})")
        await dispatch_node_message("verifier", f"Verification result: {layer1.status} (uid={experiment_uid or 'N/A'})")
    else:
        tracker.complete(f"Verification result: {layer1.status}")
        await dispatch_node_message("verifier", f"Verification result: {layer1.status}")

    result_dict = write_inject_verification(result=result, verification=verification)
    # Window origin (fault-window hold): the execute-loop just concluded —
    # this entry is the earliest moment that fact is observable. Must ride
    # the TOP-LEVEL update (not inside ``result``) to reach the state
    # channel; write-once + attribution-guarded, see the helper.
    _stamp_window_start(state, result_dict)
    if not _verified:
        result_dict.update(fail_state(
            FailureCategory.VERIFICATION_FAILED,
            f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
            state.get("messages", []),
        ))
    # Patch C — wall-clock cause labelling for verifier path.
    from chaos_agent.agent.router import mark_wall_clock_timeout
    return mark_wall_clock_timeout(state, result_dict)


# ---------------------------------------------------------------------------
# Entry point 2: Full verifier with LLM (two-layer verification)
# ---------------------------------------------------------------------------

def make_verifier(hook=None, llm=None, tools=None, registry=None):
    """Create a verifier node with two-layer verification.

    When llm and tools are provided:
    - Layer 1: Programmatically call blade_status + blade_query_k8s (deterministic)
    - Layer 2: LLM reads skill's "注入验证" section and verifies (ReAct loop)
        - Priority 1: Use skill's "注入验证" section
        - Priority 2: Generate hints from fault type (skill_name)
        - Priority 3: Skip Layer 2 + warning
    When llm is None, falls back to Layer 1 only.
    """
    if llm is None:
        return verifier

    async def _verifier_with_llm(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        skill_name = read_active_skill_name(state)
        kubeconfig = _resolve_kubeconfig(state)
        count = state.get("verifier_loop_count", 0) + 1

        # Same identity resolution as the simple entry (phase-4 T5): the
        # dispatch's message-history claim gives THIS entry the fallback it
        # previously lacked (an experiment UID living only in message
        # history used to be invisible here, silently degrading Layer 1 to
        # the no-UID branch of the default carrier).
        handle = materialize_fault_handle(state)
        provider, identity = _resolve_fault_dispatch(state)
        experiment_uid = _experiment_uid_of(identity) or _experiment_uid_of(handle)

        # Reset time_wait consecutive-call guard (mirrors execute_loop).
        # Without this, once time_wait runs in the verifier, the global
        # _last_tool_was_wait flag is never cleared and every subsequent
        # time_wait call gets a false-positive "consecutive" rejection.
        from chaos_agent.tools.wait import check_and_reset_wait_guard
        check_and_reset_wait_guard(state.get("messages", []))

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "verifier",
            f"Verifying fault injection (uid={experiment_uid or 'N/A'}, iteration={count})",
            {"experiment_uid": experiment_uid, "skill_name": skill_name, "iteration": count},
        )

        # ---- Guard: max iterations exceeded ----
        if count > settings.max_verifier_loop:
            logger.warning(f"Verifier loop exceeded max iterations ({settings.max_verifier_loop})")
            tracker.fail(f"Verifier loop exceeded max iterations ({settings.max_verifier_loop})")
            await dispatch_node_message("verifier", f"Verifier loop exceeded max iterations ({settings.max_verifier_loop})")
            verification = {
                "level": "partial",
                "layer1": {"status": "passed", "details": "Confirmed in earlier iterations"},
                "layer2": {"status": "skipped", "details": "Max iterations reached, LLM did not produce final summary"},
                "baseline_confidence": _compute_baseline_confidence(state),
                "warnings": ["Verifier loop exceeded max iterations - verification may be incomplete"],
            }
            result = {
                "task_id": task_id,
                "skill": skill_name,
                "experiment_uid": experiment_uid,
                "verified": False,  # Cannot confirm — Layer 2 was not completed
            }
            result_dict = write_inject_verification(result=result, verification=verification)
            await _cleanup_debug_pods(state, kubeconfig, task_id, result_dict)
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Cycle detection (position-based, NOT counter-based) ----
        # "Is this the first turn of the CURRENT verification cycle?" is a
        # fact about the message sequence: the cycle's marker-tagged context
        # HumanMessage either exists in the current epoch or it does not.
        # Every replan seam re-bases ``attribution_epoch_index``, so the old
        # cycle's marker falls before the boundary and the new cycle re-arms
        # — Layer 1 re-runs and a fresh context is injected — with no
        # dependency on the seam resetting ``verifier_loop_count``.
        _new_cycle = _verification_cycle_needs_context(state)

        # ---- Layer 1: blade_status + blade_query_k8s (first turn of cycle) ----
        if _new_cycle:
            # Save tracker state before Layer 1 sub-operations (defensive)
            _saved_tracker_state = tracker.save_state()
            layer1 = await run_layer1_for_state(
                state, experiment_uid, kubeconfig, task_id=task_id,
            )
            tracker.restore_state(_saved_tracker_state)

            # Emit overall Layer 1 summary
            detail_msg = f"Layer 1: {layer1.status}"
            if layer1.details:
                detail_msg += f" - {layer1.details}"
            tracker.update(detail_msg, {"layer1_status": layer1.status, "layer1_details": layer1.details})

            # Record Layer 1 result to session (programmatic operations
            # are not captured by PreReasoningHook since they bypass LLM)
            _task_id_local = state.get("task_id", "")
            if hook and getattr(hook, "session_store", None) and _task_id_local:
                hook.session_store.append_raw_message(_task_id_local, {
                    "type": "system",
                    "content": _layer1_session_content(layer1),
                    "detail": _layer1_session_detail(layer1),
                    "node": VERIFIER,
                })
        else:
            # Reuse cached result from previous iteration
            layer1 = _restore_layer1_from_state(state)

        # If Layer 1 failed, skip Layer 2
        if layer1.is_terminal():
            verification = {
                "level": "unverified",
                "layer1": layer1_to_dict(layer1),
                "layer2": {"status": "skipped", "details": "Layer 1 failed, skipping Layer 2"},
                "baseline_confidence": _compute_baseline_confidence(state),
                "warnings": [f"Layer 1 verification failed: {layer1.details}"],
            }
            result = {
                "task_id": task_id,
                "skill": skill_name,
                "experiment_uid": experiment_uid,
                "verified": False,
            }
            tracker.complete(f"Verification failed at Layer 1: {layer1.status}")
            await dispatch_node_message("verifier", f"Verification failed at Layer 1: {layer1.status}")
            result_dict = write_inject_verification(
                fail_state(
                    FailureCategory.VERIFICATION_FAILED,
                    f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}",
                    state.get("messages", []),
                ),
                result=result,
                verification=verification,
            )
            await _cleanup_debug_pods(state, kubeconfig, task_id, result_dict)
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Layer 2: LLM with tools for fault-specific verification ----
        # Call pre_reason_hook (memory compaction + session recording)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state, seed_existing=True)

        # Resolve tool pod name for Layer 2 context.
        # The injection pod is preserved here so the existing tool-pod hints
        # in _build_layer2_messages still work for non-host-access checks.
        # Host-level filesystem checks now go through
        # kubectl_read(subcommand="debug"); the verifier finalization
        # scans message history and removes any debug pods automatically.
        tool_pod_name = _recovery_vehicle_of(state)

        messages = _build_layer2_messages(
            state, layer1, experiment_uid, skill_name, kubeconfig, count,
            tool_pod_name=tool_pod_name,
            new_cycle=_new_cycle,
        )

        # Extract synthetic AIMessage+ToolMessage pairs from the local messages
        # list for state persistence. On the cycle's first turn, prepend them
        # to result_update["messages"] BEFORE the response so that
        # state["messages"][-1] remains the real AIMessage (routing-safe).
        # On later turns this is NOT empty. The pairs are already in
        # AgentState.messages, ``_build_layer2_messages`` starts from a copy of
        # it, and ``extract_synthetic_messages`` does no "already in state"
        # filtering (unlike ``extract_persistent_hm`` right below, which does),
        # so they are found again every turn. Re-persisting is idempotent
        # rather than duplicating: the pairs carry STABLE message ids, so
        # ``add_messages`` replaces them in place — measured over two turns,
        # state grows by 0 and holds exactly one copy of each half. See the
        # ``_BASELINE_MSG_ID_*`` constants in _verifier_messages.py for why the
        # ids must be stable.
        _synthetic_for_state = extract_synthetic_messages(messages, _SYNTHETIC_TOOL_CALL_IDS)

        # Extract the main verifier context HumanMessage for state persistence.
        _main_hm_for_state = extract_persistent_hm(messages, state, _VERIFIER_CONTEXT_KWARGS_KEY)

        # Corrective hints are PERSISTED, not just injected for this turn. A
        # verify loop re-derives them every iteration, so a turn-local copy reads
        # as a first-time warning forever: task-e9ee12d6 fired the stagnation
        # hint from turn 11 and the model issued the same ``kubectl_read top``
        # call 31 more times. The persisted copy carries the running count, so
        # later turns can see the mistake has a history.
        _hints_for_state: list = []
        # Counts live on state, not in the hint messages: compaction removes
        # the summarised half of history and a hint sits at its FIRST
        # occurrence, so message-derived counts reset mid-drill.
        _hint_counts = dict(state.get("hint_repeat_counts") or {})

        # Repeated tool call detection (reuse from agent_loop)
        loop_hint = detect_repeated_tool_calls(state.get("messages", []), phase="verify")
        if loop_hint:
            messages.append(persist_corrective_hint(
                _hints_for_state, state.get("messages", []),
                "loop", "verify", loop_hint,
                escalate_after=settings.hint_escalate_after,
                counts=_hint_counts, counts_out=_hint_counts,
            ))

        # Action stagnation detection (tool-name level)
        _, stagnant_tool = detect_action_stagnation(state.get("messages", []), phase="verify")
        if stagnant_tool:
            verifier_hint = build_stagnation_hint(
                stagnant_tool,
                colon_suffix="(describe, logs, etc.) to gather verification evidence",
                else_actions=[
                    "Use a DIFFERENT tool or subcommand to gather verification evidence.",
                    "Output your verification conclusion based on evidence already collected.",
                ],
            )
            messages.append(persist_corrective_hint(
                _hints_for_state, state.get("messages", []),
                "stagnation", stagnant_tool, verifier_hint,
                escalate_after=settings.hint_escalate_after,
                counts=_hint_counts, counts_out=_hint_counts,
            ))

        # Tool error introspection (runtime feedback > static docs)
        error_hint = detect_tool_error_hint(messages)
        if error_hint:
            messages.append(persist_corrective_hint(
                _hints_for_state, state.get("messages", []),
                "tool_error", "verify", error_hint,
                counts=_hint_counts, counts_out=_hint_counts,
            ))

        # --- Progress ledger (drift anchor) — TAIL append, not the head ---
        # context-cache-prefix-stability Unit A (task 2.4, design D1/D2): the
        # verify ledger moved OUT of build_verifier_prompt's head (its per-round
        # rewrite broke the cache prefix) onto the message tail via the same
        # append-only channel as the corrective hints above. NO stable id: a
        # stable id would make add_messages replace the copy IN PLACE, pinning it
        # early (out of the recency tail) AND reintroducing an early volatile
        # byte that re-bills the whole suffix every round (see execute_loop's
        # note + the measured 50%→41% vs 63%→82% prefix-share comparison).
        # Placed before the final-iteration JSON reminder so that nudge stays
        # outermost.
        from chaos_agent.agent.progress_ledger import build_ledger_tail_content
        _ledger_tail = build_ledger_tail_content(state.get("progress_ledger"))
        if _ledger_tail:
            _ledger_msg = HumanMessage(content=wrap_system_reminder(_ledger_tail))
            messages.append(_ledger_msg)
            _hints_for_state.append(_ledger_msg)

        # On last iteration, force LLM to produce a summary (unbind tools)
        # Use JSON mode (response_format) when enabled for guaranteed structured output
        if count >= settings.max_verifier_loop and settings.verifier_json_mode:
            json_llm = llm.bind(response_format={"type": "json_object"})
            json_reminder = HumanMessage(content=wrap_system_reminder(
                "You MUST output valid JSON matching this schema:\n"
                "{\n"
                '  "verification_checklist": [\n'
                f'    {{"step": 1, "status": "{_JSON_ITEM_STATUS_VOCAB}", "evidence": "brief"}},\n'
                '    ...\n'
                '  ],\n'
                '  "layer1": "passed|failed|skipped",\n'
                f'  "layer2": "{_JSON_LAYER2_VOCAB}",\n'
                '  "layer2_details": "evidence summary",\n'
                f'  "overall": "{_JSON_OVERALL_VOCAB}",\n'
                '  "warnings": ["warning text"]\n'
                "}\n"
                'layer2: "passed" = fault effect IS observable; "failed" = NOT observable.'
            ))
            _messages = list(messages) + [json_reminder]
            llm_to_call = json_llm
            messages = _messages
        elif count >= settings.max_verifier_loop:
            llm_to_call = llm
        else:
            capability_context = build_capability_context(state, "verify", tools)
            visible_tools = filter_tools_for_context(tools, capability_context)
            # Distinguish the THREE ways this can end up empty.
            #  - no static tools at all (``verifier_tools=[]`` / ``None`` at
            #    build time): nothing was gated away, and an unbound llm is the
            #    correct prose-conclusion path — it is also the only one, since
            #    a provider rejects a request carrying an empty ``tools`` array.
            #  - the stagnant filter dropping the last tool: benign, same.
            #  - the GATE refusing everything a non-empty set offered: that is
            #    the unsupported/mismatched environment, and falling back to an
            #    unbound llm there is fail-OPEN, because the model still emits
            #    calls from what the prompt showed it. Bind an empty tool set so
            #    no call can be produced at all.
            #
            # In practice the last branch is defence-in-depth: every phase's
            # static base carries non-provider tools (``submit_verification`` /
            # ``time_wait`` / ``read_*``) that the gate keeps, and an
            # unsupported profile is already refused upstream (``agent_loop``'s
            # ``capability_context.supported``). The real
            # enforcement is the ToolNode capability screen.
            if tools and not visible_tools:
                logger.warning(
                    "verifier: capability gate left no visible tools "
                    "(profile=%s) — binding an empty tool set rather than "
                    "falling back to an unbound LLM",
                    capability_context.profile,
                )
                llm_to_call = llm.bind_tools([])
            else:
                tools_this_iter = filter_stagnant_tool(visible_tools, stagnant_tool)
                llm_to_call = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

        # Record system prompt to session store (dedup handles repeated prompts)
        capability_context = build_capability_context(state, "verify", tools)
        # The progress ledger NO LONGER rides this head (Unit A task 2.4): it
        # rides the message tail (appended above) so the [system][tools] prefix
        # stays byte-stable across verify rounds.
        verifier_prompt = build_system_prompt(
            PromptMode.VERIFICATION,
            profile=capability_context.profile,
        )
        record_system_prompt(hook, state, verifier_prompt, node_name=VERIFIER)

        response = await llm_to_call.ainvoke(
            [SystemMessage(content=verifier_prompt)] + messages
        )

        # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
        # has the correct kubeconfig, even if the LLM forgot to include it.
        inject_kubeconfig_into_tool_calls(response, kubeconfig)
        inject_task_id_into_tool_calls(response, task_id)
        sync_kubewiz_runtime(state)

        # Read-only phase discipline is enforced by the verifier_screener
        # graph-edge node (graph.py) between this node and verifier_tools,
        # mirroring phase1_screener / tool_screener.

        # Build result
        result_update = {
            "verifier_loop_count": count,
            "inject_layer1_cache": layer1_to_dict(layer1),  # persist for subsequent iterations
        }
        # Window origin (fault-window hold): the execute-loop just concluded
        # — this FIRST verifier step is the earliest observable moment of
        # that fact. Write-once per attempt: verifier self-loop re-entries
        # (this node returns per ReAct step) keep the first stamp; replan
        # seams clear the field and the replanned attempt re-stamps here.
        _stamp_window_start(state, result_update)

        tool_calls = getattr(response, "tool_calls", None) or []
        # Scheme B: verifier_loop is a pure ReAct step. Persist the response
        # (+ synthetic context messages); routing decides what is next —
        # should_continue_verifier sends tool_calls -> verifier_tools, or
        # text -> finalize_verification. All finalization (parse verdict +
        # post-process + debug-pod cleanup) now lives in finalize_verification.
        result_update["messages"] = (
            _main_hm_for_state + _synthetic_for_state + _hints_for_state + [response]
        )
        if _hint_counts != (state.get("hint_repeat_counts") or {}):
            result_update["hint_repeat_counts"] = _hint_counts

        if settings.is_debug:
            post_invoke_debug(tracker, response, count, "Layer 2 iteration")
        else:
            _tc_names = [extract_tool_call_fields(tc)[0] for tc in tool_calls]
            tracker.update(
                f"Layer 2 iteration {count}: "
                + ("calling tools" if tool_calls else "emitting verdict text"),
                {"iteration": count, "tool_calls": _tc_names},
            )

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result_update, hook_updates)
        await sync_to_store(state, result_update)
        from chaos_agent.agent.router import (
            mark_loop_exhausted,
            mark_wall_clock_timeout,
        )
        result_update = mark_wall_clock_timeout(state, result_update)
        # Backstop only: on ``count >= max`` this node already forces a verdict
        # (JSON mode / unbound tools) and the router sends it to
        # finalize_verification, so a cause is normally unnecessary. It matters
        # when that forced verdict is itself empty — then the run would end with
        # neither a verdict nor a reason.
        return mark_loop_exhausted(
            result_update, count, settings.max_verifier_loop,
            category=FailureCategory.VERIFICATION_FAILED, label="verifier loop",
        )

    return _verifier_with_llm
