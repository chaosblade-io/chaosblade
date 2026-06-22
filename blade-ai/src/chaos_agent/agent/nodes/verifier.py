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
from chaos_agent.agent.nodes._debug_pod import parse_debug_pod_name, delete_debug_pod
from chaos_agent.agent.nodes._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
    sync_kubewiz_runtime,
)
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.nodes._verifier_layer1 import (
    # noqa: F401
    _run_layer1_verification,
    _restore_layer1_from_state,
    _layer1_to_dict,
    # noqa: F401
    # noqa: F401
)
from chaos_agent.agent.nodes._verifier_layer2_parse import (  # noqa: F401 — re-exports for tests
    _count_verification_steps_in_skill_case,  # noqa: F401
    _validate_step_number_coverage,  # noqa: F401
    _try_parse_json,  # noqa: F401
    _has_format_reminder,  # noqa: F401
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
from chaos_agent.agent.nodes._verifier_messages import (
    _SYNTHETIC_TOOL_CALL_IDS,
    _VERIFIER_CONTEXT_KWARGS_KEY,
    # noqa: F401  re-export for tests
    # noqa: F401  re-export for tests
    _build_layer2_messages,
    # noqa: F401
)
from chaos_agent.agent.nodes._verifier_shared import (
    _compute_baseline_confidence,
    # noqa: F401
    # noqa: F401
)

# Backward-compat aliases (some tests still import these from verifier→baseline_capture)
_parse_debug_pod_name = parse_debug_pod_name
_delete_debug_pod = delete_debug_pod
from chaos_agent.agent.nodes.llm_step_helpers import (
    build_stagnation_hint,
    filter_stagnant_tool,
    post_invoke_debug,
)
from chaos_agent.agent.nodes.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
    emit_debug_tool_messages,
    extract_persistent_hm,
    extract_synthetic_messages,
    extract_tool_call_fields,
    record_system_prompt,
)
from chaos_agent.agent.operation_outcome import write_inject_verification
from chaos_agent.agent.prompts import build_system_prompt, PromptMode
from chaos_agent.agent.skill_identity import read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)

# settings.max_verifier_loop is now configurable via settings.max_verifier_loop (default 10)


# moved to _verifier_layer1.py: Layer1Result, _EXPIRED_STATES, _RUNNING_STATES,
# _parse_blade_status_output, _QueryK8sResult, _parse_blade_query_k8s_output,
# _find_blade_query_in_messages, _map_query_k8s_to_layer1,
# _run_layer1_via_kubectl_exec, _run_layer1_verification,
# _restore_layer1_from_state, _layer1_to_dict




# Messages domain moved to _verifier_messages.py

# ---------------------------------------------------------------------------
# Entry point 1: Simple verifier (no LLM, Layer 1 only)
# ---------------------------------------------------------------------------

# _cleanup_debug_pods moved to _verifier_finalize.py (Scheme B). Re-exported
# for back-compat with callers/tests importing it from this module.
from chaos_agent.agent.nodes._verifier_finalize import _cleanup_debug_pods  # noqa: E402,F401


async def verifier(state: AgentState) -> dict:
    """Simple verifier without LLM: only Layer 1 (blade_status + blade_query_k8s)."""
    task_id = state.get("task_id", "")
    blade_uid = state.get("blade_uid", "")
    skill_name = read_active_skill_name(state)
    kubeconfig = _resolve_kubeconfig(state)

    # Defense-in-depth: if blade_uid is empty in state, try to recover it
    # from message history (e.g. when injection was done via kubectl exec).
    if not blade_uid:
        from chaos_agent.agent.nodes.execute_loop import _extract_blade_uid_from_messages
        messages = state.get("messages", [])
        blade_uid = _extract_blade_uid_from_messages(messages) or ""
        if blade_uid:
            logger.info(f"verifier: recovered blade_uid={blade_uid} from message history")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "verifier",
        f"Verifying fault injection (uid={blade_uid or 'N/A'})",
        {"blade_uid": blade_uid, "skill_name": skill_name},
    )

    # Save tracker state before Layer 1 sub-operations (defensive —
    # run_command now uses emit() so this protects against future sub-ops)
    _saved_tracker_state = tracker.save_state()
    layer1 = await _run_layer1_verification(
        blade_uid, kubeconfig, task_id=task_id, messages=state.get("messages", []),
        injection_method=state.get("injection_method"),
        injection_pod_name=state.get("kubectl_exec_pod_name"),
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
                "content": f"[Verifier Layer 1] status={layer1.status}, details={layer1.details}",
                "detail": {
                    "layer": 1,
                    "status": layer1.status,
                    "details": layer1.details,
                    "raw_output": (layer1.raw_output or "")[:500],
                },
                "node": VERIFIER,
            })
        except Exception:
            pass  # Session persistence is best-effort

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
        "layer1": _layer1_to_dict(layer1),
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
                "Only general blade_status verification was performed."
            ]
            if layer1.is_passed()
            else (
                [
                    "Fault experiment expired (Destroyed/Revoked) before verification. "
                    "The fault duration (--timeout) was too short for post-injection verification. "
                    "Recommend --duration >= 60."
                ]
                if _is_expired
                else (
                    [
                        "Non-ChaosBlade fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
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
        "blade_uid": blade_uid,
        "verified": _verified,
    }

    if _verified:
        tracker.complete(f"Verification result: {layer1.status} (uid={blade_uid or 'N/A'})")
    else:
        tracker.complete(f"Verification result: {layer1.status}")

    result_dict = write_inject_verification(result=result, verification=verification)
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
        blade_uid = state.get("blade_uid", "")
        skill_name = read_active_skill_name(state)
        kubeconfig = _resolve_kubeconfig(state)
        count = state.get("verifier_loop_count", 0) + 1

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "verifier",
            f"Verifying fault injection (uid={blade_uid or 'N/A'}, iteration={count})",
            {"blade_uid": blade_uid, "skill_name": skill_name, "iteration": count},
        )

        # ---- Guard: max iterations exceeded ----
        if count > settings.max_verifier_loop:
            logger.warning(f"Verifier loop exceeded max iterations ({settings.max_verifier_loop})")
            tracker.fail(f"Verifier loop exceeded max iterations ({settings.max_verifier_loop})")
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
                "blade_uid": blade_uid,
                "verified": False,  # Cannot confirm — Layer 2 was not completed
            }
            result_dict = write_inject_verification(result=result, verification=verification)
            await _cleanup_debug_pods(state, kubeconfig, task_id, result_dict)
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Layer 1: blade_status + blade_query_k8s (only on first iteration) ----
        if count == 1:
            # Save tracker state before Layer 1 sub-operations (defensive)
            _saved_tracker_state = tracker.save_state()
            layer1 = await _run_layer1_verification(
                blade_uid, kubeconfig, task_id=task_id, messages=state.get("messages", []),
                injection_method=state.get("injection_method"),
                injection_pod_name=state.get("kubectl_exec_pod_name"),
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
                    "content": f"[Verifier Layer 1] status={layer1.status}, details={layer1.details}",
                    "detail": {
                        "layer": 1,
                        "status": layer1.status,
                        "details": layer1.details,
                        "raw_output": (layer1.raw_output or "")[:500],
                    },
                    "node": VERIFIER,
                })
        else:
            # Reuse cached result from previous iteration
            layer1 = _restore_layer1_from_state(state)

        # If Layer 1 failed, skip Layer 2
        if layer1.is_terminal():
            verification = {
                "level": "unverified",
                "layer1": _layer1_to_dict(layer1),
                "layer2": {"status": "skipped", "details": "Layer 1 failed, skipping Layer 2"},
                "baseline_confidence": _compute_baseline_confidence(state),
                "warnings": [f"Layer 1 verification failed: {layer1.details}"],
            }
            result = {
                "task_id": task_id,
                "skill": skill_name,
                "blade_uid": blade_uid,
                "verified": False,
            }
            tracker.complete(f"Verification failed at Layer 1: {layer1.status}")
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
        # kubectl_verify(subcommand="debug"); the verifier finalization
        # scans message history and removes any debug pods automatically.
        tool_pod_name = state.get("kubectl_exec_pod_name")

        messages = _build_layer2_messages(
            state, layer1, blade_uid, skill_name, kubeconfig, count,
            tool_pod_name=tool_pod_name,
        )

        # Extract synthetic AIMessage+ToolMessage pairs from the local messages
        # list for state persistence. On count==1, prepend them to
        # result_update["messages"] BEFORE the response so that
        # state["messages"][-1] remains the real AIMessage (routing-safe).
        # On count>1, they are already in AgentState.messages (persisted
        # from count==1) and _build_layer2_messages detected them via the
        # _already_in_state check, so _synthetic_for_state is empty.
        _synthetic_for_state = extract_synthetic_messages(messages, _SYNTHETIC_TOOL_CALL_IDS)

        # Extract the main verifier context HumanMessage for state persistence.
        _main_hm_for_state = extract_persistent_hm(messages, state, _VERIFIER_CONTEXT_KWARGS_KEY)

        # Repeated tool call detection (reuse from agent_loop)
        loop_hint = detect_repeated_tool_calls(state.get("messages", []), phase="verify")
        if loop_hint:
            messages.append(HumanMessage(content=loop_hint))

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
            messages.append(HumanMessage(content=verifier_hint))

        # Tool error introspection (runtime feedback > static docs)
        error_hint = detect_tool_error_hint(messages)
        if error_hint:
            messages.append(HumanMessage(content=error_hint))

        # On last iteration, force LLM to produce a summary (unbind tools)
        # Use JSON mode (response_format) when enabled for guaranteed structured output
        if count >= settings.max_verifier_loop and settings.verifier_json_mode:
            json_llm = llm.bind(response_format={"type": "json_object"})
            json_reminder = HumanMessage(content=(
                "You MUST output valid JSON matching this schema:\n"
                "{\n"
                '  "verification_checklist": [\n'
                '    {"step": 1, "status": "passed|failed|skipped|recovered_before_observation", "evidence": "brief"},\n'
                '    ...\n'
                '  ],\n'
                '  "layer1": "passed|failed|skipped",\n'
                '  "layer2": "passed|failed|skipped|partial|recovered_before_observation",\n'
                '  "layer2_details": "evidence summary",\n'
                '  "overall": "verified|partial|unverified",\n'
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
            tools_this_iter = filter_stagnant_tool(tools, stagnant_tool)
            llm_to_call = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

        # Record system prompt to session store (dedup handles repeated prompts)
        verifier_prompt = build_system_prompt(PromptMode.VERIFICATION)
        record_system_prompt(hook, state, verifier_prompt, node_name=VERIFIER)

        response = await llm_to_call.ainvoke(
            [SystemMessage(content=verifier_prompt)] + messages
        )

        # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
        # has the correct kubeconfig, even if the LLM forgot to include it.
        inject_kubeconfig_into_tool_calls(response, kubeconfig)
        sync_kubewiz_runtime(state)

        # Build result
        result_update = {
            "verifier_loop_count": count,
            "inject_layer1_cache": _layer1_to_dict(layer1),  # persist for subsequent iterations
        }

        tool_calls = getattr(response, "tool_calls", None) or []
        # Scheme B: verifier_loop is a pure ReAct step. Persist the response
        # (+ synthetic context messages); routing decides what is next —
        # should_continue_verifier sends tool_calls -> verifier_tools, or
        # text -> finalize_verification. All finalization (parse verdict +
        # post-process + debug-pod cleanup) now lives in finalize_verification.
        result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response]

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
        from chaos_agent.agent.router import mark_wall_clock_timeout
        return mark_wall_clock_timeout(state, result_update)

    return _verifier_with_llm
