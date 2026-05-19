"""Agent loop node: Phase 1 ReAct planning (skill activation + target verification + plan generation)."""

import json
import logging
import shlex

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.env_info import compute_env_info
from chaos_agent.agent.nodes._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
)
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.nodes.react_helpers import (
    detect_repeated_tool_calls,
    emit_debug_tool_messages,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    summarize_llm_response,
)
from chaos_agent.agent.prompts import (
    build_system_prompt,
    PromptMode,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.errors import FailureReason, enrich_failure_reason
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


def _extract_target_from_kubectl_get(
    v_args: str,
    existing_target: dict | None,
    state_target: dict | None,
    blacklist: list[str],
) -> dict:
    """Extract target info (namespace, labels, resource_type) from a kubectl get call.

    Uses write-once semantics: each field is only set if not already present
    in *existing_target*.  Additionally, namespaces in *blacklist* are never
    set (auxiliary queries to kube-system etc. must not pollute the target).

    Returns the updated target dict (may be empty if nothing was extracted).
    """
    target = dict(existing_target or {})
    v_parts = _split_args(v_args)

    # Parse namespace: -n <ns> / --namespace <ns> / --namespace=<ns>
    for i, p in enumerate(v_parts):
        ns_candidate = None
        if p in ("-n", "--namespace") and i + 1 < len(v_parts):
            ns_candidate = v_parts[i + 1]
        elif p.startswith("--namespace="):
            ns_candidate = p.split("=", 1)[1]
        if ns_candidate and not target.get("namespace") and ns_candidate not in blacklist:
            target["namespace"] = ns_candidate

    # Parse label selector: -l <sel> / --selector <sel> / --selector=<sel>
    for i, p in enumerate(v_parts):
        label_candidate = None
        if p in ("-l", "--selector") and i + 1 < len(v_parts):
            label_candidate = v_parts[i + 1]
        elif p.startswith("--selector="):
            label_candidate = p.split("=", 1)[1]
        if label_candidate and not target.get("labels"):
            target["labels"] = label_candidate

    # Determine resource_type from first non-flag token
    for p in v_parts:
        if not p.startswith("-"):
            if not target.get("resource_type"):
                if p in ("nodes", "node", "no"):
                    target["resource_type"] = "node"
                elif p in ("pods", "pod", "po"):
                    target["resource_type"] = "pod"
            break

    # Preserve namespace from existing state for cluster-scoped queries
    if not target.get("namespace"):
        existing_ns = (state_target or {}).get("namespace", "")
        if existing_ns:
            target["namespace"] = existing_ns

    return target


def _split_args(args: str) -> list[str]:
    """Split args string respecting shell quoting.

    Uses shlex.split to properly handle quoted arguments.
    Falls back to str.split() if shlex encounters unmatched quotes.
    """
    if not args:
        return []
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


MAX_AGENT_LOOP = settings.max_agent_loop


async def agent_loop(state: AgentState) -> dict:
    """Phase 1: ReAct loop for planning.

    The LLM analyzes the request, activates the appropriate skill,
    verifies the target, and generates an execution plan.

    Returns updated state fields.
    """
    task_id = state.get("task_id", "unknown")
    count = state.get("agent_loop_count", 0) + 1
    skill_name = state.get("skill_name", "")

    # Emit status event
    tracker = get_tracker(task_id)
    if skill_name:
        tracker.start(
            StatusCategory.NODE,
            "agent_loop",
            f"Agent loop iteration {count}: thinking with skill '{skill_name}'",
            {"iteration": count, "skill_name": skill_name},
        )
    else:
        tracker.start(
            StatusCategory.NODE,
            "agent_loop",
            f"Agent loop iteration {count}: deep thinking and planning...",
            {"iteration": count},
        )

    if count > MAX_AGENT_LOOP:
        logger.warning(
            f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP}) for task "
            f"{task_id}"
        )
        tracker.fail(f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP})")
        return {
            "error": f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP})",
            "safety_status": "rejected",
            "failure_reason": f"{FailureReason.PLANNING_TIMEOUT.value}: Agent loop exceeded max iterations ({MAX_AGENT_LOOP})",
        }

    # The actual LLM reasoning is handled by LangGraph's ReAct pattern
    # This node just tracks the iteration count
    tracker.complete(f"Agent loop iteration {count} done")
    return {"agent_loop_count": count}


def make_agent_loop(hook=None, llm=None, tools=None, skill_catalog: str = ""):
    """Create an agent_loop node with optional PreReasoningHook and LLM.

    When llm is provided, the node performs actual LLM reasoning
    (calling the model with bound tools, returning the response as a message).
    When llm is None, behaves identically to the plain agent_loop
    (only tracks iteration count, for test compatibility).
    """
    if llm is None and hook is None:
        return agent_loop

    async def _agent_loop_with_llm(state: AgentState) -> dict:
        # 1. Iteration count + limit check (original logic preserved)
        task_id = state.get("task_id", "unknown")
        count = state.get("agent_loop_count", 0) + 1
        skill_name = state.get("skill_name", "")

        # --- Replan entry detection ---
        replan_context = state.get("replan_context")
        replan_history = state.get("replan_history")
        is_replan = replan_context is not None and state.get("replan_count", 0) > 0

        if is_replan:
            # Reset agent_loop_count for fresh planning budget
            count = 1

        tracker = get_tracker(task_id)
        if skill_name:
            tracker.start(
                StatusCategory.NODE,
                "agent_loop",
                f"Agent loop iteration {count}: thinking with skill '{skill_name}'",
                {"iteration": count, "skill_name": skill_name},
            )
        else:
            tracker.start(
                StatusCategory.NODE,
                "agent_loop",
                f"Agent loop iteration {count}: deep thinking and planning...",
                {"iteration": count},
            )

        if count > MAX_AGENT_LOOP:
            logger.warning(
                f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP}) for task "
                f"{task_id}"
            )
            tracker.fail(f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP})")
            base = f"{FailureReason.PLANNING_TIMEOUT.value}: Agent loop exceeded max iterations ({MAX_AGENT_LOOP})"
            result = {
                "error": f"Agent loop exceeded max iterations ({MAX_AGENT_LOOP})",
                "safety_status": "rejected",
                "failure_reason": enrich_failure_reason(base, state.get("messages", [])),
            }
            await sync_to_store(state, result)
            return result

        # 2. Call pre_reason_hook (memory compaction)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # 2b. Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state)

        # 3. Collect environment info and call LLM with bound tools
        if llm is not None:
            messages = list(state.get("messages", []))

            # Inject replan error context as HumanMessage
            if is_replan:
                error_msg = HumanMessage(content=(
                    f"[REPLAN CONTEXT] Phase 2 execution failed. Please analyze the error "
                    f"and generate a corrected plan.\n\n"
                    f"Error: {replan_context.get('error_summary', 'Unknown')}\n"
                    f"Failed tool calls: {json.dumps(replan_context.get('failed_tool_calls', []), ensure_ascii=False)}\n"
                    f"Existing blade UIDs: {replan_context.get('existing_blade_uids', [])}\n"
                ))
                messages = messages + [error_msg]

            # Collect env info (cached per task_id)
            env_info = await compute_env_info(task_id)

            # P1: Use build_system_prompt with PromptMode dispatch
            # PATD: skill index is now in stable section of system prompt;
            #       P2 tool_result injection removed (3× redundancy eliminated)
            system_prompt = build_system_prompt(
                PromptMode.FULL,
                skill_catalog=skill_catalog,
                input_is_nl=bool(state.get("input")),
                env_info=env_info,
                replan_context=replan_context if is_replan else None,
                replan_history=replan_history if is_replan else None,
            )

            # PATD: P2 skill injection removed — skill index is now in the
            # stable section of system prompt (not a separate tool_result).
            # This eliminates the 3× redundancy (system prompt + P2 + docstring).
            _injections_for_state = []

            # --- Inject structured fault_intent from intent_clarification ---
            fault_intent = state.get("fault_intent")
            if count == 1 and fault_intent and not is_replan:
                fi_lines = [
                    "[FAULT INTENT — collected from user dialogue]",
                    f"Fault type: {fault_intent.get('fault_type', '?')}",
                    f"Scope: {fault_intent.get('scope', '?')}",
                    f"Target: {fault_intent.get('target', '?')}",
                    f"Action: {fault_intent.get('action', '?')}",
                    f"Namespace: {fault_intent.get('namespace', '?')}",
                ]
                if fault_intent.get("labels"):
                    fi_lines.append(f"Labels: {fault_intent['labels']}")
                if fault_intent.get("names"):
                    fi_lines.append(f"Names: {', '.join(fault_intent['names'])}")
                if fault_intent.get("params"):
                    fi_lines.append(f"Params: {json.dumps(fault_intent['params'], ensure_ascii=False)}")
                if fault_intent.get("user_description"):
                    fi_lines.append(f"User request: {fault_intent['user_description']}")
                fi_lines.append(
                    "\nProceed directly: activate the matching skill, verify the "
                    "target if not already verified, and generate your execution plan."
                )
                fi_msg = HumanMessage(content="\n".join(fi_lines))
                messages.append(fi_msg)
                _injections_for_state.append(fi_msg)

            # --- Repeated tool call detection (loop breaking) ---
            loop_hint = detect_repeated_tool_calls(messages)
            if loop_hint:
                messages.append(HumanMessage(content=loop_hint))

            # --- Convergence hints (planning conclusion prompts) ---
            # Aligned with execute_loop's 3-tier convergence system.
            # Without these, the LLM has no awareness of its iteration budget
            # and may loop indefinitely making tool calls.
            remaining = MAX_AGENT_LOOP - count
            if MAX_AGENT_LOOP - 5 <= count < MAX_AGENT_LOOP - 1:
                # Tier 1: Soft warning — iterations running low
                messages.append(HumanMessage(content=(
                    f"**Iteration Progress**: You are on iteration {count} of max "
                    f"{MAX_AGENT_LOOP} ({remaining} remaining). "
                    f"If you have already activated a skill and verified the target, "
                    f"output your FINAL planning summary now (pure text, no tool calls) "
                    f"so execution can begin. Do not repeat queries you have already made."
                )))
            elif count == MAX_AGENT_LOOP - 1:
                # Tier 2: Urgent warning — second-to-last iteration
                messages.append(HumanMessage(content=(
                    f"**CRITICAL WARNING**: This is iteration {count} of max "
                    f"{MAX_AGENT_LOOP} — your SECOND-TO-LAST iteration.\n"
                    f"If a skill is activated and you have gathered enough context:\n"
                    f"  - Output your final planning summary NOW (no tool calls).\n"
                    f"If you absolutely need one more piece of information:\n"
                    f"  - Make ONE final tool call — you MUST conclude on the next "
                    f"iteration.\n"
                    f"Do NOT repeat any previous queries."
                )))
            elif count >= MAX_AGENT_LOOP:
                # Tier 3: Final conclusion — tools will be unbound
                messages.append(HumanMessage(content=(
                    f"**FINAL ITERATION**: This is iteration {count} of max "
                    f"{MAX_AGENT_LOOP}. NO more iterations are available. "
                    f"Tools are no longer available.\n"
                    f"You MUST provide your final planning summary NOW:\n"
                    f"1. What skill was activated and what fault type is being injected\n"
                    f"2. What target was identified (namespace, resource, names)\n"
                    f"3. What execution steps should be followed\n\n"
                    f"Your response will be used to proceed to the execution phase."
                )))

            # On last iteration, unbind tools to force text conclusion
            if count >= MAX_AGENT_LOOP:
                llm_with_tools = llm
            else:
                llm_with_tools = llm.bind_tools(tools) if tools else llm

            # Record system prompt to session store (dedup handles repeated prompts)
            record_system_prompt(hook, state, system_prompt)

            response = await llm_with_tools.ainvoke(
                [SystemMessage(content=system_prompt)] + messages
            )
        else:
            response = None

        # 4. Build result
        result = {"agent_loop_count": count}

        # Reset safety_status for replan so safety_check re-evaluates the corrected plan
        if is_replan:
            result["safety_status"] = "pending"
            result["needs_confirmation"] = False
            # Clear replan_requested so Phase 2 doesn't immediately re-trigger
            result["replan_requested"] = False

        if response is not None:
            # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
            # has the correct kubeconfig, even if the LLM forgot to include it.
            kubeconfig = _resolve_kubeconfig(state)
            inject_kubeconfig_into_tool_calls(response, kubeconfig)

            result["messages"] = _injections_for_state + [response]

            # Immediately save AI message (including reasoning_content) to session
            record_ai_message(hook, state, response)

            # Diagnostic log for reasoning_content presence
            log_reasoning_content(response, "Agent loop", count)

            # Extract skill_name and target from tool calls
            tool_calls = getattr(response, "tool_calls", None) or []
            for tc in tool_calls:
                tc_name, tc_args = extract_tool_call_fields(tc)

                if tc_name == "activate_skill" and tc_args.get("skill_name"):
                    result["skill_name"] = tc_args["skill_name"]
                    logger.info(f"Skill activated: {tc_args['skill_name']}")

                # Extract target info from kubectl(subcommand="get") calls
                if tc_name == "kubectl" and tc_args.get("subcommand") == "get":
                    updated = _extract_target_from_kubectl_get(
                        v_args=tc_args.get("v_args", ""),
                        existing_target=result.get("target"),
                        state_target=state.get("target"),
                        blacklist=settings.blacklist_namespaces,
                    )
                    if updated:
                        result["target"] = updated

                # Extract plan from save_fault_plan calls
                if tc_name == "save_fault_plan":
                    plan_content = tc_args.get("plan_content", "")
                    if plan_content:
                        result["is_complex"] = True
                        result["plan"] = plan_content
                        logger.info(f"Fault plan generated ({len(plan_content)} chars)")

            # Emit debug-level status event with LLM reasoning summary
            if settings.is_debug:
                debug_info, tool_names = summarize_llm_response(response)
                tracker.update(
                    f"Iteration {count} LLM:\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": tool_names},
                )

        # Extract plan_path from save_fault_plan ToolMessage results
        if result.get("is_complex"):
            messages = state.get("messages", [])
            for msg in reversed(messages):
                if not isinstance(msg, ToolMessage):
                    continue
                msg_name = getattr(msg, "name", "") or ""
                if msg_name != "save_fault_plan":
                    continue
                content = msg.content if isinstance(msg.content, str) else ""
                # Response format: "Plan saved to /path/to/file\n\n..."
                if content.startswith("Plan saved to "):
                    first_line = content.split("\n")[0]
                    result["plan_path"] = first_line.replace("Plan saved to ", "").strip()
                    break

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result, hook_updates)

        tracker.complete(f"Agent loop iteration {count} done")
        await sync_to_store(state, result)
        return result

    return _agent_loop_with_llm
