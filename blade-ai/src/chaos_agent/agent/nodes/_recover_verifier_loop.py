"""Recover verifier loop: entry functions for two-layer post-recovery verification."""

import logging

from langchain_core.messages import SystemMessage, HumanMessage, ToolMessage

from chaos_agent.agent.nodes._injection_detection import (
    _was_kubectl_blade_injection_successful,
    _was_blade_create_attempted,
)
from chaos_agent.agent.nodes._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
)
from chaos_agent.agent.nodes._recover_layer1 import (
    RecoverLayer1Result,
    _RECOVER_BASELINE_TOOL_CALL_ID,
    _RECOVER_CONTEXT_KWARGS_KEY,
    _RECOVER_SYNTHETIC_TOOL_CALL_IDS,
    _build_layer1_recovery_prompt,
    _build_recover_baseline_tool_messages,
    _recover_layer1_to_dict,
    # noqa: F401 — backward-compat re-export for tests
    # noqa: F401 — backward-compat re-export for tests
    _parse_layer1_recovery_result,
    _run_recover_layer1,
)
from chaos_agent.agent.nodes._recover_layer2_parse import (
    _build_recover_verifier_prompt,
    _extract_recovery_verification_section,
    _count_recovery_steps_in_skill_case,
    _parse_recovery_verification_result,
    # noqa: F401 — backward-compat re-export for tests
    # noqa: F401
    # noqa: F401
    # noqa: F401
    # noqa: F401
)
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.nodes._verifier_hints import _PARAM_HINT_GENERATORS
from chaos_agent.agent.nodes._verifier_shared import (
    _IMAGEFS_PATHS,
    _NODEFS_PATHS,
    _compute_baseline_confidence,
)
from chaos_agent.agent.nodes.baseline_capture import _parse_debug_pod_name, _delete_debug_pod
from chaos_agent.agent.nodes.react_helpers import (
    emit_debug_tool_messages,
    extract_persistent_hm,
    extract_synthetic_messages,
    extract_tool_call_fields,
    record_system_prompt,
    summarize_llm_response,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.errors import FailureReason
from chaos_agent.memory.session_store import NO_SESSION_MARKER
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

# settings.max_recover_verifier_loop is now configurable via settings.max_recover_verifier_loop (default 10)


# ---------------------------------------------------------------------------
# Entry: Simple recover verifier (no LLM, Layer 1 only)
# ---------------------------------------------------------------------------

async def recover_verifier(state: AgentState) -> dict:
    """Simple recover verifier without LLM: Layer 1 only."""
    task_id = state.get("task_id", "")
    blade_uid = state.get("blade_uid", "")
    skill_name = state.get("skill_name", "")
    kubeconfig = _resolve_kubeconfig(state)

    # Defense-in-depth: recover blade_uid from message history if missing in state
    if not blade_uid:
        from chaos_agent.agent.nodes.execute_loop import _extract_blade_uid_from_messages
        messages = state.get("messages", [])
        blade_uid = _extract_blade_uid_from_messages(messages) or ""
        if blade_uid:
            logger.info(f"recover_verifier: recovered blade_uid={blade_uid} from message history")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "recover_verifier",
        f"Verifying fault recovery (uid={blade_uid or 'N/A'})",
        {"blade_uid": blade_uid, "skill_name": skill_name},
    )

    _is_kubectl_exec = _was_kubectl_blade_injection_successful(state.get("messages", []))
    if blade_uid and _is_kubectl_exec:
        # ChaosBlade experiment created via kubectl exec: host blade_destroy cannot
        # destroy it. Without LLM, we can't do kubectl exec recovery either.
        layer1 = RecoverLayer1Result(
            status="skipped",
            details=f"ChaosBlade experiment (uid={blade_uid}) was created via kubectl exec, "
                    f"host blade_destroy cannot destroy it — LLM-based recovery required",
        )
    elif blade_uid:
        layer1 = await _run_recover_layer1(blade_uid, kubeconfig, messages=state.get("messages", []))
    else:
        layer1 = RecoverLayer1Result(
            status="skipped",
            details="Non-ChaosBlade fault (no blade_uid), Layer 1 recovery not applicable",
        )
    if _is_kubectl_exec:
        # ChaosBlade experiment created via kubectl exec, host blade_destroy cannot destroy it
        _verification_level = "unrecovered"
        _recovered = False
    elif not blade_uid:
        # Non-ChaosBlade fault: cannot verify recovery without LLM (Layer 2)
        _verification_level = "unrecovered"
        _recovered = False
    else:
        _verification_level = "recovered" if layer1.is_passed() else "unrecovered"
        _recovered = layer1.is_passed()
    verification = {
        "level": _verification_level,
        "layer1": _recover_layer1_to_dict(layer1),
        "layer2": {"status": "skipped", "details": "No LLM available for specific verification"},
        "baseline_confidence": _compute_baseline_confidence(state),
        "warnings": (
            [
                f"ChaosBlade experiment (uid={blade_uid}) created via kubectl exec cannot be "
                f"destroyed from host (blade_destroy). Use LLM-based recovery "
                f"(blade-ai recover with LLM) to destroy via kubectl exec: "
                f"kubectl exec <pod> -n chaosblade -- blade destroy {blade_uid}"
            ]
            if _is_kubectl_exec
            else (
                [
                    "Non-ChaosBlade fault: Layer 1 not applicable, Layer 2 skipped (no LLM). "
                    "Recovery could NOT be verified — the fault may still be active."
                ]
                if not blade_uid
                else (
                    [
                        "Layer 2 (fault-specific) recovery verification was skipped. "
                        "Only blade_destroy + blade_status verification was performed."
                    ]
                    if layer1.is_passed()
                    else []
                )
            )
        ),
    }

    result = {
        "task_id": task_id,
        "skill": skill_name,
        "blade_uid": blade_uid,
        "recovered": _recovered,
    }

    if _recovered:
        tracker.complete(f"Recovery verification: {layer1.status} (uid={blade_uid or 'N/A'})")
    else:
        tracker.complete(f"Recovery verification: {layer1.status}")

    result_dict = {"result": result, "recover_verification": verification, "finished_at": now_iso()}
    if not _recovered:
        base = (
            f"{FailureReason.RECOVERY_FAILED.value}: "
            f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
        )
        result_dict["failure_reason"] = base
    return result_dict


# ---------------------------------------------------------------------------
# Entry: Full recover verifier with LLM (two-layer)
# ---------------------------------------------------------------------------

def make_recover_verifier(hook=None, llm=None, tools=None, registry=None):
    """Create a recover verifier node with two-layer verification.

    Layer 1 (Execute recovery):
      - ChaosBlade faults: blade_destroy + blade_status (deterministic)
      - Non-ChaosBlade faults: LLM executes recovery actions via kubectl tools
    Layer 2 (Verify recovery):
      - LLM verifies fault effect is removed (ReAct loop)
        - Priority 1: Use skill's "恢复验证" section
        - Priority 2: LLM designs verification based on fault context
        - Priority 3: LLM outputs skipped → auto-warning
    When llm is None, falls back to Layer 1 only.
    """
    if llm is None:
        return recover_verifier

    async def _recover_verifier_with_llm(state: AgentState) -> dict:
        task_id = state.get("task_id", "")
        blade_uid = state.get("blade_uid", "")
        skill_name = state.get("skill_name", "")
        kubeconfig = _resolve_kubeconfig(state)
        count = state.get("verifier_loop_count", 0) + 1

        # Defense-in-depth: recover blade_uid from message history if missing in state
        if not blade_uid:
            from chaos_agent.agent.nodes.execute_loop import _extract_blade_uid_from_messages
            messages = state.get("messages", [])
            blade_uid = _extract_blade_uid_from_messages(messages) or ""
            if blade_uid:
                logger.info(f"recover_verifier_with_llm: recovered blade_uid={blade_uid} from message history")

        tracker = get_tracker(task_id)
        tracker.start(
            StatusCategory.NODE,
            "recover_verifier",
            f"Verifying fault recovery (uid={blade_uid or 'N/A'}, iteration={count})",
            {"blade_uid": blade_uid, "skill_name": skill_name, "iteration": count},
        )

        # ---- Guard: max iterations exceeded ----
        if count > settings.max_recover_verifier_loop:
            logger.warning(f"Recover verifier loop exceeded max iterations ({settings.max_recover_verifier_loop})")
            verification = {
                "level": "partial",
                "layer1": {"status": "passed", "details": "Confirmed in earlier iterations"},
                "layer2": {"status": "skipped", "details": "Max iterations reached, could not confirm recovery"},
                "baseline_confidence": _compute_baseline_confidence(state),
                "warnings": ["Recover verifier loop exceeded max iterations — fault may still be active"],
            }
            result = {
                "task_id": task_id,
                "skill": skill_name,
                "blade_uid": blade_uid,
                "recovered": False,  # Cannot confirm recovered
            }
            result_dict = {
                "result": result,
                "recover_verification": verification,
                "finished_at": now_iso(),
                "failure_reason": (
                    f"{FailureReason.RECOVERY_VERIFICATION_TIMEOUT.value}: "
                    f"Recover verifier exceeded max iterations ({settings.max_recover_verifier_loop}), "
                    f"could not confirm recovery"
                ),
            }
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Layer 1: Execute recovery (first iteration only for ChaosBlade) ----
        # For non-ChaosBlade faults, Layer 1 runs as part of the main ReAct loop
        # (recover_phase == "layer1_recovery") and transitions to Layer 2 when done.
        recover_phase = state.get("recover_phase", "layer1_recovery")

        if recover_phase == "layer1_recovery" and count == 1:
            # When injection was done via kubectl exec, the host blade binary
            # cannot destroy the experiment (record not found). Route these
            # cases through the non-ChaosBlade Layer 1 flow (LLM-driven
            # recovery via kubectl tools) instead of blade_destroy.
            _kubectl_injection = _was_kubectl_blade_injection_successful(state.get("messages", []))

            if blade_uid and not _kubectl_injection:
                # ChaosBlade on host: deterministic blade_destroy + blade_status
                layer1 = await _run_recover_layer1(blade_uid, kubeconfig, messages=state.get("messages", []))
            elif _was_blade_create_attempted(state.get("messages", [])):
                # ChaosBlade injection was done but UID unavailable
                layer1 = RecoverLayer1Result(
                    status="failed",
                    details="blade_create was called during injection but no UID available for recovery",
                )
            else:
                # Non-ChaosBlade OR kubectl exec injection: LLM-driven Layer 1
                # (Layer 1 runs in the main ReAct loop, not a separate sub-loop)
                inject_context = state.get("inject_context", "")

                # For kubectl exec injection, append blade_uid recovery instructions
                if _kubectl_injection and blade_uid:
                    original_pod = state.get("kubectl_exec_pod_name")
                    pod_hint = ""
                    if original_pod:
                        pod_hint = (
                            f"原始注入 Pod: `{original_pod}`，优先使用该 Pod 执行 blade destroy。\n"
                            f"若该 Pod 已不存在，通过 kubectl get pods 发现当前 running 的 tool pod。\n"
                        )
                    inject_context += (
                        f"\n\n## ChaosBlade Experiment Recovery (kubectl exec)\n"
                        f"The fault was injected using ChaosBlade via `kubectl exec` into a cluster pod.\n"
                        f"Experiment UID: `{blade_uid}`\n"
                        f"{pod_hint}"
                        f"To recover, you MUST destroy the experiment using kubectl exec:\n"
                        f"  kubectl(subcommand='exec', pod='{original_pod or '<pod-with-blade>'}', namespace='chaosblade', "
                        f"command='blade destroy {blade_uid}', kubeconfig='{kubeconfig or '<path>'}')\n"
                        f"If you don't know the pod name, find it:\n"
                        f"  kubectl(subcommand='get', args='pods -n chaosblade -l app=otel-c-tool', kubeconfig='{kubeconfig or '<path>'}')\n"
                    )
                    logger.info(
                        f"kubectl exec injection detected for uid={blade_uid}, "
                        f"routing to non-ChaosBlade Layer 1 recovery flow"
                    )

                if not inject_context:
                    # No inject context — skip Layer 1
                    logger.info(f"No inject context for {skill_name}, skipping Layer 1")
                    layer1 = RecoverLayer1Result(
                        status="skipped",
                        details="Non-ChaosBlade fault: no inject context available",
                    )
                    if tracker:
                        tracker.update(
                            "Recover Layer 1 (non-ChaosBlade): skipped - no inject context",
                            {"layer1_status": "skipped", "layer1_type": "non_chaosblade"},
                        )
                else:
                    # Build Layer 1 prompt and add to state.messages
                    layer1_system_prompt = _build_layer1_recovery_prompt(is_kubectl_blade=bool(_kubectl_injection))
                    from chaos_agent.agent.fault_spec import read_fault_spec as _rfs_rvl
                    _spec_rvl = _rfs_rvl(state)
                    layer1_human_content = (
                        f"## Fault Context\n"
                        f"Skill: {skill_name}\n"
                        f"Target namespace: {_spec_rvl.namespace if _spec_rvl else ''}\n"
                        f"Target names: {list(_spec_rvl.names) if _spec_rvl else []}\n"
                        f"Kubeconfig: {kubeconfig or '(default)'}\n"
                    )
                    # Structured key parameters from parsed flags (e.g. path, percent, size)
                    _blade_parsed = state.get("blade_parsed_flags") or {}
                    if _blade_parsed:
                        layer1_human_content += f"Blade key parameters: {_blade_parsed}\n"
                    layer1_human_content += (
                        "\n## Recovery Guidance\n"
                        "Refer to the Injection Phase Context above to understand what was injected and determine the correct recovery actions. "
                        "Execute the recovery using available kubectl tools.\n\n"
                    )
                    if kubeconfig:
                        layer1_human_content += (
                            f"**IMPORTANT**: You MUST pass `kubeconfig='{kubeconfig}'` to EVERY "
                            f"kubectl tool call. The default kubeconfig cannot access this cluster. "
                            f"Do NOT omit the kubeconfig parameter.\n"
                        )
                    layer1_human_content += (
                        "\nPlease execute the above recovery actions now. "
                        "After completing all actions, output your RECOVERY_EXECUTION_RESULT summary."
                    )

                    # Add Layer 1 prompt as messages to state (not a separate sub-loop)
                    result_update = {
                        "verifier_loop_count": count,
                        "recover_layer1_cache": None,
                        "layer1_iteration_count": 1,
                    }

                    # Build the full messages list for LLM: SystemMessage + existing state messages + inject context + new HumanMessage
                    inject_msg = None
                    if inject_context:
                        inject_msg = HumanMessage(
                            content=(
                                f"## Injection Phase Context (EXPIRED — fault-state data, NOT current)\n"
                                f"The following context was captured during fault injection. "
                                f"It describes what fault was injected and what was observed WHILE THE FAULT WAS ACTIVE.\n"
                                f"This data is STALE — it does NOT represent the current post-recovery state.\n"
                                f"You MUST re-execute kubectl commands to obtain CURRENT observations.\n\n"
                                f"{inject_context}\n\n"
                            ),
                            additional_kwargs={NO_SESSION_MARKER: True},
                        )

                    messages = list(state.get("messages", []))
                    if inject_msg:
                        messages.append(inject_msg)
                    messages.append(HumanMessage(content=layer1_human_content))

                    # Record system prompt to session store
                    record_system_prompt(hook, state, layer1_system_prompt)

                    # Bind tools and call LLM
                    max_l1 = settings.max_recover_layer1_iterations
                    is_last_l1 = 1 >= max_l1

                    # Deadline/final prompts for edge case where max == 1
                    if is_last_l1:
                        messages.append(HumanMessage(content=(
                            "**RECOVERY EXECUTION DEADLINE**: This is the ONLY iteration available.\n"
                            "Tools are unavailable. You MUST provide your recovery execution conclusion "
                            "in this EXACT format:\n\n"
                            "RECOVERY_EXECUTION_RESULT:\n"
                            "- Status: [success/failed]\n"
                            "- Actions: [summary of all actions taken]\n"
                            "- Details: [errors, warnings, or notes — if failed, explain WHY recovery could not be completed]\n\n"
                            "If you cannot determine the result, set Status to \"failed\" and explain why in Details."
                        )))

                    llm_to_call = llm if is_last_l1 else (llm.bind_tools(tools) if tools else llm)

                    try:
                        response = await llm_to_call.ainvoke(
                            [SystemMessage(content=layer1_system_prompt)] + messages
                        )
                    except Exception as e:
                        logger.error(f"Recover Layer 1 (non-ChaosBlade) LLM call failed: {e}")
                        layer1 = RecoverLayer1Result(status="error", details=f"LLM call failed: {e}", raw_output=str(e))
                        # Fall through to Layer 1 terminal check below
                    else:
                        # Ensure kubeconfig in tool calls
                        inject_kubeconfig_into_tool_calls(response, kubeconfig)

                        tool_calls = getattr(response, "tool_calls", None) or []

                        if tool_calls:
                            # LLM wants to call tools — continue Layer 1 ReAct loop
                            msg_list = []
                            if inject_msg:
                                msg_list.append(inject_msg)
                            msg_list.append(HumanMessage(content=layer1_human_content))
                            msg_list.append(response)
                            result_update["messages"] = msg_list

                            if settings.is_debug:
                                debug_info, tool_names = summarize_llm_response(response)
                                tracker.update(
                                    f"Recover Layer 1 (non-ChaosBlade) iteration 1 LLM:\n{debug_info}",
                                    {"debug": True, "iteration": 1, "tool_calls": tool_names},
                                )
                            else:
                                tool_names = [
                                    extract_tool_call_fields(tc)[0]
                                    for tc in tool_calls
                                ]
                                tracker.update(
                                    "Recover Layer 1 (non-ChaosBlade) iteration 1: calling tools",
                                    {"iteration": 1, "tool_calls": tool_names},
                                )

                            # Store system prompt text in cache for subsequent iterations
                            result_update["recover_layer1_cache"] = {
                                "status": "in_progress",
                                "details": "",
                                "raw_output": "",
                                "system_prompt": layer1_system_prompt,
                            }
                            await sync_to_store(state, result_update)
                            return result_update
                        else:
                            # LLM produced final text — parse Layer 1 result
                            content = getattr(response, "content", "") or ""

                            if settings.is_debug:
                                debug_info, _ = summarize_llm_response(response)
                                tracker.update(
                                    f"Recover Layer 1 (non-ChaosBlade) iteration 1 LLM (final):\n{debug_info}",
                                    {"debug": True, "iteration": 1, "tool_calls": []},
                                )

                            layer1 = _parse_layer1_recovery_result(content)

                            if tracker:
                                tracker.update(
                                    f"Recover Layer 1 (non-ChaosBlade): {layer1.status} - {layer1.details[:100]}",
                                    {"layer1_status": layer1.status, "layer1_type": "non_chaosblade"},
                                )

                            # Store Layer 1 output in state.messages for Layer 2 to see
                            msg_list = []
                            if inject_msg:
                                msg_list.append(inject_msg)
                            msg_list.append(HumanMessage(content=layer1_human_content))
                            msg_list.append(response)
                            result_update["messages"] = msg_list
                            result_update["recover_layer1_cache"] = _recover_layer1_to_dict(layer1)

                            if layer1.is_terminal():
                                # Layer 1 failed — skip Layer 2
                                verification = {
                                    "level": "unrecovered",
                                    "layer1": _recover_layer1_to_dict(layer1),
                                    "layer2": {"status": "skipped", "details": "Layer 1 failed, skipping Layer 2"},
                                    "baseline_confidence": _compute_baseline_confidence(state),
                                    "warnings": [f"Layer 1 recovery verification failed: {layer1.details}"],
                                }
                                result = {
                                    "task_id": task_id,
                                    "skill": skill_name,
                                    "blade_uid": blade_uid,
                                    "recovered": False,
                                }
                                tracker.complete(f"Recovery failed at Layer 1: {layer1.status}")
                                base = (
                                    f"{FailureReason.RECOVERY_FAILED.value}: "
                                    f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
                                )
                                result_dict = {
                                    "result": result,
                                    "recover_verification": verification,
                                    "finished_at": now_iso(),
                                    "failure_reason": base,
                                }
                                result_update.update(result_dict)
                                await sync_to_store(state, result_update)
                                return result_update

                            # Layer 1 passed — transition to Layer 2
                            result_update["recover_phase"] = "layer2_verification"
                            result_update["recover_layer1_type"] = "llm_driven"
                            await sync_to_store(state, result_update)
                            # Return and let the next iteration handle Layer 2
                            # (this iteration already consumed an LLM call for Layer 1)
                            return result_update

                    # If we get here, layer1 was set from the exception case
                    # Fall through to the detail_msg update below

            detail_msg = f"Recover Layer 1: {layer1.status}"
            if layer1.details:
                detail_msg += f" - {layer1.details}"
            tracker.update(detail_msg, {"layer1_status": layer1.status})

            # Record Layer 1 result to session (programmatic operations
            # are not captured by PreReasoningHook since they bypass LLM)
            _task_id_local = state.get("task_id", "")
            if hook and getattr(hook, "session_store", None) and _task_id_local:
                hook.session_store.append_raw_message(_task_id_local, {
                    "type": "system",
                    "content": f"[Recover Layer 1] status={layer1.status}, details={layer1.details}",
                    "detail": {
                        "layer": 1,
                        "status": layer1.status,
                        "details": layer1.details,
                        "raw_output": (layer1.raw_output or "")[:500],
                    },
                })

        elif recover_phase == "layer1_recovery" and count > 1:
            # Continue Layer 1 ReAct loop (non-ChaosBlade)
            layer1_iteration = state.get("layer1_iteration_count", 0) + 1

            # Check max iterations for Layer 1
            if layer1_iteration > settings.max_recover_layer1_iterations:
                logger.warning(f"Layer 1 (non-ChaosBlade) exceeded max iterations ({settings.max_recover_layer1_iterations})")
                layer1 = RecoverLayer1Result(
                    status="error",
                    details=f"Layer 1 recovery execution exceeded max iterations ({settings.max_recover_layer1_iterations})",
                    raw_output="",
                )
                verification = {
                    "level": "unrecovered",
                    "layer1": _recover_layer1_to_dict(layer1),
                    "layer2": {"status": "skipped", "details": "Layer 1 exceeded max iterations"},
                    "baseline_confidence": _compute_baseline_confidence(state),
                    "warnings": ["Layer 1 recovery execution exceeded max iterations"],
                }
                result = {
                    "task_id": task_id,
                    "skill": skill_name,
                    "blade_uid": blade_uid,
                    "recovered": False,
                }
                tracker.complete("Recovery failed at Layer 1: max iterations exceeded")
                result_dict = {
                    "result": result,
                    "recover_verification": verification,
                    "finished_at": now_iso(),
                    "failure_reason": f"{FailureReason.RECOVERY_FAILED.value}: Layer1=error, Layer2=skipped",
                }
                await sync_to_store(state, result_dict)
                return result_dict

            # Get Layer 1 system prompt from cache
            cache = state.get("recover_layer1_cache") or {}
            layer1_system_prompt = cache.get("system_prompt", _build_layer1_recovery_prompt())

            # Build messages from state
            messages = list(state.get("messages", []))

            max_l1 = settings.max_recover_layer1_iterations

            # Convergence hint: encourage conclusion if already several iterations
            if layer1_iteration >= 3:
                messages.append(HumanMessage(content=(
                    "You have already executed several recovery actions. If the actions are complete, "
                    "output your RECOVERY_EXECUTION_RESULT summary now rather than taking more actions."
                )))

            # Deadline prompt: tools will be unbound next iteration
            if layer1_iteration >= max_l1 - 1:
                messages.append(HumanMessage(content=(
                    f"**RECOVERY EXECUTION DEADLINE**: This is iteration {layer1_iteration} of max {max_l1}.\n"
                    f"Based on ALL actions executed so far:\n"
                    f"  - If recovery actions are complete, output the RECOVERY_EXECUTION_RESULT format NOW.\n"
                    f"  - This is your last chance to use tools — on the next iteration tools will be unavailable.\n\n"
                    f"Your Status must be one of:\n"
                    f"  - **success**: Recovery actions have been executed successfully\n"
                    f"  - **failed**: Recovery actions could not be completed — explain why in Details\n"
                )))

            # Final iteration: no tools, force structured output
            if layer1_iteration >= max_l1:
                messages.append(HumanMessage(content=(
                    f"**FINAL RECOVERY EXECUTION ITERATION**: This is iteration {layer1_iteration} of max {max_l1}. "
                    f"NO more iterations available. Tools are no longer available.\n"
                    f"You MUST provide your final recovery execution conclusion NOW in this EXACT format:\n\n"
                    f"RECOVERY_EXECUTION_RESULT:\n"
                    f"- Status: [success/failed]\n"
                    f"- Actions: [summary of all actions taken]\n"
                    f"- Details: [errors, warnings, or notes — if failed, explain WHY recovery could not be completed]\n\n"
                    f"If you cannot determine the result, set Status to \"failed\" and explain why in Details."
                )))

            # Per-iteration kubeconfig reminder
            if kubeconfig:
                messages.append(HumanMessage(content=(
                    f"**Reminder**: You MUST pass kubeconfig='{kubeconfig}' to every kubectl tool call."
                )))

            is_last_l1 = layer1_iteration >= max_l1
            llm_to_call = llm if is_last_l1 else (llm.bind_tools(tools) if tools else llm)

            try:
                response = await llm_to_call.ainvoke(
                    [SystemMessage(content=layer1_system_prompt)] + messages
                )
            except Exception as e:
                logger.error(f"Recover Layer 1 (non-ChaosBlade) LLM call failed at iteration {layer1_iteration}: {e}")
                layer1 = RecoverLayer1Result(status="error", details=f"LLM call failed: {e}", raw_output=str(e))
                # Fall through to terminal check
            else:
                inject_kubeconfig_into_tool_calls(response, kubeconfig)
                tool_calls = getattr(response, "tool_calls", None) or []

                result_update = {
                    "verifier_loop_count": count,
                    "layer1_iteration_count": layer1_iteration,
                }

                if tool_calls:
                    # Continue Layer 1 ReAct loop
                    result_update["messages"] = [response]

                    if settings.is_debug:
                        debug_info, tool_names = summarize_llm_response(response)
                        tracker.update(
                            f"Recover Layer 1 (non-ChaosBlade) iteration {layer1_iteration} LLM:\n{debug_info}",
                            {"debug": True, "iteration": layer1_iteration, "tool_calls": tool_names},
                        )
                    else:
                        tool_names = [
                            extract_tool_call_fields(tc)[0]
                            for tc in tool_calls
                        ]
                        tracker.update(
                            f"Recover Layer 1 (non-ChaosBlade) iteration {layer1_iteration}: calling tools",
                            {"iteration": layer1_iteration, "tool_calls": tool_names},
                        )

                    await sync_to_store(state, result_update)
                    return result_update
                else:
                    # Layer 1 completed — parse result
                    content = getattr(response, "content", "") or ""

                    if settings.is_debug:
                        debug_info, _ = summarize_llm_response(response)
                        tracker.update(
                            f"Recover Layer 1 (non-ChaosBlade) iteration {layer1_iteration} LLM (final):\n{debug_info}",
                            {"debug": True, "iteration": layer1_iteration, "tool_calls": []},
                        )

                    layer1 = _parse_layer1_recovery_result(content)

                    if tracker:
                        tracker.update(
                            f"Recover Layer 1 (non-ChaosBlade): {layer1.status} - {layer1.details[:100]}",
                            {"layer1_status": layer1.status, "layer1_type": "non_chaosblade"},
                        )

                    result_update["messages"] = [response]
                    result_update["recover_layer1_cache"] = _recover_layer1_to_dict(layer1)

                    if layer1.is_terminal():
                        # Layer 1 failed — skip Layer 2
                        verification = {
                            "level": "unrecovered",
                            "layer1": _recover_layer1_to_dict(layer1),
                            "layer2": {"status": "skipped", "details": "Layer 1 failed, skipping Layer 2"},
                            "warnings": [f"Layer 1 recovery verification failed: {layer1.details}"],
                            "baseline_confidence": _compute_baseline_confidence(state),
                        }
                        result = {
                            "task_id": task_id,
                            "skill": skill_name,
                            "blade_uid": blade_uid,
                            "recovered": False,
                        }
                        tracker.complete(f"Recovery failed at Layer 1: {layer1.status}")
                        base = (
                            f"{FailureReason.RECOVERY_FAILED.value}: "
                            f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
                        )
                        result_dict = {
                            "result": result,
                            "recover_verification": verification,
                            "finished_at": now_iso(),
                            "failure_reason": base,
                        }
                        result_update.update(result_dict)
                        await sync_to_store(state, result_update)
                        return result_dict

                    # Layer 1 passed — transition to Layer 2
                    result_update["recover_phase"] = "layer2_verification"
                    result_update["recover_layer1_type"] = "llm_driven"
                    await sync_to_store(state, result_update)
                    return result_update

            # If we get here, layer1 was set from the exception case above
            detail_msg = f"Recover Layer 1: {layer1.status}"
            if layer1.details:
                detail_msg += f" - {layer1.details}"
            tracker.update(detail_msg, {"layer1_status": layer1.status})

            # Record Layer 1 result to session (programmatic operations
            # are not captured by PreReasoningHook since they bypass LLM)
            _task_id_local = state.get("task_id", "")
            if hook and getattr(hook, "session_store", None) and _task_id_local:
                hook.session_store.append_raw_message(_task_id_local, {
                    "type": "system",
                    "content": f"[Recover Layer 1] status={layer1.status}, details={layer1.details}",
                    "detail": {
                        "layer": 1,
                        "status": layer1.status,
                        "details": layer1.details,
                        "raw_output": (layer1.raw_output or "")[:500],
                    },
                })

        else:
            # Restore Layer 1 result from previous iteration's cache
            cache = state.get("recover_layer1_cache") or {}
            layer1 = RecoverLayer1Result(
                status=cache.get("status", "unknown"),
                details=cache.get("details", ""),
                raw_output=cache.get("raw_output", ""),
            )

        # If Layer 1 failed, skip Layer 2
        if layer1.is_terminal():
            verification = {
                "level": "unrecovered",
                "layer1": _recover_layer1_to_dict(layer1),
                "layer2": {"status": "skipped", "details": "Layer 1 failed, skipping Layer 2"},
                "warnings": [f"Layer 1 recovery verification failed: {layer1.details}"],
                "baseline_confidence": _compute_baseline_confidence(state),
            }
            result = {
                "task_id": task_id,
                "skill": skill_name,
                "blade_uid": blade_uid,
                "recovered": False,
            }
            tracker.complete(f"Recovery failed at Layer 1: {layer1.status}")
            base = (
                f"{FailureReason.RECOVERY_FAILED.value}: "
                f"Layer1={layer1.status}, Layer2=skipped, details={layer1.details[:200]}"
            )
            result_dict = {
                "result": result,
                "recover_verification": verification,
                "finished_at": now_iso(),
                "failure_reason": base,
            }
            await sync_to_store(state, result_dict)
            return result_dict

        # ---- Layer 2: LLM with tools for fault-specific recovery verification ----
        # Call pre_reason_hook (memory compaction + session recording)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state, seed_existing=True)

        # Only resolve recovery instructions on first Layer 2 iteration
        is_first_layer2 = not state.get("layer2_context_added", False)
        inject_context = state.get("inject_context", "")

        # Determine Layer 1 type: "deterministic" (blade_destroy on host) or "llm_driven"
        # (non-ChaosBlade or kubectl exec injection). Used by Layer 2 prompt and context.
        _layer1_is_deterministic = state.get("recover_layer1_type", "deterministic" if blade_uid else "llm_driven") == "deterministic"

        # Build messages for LLM
        # inject_ctx_msg: for ChaosBlade faults, Layer 1 didn't add inject context to state.messages
        inject_ctx_msg = None
        messages = list(state.get("messages", []))

        # ── Position-optimized baseline ToolMessage injection ──
        # Baseline ToolMessage before any HumanMessage (early placement
        # gets higher attention per Lost in the Middle).
        # Inject on EVERY iteration, not just count==1, because they are
        # ephemeral (not in AgentState.messages by default).  On count>1,
        # check if they're already in state history (persisted from
        # count==1 via result_update) to avoid duplication.
        _baseline = state.get("baseline_data")
        if _baseline and _baseline.get("success_count", 0) > 0:
            _baseline_in_state = any(
                getattr(m, "tool_call_id", "") == _RECOVER_BASELINE_TOOL_CALL_ID
                for m in messages if isinstance(m, ToolMessage)
            )
            if not _baseline_in_state:
                messages.extend(_build_recover_baseline_tool_messages(_baseline))

        if is_first_layer2:
            from chaos_agent.agent.fault_spec import read_fault_spec as _rfs_rvl2
            _spec_rvl2 = _rfs_rvl2(state)
            target = {
                "namespace": _spec_rvl2.namespace if _spec_rvl2 else "",
                "names": list(_spec_rvl2.names) if _spec_rvl2 else [],
                "labels": dict(_spec_rvl2.labels) if _spec_rvl2 else {},
                "resource_type": _spec_rvl2.scope if _spec_rvl2 else "",
            }

            # For ChaosBlade faults, Layer 1 didn't use LLM so inject_context
            # wasn't added to state.messages. Add it now with _no_session marker.
            # For non-ChaosBlade faults, inject_context was added in Layer 1
            # and is already in state.messages — the any() guard skips duplicates.
            if inject_context and not any(
                isinstance(m, HumanMessage) and
                getattr(m, "additional_kwargs", {}).get(NO_SESSION_MARKER)
                for m in messages
            ):
                inject_ctx_msg = HumanMessage(
                    content=(
                        f"## Injection Phase Context (EXPIRED — fault-state data, NOT current)\n"
                        f"The following context was captured during fault injection. "
                        f"Use this ONLY to understand what fault was injected.\n"
                        f"⚠️ This data is STALE — it does NOT represent the current post-recovery state.\n"
                        f"DO NOT use injection-phase kubectl outputs as 'current' evidence.\n"
                        f"You MUST re-execute kubectl commands to obtain CURRENT observations.\n\n"
                        f"{inject_context}\n\n"
                    ),
                    additional_kwargs={NO_SESSION_MARKER: True},
                )
                messages.append(inject_ctx_msg)

            # Build instructions section (skill-first strategy)
            # P1-4: Only inject recovery verification section + cross-referenced
            # 注入验证 steps, not the entire skill case file. Reduces HumanMessage
            # size by 70-75% while preserving all actionable content.
            skill_case = state.get("skill_case_content", "")
            if skill_case:
                # Extract only the 恢复验证 section + cross-references
                recovery_section = _extract_recovery_verification_section(skill_case)
                # Count expected steps from the extracted section
                expected_steps = _count_recovery_steps_in_skill_case(skill_case)
                step_hint = ""
                if expected_steps > 0:
                    step_hint = (
                        f"\n**Expected Verification Steps**: {expected_steps} "
                        f"recovery verification step(s). Your RECOVERY_VERIFICATION_CHECKLIST MUST have "
                        f"at least {expected_steps} items.\n"
                    )
                if recovery_section:
                    instructions_section = (
                        f"\n## Recovery Verification Instructions\n"
                        f"Follow the recovery verification approach below as the primary reference.\n\n"
                        f"<recovery-verification>\n{recovery_section}\n</recovery-verification>\n\n"
                        f"1. Follow the **恢复验证** section above exactly. "
                        f"Execute every verification step it specifies.\n"
                        f"2. If a step cannot be executed, note it and design an equivalent check.\n"
                        f"3. If ALL steps pass, conclude Layer2 as 'passed'.\n"
                        f"4. If ANY step fails, conclude accordingly.\n"
                        f"5. You MUST produce a RECOVERY_VERIFICATION_CHECKLIST with one item per step.\n"
                        f"{step_hint}\n"
                    )
                else:
                    # Fallback: inject full content if extraction failed
                    instructions_section = (
                        f"\n## Recovery Verification Instructions\n"
                        f"Follow the **恢复验证** section in the skill case as the primary reference.\n\n"
                        f"<skill-case>\n{skill_case}\n</skill-case>\n\n"
                        f"1. Follow the **恢复验证** section exactly.\n"
                        f"2. If a step cannot be executed, note it and design an equivalent check.\n"
                        f"3. You MUST produce a RECOVERY_VERIFICATION_CHECKLIST with one item per step.\n"
                        f"{step_hint}\n"
                    )
            else:
                instructions_section = (
                    "\n## Recovery Verification Instructions\n"
                    "No skill use-case content is available. You MUST use `read_skill_resource` "
                    "to try to load the recovery verification instructions, OR design verification based on "
                    "the fault type and your experience.\n"
                    "**WARNING**: Without skill guidance, verification may be incomplete. "
                    "At minimum, you MUST verify the fault effect has been removed from the target.\n"
                    "**Knowledge docs**: Check the Domain Knowledge Index for documents whose "
                    "\"When to read\" field covers your current scenario (e.g., recovery verification, "
                    "kubectl field reference). Use `read_knowledge_resource` to "
                    "load them before designing your verification plan.\n"
                    "**CHECKLIST REQUIRED**: Even without skill guidance, you MUST produce a "
                    "RECOVERY_VERIFICATION_CHECKLIST covering each aspect you verify. "
                    "This ensures recovery completeness is tracked.\n\n"
                )

            # Build Layer 1 context section (adapted for non-ChaosBlade vs ChaosBlade)

            if layer1.status == "skipped":
                # No recovery actions found — Layer 1 not applicable
                if _was_kubectl_blade_injection_successful(state.get("messages", [])):
                    layer1_context = (
                        "## Layer 1 Result\n"
                        "Layer 1 skipped: ChaosBlade experiment was created via kubectl exec and "
                        "recovery is being handled through the LLM-driven recovery flow.\n\n"
                    )
                    layer2_instruction = (
                        "This is a ChaosBlade fault that was injected via kubectl exec. "
                        "Verify the fault effect has been removed using kubectl tools.\n"
                    )
                else:
                    layer1_context = (
                        "## Layer 1 Result\n"
                        "Layer 1 skipped: non-ChaosBlade fault with no recovery actions in skill files. "
                        "Proceed directly to Layer 2 recovery verification.\n\n"
                    )
                    layer2_instruction = (
                        "This is a non-ChaosBlade fault recovery. "
                        "Verify the fault effect has been removed using kubectl tools.\n"
                    )
            elif not _layer1_is_deterministic:
                # kubectl exec injection or non-ChaosBlade: Layer 1 executed recovery via LLM
                _is_kubectl_blade = _was_kubectl_blade_injection_successful(state.get("messages", []))
                fault_type_desc = (
                    "ChaosBlade fault (injected via kubectl exec)"
                    if _is_kubectl_blade
                    else "non-ChaosBlade fault"
                )
                layer1_context = (
                    f"## Layer 1 Result (Recovery Execution)\n"
                    f"This is a {fault_type_desc}. Recovery actions executed: {layer1.status}\n"
                    f"Details: {layer1.details}\n\n"
                )
                layer2_instruction = (
                    "PHASE TRANSITION: Layer 1 (recovery execution) is COMPLETE. "
                    "You are now in Layer 2 (VERIFICATION). "
                    "DO NOT execute more recovery actions — only VERIFY the fault effect is removed. "
                    "Use kubectl only to CHECK status, not to modify resources. "
                    "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
                )
            else:
                # ChaosBlade: Layer 1 executed blade_destroy
                layer1_context = (
                    f"## Layer 1 Result (already completed)\n"
                    f"blade_destroy for UID {blade_uid}: {layer1.status}\n"
                    f"Details: {layer1.details}\n"
                    f"Raw output: {layer1.raw_output[:500]}\n\n"
                )
                layer2_instruction = (
                    "PHASE TRANSITION: Layer 1 PASSED (blade_destroy reported success and blade_status confirms Destroyed). "
                    "You are now in Layer 2 (VERIFICATION). "
                    "Verify the fault effect has ACTUALLY been removed from the target's runtime state. "
                    "Use kubectl tools to check the target resource. "
                    "Output RECOVERY_VERIFICATION_RESULT format, NOT RECOVERY_EXECUTION_RESULT.\n"
                )

            context = (
                f"{layer1_context}"
                f"## Fault Context\n"
                f"Skill: {skill_name}\n"
                f"Target namespace: {target.get('namespace', '')}\n"
                f"Target names: {target.get('names', [])}\n"
                f"Kubeconfig: {kubeconfig or '(default)'}\n"
            )
            # Structured key parameters from parsed flags (e.g. path, percent, size)
            _blade_parsed = state.get("blade_parsed_flags") or {}
            if _blade_parsed:
                context += f"Blade key parameters: {_blade_parsed}\n"
            # Inline path semantics for node-disk recovery verification
            _blade_scope_rv = _spec_rvl2.scope if _spec_rvl2 else ""
            _blade_target_rv = _spec_rvl2.blade_target if _spec_rvl2 else ""
            if _blade_target_rv == "disk" and _blade_scope_rv == "node" and "path" in _blade_parsed:
                _path_val = _blade_parsed["path"]
                _path_norm = _path_val.rstrip("/")
                if _path_norm in _IMAGEFS_PATHS or any(
                    _path_norm.startswith(p.rstrip("/") + "/") for p in _IMAGEFS_PATHS
                ):
                    context += (
                        f"⚠ CRITICAL path semantics: --path {_path_val} in K8s CRD mode filled "
                        f"INSIDE the container overlay (typically backed by imagefs if the node has "
                        f"a separate imagefs; otherwise on nodefs), NOT the host path "
                        f"/host{_path_val}.\n"
                        f"COMMAND PRIORITY: Your FIRST disk check MUST be `df -h` (bare, no path argument) "
                        f"to identify ALL partitions. Verify with `df -h` (bare) inside kubectl debug "
                        f"to confirm imagefs usage has dropped. `df -h /host` shows nodefs ONLY — "
                        f"if fill targeted imagefs, it is irrelevant for recovery verification.\n"
                    )
                elif _path_norm in _NODEFS_PATHS or any(
                    _path_norm.startswith(p.rstrip("/") + "/") for p in _NODEFS_PATHS
                ):
                    context += (
                        f"⚠ CRITICAL path semantics: --path {_path_val} in K8s CRD mode filled "
                        f"typically the nodefs (root filesystem). NOTE: /var/lib/docker and "
                        f"/var/lib/containerd on a separate disk define imagefs — if this node has "
                        f"a separate imagefs, this path may have been on imagefs instead.\n"
                        f"COMMAND PRIORITY: Use `df -h` (bare) first to list all partitions, then "
                        f"`df -h /host` to confirm nodefs recovery. `df -h /host` shows the root partition usage.\n"
                    )
                else:
                    context += (
                        f"⚠ CRITICAL path semantics: --path {_path_val} — unable to determine "
                        f"target partition automatically. YOU MUST use `df -h` (bare, no path) to list "
                        f"ALL mounted filesystems and identify which partition shows decreased usage.\n"
                    )
            # Dynamic parameter-dependent hints (e.g., partition-aware verification for disk fill)
            _compound_key_rv = (
                _spec_rvl2.blade_target if _spec_rvl2 else "",
                _spec_rvl2.blade_action if _spec_rvl2 else "",
            )
            _generator_rv = _PARAM_HINT_GENERATORS.get(_compound_key_rv)
            if _generator_rv and _blade_parsed:
                _dynamic_hint_rv = _generator_rv(_blade_parsed)
                if _dynamic_hint_rv:
                    context += f"\n{_dynamic_hint_rv}\n"
            # Timeout info: simplified informational note
            _timeout_val_rv = _blade_parsed.get("timeout")
            if _timeout_val_rv:
                try:
                    _timeout_sec_rv = int(str(_timeout_val_rv).strip())
                    if _timeout_sec_rv < 600:
                        context += (
                            f"ℹ Duration note: Original --timeout was {_timeout_sec_rv}s. "
                            f"The fault may have already auto-expired before manual recovery. "
                            f"If recovery verification shows no residual fault effects, this is expected.\n"
                        )
                except (ValueError, TypeError):
                    pass
            if kubeconfig:
                context += (
                    f"**IMPORTANT**: You MUST pass `kubeconfig='{kubeconfig}'` to EVERY "
                    f"kubectl tool call. The default kubeconfig cannot access this cluster. "
                    f"Do NOT omit the kubeconfig parameter.\n"
                )
            # Tool pod context: provide accurate information about tool pod capabilities
            _blade_scope = _spec_rvl2.scope if _spec_rvl2 else ""
            _blade_target = _spec_rvl2.blade_target if _spec_rvl2 else ""
            _blade_action = _spec_rvl2.blade_action if _spec_rvl2 else ""
            _tool_pod_name = state.get("kubectl_exec_pod_name")
            if _blade_scope == "node" and _tool_pod_name:
                context += (
                    f"\n## Available Tool Pod\n"
                    f"A tool pod is available for cluster-level operations:\n"
                    f"- Pod name: `{_tool_pod_name}`\n"
                    f"- Namespace: `chaosblade`\n"
                    f"- Capabilities: ChaosBlade commands, kubectl API checks\n"
                    f"- LIMITATION: This pod does NOT mount /host. `df -h` shows overlay, NOT host disk.\n"
                    f"  For host filesystem verification, use kubectl debug "
                    f"('node/<node_name> --image=busybox -- sleep 3600').\n"
                )
                if blade_uid:
                    context += (
                        f"- **UID Dual Mapping**: The blade_uid ({blade_uid}) is the CRD resource name. "
                        f"Inside the tool pod, `blade status <uid>` searches the LOCAL experiment database "
                        f"and will likely return 'record not found' (because the experiment was created "
                        f"via CRD, not via the local CLI). "
                        f"**CORRECT**: `blade query k8s create <uid>` — queries the CRD status via API server. "
                        f"**CORRECT**: `kubectl describe chaosblade <uid>` — checks the CRD directly. "
                        f"**FORBIDDEN**: `blade status <uid>` inside a tool pod — will return 'record not found' "
                        f"and cause false failure. NEVER use this command for CRD-mode experiments.\n"
                    )
            # Multi-disk topology hints for node-disk scenarios
            if _blade_target == "disk" and _blade_scope == "node":
                from chaos_agent.agent.nodes._verifier_shared import _get_node_disk_topology_hints
                context += f"\n{_get_node_disk_topology_hints(_blade_action)}\n"
            # Injection verification baseline (from inject phase Layer 2 observations)
            inject_summary = state.get("inject_verification_summary", "")
            if inject_summary:
                context += (
                    f"\n## Injection Verification Baseline (for comparison — NOT current state)\n"
                    f"During the injection phase, the following was observed when the fault was active:\n"
                    f"{inject_summary}\n\n"
                    f"Compare your CURRENT kubectl observations against this. "
                    f"If the current state matches what was observed during injection, the fault "
                    f"has NOT been recovered.\n"
                    f"**Baseline integrity**: Ensure you compare metrics from the SAME resource "
                    f"(same partition, same node, same pod). See BASELINE INTEGRITY rules below.\n"
                )
            # Baseline data is now injected as synthetic AIMessage+ToolMessage pairs
            # (via _build_recover_baseline_tool_messages) BEFORE the main
            # HumanMessage, instead of as plain-text inside HumanMessage.
            # Same pattern as verifier.py inject verifier — see academic
            # basis there (Lost in the Middle, TIM-PRM, VERITAS).
            # Instructions are injected UNCONDITIONALLY (regardless of
            # baseline availability) — they contain polling strategy and
            # verification method guidance that the LLM always needs.
            context += (
                f"{instructions_section}\n"
                f"{layer2_instruction}"
                    "For example, if CPU stress was injected, use kubectl(subcommand='exec', ...) to run `top -bn1` inside the pod "
                    "or kubectl(subcommand='describe', ...) to check Pod restart count and conditions.\n"
                    "**POLLING STRATEGY**: Fault recovery may have a short delay before effects fully clear. Follow this approach:\n"
                    "  - Perform your first verification check. If it clearly shows the fault has fully cleared "
                    "(e.g., CPU back to baseline, pod running normally, disk usage back to normal), "
                    "wait ~10 seconds and do ONE more confirmation check.\n"
                    "  - If the confirmation check also shows recovery, output your RECOVERY_VERIFICATION_RESULT immediately — no further checks needed.\n"
                    "  - If the first check shows the fault is still present, wait ~10 seconds and check again. "
                    "Repeat every ~10 seconds until recovery is confirmed.\n"
                    "  - If the fault persists after multiple checks (approaching the iteration limit), conclude based on overall trend.\n"
                    "**ADAPTIVE VERIFICATION PRINCIPLE**:\n"
                    "If the SAME verification command produces the SAME result twice:\n"
                    "1. STOP repeating — the result will not change with a 3rd attempt.\n"
                    "2. ASK yourself: Is there a DIFFERENT metric, partition, resource, or "
                    "namespace I should be checking instead?\n"
                    "3. EXAMPLES of strategy pivots:\n"
                    "   - Disk: df showed no change on one partition → check OTHER partitions "
                    "(overlay vs root), or check node conditions (DiskPressure) instead\n"
                    "   - CPU: top showed no change → check cgroups, or check application latency\n"
                    "   - Network: curl succeeded → check different endpoints, or check packet loss "
                    "with a different tool\n"
                    "4. Maximum 2 identical checks per verification method — then SWITCH to a "
                    "different approach. Repeating a non-productive command wastes verification "
                    "budget and delays detection of the real issue.\n"
                    "\n**Baseline Comparison**: Apply Baseline Comparison Rules from your system prompt when comparing metrics.\n"
                    "**Layer 1 Limitation**: Layer 1 confirms blade_destroy succeeded — this does NOT prove the fault effect is gone. "
                    "Your Layer 2 kubectl observations are the ONLY way to confirm actual recovery.\n"
                    "**Minimal containers**: If kubectl exec returns 'command not found', use kubectl describe instead.\n"
            )
            messages.append(HumanMessage(
                content=context,
                additional_kwargs={_RECOVER_CONTEXT_KWARGS_KEY: True},
            ))

        # Convergence hint (reworded to avoid contradicting "must execute kubectl" rule)
        if count >= 4:
            messages.append(HumanMessage(content=(
                "You have gathered sufficient CURRENT (post-recovery) evidence across multiple iterations. "
                "If your kubectl observations clearly show recovery, output RECOVERY_VERIFICATION_RESULT now. "
                "Do NOT repeat the same check — conclude based on evidence already collected in THIS Layer 2 iteration."
            )))
        if count >= settings.max_recover_verifier_loop - 1:
            messages.append(HumanMessage(content=(
                f"**RECOVERY VERIFICATION DEADLINE**: This is iteration {count} of max {settings.max_recover_verifier_loop}.\n"
                f"Based on ALL evidence gathered so far:\n"
                f"  - If you have sufficient data, output the RECOVERY_VERIFICATION_RESULT format NOW.\n"
                f"  - This is your last chance to use tools — on the next iteration tools will be unavailable.\n\n"
                f"Your Overall conclusion must be one of:\n"
                f"  - **recovered**: Fault effect has been removed, target is back to normal\n"
                f"  - **unrecovered**: Fault effect is STILL present despite recovery attempt\n"
            )))

        # Per-iteration kubeconfig reminder (first Layer 2 iteration already has it in the main context)
        if state.get("layer2_context_added", False) and kubeconfig:
            messages.append(HumanMessage(content=(
                f"**Reminder**: You MUST pass kubeconfig='{kubeconfig}' to every kubectl tool call."
            )))

        # Final-iteration conclusion prompt (tools already unbound at max-1)
        if count >= settings.max_recover_verifier_loop:
            layer1_label = "blade_destroy" if blade_uid else "recovery execution"
            messages.append(HumanMessage(content=(
                f"**FINAL RECOVERY VERIFICATION ITERATION**: This is iteration {count} of max {settings.max_recover_verifier_loop}. "
                f"NO more iterations available. Tools are no longer available.\n"
                f"You MUST provide your final recovery verification conclusion NOW in this EXACT format:\n\n"
                f"RECOVERY_VERIFICATION_RESULT:\n"
                f"- Layer1 ({layer1_label}): passed\n"
                f"- Layer2 (fault-specific): [passed/failed/skipped] - [details with evidence summary]\n"
                f"- BaselineUsed: [true/false]\n"
                f"- Overall: [recovered/unrecovered]\n"
                f"- Warnings: [any warnings, or \"none\"]\n\n"
                f"If you cannot determine the result, set Overall to \"unrecovered\" and explain why in Layer2 details."
            )))

        # Bind tools for LLM (unbind one iteration early to force summary)
        if count >= settings.max_recover_verifier_loop - 1:
            llm_to_call = llm
        elif is_first_layer2 and tools:
            # P0-4: Force at least one tool call on the first Layer 2 iteration.
            # Prevents "lazy verification" (LLM outputs conclusion without
            # executing any kubectl commands). This complements the prompt-level
            # CRITICAL RULE 1 ("Execute kubectl to observe CURRENT state").
            #
            # NOTE: DashScope's OpenAI-compatible endpoint only supports
            # tool_choice "none" and "auto" — it rejects "required"/"any".
            # We use "auto" here, relying on the prompt-level rule to ensure
            # the LLM calls at least one tool. The programmatic guarantee is
            # weakened compared to OpenAI's "required", but this is an API
            # constraint we cannot bypass.
            llm_to_call = llm.bind_tools(tools)  # tool_choice defaults to "auto"
        else:
            llm_to_call = llm.bind_tools(tools) if tools else llm

        # Extract synthetic AIMessage+ToolMessage pairs from the local messages
        # list for state persistence. On count==1, prepend them to
        # result_update["messages"] BEFORE the response so that
        # state["messages"][-1] remains the real AIMessage (routing-safe).
        _synthetic_for_state = extract_synthetic_messages(messages, _RECOVER_SYNTHETIC_TOOL_CALL_IDS)

        # Extract the main recover context HumanMessage for state persistence.
        _main_hm_for_state = extract_persistent_hm(messages, state, _RECOVER_CONTEXT_KWARGS_KEY)

        system_prompt = _build_recover_verifier_prompt(is_chaosblade=_layer1_is_deterministic)

        # Record system prompt to session store (dedup handles repeated prompts)
        record_system_prompt(hook, state, system_prompt)

        response = await llm_to_call.ainvoke(
            [SystemMessage(content=system_prompt)] + messages
        )

        # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
        # has the correct kubeconfig, even if the LLM forgot to include it.
        inject_kubeconfig_into_tool_calls(response, kubeconfig)

        # Build result
        result_update = {
            "verifier_loop_count": count,
            "recover_layer1_cache": _recover_layer1_to_dict(layer1),  # persist for subsequent iterations
            "layer2_context_added": True,  # mark Layer 2 context as built
        }

        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            # LLM wants to call tools — continue ReAct loop
            # Persist inject_ctx_msg to state for ChaosBlade faults (non-ChaosBlade already has it from Layer 1)
            # Synthetic messages prepend BEFORE inject_ctx_msg and response so
            # routing checks state[-1] = response (real AIMessage).
            result_update["messages"] = _main_hm_for_state + _synthetic_for_state + ([inject_ctx_msg] if inject_ctx_msg else []) + [response]

            # Emit debug-level status event with LLM reasoning summary
            if settings.is_debug:
                debug_info, tool_names = summarize_llm_response(response)
                tracker.update(
                    f"Recover Layer 2 iteration {count} LLM:\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": tool_names},
                )
            else:
                tool_names = [
                    extract_tool_call_fields(tc)[0]
                    for tc in tool_calls
                ]
                tracker.update(
                    f"Recover Layer 2 iteration {count}: calling tools",
                    {"iteration": count, "tool_calls": tool_names},
                )
        else:
            # LLM produced final text — parse verification result
            content = getattr(response, "content", "") or ""

            # Programmatic guard: reject conclusions without verification commands
            # on the FIRST Layer 2 iteration. LLM must execute at least one kubectl
            # command to observe the CURRENT post-recovery state. Using baseline
            # data (pre-injection) as "post-recovery" evidence is INVALID.
            if is_first_layer2:
                # Check if ANY kubectl/blade tool call was executed in Layer 2.
                # Layer 1's tool calls (blade destroy, kubectl get pods) are in
                # state.messages, so we need to distinguish them. The simplest
                # heuristic: if is_first_layer2 AND no tool_calls in this response,
                # the LLM skipped verification entirely.
                logger.warning(
                    f"Recover Layer 2 first iteration produced conclusion without "
                    f"executing any verification commands for task {task_id}. "
                    f"Forcing re-verification."
                )
                tracker.update(
                    "Layer 2 first iteration: conclusion without verification commands — forcing re-check",
                    {"iteration": count, "guard": "no_verification_commands"},
                )
                # Inject a mandatory verification prompt and continue the loop
                # instead of accepting the conclusion.
                result_update["messages"] = _main_hm_for_state + _synthetic_for_state + ([inject_ctx_msg] if inject_ctx_msg else []) + [response, HumanMessage(content=(
                    "⚠️ Your verification conclusion was rejected because you did NOT execute "
                    "any kubectl verification commands in this Layer 2 iteration. "
                    "The baseline data provided earlier was captured BEFORE fault injection — "
                    "it is NOT the current post-recovery state.\n\n"
                    "You MUST now execute kubectl commands (e.g., kubectl exec to check disk usage, "
                    "kubectl describe to check pod status) to observe the CURRENT state of the target. "
                    "Only then can you output a valid RECOVERY_VERIFICATION_RESULT.\n\n"
                    "Do NOT output RECOVERY_VERIFICATION_RESULT again until you have executed at "
                    "least one verification command and observed the CURRENT state."
                ))]
                await sync_to_store(state, result_update)
                return result_update

            # Emit RUNNING event for the final reasoning before completing
            if settings.is_debug:
                debug_info, _ = summarize_llm_response(response)
                tracker.update(
                    f"Recover Layer 2 iteration {count} LLM (final):\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": []},
                )
            else:
                tracker.update(
                    f"Recover Layer 2 iteration {count}: producing final verification",
                    {"iteration": count, "tool_calls": []},
                )

            verification = _parse_recovery_verification_result(content, skill_name=skill_name)
            verification["layer1"] = _recover_layer1_to_dict(layer1)

            # Baseline confidence fallback + Programmatic Fact Enforcement
            if "baseline_confidence" not in verification:
                verification["baseline_confidence"] = _compute_baseline_confidence(state)
            if verification.get("baseline_confidence") == "high" and not verification.get("baseline_used"):
                verification.setdefault("warnings", []).append(
                    "Pre-injection baseline was available (confidence=high) but LLM "
                    "did not perform baseline comparison. Verification relies on "
                    "absolute thresholds instead of more reliable before/after delta."
                )

            # If Layer 2 says fault still active, retry recovery ONCE (only on first failure)
            # to handle ChaosBlade's known behavior where blade_destroy returns success
            # but the stress process may not be actually killed.
            # For non-ChaosBlade faults, inject a recovery retry prompt instead of blade_destroy.
            l2_status = verification.get("layer2", {}).get("status", "unknown")
            already_retried = any(
                isinstance(m, HumanMessage) and "recovery retry" in (getattr(m, "content", "") or "")
                for m in state.get("messages", [])
            )
            if l2_status == "failed" and not already_retried and count < settings.max_recover_verifier_loop - 1:
                if blade_uid and _layer1_is_deterministic:
                    # ChaosBlade path: retry blade_destroy on host
                    logger.warning(
                        f"Layer 2 detected fault still active for task {task_id}, "
                        f"retrying blade_destroy (uid={blade_uid})"
                    )
                    tracker.update(
                        "Layer 2 detected fault still active, retrying blade_destroy",
                        {"retry": True, "blade_uid": blade_uid},
                    )
                    try:
                        from chaos_agent.tools.blade import blade_destroy as _blade_destroy
                        retry_output = await _blade_destroy.ainvoke(
                            {"uid": blade_uid, "kubeconfig": kubeconfig}
                        )
                        retry_raw = retry_output if isinstance(retry_output, str) else str(retry_output)
                        logger.info(f"blade_destroy retry output: {retry_raw[:200]}")
                        # Inject retry result as a HumanMessage for the next iteration
                        result_update["messages"] = _main_hm_for_state + _synthetic_for_state + ([inject_ctx_msg] if inject_ctx_msg else []) + [HumanMessage(content=(
                            f"**recovery retry executed**\n"
                            f"blade_destroy output: {retry_raw[:500]}\n\n"
                            f"Please verify again whether the fault has been removed."
                        ))]
                        # Don't finalize — let the next iteration re-verify
                        await sync_to_store(state, result_update)
                        return result_update
                    except Exception as retry_err:
                        logger.warning(f"blade_destroy retry failed: {retry_err}")
                else:
                    # Non-ChaosBlade path: inject recovery retry prompt for LLM
                    logger.warning(
                        f"Layer 2 detected fault still active for task {task_id}, "
                        f"injecting recovery retry prompt (non-ChaosBlade, no blade_uid)"
                    )
                    tracker.update(
                        "Layer 2 detected fault still active, injecting recovery retry prompt",
                        {"retry": True, "blade_uid": blade_uid},
                    )
                    result_update["messages"] = _main_hm_for_state + _synthetic_for_state + ([inject_ctx_msg] if inject_ctx_msg else []) + [HumanMessage(content=(
                        "**recovery retry required**: The fault effect is STILL PRESENT.\n"
                        "Please re-attempt recovery using alternative methods. "
                        "For example, if kubectl(subcommand=\"patch\") failed, try kubectl(subcommand=\"delete\") with --force --grace-period=0. "
                        "If one approach didn't work, try a different programmatic approach.\n\n"
                        "After re-attempting recovery, verify again whether the fault has been removed."
                    ))]
                    await sync_to_store(state, result_update)
                    return result_update

            result = {
                "task_id": task_id,
                "skill": skill_name,
                "blade_uid": blade_uid,
                "recovered": verification["level"] in ("recovered", "partial"),
                "recovery_level": verification["level"],
            }
            result_update["result"] = result
            result_update["recover_verification"] = verification
            result_update["finished_at"] = now_iso()
            result_update["messages"] = _main_hm_for_state + _synthetic_for_state + [response]

            # Set failure_reason when recovery is not confirmed
            if not result["recovered"]:
                base = (
                    f"{FailureReason.RECOVERY_FAILED.value}: "
                    f"Layer1={layer1.status}, Layer2={l2_status}, level={verification['level']}"
                )
                result_update["failure_reason"] = base

            level = verification["level"]
            l1_status = layer1.status
            warnings = verification.get("warnings", [])
            status_msg = f"Recovery verification: {level} (Layer1: {l1_status}, Layer2: {l2_status})"
            if warnings:
                status_msg += f" (warnings: {len(warnings)})"
            tracker.complete(status_msg)

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result_update, hook_updates)

        # ---- Programmatic Debug Pod Cleanup ----
        # Same pattern as verifier.py: scan ToolMessages for kubectl debug pod
        # names and delete them deterministically.
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
        return result_update

    return _recover_verifier_with_llm
