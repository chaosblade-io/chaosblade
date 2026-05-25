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

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
)
from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.nodes._verifier_hints import (
    # Re-exported for backward compatibility (test_verifier.py imports
    # these symbols from verifier.py — keep them resolvable here even
    # though their definitions live in the split helper modules).
    _resolve_target_node,
    _discover_tool_pod_for_verification,
    _disk_fill_param_hints,  # noqa: F401
    _derive_disk_fill_partition,  # noqa: F401
    _PARAM_HINT_GENERATORS,  # noqa: F401
    _BASELINE_INTEGRITY_PROMPT,  # noqa: F401
    _COMMAND_PRIORITY_HINT,  # noqa: F401
)
from chaos_agent.agent.nodes._verifier_layer1 import (
    Layer1Result,  # noqa: F401
    _run_layer1_verification,
    _restore_layer1_from_state,
    _layer1_to_dict,
    _find_blade_query_in_messages,  # noqa: F401
    _run_layer1_via_kubectl_exec,  # noqa: F401
)
from chaos_agent.agent.nodes._verifier_layer2_parse import (
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
)
from chaos_agent.agent.nodes._verifier_messages import (
    _SYNTHETIC_TOOL_CALL_IDS,
    _VERIFIER_CONTEXT_KWARGS_KEY,
    _METRICS_TOOL_CALL_ID,  # noqa: F401  re-export for tests
    _BASELINE_TOOL_CALL_ID,  # noqa: F401  re-export for tests
    _check_container_restart_fast_path,
    _fresh_restart_count,
    _build_layer2_messages,
    _build_baseline_tool_messages,  # noqa: F401
)
from chaos_agent.agent.nodes._verifier_shared import (
    _compute_baseline_confidence,
    _IMAGEFS_PATHS,  # noqa: F401
    _NODEFS_PATHS,  # noqa: F401
)
from chaos_agent.agent.nodes.baseline_capture import _parse_debug_pod_name, _delete_debug_pod
from chaos_agent.agent.nodes.react_helpers import (
    detect_repeated_tool_calls,
    emit_debug_tool_messages,
    extract_persistent_hm,
    extract_synthetic_messages,
    extract_tool_call_fields,
    record_system_prompt,
    summarize_llm_response,
)
from chaos_agent.agent.prompts import build_system_prompt, PromptMode
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.errors import FailureReason, enrich_failure_reason
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

async def verifier(state: AgentState) -> dict:
    """Simple verifier without LLM: only Layer 1 (blade_status + blade_query_k8s)."""
    task_id = state.get("task_id", "")
    blade_uid = state.get("blade_uid", "")
    skill_name = state.get("skill_name", "")
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

    result_dict = {"result": result, "verification": verification}
    if not _verified:
        base = (
            f"{FailureReason.VERIFICATION_FAILED.value}: "
            f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
        )
        result_dict["failure_reason"] = enrich_failure_reason(
            base, state.get("messages", [])
        )
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
        skill_name = state.get("skill_name", "")
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
            result_dict = {"result": result, "verification": verification}
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
            base = (
                f"{FailureReason.VERIFICATION_FAILED.value}: "
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
            )
            result_dict = {
                "result": result,
                "verification": verification,
                "failure_reason": enrich_failure_reason(
                    base, state.get("messages", [])
                ),
            }
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Container Restart Fast Path ----
        # Deterministic pre-check before entering LLM Layer 2 loop.
        # If the target container restarted during injection AND L1 passed,
        # the fault executed but evidence was destroyed by the restart.
        # Return recovered_before_observation with side_effects so
        # infer_phase maps to verification_passed (valid drill finding).
        #
        # IMPORTANT: The fast path result is stored in a local variable
        # (_restart_precheck) and passed directly to _build_layer2_messages
        # because LangGraph state is immutable within a single node invocation
        # — we cannot write to state and read it back in the same iteration.
        _restart_precheck: dict | None = None
        if count == 1:
            restart_result = await _check_container_restart_fast_path(
                state, layer1, kubeconfig, task_id=task_id,
            )
            # Store fast path result (including negative conclusion) as
            # AUTHORITATIVE FACT for Layer 2 prompt injection.
            # This prevents the LLM from re-deriving restart status from
            # raw kubectl output and making timestamp comparison errors.
            if restart_result is not None:
                _restart_precheck = {
                    "restart_detected": restart_result.restart_detected,
                    "restart_count": restart_result.restart_count,
                    "baseline_restart_count": restart_result.baseline_restart_count,
                    "restart_delta": restart_result.restart_delta,
                    "reason": restart_result.reason,
                    "finished_at": restart_result.finished_at,
                }
            else:
                # fast path returned None = kubectl/JSON failure, data unavailable
                _restart_precheck = {
                    "restart_detected": False,
                    "restart_count": -1,
                    "baseline_restart_count": -1,
                    "restart_delta": -1,
                    "reason": "data_unavailable",
                    "finished_at": "",
                    "conclusion": "no_restart_data_unavailable",
                }
            if restart_result is not None and restart_result.restart_detected:
                verification = {
                    "level": "partial",
                    "layer1": _layer1_to_dict(layer1),
                    "layer2": {
                        "status": "recovered_before_observation",
                        "details": (
                            f"Container restarted during injection "
                            f"(reason: {restart_result.reason}, "
                            f"restartCount: {restart_result.restart_count}). "
                            f"Fault executed (L1 confirmed) but evidence "
                            f"destroyed by restart. The restart is CONSISTENT WITH "
                            f"the fault having an effect, but does not constitute "
                            f"direct confirmation — side effects are weak evidence."
                        ),
                    },
                    "baseline_confidence": _compute_baseline_confidence(state),
                    "side_effects": {
                        "container_restarts": [
                            {
                                "pod": restart_result.pod_name,
                                "restart_count": restart_result.restart_count,
                                "reason": restart_result.reason,
                                "finished_at": restart_result.finished_at,
                            }
                        ],
                    },
                    "warnings": [
                        f"Container restart detected during injection "
                        f"({restart_result.reason}). Fault evidence destroyed, "
                        f"but the restart alone cannot confirm causation. "
                        f"This is a side effect (weak evidence). "
                        f"Direct primary evidence (burn files, I/O metrics) "
                        f"was not observed and could not be verified.",
                    ],
                }
                result = {
                    "task_id": task_id,
                    "skill": skill_name,
                    "blade_uid": blade_uid,
                    "verified": False,  # level="partial" → not fully verified
                }
                _restart_msg = (
                    f"Verification: partial (container restart fast path) "
                    f"(Layer1: {layer1.status}, "
                    f"Layer2: recovered_before_observation, "
                    f"reason: {restart_result.reason})"
                )
                tracker.complete(_restart_msg)
                result_dict = {
                    "result": result,
                    "verification": verification,
                    "verifier_loop_count": count,
                    "inject_layer1_cache": _layer1_to_dict(layer1),
                    "restart_precheck": _restart_precheck,
                    "inject_verification_summary": (
                        f"Layer2=recovered_before_observation, "
                        f"Details=Container restart ({restart_result.reason}), "
                        f"evidence destroyed"
                    ),
                }
                # Record fast path to session
                _task_id_local = state.get("task_id", "")
                if hook and getattr(hook, "session_store", None) and _task_id_local:
                    hook.session_store.append_raw_message(_task_id_local, {
                        "type": "system",
                        "content": (
                            f"[Verifier Fast Path] Container restart detected "
                            f"on {restart_result.pod_name} "
                            f"(reason={restart_result.reason}). "
                            f"Skipping Layer 2 LLM iterations. "
                            f"Classification: recovered_before_observation."
                        ),
                        "detail": {
                            "layer": "fast_path",
                            "status": "recovered_before_observation",
                            "pod_name": restart_result.pod_name,
                            "restart_count": restart_result.restart_count,
                            "reason": restart_result.reason,
                        },
                    })
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
        # For node-level faults, the injection pod may be on a DIFFERENT node
        # (kubectl exec fallback picks any running pod). The PRIMARY verification
        # target for node-level faults is the tool pod ON THE TARGET NODE, because:
        # - CRD-mode disk fill writes to THAT pod's overlay filesystem (imagefs)
        # - Host filesystem checks (kubectl debug) are for nodefs, not imagefs
        # Always discover the target-node tool pod for node-scope faults.
        tool_pod_name = state.get("kubectl_exec_pod_name")
        from chaos_agent.agent.fault_spec import read_fault_spec as _rfs
        _spec = _rfs(state)
        _blade_scope = _spec.scope if _spec else ""

        if _blade_scope == "node":
            target_node = _resolve_target_node(state)
            if target_node:
                discovered = await _discover_tool_pod_for_verification(
                    kubeconfig, task_id=task_id, target_node=target_node
                )
                if discovered:
                    tool_pod_name = discovered
                    logger.info(
                        f"Node-level fault: using target-node tool pod {discovered} "
                        f"(on {target_node}) for Layer 2 verification"
                    )
                elif tool_pod_name:
                    logger.warning(
                        f"No tool pod found on target node {target_node}. "
                        f"Injection pod {tool_pod_name} is on a DIFFERENT node — "
                        f"clearing tool_pod_name to avoid misleading PRIMARY VERIFICATION hints."
                    )
                    tool_pod_name = None

        messages = _build_layer2_messages(
            state, layer1, blade_uid, skill_name, kubeconfig, count,
            tool_pod_name=tool_pod_name,
            restart_precheck_result=_restart_precheck,
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
        loop_hint = detect_repeated_tool_calls(state.get("messages", []))
        if loop_hint:
            messages.append(HumanMessage(content=loop_hint))

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
            llm_to_call = llm.bind_tools(tools) if tools else llm

        # Record system prompt to session store (dedup handles repeated prompts)
        verifier_prompt = build_system_prompt(PromptMode.VERIFICATION)
        record_system_prompt(hook, state, verifier_prompt)

        response = await llm_to_call.ainvoke(
            [SystemMessage(content=verifier_prompt)] + messages
        )

        # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
        # has the correct kubeconfig, even if the LLM forgot to include it.
        inject_kubeconfig_into_tool_calls(response, kubeconfig)

        # Build result
        result_update = {
            "verifier_loop_count": count,
            "inject_layer1_cache": _layer1_to_dict(layer1),  # persist for subsequent iterations
        }
        # Persist restart_precheck to state so subsequent iterations can
        # recover it (the first iteration computes it, later ones need it
        # when rebuilding the L2 prompt).
        if _restart_precheck is not None:
            result_update["restart_precheck"] = _restart_precheck

        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            # LLM wants to call tools — continue ReAct loop
            # Synthetic messages prepend BEFORE response so routing checks
            # state[-1] = response (real AIMessage), not synthetic AIMessage.
            result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response]

            # Emit debug-level status event with LLM reasoning summary
            if settings.is_debug:
                debug_info, tool_names = summarize_llm_response(response)
                tracker.update(
                    f"Layer 2 iteration {count} LLM:\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": tool_names},
                )
            else:
                tool_names = [
                    extract_tool_call_fields(tc)[0]
                    for tc in tool_calls
                ]
                tracker.update(
                    f"Layer 2 iteration {count}: calling tools",
                    {"iteration": count, "tool_calls": tool_names},
                )
        else:
            # LLM produced final text — parse verification result
            content = getattr(response, "content", "") or ""
            # Emit RUNNING event for the final reasoning before completing
            if settings.is_debug:
                debug_info, _ = summarize_llm_response(response)
                tracker.update(
                    f"Layer 2 iteration {count} LLM (final):\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": []},
                )
            else:
                tracker.update(
                    f"Layer 2 iteration {count}: producing final verification",
                    {"iteration": count, "tool_calls": []},
                )

            # Try JSON parsing first (JSON mode final iteration path)
            verification = _try_parse_json(content)
            if verification is None:
                # JSON failed or not JSON mode → fall back to text parsing
                verification = _parse_verification_result(content)

            verification["layer1"] = _layer1_to_dict(layer1)

            # ---- Programmatic Fact Enforcement ----
            # The LLM may ignore AUTHORITATIVE facts injected into the prompt and
            # re-derive incorrect conclusions from raw kubectl output (e.g.,
            # attributing a pre-existing OOMKill to the injection window despite
            # restart_precheck confirming no restart). This block programmatically
            # overrides LLM conclusions that contradict known programmatic facts.
            # Unlike prompt-based instructions, this enforcement is deterministic
            # and cannot be overridden by the LLM.
            _precheck_enforce = _restart_precheck or state.get("restart_precheck")
            _burn_enforce = state.get("disk_burn_post_check")
            _enforcement_applied = False

            # Enforcement 1: restart_precheck says NO restart, but LLM blamed restart
            if _precheck_enforce and not _precheck_enforce.get("restart_detected"):
                _l2_status_val = verification.get("layer2", {}).get("status", "unknown")
                _l2_details_lower = verification.get("layer2", {}).get("details", "").lower()
                _restart_kws = (
                    "oomkill", "oom kill", "restart", "restarted",
                    "crashloop", "container restart",
                )
                _llm_blamed_restart = any(kw in _l2_details_lower for kw in _restart_kws)
                # Also check checklist items for restart attribution
                if not _llm_blamed_restart:
                    for _ci in verification.get("checklist", {}).get("items", []):
                        if _ci.get("status") in ("recovered_before_observation", "failed", "partial"):
                            _ev_lower = _ci.get("evidence", "").lower()
                            if any(kw in _ev_lower for kw in _restart_kws):
                                _llm_blamed_restart = True
                                break
                if _l2_status_val in ("recovered_before_observation", "failed", "partial") and _llm_blamed_restart:
                    # Freshness check: re-verify restart count hasn't changed
                    # since precheck.  If the container restarted during the
                    # LLM's ReAct loop, the precheck data is stale and we must
                    # NOT override the LLM's (now correct) conclusion.
                    _precheck_rc = _precheck_enforce.get("restart_count")
                    _fresh_rc = await _fresh_restart_count(
                        state, kubeconfig, task_id=task_id,
                    )
                    if _fresh_rc is not None and _precheck_rc is not None and _fresh_rc != _precheck_rc:
                        logger.info(
                            "Programmatic enforcement ABORTED: restart count changed "
                            "since precheck (precheck=%s, current=%s). Container "
                            "likely restarted during LLM ReAct loop. LLM conclusion stands.",
                            _precheck_rc, _fresh_rc,
                        )
                        # The restart happened AFTER precheck but DURING the
                        # injection window — the fault likely CAUSED it.
                        # Set side_effects so infer_task_state maps to
                        # "injected" (success) rather than "failed".
                        from chaos_agent.agent.fault_spec import read_fault_spec as _rfs2
                        _spec2 = _rfs2(state)
                        _target_names = list(_spec2.names) if _spec2 else []
                        verification.setdefault("side_effects", {})["container_restarts"] = [
                            {
                                "pod": _target_names[0] if _target_names else "unknown",
                                "restart_count": _fresh_rc,
                                "reason": "detected_by_freshness_check",
                                "note": "restart occurred after precheck, likely caused by fault injection",
                            }
                        ]
                        _enforcement_applied = True  # trigger level recalc
                    else:
                        _delta = _precheck_enforce.get("restart_delta", 0)
                        logger.info(
                            "Programmatic enforcement: LLM concluded %s due to restart/OOMKill, "
                            "but restart_precheck confirmed NO restart (delta=%s, fresh_rc=%s). Overriding.",
                            _l2_status_val, _delta, _fresh_rc,
                        )
                        verification["layer2"]["status"] = "passed"
                        verification["layer2"]["details"] = (
                            f"Programmatic restart pre-check: no container restart during injection "
                            f"(delta={_delta}). OOMKill in LastState is PRE-EXISTING. "
                            f"LLM conclusion overridden."
                        )
                        # Fix checklist items incorrectly attributed to restart.
                        # When no restart occurred, ALL "recovered_before_observation"
                        # items are invalid — the premise "evidence was destroyed
                        # by restart" is false.  Override them regardless of whether
                        # the evidence text mentions restart keywords.
                        for _ci in verification.get("checklist", {}).get("items", []):
                            if _ci.get("status") == "recovered_before_observation":
                                _ci["status"] = "passed"
                                _ci["evidence"] = (
                                    f"[OVERRIDE] Programmatic check: no restart (delta={_delta}). "
                                    f"Pre-existing OOMKill did not destroy evidence. "
                                    f"Fault effect is still observable."
                                )
                        verification.setdefault("warnings", []).append(
                            "Programmatic override: LLM attributed pre-existing OOMKill to "
                            f"injection, but restart_precheck confirmed no restart (delta={_delta})."
                        )
                        _enforcement_applied = True

            # Enforcement 2: disk_burn_post_check says I/O ACTIVE, but LLM concluded otherwise.
            # When I/O is confirmed ACTIVE, the fault has NOT "recovered before observation" —
            # it is still in effect.  Override ALL non-passed checklist items, not just those
            # whose evidence text happens to contain I/O keywords.  The LLM may have marked
            # steps as recovered/failed because it couldn't detect the burn from its limited
            # tool choices (df -h, ls), not because the burn actually stopped.
            if _burn_enforce and _burn_enforce.get("burn_io_detected"):
                _active_parts = _burn_enforce.get("active_partitions", [])
                _parts_str = ", ".join(
                    f"{p['name']}: ~{p['write_throughput_mb_s']} MB/s"
                    for p in _active_parts[:3]
                ) or "measured"
                _io_overridden = False
                for _ci in verification.get("checklist", {}).get("items", []):
                    if _ci.get("status") in ("failed", "recovered_before_observation", "partial"):
                        _ci["status"] = "passed"
                        _ci["evidence"] = (
                            f"[OVERRIDE] Programmatic I/O check confirmed ACTIVE "
                            f"(write throughput: {_parts_str}). "
                            f"Fault is still in effect — LLM observation was insufficient, "
                            f"not evidence of recovery."
                        )
                        _io_overridden = True
                if _io_overridden:
                    logger.info(
                        "Programmatic enforcement: disk_burn_post_check confirmed I/O ACTIVE, "
                        "but LLM checklist marked steps as failed/recovered. Overridden."
                    )
                    # Also override L2 status: if I/O is proven ACTIVE, the fault
                    # is in effect and L2 cannot be failed/recovered.
                    _l2_val = verification.get("layer2", {}).get("status", "unknown")
                    if _l2_val in ("failed", "recovered_before_observation", "partial"):
                        verification["layer2"]["status"] = "passed"
                        verification["layer2"]["details"] = (
                            f"Programmatic I/O check: disk burn ACTIVE "
                            f"(write throughput: {_parts_str}). "
                            f"LLM conclusion overridden."
                        )
                    if _l2_val in ("failed", "recovered_before_observation", "partial"):
                        _l2_desc = (
                            "the fault was absent" if _l2_val == "failed"
                            else "the fault effect had already dissipated before observation"
                            if _l2_val == "recovered_before_observation"
                            else "the fault effect was only partially confirmed"
                        )
                        verification.setdefault("warnings", []).append(
                            f"Programmatic override: disk_burn_post_check confirmed I/O ACTIVE "
                            f"(write throughput: {_parts_str}), but LLM concluded "
                            f"{_l2_desc} (original status: '{_l2_val}')."
                        )
                    else:
                        verification.setdefault("warnings", []).append(
                            f"Programmatic override: disk_burn_post_check confirmed I/O ACTIVE "
                            f"(write throughput: {_parts_str}) "
                            f"(LLM Layer2 concluded '{_l2_val}'; override applied to individual checklist steps only)."
                        )
                    _enforcement_applied = True

            # Recalculate verification level after enforcement
            if _enforcement_applied:
                _all_items = verification.get("checklist", {}).get("items", [])
                if _all_items:
                    _remaining_bad = sum(
                        1 for _ci in _all_items
                        if _ci.get("status") in ("failed", "recovered_before_observation", "partial")
                    )
                    if _remaining_bad == 0 and verification.get("layer2", {}).get("status") == "passed":
                        verification["level"] = "verified"
                    elif verification.get("layer2", {}).get("status") == "passed" and _remaining_bad > 0:
                        verification["level"] = "partial"

            # Format guard: re-prompt if LLM didn't follow output format
            # Only on non-final iterations — final iteration stays as-is
            if (verification["layer2"]["status"] == "unknown"
                    and count < settings.max_verifier_loop
                    and not _has_format_reminder(state.get("messages", []))):

                tracker.update(
                    f"Layer 2 iteration {count}: LLM output missing VERIFICATION_RESULT format, "
                    f"re-prompting for structured output",
                    {"debug": True, "iteration": count, "re_prompt": True},
                )

                reminder = HumanMessage(content=(
                    "上一轮输出缺少要求的 VERIFICATION_RESULT 格式。请按 EXACT 格式重新输出：\n\n"
                    "VERIFICATION_CHECKLIST:\n"
                    "- Step 1: passed/failed/skipped — 证据\n"
                    "- ...\n\n"
                    "VERIFICATION_RESULT:\n"
                    "- Layer1 (blade_status): passed/failed/skipped\n"
                    "- Layer2 (fault-specific): passed/failed/skipped - 摘要\n"
                    "- Overall: verified/partial/unverified\n"
                    "- BaselineUsed: true/false\n"
                    "- Warnings: any warnings, or \"none\"\n\n"
                    "Layer2: 'passed'=故障效果可观测; 'failed'=故障效果不可观测。\n"
                    "勿用 markdown 表格或 emoji，仅纯文本 bullet 格式。"
                ))

                result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response, reminder]
                result_update["verifier_loop_count"] = count
                result_update["inject_layer1_cache"] = _layer1_to_dict(layer1)
                from chaos_agent.memory.hook import merge_hook_updates
                merge_hook_updates(result_update, hook_updates)
                # NOTE: verification is NOT set → router returns "continue"
                await sync_to_store(state, result_update)
                return result_update

            # Step coverage: compare executed steps against expected from skill case
            skill_case = state.get("skill_case_content", "")
            missing_step_nums = None  # Initialize before conditional assignment
            deviated_step_nums = None
            if skill_case and verification.get("checklist"):
                expected_steps = _count_verification_steps_in_skill_case(skill_case)
                executed_steps = verification["checklist"].get("total_executed", 0)

                # Step-number-level coverage validation (P3 improvement)
                checklist_items = verification["checklist"].get("items", [])
                missing_step_nums, deviated_step_nums = _validate_step_number_coverage(
                    skill_case, checklist_items,
                )

                if missing_step_nums:
                    # Specific step numbers are missing from the checklist
                    step_list = ", ".join(str(s) for s in missing_step_nums)
                    verification["warnings"].append(
                        f"Step coverage: steps {step_list} from skill case "
                        f"are missing from the verification checklist. "
                        f"Every step must be accounted for (even if marked skipped). "
                        f"Verification may be incomplete."
                    )
                    if not _enforcement_applied:
                        # Only downgrade when enforcement has NOT confirmed the fault
                        # effect. When enforcement is active, programmatic checks are
                        # authoritative; missing LLM steps do not negate confirmed
                        # physical evidence.
                        if verification["layer2"]["status"] == "passed":
                            verification["layer2"]["status"] = "partial"
                            if verification.get("level") == "verified":
                                verification["level"] = "partial"
                elif expected_steps > 0 and executed_steps < expected_steps:
                    # Fallback: count-based check when step numbers aren't parseable
                    missing = expected_steps - executed_steps
                    verification["warnings"].append(
                        f"Step coverage: {executed_steps}/{expected_steps} steps executed. "
                        f"{missing} step(s) never attempted (not even marked [SKIPPED]). "
                        f"Verification may be incomplete."
                    )
                    if not _enforcement_applied:
                        if verification["layer2"]["status"] == "passed":
                            verification["layer2"]["status"] = "partial"
                            if verification.get("level") == "verified":
                                verification["level"] = "partial"

            # Programmatic coverage warning (safety net)
            layer1_affected = layer1.affected_count
            from chaos_agent.agent.fault_spec import read_fault_spec as _rfs3
            _spec3 = _rfs3(state)
            target_names = list(_spec3.names) if _spec3 else []
            if layer1_affected > 0 and len(target_names) > layer1_affected:
                coverage_warning = (
                    f"Coverage: {layer1_affected}/{len(target_names)} target resources "
                    f"affected by ChaosBlade experiment."
                )
                warnings = verification.get("warnings", [])
                if coverage_warning not in warnings:
                    warnings.append(coverage_warning)
                    verification["warnings"] = warnings

            # P2: Verification integrity guard — detect gaps and trigger re-verification
            from chaos_agent.utils.fault_context import (
                VerificationGap, lookup_adaptations,
            )
            gaps: list[VerificationGap] = []

            # Clear previous reverify_gaps at the start of each iteration.
            # If gaps are still found, reverify_gaps will be re-set below.
            # This prevents stale gaps from causing infinite loops via
            # should_continue_verifier's reverify check.
            if state.get("reverify_gaps"):
                result_update["reverify_gaps"] = None

            # Gap A: step coverage gap (already detected above)
            # Skip step_gap when enforcement confirmed the fault effect --
            # re-verification won't help because LLM attention degradation
            # is the root cause, not missing information.
            if not _enforcement_applied:
                if missing_step_nums:
                    gaps.append(VerificationGap(
                        gap_type="step_gap",
                        description=f"Steps {missing_step_nums} from skill case missing from checklist",
                        missing_steps=missing_step_nums,
                    ))
                elif expected_steps > 0 and executed_steps < expected_steps:
                    missing_count = expected_steps - executed_steps
                    gaps.append(VerificationGap(
                        gap_type="step_gap",
                        description=f"{executed_steps}/{expected_steps} steps executed, {missing_count} missing",
                    ))

            # Gap B: Layer1 evidence contradiction (blade Success but 0 affected)
            if layer1.status == "passed" and layer1.affected_count == 0:
                gaps.append(VerificationGap(
                    gap_type="layer1_contradiction",
                    description="blade reports Success but 0 resources affected",
                ))

            # Gap C: Layer2 conclusion contradicts Layer1 facts (e.g., OOMKill detected)
            l2_status_val = verification.get("layer2", {}).get("status", "unknown")
            side_effects = verification.get("side_effects") or {}
            container_restarts = side_effects.get("container_restarts", False)
            if l2_status_val == "passed" and container_restarts:
                gaps.append(VerificationGap(
                    gap_type="layer2_layer1_conflict",
                    description="Layer2 says verified but container restarts (OOMKill) detected in Layer1",
                ))

            # Gap D: Baseline available but LLM did not use it
            _baseline = state.get("baseline_data")
            _baseline_available = (
                _baseline and _baseline.get("success_count", 0) > 0
            )
            _baseline_used = verification.get("baseline_used", False)
            if _baseline_available and not _baseline_used:
                gaps.append(VerificationGap(
                    gap_type="baseline_used_check",
                    description=(
                        "Pre-injection baseline data was available but BaselineUsed=false. "
                        "You MUST compare your observations against the pre-injection baseline "
                        "data. Set BaselineUsed: true and include "
                        "baseline → current (Δchange) comparisons in your checklist evidence."
                    ),
                ))

            # Gap E: PrimaryEvidenceObserved inconsistent with Overall
            _peo = verification.get("primary_evidence_observed", False)
            _overall = verification.get("overall", "")
            if not _peo and _overall == "verified":
                gaps.append(VerificationGap(
                    gap_type="primary_evidence_consistency",
                    description=(
                        "PrimaryEvidenceObserved=false but Overall=verified. "
                        "When no primary evidence was observed, Overall MUST be 'partial' "
                        "or 'unverified', NOT 'verified'. Correct your Overall verdict."
                    ),
                ))

            # If gaps found and re-verification budget allows, inject re-verify prompt
            if gaps:
                reverify_count = state.get("reverify_count", 0)
                target_metadata = state.get("target_metadata") or {}
                from chaos_agent.agent.fault_spec import read_fault_spec as _rfs4
                _spec4 = _rfs4(state)
                adaptations = lookup_adaptations(
                    _spec4.scope if _spec4 else "",
                    _spec4.blade_target if _spec4 else "",
                    _spec4.blade_action if _spec4 else "",
                    target_metadata,
                    rule_type="verification_integrity_guard",
                )
                max_attempts = 1
                if adaptations:
                    max_attempts = adaptations[0].action.get("max_reverify_attempts", 1)

                if reverify_count < max_attempts:
                    gap_descriptions = "; ".join(g.description for g in gaps)
                    logger.info(
                        "P2 verification gaps detected: %s — triggering re-verification (attempt %d/%d)",
                        gap_descriptions, reverify_count + 1, max_attempts,
                    )
                    # Inject re-verification prompt into messages
                    # Build gap-specific remediation instructions for each gap type
                    _gap_instructions = []
                    for _g in gaps:
                        if _g.gap_type == "step_gap":
                            _missing = _g.missing_steps or []
                            _step_str = ", ".join(str(s) for s in _missing) if _missing else "unknown"
                            _gap_instructions.append(
                                f"- STEP GAP: Skill case steps [{_step_str}] are missing from your "
                                f"checklist. Add each missing step with status and evidence. "
                                f"Use [SKIPPED] only if the tool is genuinely unavailable, "
                                f"with the specific reason."
                            )
                        elif _g.gap_type == "layer1_contradiction":
                            _gap_instructions.append(
                                "- LAYER1 CONTRADICTION: blade reports Success but 0 resources "
                                "affected. Explain why 0 affected resources is consistent (or "
                                "inconsistent) with your Layer2 conclusion."
                            )
                        elif _g.gap_type == "layer2_layer1_conflict":
                            _gap_instructions.append(
                                "- LAYER2/LAYER1 CONFLICT: You marked Layer2=passed but "
                                "Layer1 detected container restarts (e.g., OOMKill). A restart "
                                "is a SIDE EFFECT, not primary evidence of the fault's intended "
                                "physical effect. Reconcile: is the restart evidence of the "
                                "fault, or did the restart destroy primary evidence?"
                            )
                        elif _g.gap_type == "baseline_used_check":
                            _gap_instructions.append(
                                "- BASELINE NOT USED: Pre-injection baseline data was available "
                                "in the HumanMessage. You MUST include \"baseline: X → current: Y "
                                "(ΔZ)\" comparisons in your checklist evidence and set "
                                "BaselineUsed: true."
                            )
                        elif _g.gap_type == "primary_evidence_consistency":
                            _gap_instructions.append(
                                "- EVIDENCE/CONCLUSION CONFLICT: PrimaryEvidenceObserved=false "
                                "but Overall=verified. When no primary evidence was directly "
                                "observed, Overall MUST be 'partial' or 'unverified', NOT 'verified'."
                            )
                        else:
                            _gap_instructions.append(f"- {_g.description}")
                    _instructions_str = "\n".join(_gap_instructions)
                    reverify_msg = (
                        f"Verification gaps detected:\n{_instructions_str}\n\n"
                        f"You MUST account for ALL gaps above in your re-attempted verification. "
                        f"Re-attempt verification now."
                    )
                    from langchain_core.messages import HumanMessage as _HM
                    # Prepend synthetic messages before response + reverify HM
                    # (this will be overwritten by the final result_update["messages"]
                    # assignment below, which also includes _synthetic_for_state)
                    result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response, _HM(content=reverify_msg)]
                    result_update["reverify_count"] = reverify_count + 1
                    result_update["reverify_gaps"] = [g.gap_type for g in gaps]
                    # Clear verification to force another verifier loop iteration
                    # (should_continue_verifier checks reverify_gaps)
                    sync_node_status_to_session(state, "verifier",
                        f"P2 re-verification triggered: {gap_descriptions} (attempt {reverify_count + 1}/{max_attempts})",
                        detail={"gap_types": [g.gap_type for g in gaps],
                                "attempt": reverify_count + 1, "max_attempts": max_attempts})
                    if settings.is_debug and tracker:
                        tracker.update(
                            f"[P2] re-verification triggered: {gap_descriptions} (attempt {reverify_count + 1}/{max_attempts})"[:200],
                            {"debug": True, "fcat": True},
                        )
                else:
                    logger.info(
                        "P2 verification gaps detected but max reverify attempts (%d) reached — degrading to partial",
                        max_attempts,
                    )
                    sync_node_status_to_session(state, "verifier",
                        f"P2 re-verification max attempts reached, degrading to partial ({max_attempts} attempts)",
                        detail={"gap_types": [g.gap_type for g in gaps], "max_attempts": max_attempts})
                    if settings.is_debug and tracker:
                        tracker.update(
                            f"[P2] max attempts reached, degrading to partial ({max_attempts})"[:200],
                            {"debug": True, "fcat": True},
                        )

            result = {
                "task_id": task_id,
                "skill": skill_name,
                "blade_uid": blade_uid,
                "verified": verification["level"] == "verified",
            }
            result_update["result"] = result
            # Ensure baseline_confidence is set on LLM-parsed verification dicts
            if "baseline_confidence" not in verification:
                verification["baseline_confidence"] = _compute_baseline_confidence(state)
            # Programmatic Fact Enforcement: when baseline data was available
            # (confidence=high or partial) but LLM did not declare BaselineUsed=true,
            # force it to true and add an auditable warning. BaselineUsed is a FACT
            # (baseline data exists in the HumanMessage) not an LLM opinion — the LLM
            # may ignore it but the data was there for comparison. This mirrors the
            # restart_precheck / disk_burn_post_check override pattern — deterministic
            # code checks LLM conclusion against a programmatic fact.
            _bl_conf = verification.get("baseline_confidence", "none")
            if _bl_conf in ("high", "partial") and not verification.get("baseline_used"):
                _bl_used_orig = verification.get("baseline_used")  # None or False before override
                verification["baseline_used"] = True
                verification.setdefault("warnings", []).append(
                    f"Programmatic override: BaselineUsed forced to true — pre-injection "
                    f"baseline was available (confidence={_bl_conf}) but LLM declared "
                    f"BaselineUsed={_bl_used_orig}. Verification data included baseline metrics; "
                    f"LLM may have ignored them, but the comparison opportunity existed."
                )
                logger.info(
                    "Programmatic enforcement: BaselineUsed forced from %s to true "
                    "(baseline_confidence=%s)",
                    _bl_used_orig, _bl_conf,
                )
            result_update["verification"] = verification
            result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response]

            # Save injection verification summary for recover phase baseline comparison
            l2_details = verification.get("layer2", {}).get("details", "")
            if l2_details:
                result_update["inject_verification_summary"] = (
                    f"Layer2={verification.get('layer2', {}).get('status', 'unknown')}, "
                    f"Details={l2_details}"
                )

            level = verification["level"]
            l1_status = layer1.status
            l2_status = verification.get("layer2", {}).get("status", "unknown")
            warnings = verification.get("warnings", [])
            status_msg = f"Verification: {level} (Layer1: {l1_status}, Layer2: {l2_status})"
            if warnings:
                status_msg += f" | warnings: {'; '.join(warnings)}"
            tracker.complete(status_msg)

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result_update, hook_updates)

        # ---- Programmatic Debug Pod Cleanup ----
        # Scan all ToolMessages from kubectl tool calls during this verification
        # to extract debug pod names created by LLM, then delete them.
        # This enforces cleanup deterministically — prompt-only approach is
        # unreliable (matches the Programmatic Fact Enforcement paradigm).
        debug_pods_created: set[str] = set()
        for msg in state.get("messages", []):
            if isinstance(msg, ToolMessage) and getattr(msg, "name", "") == "kubectl":
                msg_content = msg.content if isinstance(msg.content, str) else str(msg.content)
                pod_name = _parse_debug_pod_name(msg_content)
                if pod_name:
                    debug_pods_created.add(pod_name)
        for pod_name in debug_pods_created:
            logger.info(f"Programmatic cleanup: deleting debug pod {pod_name}")
            await _delete_debug_pod(pod_name, kubeconfig, task_id)

        await sync_to_store(state, result_update)
        # Patch C — wall-clock cause labelling. The router will return
        # "done" on the next conditional-edge tick if the budget has
        # been exceeded; stamp failure_reason here so the result is
        # honest about why verification stopped.
        from chaos_agent.agent.router import mark_wall_clock_timeout
        return mark_wall_clock_timeout(state, result_update)

    return _verifier_with_llm
