"""Agent loop node: Phase 1 ReAct planning (skill activation + target verification + plan generation)."""

import json
import logging
import shlex

from langchain_core.messages import HumanMessage, SystemMessage

from chaos_agent.agent.env_info import compute_env_info
from chaos_agent.agent.capabilities import (
    build_capability_context,
    filter_tools_for_context,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.node_names import AGENT_LOOP
from chaos_agent.agent.nodes.execute._kubeconfig_inject import (
    _resolve_kubeconfig,
    inject_kubeconfig_into_tool_calls,
    inject_task_id_into_tool_calls,
    sync_kubewiz_runtime,
)
from chaos_agent.agent.nodes.store._store_sync import sync_to_store
from chaos_agent.agent.nodes.execute.llm_step_helpers import (
    filter_stagnant_tool,
    persist_corrective_hint,
    persist_replaceable_hint,
    post_invoke_debug,
)
from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
    detect_tool_error_hint,
    emit_debug_tool_messages,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    handle_truncated_response,
)
from chaos_agent.agent.prompts import (
    build_system_prompt,
    PromptMode,
)
from chaos_agent.agent.spec.skill_identity import has_active_skill, read_active_skill_name
from chaos_agent.agent.state import AgentState
from chaos_agent.agent.state_mgmt.state_helpers import fail_state
from chaos_agent.agent.result.verdict import FailureCategory
from chaos_agent.config.settings import settings
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)

logger = logging.getLogger(__name__)

_WORKLOAD_SCOPES = frozenset({
    "container", "pod", "deployment", "service",
    "statefulset", "daemonset",
})
_INFRA_SCOPES = frozenset({"node"})


def _ledger_section_for_planning(state) -> str:
    """Render the progress-ledger section for the planning phase (b-plan).

    Planning does NOT freeze an anchor: the fault_spec is still converging here,
    so anchoring it would anchor a moving target. The ledger is simply whatever
    the model has recorded so far (state + log, empty anchor) — it exists to show
    established facts and progress, and ``render_ledger`` skips the anchor block
    while it is empty. The anchor is frozen later, once execute_loop runs on the
    approved spec.
    """
    from chaos_agent.agent.progress_ledger import build_ledger_prompt_section
    return build_ledger_prompt_section(state.get("progress_ledger"))


def _build_replan_context_message(
    replan_context: dict,
    replan_history: list | None,
    attempt: int,
) -> SystemMessage:
    """Build the one persistent handoff message for a replan attempt.

    Phase 1 is a ReAct loop, so injecting the same failure as a fresh
    HumanMessage on every iteration makes the model restart its analysis after
    every tool result.  A persisted SystemMessage gives the first iteration the
    handoff and lets all later iterations continue from normal message history.
    """
    trigger = replan_context.get("trigger", "execute_loop")
    lines = [
        f"[REPLAN CONTEXT attempt={attempt}]",
        "This is an internal execution handoff, not a new user request.",
    ]
    if trigger == "verify_replan":
        lines.extend([
            "Verification found that the previous injection did not take effect.",
            f"Summary: {replan_context.get('error_summary', 'Unknown')}",
            f"Residuals cleaned: {replan_context.get('residuals_description', 'None')}",
        ])
    else:
        lines.extend([
            "Phase 2 execution failed. Analyze the failure chain once, then continue",
            "the normal Phase 1 ReAct investigation without repeating that analysis.",
            f"Error: {replan_context.get('error_summary', 'Unknown')}",
            "Failed tool calls: "
            f"{json.dumps(replan_context.get('failed_tool_calls', []), ensure_ascii=False)}",
            f"Existing blade UIDs: {replan_context.get('existing_blade_uids', [])}",
        ])
    if replan_history:
        lines.append(
            "Previous attempts: "
            f"{json.dumps(replan_history, ensure_ascii=False)}"
        )
    return SystemMessage(
        content="\n".join(lines),
        additional_kwargs={"kind": "replan_context", "attempt": attempt},
    )


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
    _SHELL_OPERATORS = {"|", "||", "&&", ";", ">", ">>", "<"}
    i = 0
    while i < len(v_parts):
        p = v_parts[i]
        if p in _SHELL_OPERATORS:
            break
        if p in ("-n", "--namespace", "-l", "--selector",
                 "-o", "--output", "--field-selector", "--kubeconfig"):
            i += 2  # skip flag + value
            continue
        if p.startswith("--") and "=" not in p:
            # Unknown --flag with separate value: skip flag + value
            # to prevent flag values from leaking into names.
            i += 2
            continue
        if p.startswith("-"):
            i += 1
            continue
        positionals.append(p)
        i += 1
    if positionals:
        canonical = _SCOPE_ALIASES.get(positionals[0].lower())
        if canonical:
            # Only infer scope if the command targets a SPECIFIC resource
            # (has names or labels). A bare "kubectl get nodes" is an
            # environment probe, not a target declaration.
            names = [n for n in positionals[1:] if not n.startswith("-")]
            has_labels = "labels" in updates
            if names or has_labels:
                updates["scope"] = canonical
                if names:
                    updates["names"] = tuple(names)
    return updates


def _drop_vehicle_names(updates: dict, state: dict, v_args: str) -> None:
    """Strip vehicle names from lazy-derived spec updates in place.

    task-29848471: an exploratory ``kubectl get`` against a debug/tool pod
    must never promote that transient vehicle into the fault target. When
    any derived name is a known injection vehicle the whole ``names``
    update is dropped (other derived fields are kept) and a warning is
    logged for post-mortem attribution.
    """
    if "names" not in updates:
        return
    from chaos_agent.agent.execution_artifacts import is_vehicle_name

    vehicle_hits = [n for n in updates["names"] if is_vehicle_name(n, state)]
    if vehicle_hits:
        logger.warning(
            "spec-write blocked: writer=agent_loop lazy derivation "
            "vehicle name(s) %s rejected (v_args=%r)",
            vehicle_hits, v_args,
        )
        del updates["names"]


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


def make_agent_loop(hook=None, llm=None, tools=None, skill_catalog: str = "", registry=None):
    """Create an agent_loop node with optional PreReasoningHook and LLM.

    When llm is provided, the node performs actual LLM reasoning
    (calling the model with bound tools, returning the response as a message).
    When llm is None (and hook is None), the node only tracks the
    iteration count and enforces the max-iteration limit — a lightweight
    path used for testing.
    """

    async def _agent_loop_with_llm(state: AgentState) -> dict:
        # 1. Iteration count + limit check (original logic preserved)
        task_id = state.get("task_id", "") or ""
        count = state.get("agent_loop_count", 0) + 1
        skill_name = read_active_skill_name(state)

        # --- Replan entry detection ---
        replan_context = state.get("replan_context")
        replan_history = state.get("replan_history")
        # is_replan is True when replan_context exists AND at least one replan
        # counter (execute_loop or verify) has been incremented.
        _total_replan = state.get("replan_count", 0) + state.get("verify_replan_count", 0)
        is_replan = replan_context is not None and _total_replan > 0
        is_replan_entry = (
            is_replan
            and state.get("replan_context_injected_attempt") != _total_replan
        )

        if is_replan and count > 1 and state.get("_replan_loop_reset") != _total_replan:
            # Reset agent_loop_count once on first entry after replan.
            # Subsequent iterations must NOT reset (otherwise MAX_AGENT_LOOP
            # safety check can never trigger — unbounded loop risk).
            # We detect "first entry" by checking if _replan_loop_reset
            # hasn't been set to current total replan count yet.
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
            rejection_reason = state.get("_planning_rejection_reason", "")
            alternatives = state.get("_planning_alternatives", "") if rejection_reason else ""
            category = FailureCategory.PLANNING_REJECTED if rejection_reason else FailureCategory.PLANNING_TIMEOUT
            analysis = rejection_reason or (
                f"The agent could not finish planning within {MAX_AGENT_LOOP} iterations. "
                "Likely causes: the target resource does not exist, a parameter is invalid, "
                "or the LLM could not converge on a viable plan. "
                "Check the target resource's state and retry."
            )
            result = {
                "safety_status": "rejected",
                **fail_state(
                    category,
                    rejection_reason or f"max_iterations={MAX_AGENT_LOOP}",
                    state.get("messages", []),
                    alternatives=alternatives,
                    llm_analysis=analysis,
                ),
            }
            await sync_to_store(state, result)
            return result

        # Lightweight path: no LLM, no hook — only track iteration count.
        # Used for testing and backward compatibility.
        if llm is None and hook is None:
            tracker.complete(f"Agent loop iteration {count} done")
            from chaos_agent.agent.router import mark_wall_clock_timeout
            return mark_wall_clock_timeout(state, {"agent_loop_count": count})

        # 2. Call pre_reason_hook (memory compaction)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # 2b. Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state)

        # 3. Collect environment info and call LLM with bound tools
        _injections_for_state = []
        # Counts live on state; compaction may summarise the hint
        # messages away but must not reset the number.
        _hint_counts = dict(state.get("hint_repeat_counts") or {})
        if llm is not None:
            messages = list(state.get("messages", []))

            # Persist the replan handoff exactly once per attempt.  Subsequent
            # Phase 1 iterations see it in message history and continue ReAct.
            if is_replan_entry:
                replan_message = _build_replan_context_message(
                    replan_context, replan_history, _total_replan,
                )
                messages.append(replan_message)
                _injections_for_state.append(replan_message)

            # Collect env info (cached per task_id)
            env_info = await compute_env_info(task_id)

            # P1: Use build_system_prompt with PromptMode dispatch
            # PATD: skill index is now in stable section of system prompt;
            #       P2 tool_result injection removed (3× redundancy eliminated)
            capability_context = build_capability_context(state, "plan", tools)
            if not capability_context.supported:
                spec = read_fault_spec(state)
                selected_domain = spec.scope if spec and spec.scope else "unknown"
                # Two distinct causes, previously reported with one sentence that
                # named neither: the old wording said intent recognition finished
                # "without choosing a transport", which is wrong for the common
                # case — a transport WAS resolved, it just cannot run this domain.
                # That sent readers looking for a missing selection instead of an
                # incompatible pair.
                #
                # A session with no connection field at all is the other cause. Its
                # resolved channel is a process-wide ``settings`` default the user
                # never chose, so naming that channel would mislead just as badly;
                # say the environment has no transport configured instead.
                #
                # Reached mainly by paths that skip the earlier submit-time gate in
                # ``intent_clarification`` (batch replans, checkpoint resumes), so
                # the message still has to stand on its own.
                _has_conn_field = any(
                    state.get(k)
                    for k in (
                        "kube_connection_mode", "host_name", "ssh_host",
                        "kubeconfig", "kubewiz_cluster_uuid",
                    )
                )
                if _has_conn_field:
                    from chaos_agent.agent.spec.fault_registry import (
                        family_for_scope,
                    )
                    from chaos_agent.transports.registry import (
                        profile_of,
                        resolve_channel_name,
                    )

                    _family = family_for_scope(selected_domain or "")
                    _domain_profile = _family.profile if _family else "unknown"
                    _channel = resolve_channel_name(state)
                    message = (
                        f"The requested fault domain '{selected_domain}' "
                        f"(profile: {_domain_profile}) cannot run through the "
                        f"configured transport '{_channel}' "
                        f"(profile: {profile_of(_channel)}). Select an "
                        f"environment matching this domain, or revise the intent "
                        f"to a fault type this environment supports."
                    )
                else:
                    message = (
                        "The drill environment has no usable transport "
                        "configured (no kubeconfig, KubeWiz cluster, KubeWiz host "
                        "or SSH target). Configure a connection method on the "
                        "environment, or bind an environment that already has "
                        f"one. Selected domain: {selected_domain}."
                    )
                result = {
                    "agent_loop_count": count,
                    "safety_status": "rejected",
                    "planning_rejected": True,
                    **fail_state(
                        FailureCategory.PLANNING_REJECTED,
                        message,
                        state.get("messages", []),
                        llm_analysis=message,
                    ),
                }
                from chaos_agent.memory.hook import merge_hook_updates
                merge_hook_updates(result, hook_updates)
                tracker.fail(message)
                await sync_to_store(state, result)
                return result
            system_prompt = build_system_prompt(
                PromptMode.FULL,
                skill_catalog=registry.build_catalog_prompt() if registry else skill_catalog,
                input_is_nl=bool(state.get("input")),
                env_info=env_info,
                replan_context=replan_context if is_replan_entry else None,
                replan_history=replan_history if is_replan_entry else None,
                profile=capability_context.profile,
                fault_spec=state.get("fault_spec"),
                progress_ledger_section=_ledger_section_for_planning(state),
            )

            # --- Inject structured fault context from FaultSpec ---
            _spec = read_fault_spec(state)
            if count == 1 and _spec and not is_replan and _spec.is_complete:
                fi_lines = [
                    "[FAULT INTENT — UNVERIFIED parameters from user dialogue]",
                    "⚠️ These parameters have NOT been validated against the current target authority.",
                    "You MUST verify target identity with currently bound read-only tools. "
                    "If a parameter conflicts with runtime evidence, discover and use the "
                    "correct value — do NOT trust the original value.",
                    "",
                    f"Fault type: {_spec.fault_type or '?'}",
                    f"Scope: {_spec.scope or '?'}",
                    f"Target: {_spec.blade_target or '?'}",
                    f"Action: {_spec.blade_action or '?'}",
                ]
                # Identity fields are emitted purely data-driven: whatever the
                # FaultSpec carries is shown, with no profile branch. Namespace /
                # labels are k8s-only spec fields and simply stay empty (thus
                # omitted) on host. Target grounding itself is enforced uniformly
                # by the generic Workflow (Step 3) and the environment_profile
                # target-authority fragment — not by profile-specific prose here.
                if _spec.namespace:
                    fi_lines.append(f"Namespace: {_spec.namespace}")
                if _spec.labels:
                    fi_lines.append(f"Labels: {dict(_spec.labels)}")
                if _spec.names:
                    fi_lines.append(f"Names: {', '.join(_spec.names)}")
                if _spec.params:
                    fi_lines.append(f"Params: {json.dumps(dict(_spec.params), ensure_ascii=False)}")
                if _spec.user_description:
                    fi_lines.append(f"User request: {_spec.user_description}")

                _all_matches: list[str] = []
                if registry:
                    _all_matches = registry.match_use_cases(
                        _spec.scope, _spec.blade_target, _spec.blade_action,
                    )
                if _all_matches:
                    fi_lines.append(f"\nCatalogue candidates ({len(_all_matches)}):")
                    candidate_dirs: list[str] = []
                    for m in _all_matches:
                        parts = m.split("/")
                        cat_idx = parts.index("catalogue") if "catalogue" in parts else -1
                        if cat_idx >= 0 and cat_idx + 1 < len(parts):
                            d = "/".join(parts[:cat_idx + 2]) + "/"
                            if d not in candidate_dirs:
                                candidate_dirs.append(d)
                    for d in candidate_dirs:
                        fi_lines.append(f"  - {d}")
                    fi_lines.append("")
                    fi_lines.append(
                        "You MUST browse these directories with read_skill_resource, "
                        "read the candidate case files, and select the one that best "
                        "matches the user's fault scenario. Do NOT assume the first "
                        "candidate is correct \u2014 review the case content before deciding."
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
            # Corrective hints go through ``persist_corrective_hint``: the notice
            # must land in history, not only in this turn's local copy, or the
            # next iteration reads it as a first-time warning and the model never
            # learns it has already been told (task-e9ee12d6).
            loop_hint = detect_repeated_tool_calls(messages, phase="planning")
            if loop_hint:
                messages.append(persist_corrective_hint(
                    _injections_for_state, state.get("messages", []),
                    "loop", "planning", loop_hint,
                    escalate_after=settings.hint_escalate_after,
                    counts=_hint_counts, counts_out=_hint_counts,
                ))

            # --- Action stagnation detection (tool-name level, ignores args) ---
            stagnation_hint, stagnant_tool = detect_action_stagnation(messages, phase="planning")
            if stagnation_hint:
                messages.append(persist_corrective_hint(
                    _injections_for_state, state.get("messages", []),
                    "stagnation", stagnant_tool or "planning", stagnation_hint,
                    escalate_after=settings.hint_escalate_after,
                    counts=_hint_counts, counts_out=_hint_counts,
                ))

            # --- Tool error introspection (runtime feedback > static docs) ---
            error_hint = detect_tool_error_hint(messages)
            if error_hint:
                messages.append(persist_corrective_hint(
                    _injections_for_state, state.get("messages", []),
                    "tool_error", "planning", error_hint,
                    counts=_hint_counts, counts_out=_hint_counts,
                ))

            # --- Convergence hints (planning conclusion prompts) ---
            # Aligned with execute_loop's 3-tier convergence system.
            # Without these, the LLM has no awareness of its iteration budget
            # and may loop indefinitely making tool calls.
            remaining = MAX_AGENT_LOOP - count
            if MAX_AGENT_LOOP - 5 <= count < MAX_AGENT_LOOP - 1:
                # Budget information, not a prescribed final-action script.
                messages.append(persist_replaceable_hint(
                    _injections_for_state, "budget", "planning",
                    f"**Iteration Progress**: You are on iteration {count} of max "
                    f"{MAX_AGENT_LOOP} ({remaining} remaining). "
                    "Think the next step through before acting: name what you already "
                    "know, what is still genuinely unknown, and what this next call "
                    "would add that the previous one did not. "
                    "Use the remaining budget deliberately. Prefer an action that verifies "
                    "a material assumption, resolves an important unknown, or completes a "
                    "well-grounded plan. Avoid unchanged repetition unless new evidence, "
                    "changed input, or expected propagation timing justifies it."
                ))
            elif count == MAX_AGENT_LOOP - 1:
                # The model still chooses whether evidence is sufficient.
                messages.append(persist_replaceable_hint(
                    _injections_for_state, "budget", "planning",
                    f"**CRITICAL WARNING - Planning Budget**: iteration {count} of {MAX_AGENT_LOOP}; one "
                    "tool-enabled iteration remains after this one. Reason it through "
                    "before acting: with one action left, name the single unknown it "
                    "would resolve — if you cannot name one, there is nothing left to "
                    "gather. Assess the evidence "
                    "already gathered and decide whether the plan is ready, one specific "
                    "unknown still matters, or the request is genuinely infeasible. Do not "
                    "let the budget turn an ordinary tool error into an unsupported conclusion."
                ))
            elif count >= MAX_AGENT_LOOP:
                # Tier 3: final handoff — tools are unbound by code.
                messages.append(persist_replaceable_hint(
                    _injections_for_state, "budget", "planning",
                    f"**FINAL ITERATION**: This is iteration {count} of max "
                    f"{MAX_AGENT_LOOP}. NO more iterations are available. "
                    "Tools are no longer available. Think through what the evidence "
                    "actually supports, then provide a concise handoff that separates "
                    "verified facts, remaining uncertainty, the plan status, and any evidence "
                    "that requires replan or rejection."
                ))

            # On last iteration, unbind tools to force text conclusion
            if count >= MAX_AGENT_LOOP:
                llm_with_tools = llm
            else:
                visible_tools = filter_tools_for_context(tools, capability_context)
                # An empty GATE result must not degrade to an unbound LLM (the
                # model would still emit calls the static ToolNode would run) —
                # but only when a NON-EMPTY set was gated away. With no static
                # tools at all, or after the benign stagnant filter, the unbound
                # LLM is the intended prose path and the only usable one (a
                # provider rejects a request with an empty ``tools`` array).
                if tools and not visible_tools:
                    logger.warning(
                        "agent_loop: capability gate left no visible tools "
                        "(profile=%s) — binding an empty tool set",
                        capability_context.profile,
                    )
                    llm_with_tools = llm.bind_tools([])
                else:
                    tools_this_iter = filter_stagnant_tool(visible_tools, stagnant_tool)
                    llm_with_tools = llm.bind_tools(tools_this_iter) if tools_this_iter else llm

            # Record system prompt to session store (dedup handles repeated prompts)
            record_system_prompt(hook, state, system_prompt, node_name=AGENT_LOOP)

            response = await llm_with_tools.ainvoke(
                [SystemMessage(content=system_prompt)] + messages
            )
        else:
            response = None

        # 4. Build result
        result = {"agent_loop_count": count, "planning_rejected": False}

        # Mark that the replan loop counter has been reset for this replan
        # attempt, so subsequent iterations don't reset again.
        if is_replan and count == 1:
            result["_replan_loop_reset"] = state.get("replan_count", 0) + state.get("verify_replan_count", 0)
        if is_replan_entry:
            result["replan_context_injected_attempt"] = _total_replan

        # Reset safety_status for replan so safety_check re-evaluates the corrected plan
        if is_replan:
            result["safety_status"] = "pending"
            result["needs_confirmation"] = False
            # Clear replan_requested so Phase 2 doesn't immediately re-trigger
            result["replan_requested"] = False
            result["blast_radius_scope"] = None
            result["blast_radius_detail"] = None

        if response is not None:
            # Programmatic kubeconfig injection: ensure every kubectl/blade tool call
            # has the correct kubeconfig, even if the LLM forgot to include it.
            kubeconfig = _resolve_kubeconfig(state)
            inject_kubeconfig_into_tool_calls(response, kubeconfig)
            inject_task_id_into_tool_calls(response, state.get("task_id", ""))
            sync_kubewiz_runtime(state)

            result["messages"] = _injections_for_state + [response]
            if _hint_counts != (state.get("hint_repeat_counts") or {}):
                result["hint_repeat_counts"] = _hint_counts

            # Output-limit truncation: this response's tool calls may carry
            # silently incomplete arguments. Parseable calls get a synthetic
            # error answer; calls whose JSON is broken are stripped instead
            # (answering those makes the provider parse the args and reject the
            # request). Either way the screener is flagged to route back here
            # instead of forwarding the batch to the ToolNode.
            truncated = handle_truncated_response(response)
            if truncated is not None:
                safe_message, truncated_results = truncated
                logger.warning(
                    "agent_loop: response truncated by output token limit — "
                    "%d tool call(s) failed unexecuted, %d unparseable call(s) dropped",
                    len(truncated_results),
                    len(getattr(response, "invalid_tool_calls", None) or []),
                )
                result["messages"] = (
                    _injections_for_state + [safe_message] + truncated_results
                )
                result["truncated_tool_calls"] = True
            else:
                # Clear at the source every non-truncated turn: a flag left set
                # by a turn that exited without passing the screener that
                # consumes it must not reach a later, healthy batch.
                result["truncated_tool_calls"] = False

            # Immediately save AI message (including reasoning_content) to session
            record_ai_message(hook, state, response, node_name=AGENT_LOOP)

            # Diagnostic log for reasoning_content presence
            log_reasoning_content(response, "Agent loop", count)

            # Extract skill_name and target from tool calls.
            #
            # Skipped on a truncated turn: these calls were NOT executed (the
            # screener routes the batch back here), so recording their effects
            # would describe work that never happened. Concretely, a recorded
            # ``skill_name`` makes ``build_*_prompt`` drop the skill catalogue
            # (``if not skill_name``) while the skill's own content was never
            # loaded — leaving the model with neither, which is strictly worse
            # than recording nothing. The model re-issues the call next turn.
            tool_calls = (
                []
                if result.get("truncated_tool_calls")
                else (getattr(response, "tool_calls", None) or [])
            )
            for tc in tool_calls:
                tc_name, tc_args = extract_tool_call_fields(tc)

                if tc_name == "activate_skill" and tc_args.get("skill_name"):
                    result["skill_name"] = tc_args["skill_name"]
                    logger.info(f"Skill activated: {tc_args['skill_name']}")

                if tc_name == "finish_planning":
                    br_scope = tc_args.get("blast_radius_scope", "")
                    br_detail = tc_args.get("blast_radius_detail", "")
                    if br_scope:
                        result["blast_radius_scope"] = br_scope
                    if br_detail:
                        result["blast_radius_detail"] = br_detail

                # Lazy spec derivation from LLM's kubectl get probes.
                #
                # Why this exists: CLI NL mode (``blade-ai inject
                # --input "..."``) doesn't go through
                # intent_clarification (route_pipeline_start keys
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
                    tc_name in ("kubectl", "kubectl_read")
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
                        # Strict write-once: only fill fields the spec
                        # is missing. Scope is NOT overridden here —
                        # extract_planning_metadata derives the
                        # authoritative scope from the skill case.
                        # Allowing kubectl queries to override scope
                        # (the old has_precise_scope logic) caused
                        # exploratory node queries to corrupt the scope
                        # from "deployment" to "node".
                        updates: dict = {}
                        for k, v in derived.items():
                            if k == "labels":
                                continue  # labels written by validated extraction only
                            current = getattr(_spec_now, k, None)
                            if not current:
                                updates[k] = v
                        if "names" in updates:
                            _drop_vehicle_names(updates, state, tc_args.get("v_args", ""))
                        if updates:
                            new_spec = _spec_now.replace(**updates)
                            result["fault_spec"] = new_spec.to_dict()
                            if "names" in updates:
                                logger.debug(
                                    "spec-write: writer=agent_loop lazy "
                                    "derivation names %s -> %s "
                                    "basis=kubectl %s v_args=%r",
                                    list(_spec_now.names),
                                    list(updates["names"]),
                                    tc_args.get("subcommand"),
                                    tc_args.get("v_args", ""),
                                )
                            logger.info(
                                "agent_loop: derived spec fields from "
                                "LLM kubectl get: %s", updates,
                            )


            post_invoke_debug(tracker, response, count, "Iteration")

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result, hook_updates)

        # --- Terminal conclusion detection ---
        # In Phase 1, the LLM has tools bound (activate_skill,
        # read_skill_resource, kubectl_read, finish_planning). Text-only
        # output without activating a skill means the LLM concluded it
        # cannot plan this injection. Set error so the router's
        # error-branch routes to "reject" — without this, the router
        # returns "continue" and the LLM repeats the same conclusion.
        #
        # A single spurious text-only turn (e.g. a model that "thinks out
        # loud" once before acting) must NOT insta-kill planning. We count
        # CONSECUTIVE text-only stalls and nudge until the budget is spent;
        # any productive turn (tool_calls issued or a skill activated)
        # resets the streak. On the final forced-text iteration (tools are
        # unbound below), text IS the expected handoff — terminate directly.
        #
        # Skipped on a truncated turn: it was cut off mid-emission, so its
        # (possibly empty) tool_calls are not evidence of a text-only stall. A
        # nudge appended here would also land AFTER the synthetic tool results,
        # breaking the "answers are the last messages" precondition the screener
        # checks before diverting the batch away from the ToolNode.
        if response is not None and not result.get("truncated_tool_calls"):
            _has_tool_calls = bool(getattr(response, "tool_calls", None))
            _has_skill = bool(result.get("skill_name") or has_active_skill(state))
            if _has_tool_calls or _has_skill:
                if state.get("_plan_text_stall_count"):
                    result["_plan_text_stall_count"] = 0
            elif not result.get("error"):
                _conclusion = (getattr(response, "content", "") or "").strip()
                if _conclusion:
                    if count >= MAX_AGENT_LOOP:
                        # Final iteration: tools were unbound to force a text
                        # handoff — accept it as the terminal conclusion.
                        result.update(fail_state(
                            FailureCategory.PLANNING_TIMEOUT,
                            "LLM concluded without tool use or skill activation",
                            state.get("messages", []) + result.get("messages", []),
                        ))
                    else:
                        try:
                            max_stalls = int(settings.max_plan_text_stalls)
                        except (TypeError, ValueError):
                            max_stalls = 3
                        if max_stalls < 1:
                            max_stalls = 1
                        stall_count = state.get("_plan_text_stall_count", 0) + 1
                        if stall_count < max_stalls:
                            result.setdefault("messages", []).append(
                                HumanMessage(content=(
                                    "**PLANNING ACTION REQUIRED**: You output text "
                                    "without calling a tool or activating a skill. "
                                    "You are in Phase 1 (planning). Take a concrete "
                                    "action NOW — browse the skill catalogue with "
                                    "`read_skill_resource`, inspect the target with a "
                                    "bound read-only tool, `activate_skill`, or "
                                    "`finish_planning`. Do NOT conclude in prose."
                                ))
                            )
                            result["_plan_text_stall_count"] = stall_count
                        else:
                            result.update(fail_state(
                                FailureCategory.PLANNING_TIMEOUT,
                                "LLM concluded without tool use or skill activation",
                                state.get("messages", []) + result.get("messages", []),
                            ))

        tracker.complete(f"Agent loop iteration {count} done")
        await sync_to_store(state, result)
        # Budget-exhaustion backstop. The final-iteration branch above only
        # stamps a cause when the model produced non-empty text; task-ff057e7f's
        # executor returned ``content=""`` with the whole conclusion written as a
        # ``<function=...>`` string inside reasoning_content, so that branch was
        # skipped and the router rejected with no recorded reason. A backstop at
        # the single exit does not depend on any branch remembering.
        # ``FailureCategory`` is imported at module level; re-importing it here
        # would make it a LOCAL name for the whole function and shadow every
        # earlier use (UnboundLocalError on the branches above).
        from chaos_agent.agent.router import mark_loop_exhausted
        return mark_loop_exhausted(
            result, count, MAX_AGENT_LOOP,
            category=FailureCategory.PLANNING_TIMEOUT, label="planning loop",
        )

    return _agent_loop_with_llm


# Module-level export: lightweight agent_loop (no LLM) for backward
# compatibility with tests and integration code that import agent_loop directly.
agent_loop = make_agent_loop()
