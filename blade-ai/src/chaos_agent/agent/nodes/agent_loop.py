"""Agent loop node: Phase 1 ReAct planning (skill activation + target verification + plan generation)."""

import json
import logging
import shlex

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.env_info import compute_env_info
from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.nodes._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
)
from chaos_agent.agent.nodes._store_sync import sync_to_store
from chaos_agent.agent.nodes.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
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
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)


def _derive_spec_fields_from_kubectl_get(
    v_args: str,
    blacklist: list[str],
) -> dict:
    """Parse a ``kubectl get`` v_args string into FaultSpec field updates.

    Returns a dict of fields that can be passed to ``FaultSpec.replace(**dict)``.
    Each field is only included if successfully parsed; the caller
    applies write-once semantics (don't override a spec field that
    already has a value).

    What we parse:
      - ``namespace`` from ``-n NS`` / ``--namespace NS`` / ``--namespace=NS``
      - ``labels`` from ``-l selector`` / ``--selector selector`` (dict-typed,
        unlike the old code which stored raw str)
      - ``scope`` from the first non-flag positional (``pods`` / ``nodes`` / etc.)
      - ``names`` from positional 2+ (``kubectl get pod my-pod``)

    The previous code in this slot stored labels as a raw string and
    never extracted names — those gaps are what made the original
    NL-mode bug silent. Both are fixed here.

    Used by CLI NL path where ``intent_clarification`` doesn't run,
    so the spec must be built lazily from the LLM's planning actions.
    """
    updates: dict = {}
    v_parts = _split_args(v_args)

    # namespace
    for i, p in enumerate(v_parts):
        if p in ("-n", "--namespace") and i + 1 < len(v_parts):
            ns = v_parts[i + 1]
        elif p.startswith("--namespace="):
            ns = p.split("=", 1)[1]
        else:
            continue
        if ns and ns not in blacklist:
            updates["namespace"] = ns
            break

    # labels — parse selector into dict[str, str] (not the raw str
    # the old code stored, which violated the FaultSpec.labels contract)
    for i, p in enumerate(v_parts):
        if p in ("-l", "--selector") and i + 1 < len(v_parts):
            sel = v_parts[i + 1]
        elif p.startswith("--selector="):
            sel = p.split("=", 1)[1]
        else:
            continue
        parsed: dict = {}
        for piece in sel.split(","):
            piece = piece.strip()
            if "=" in piece:
                k, _, v = piece.partition("=")
                parsed[k.strip()] = v.strip()
        if parsed:
            updates["labels"] = parsed
        break

    # scope + names from positionals (skip flag tokens AND their values)
    _SCOPE_ALIASES = {
        "pods": "pod", "pod": "pod", "po": "pod",
        "nodes": "node", "node": "node", "no": "node",
        "deployments": "deployment", "deployment": "deployment", "deploy": "deployment",
        "services": "service", "service": "service", "svc": "service",
    }
    positionals: list[str] = []
    i = 0
    while i < len(v_parts):
        p = v_parts[i]
        if p in ("-n", "--namespace", "-l", "--selector",
                 "-o", "--output", "--field-selector"):
            i += 2  # skip flag + value
            continue
        if p.startswith("-"):
            i += 1
            continue
        positionals.append(p)
        i += 1
    if positionals:
        canonical = _SCOPE_ALIASES.get(positionals[0].lower())
        if canonical:
            updates["scope"] = canonical
            # positional[1:] are specific resource names
            names = [n for n in positionals[1:] if not n.startswith("-")]
            if names:
                updates["names"] = tuple(names)
    return updates


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

    # Patch C — stamp the wall-clock start once per inject turn. Done
    # at agent_loop entry (the earliest LLM-driven node) so subsequent
    # router checks have something to compare against. The 0.0
    # sentinel makes this idempotent for re-entry after replan.
    import time as _time
    if not state.get("pipeline_started_at"):
        # Caller will merge this into state via the returned dict;
        # in-place mutation here is also picked up by LangGraph because
        # AgentState is a TypedDict.
        state["pipeline_started_at"] = _time.time()

    # Patch E — record this as the first pipeline attempt the very
    # first time agent_loop runs in a turn. Subsequent re-entries
    # (graph replan / LLM target switch / user rerun) bump the
    # counter from their respective call sites; agent_loop only
    # owns the "initial" reason.
    if int(state.get("pipeline_attempt", 0) or 0) == 0:
        from chaos_agent.agent.attempt_tracker import (
            REASON_INITIAL,
            begin_attempt,
        )
        # attempt_tracker stores the target snapshot for audit; we
        # pass the FaultSpec dict (richer than the old legacy 4-field
        # target — includes blade_target/action/params so the history
        # entry can answer "what fault did attempt N try to inject").
        delta = begin_attempt(
            state,
            target=state.get("fault_spec"),
            reason=REASON_INITIAL,
        )
        state.update(delta)

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
            "safety_status": "rejected",
            **fail_state(FailureCategory.PLANNING_TIMEOUT, f"max_iterations={MAX_AGENT_LOOP}"),
        }

    # The actual LLM reasoning is handled by LangGraph's ReAct pattern
    # This node just tracks the iteration count
    tracker.complete(f"Agent loop iteration {count} done")
    # Patch C — annotate result with WALL_CLOCK_TIMEOUT cause if the
    # router is about to terminate due to the wall-clock budget. The
    # router itself can't write state (it's a pure routing function),
    # so the node must do the labelling on its way out.
    from chaos_agent.agent.router import mark_wall_clock_timeout
    return mark_wall_clock_timeout(state, {"agent_loop_count": count})


def make_agent_loop(hook=None, llm=None, tools=None, skill_catalog: str = "", registry=None):
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
            result = {
                "safety_status": "rejected",
                **fail_state(FailureCategory.PLANNING_TIMEOUT, f"max_iterations={MAX_AGENT_LOOP}", state.get("messages", [])),
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
        _resolved_use_case_path = None
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

            # --- Inject structured fault context from FaultSpec ---
            _spec = read_fault_spec(state)
            if count == 1 and _spec and not is_replan and _spec.is_complete:
                fi_lines = [
                    "[FAULT INTENT — collected from user dialogue]",
                    f"Fault type: {_spec.fault_type or '?'}",
                    f"Scope: {_spec.scope or '?'}",
                    f"Target: {_spec.blade_target or '?'}",
                    f"Action: {_spec.blade_action or '?'}",
                    f"Namespace: {_spec.namespace or '?'}",
                ]
                if _spec.labels:
                    fi_lines.append(f"Labels: {dict(_spec.labels)}")
                if _spec.names:
                    fi_lines.append(f"Names: {', '.join(_spec.names)}")
                if _spec.params:
                    fi_lines.append(f"Params: {json.dumps(dict(_spec.params), ensure_ascii=False)}")
                if _spec.user_description:
                    fi_lines.append(f"User request: {_spec.user_description}")

                _resolved_use_case_path = state.get("matched_use_case_path")
                if not _resolved_use_case_path and registry:
                    _resolved_use_case_path = registry.match_use_case(
                        _spec.scope, _spec.blade_target, _spec.blade_action,
                    )
                if _resolved_use_case_path:
                    _catalogue_dir = _resolved_use_case_path.rsplit("/", 1)[0] + "/"
                    fi_lines.append(
                        f"\nMatched catalogue directory: {_catalogue_dir}"
                        f"\nBest match: {_resolved_use_case_path}"
                        "\n→ List the directory with read_skill_resource to see all "
                        "available cases, then read the one that best matches "
                        "the user's scenario."
                    )
                    fi_lines.append(
                        "\nProceed: activate the matching skill, verify the "
                        "target if not already verified, and generate your execution plan."
                    )
                else:
                    fi_lines.append(
                        "\n⚠️ No matching catalogue case found for this fault type."
                        "\nYou MUST follow the discovery flow in SKILL.md: "
                        "use read_skill_resource to browse the catalogue, "
                        "locate a matching use-case, and load it."
                        "\nIf no match exists after discovery, inform the user "
                        "this scenario is not currently supported and STOP."
                    )
                fi_msg = HumanMessage(content="\n".join(fi_lines))
                messages.append(fi_msg)
                _injections_for_state.append(fi_msg)

            # --- Repeated tool call detection (loop breaking) ---
            loop_hint = detect_repeated_tool_calls(messages)
            if loop_hint:
                messages.append(HumanMessage(content=loop_hint))

            # --- Action stagnation detection (tool-name level, ignores args) ---
            stagnation_hint, stagnant_tool = detect_action_stagnation(messages)
            if stagnation_hint:
                messages.append(HumanMessage(content=stagnation_hint))

            # --- Tool error introspection (runtime feedback > static docs) ---
            error_hint = detect_tool_error_hint(messages)
            if error_hint:
                messages.append(HumanMessage(content=error_hint))

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
                    f"call `finish_planning` with a brief summary to proceed to execution. "
                    f"If you have determined the request cannot be fulfilled (safety violation, "
                    f"no matching use-case), call `finish_planning` with `rejected=True` "
                    f"and `rejection_reason`. "
                    f"Do not repeat queries you have already made."
                )))
            elif count == MAX_AGENT_LOOP - 1:
                # Tier 2: Urgent warning — second-to-last iteration
                messages.append(HumanMessage(content=(
                    f"**CRITICAL WARNING**: This is iteration {count} of max "
                    f"{MAX_AGENT_LOOP} — your SECOND-TO-LAST iteration.\n"
                    f"If a skill is activated and you have gathered enough context:\n"
                    f"  - Call `finish_planning` with a summary NOW (preferred).\n"
                    f"If the request is infeasible:\n"
                    f"  - Call `finish_planning(rejected=True, rejection_reason=\"...\")` now.\n"
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
                tools_this_iter = list(tools) if tools else []
                if stagnant_tool:
                    tools_this_iter = [
                        t for t in tools_this_iter
                        if getattr(t, "name", "") != stagnant_tool
                    ]
                llm_with_tools = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

            # Record system prompt to session store (dedup handles repeated prompts)
            record_system_prompt(hook, state, system_prompt)

            response = await llm_with_tools.ainvoke(
                [SystemMessage(content=system_prompt)] + messages
            )
        else:
            response = None

        # 4. Build result
        result = {"agent_loop_count": count, "planning_rejected": False}

        # Persist matched catalogue path for downstream nodes (verifier etc.)
        if _resolved_use_case_path:
            result["matched_use_case_path"] = _resolved_use_case_path

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

                # Lazy spec derivation from LLM's kubectl get probes.
                #
                # Why this exists: CLI NL mode (``blade-ai inject
                # --input "..."``) doesn't go through
                # intent_clarification (route_after_load_memory keys
                # on interaction_mode). The entry-point spec is a
                # placeholder with empty scope/names/namespace.
                # Without lazy derivation, safety_check rejects every
                # CLI NL turn with "No target specified".
                #
                # Why this is safe vs target_guard: this only fires
                # in the PLANNING phase (before confirmation_gate
                # freezes approved_target). target_guard's drift
                # protection runs in execute_loop (post-confirm),
                # where the spec is locked and any mid-loop change
                # to the approval gets caught.
                #
                # Write-once semantics: only fill fields the spec
                # is missing, never overwrite. If intent_clarification
                # already populated the spec (TUI path), this block
                # finds nothing to update and is effectively a no-op.
                # Catch ``get`` / ``describe`` / ``top`` — all three share
                # the same positional shape (``kind [name] -n ns -l sel``)
                # and the LLM uses any of them to probe. Without
                # ``describe`` / ``top`` here, CLI NL flows where the LLM
                # prefers ``kubectl describe pod foo`` over ``kubectl get
                # pod foo`` would never get namespace/names derived.
                if (
                    tc_name in ("kubectl", "kubectl_ro")
                    and tc_args.get("subcommand") in ("get", "describe", "top")
                ):
                    _spec_now = (
                        FaultSpec.from_dict(result["fault_spec"])
                        if "fault_spec" in result
                        else read_fault_spec(state)
                    )
                    if _spec_now is not None and not _spec_now.is_complete:
                        derived = _derive_spec_fields_from_kubectl_get(
                            v_args=tc_args.get("v_args", ""),
                            blacklist=settings.blacklist_namespaces,
                        )
                        # write-once: only fill missing fields
                        updates: dict = {}
                        for k, v in derived.items():
                            current = getattr(_spec_now, k, None)
                            if not current:
                                updates[k] = v
                        if updates:
                            new_spec = _spec_now.replace(**updates)
                            result["fault_spec"] = new_spec.to_dict()
                            logger.info(
                                "agent_loop: derived spec fields from "
                                "LLM kubectl get: %s", updates,
                            )

                # Extract plan from save_fault_plan calls
                if tc_name == "save_fault_plan":
                    plan_content = tc_args.get("plan_content", "")
                    if plan_content:
                        result["is_complex"] = True
                        result["plan"] = plan_content
                        logger.info(f"Fault plan generated ({len(plan_content)} chars)")

                if tc_name == "finish_planning":
                    if tc_args.get("rejected"):
                        reason = tc_args.get("rejection_reason") or tc_args.get("summary", "Agent rejected the request")
                        result.update(fail_state(
                            FailureCategory.PLANNING_REJECTED,
                            reason,
                            state.get("messages", []) + result.get("messages", []),
                        ))
                    else:
                        summary = tc_args.get("summary", "")
                        if summary and not result.get("plan"):
                            result["plan"] = summary

            # Satisfy ToolMessage contract when finish_planning bypasses ToolNode.
            # Generate synthetic ToolMessages for ALL tool_calls in this response
            # so the message history stays valid for downstream LLM calls.
            _has_finish_planning = any(
                (tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", "")) == "finish_planning"
                for tc in tool_calls
            )
            if _has_finish_planning:
                synthetic_tms = []
                for tc in tool_calls:
                    _tc_name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
                    _tc_id = (
                        tc.get("id", "") if isinstance(tc, dict)
                        else getattr(tc, "id", "")
                    ) or f"call_{_tc_name}_{count}"
                    if _tc_name == "finish_planning":
                        _fp_args = (tc.get("args") or {}) if isinstance(tc, dict) else (getattr(tc, "args", None) or {})
                        _fp_rejected = _fp_args.get("rejected", False)
                        _fp_summary = _fp_args.get("summary", "")
                        if _fp_rejected:
                            _fp_reason = _fp_args.get("rejection_reason") or _fp_summary
                            tm_content = f"Planning rejected. Reason: {_fp_reason}"
                        else:
                            tm_content = f"Planning finalized. Summary: {_fp_summary}"
                        synthetic_tms.append(ToolMessage(
                            content=tm_content,
                            tool_call_id=_tc_id,
                            name="finish_planning",
                        ))
                    else:
                        synthetic_tms.append(ToolMessage(
                            content="(skipped — finish_planning signaled end of planning phase)",
                            tool_call_id=_tc_id,
                            name=_tc_name,
                        ))
                result["messages"] = result.get("messages", []) + synthetic_tms

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

        # --- Terminal conclusion detection ---
        # In Phase 1, the LLM has tools bound (activate_skill,
        # read_skill_resource, kubectl_ro, finish_planning). Text-only
        # output without activating a skill means the LLM concluded it
        # cannot plan this injection. Set error so the router's
        # error-branch routes to "reject" — without this, the router
        # returns "continue" and the LLM repeats the same conclusion.
        if response is not None:
            _has_tool_calls = bool(getattr(response, "tool_calls", None))
            _has_skill = bool(result.get("skill_name") or state.get("skill_name"))
            if not _has_tool_calls and not _has_skill and not result.get("error"):
                _conclusion = (getattr(response, "content", "") or "").strip()
                if _conclusion:
                    result.update(fail_state(
                        FailureCategory.PLANNING_TIMEOUT,
                        "LLM concluded without tool use or skill activation",
                        state.get("messages", []) + result.get("messages", []),
                    ))

        tracker.complete(f"Agent loop iteration {count} done")
        await sync_to_store(state, result)
        return result

    return _agent_loop_with_llm
