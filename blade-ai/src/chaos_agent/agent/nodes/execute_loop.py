"""Execute loop node: Phase 2 ReAct execution (follow skill instructions to call blade)."""

import json
import logging
import re

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from chaos_agent.agent.node_names import EXECUTE_LOOP
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
    extract_rejected_params,
    extract_tool_call_fields,
    log_reasoning_content,
    record_ai_message,
    record_system_prompt,
    summarize_llm_response,
)
from chaos_agent.agent.prompts import REPLAN_MARKER
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.agent.state_helpers import fail_state
from chaos_agent.agent.verdict import FailureCategory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.blade_uid import extract_blade_uid
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)

MAX_EXECUTE_LOOP = settings.max_execute_loop


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


def _parse_blade_uid_from_content(content) -> str | None:
    """Extract a ChaosBlade UID from ToolMessage content.

    Thin wrapper around `chaos_agent.utils.blade_uid.extract_blade_uid` —
    accepts the raw `content` field of a ToolMessage (string or other) and
    delegates multi-strategy parsing to the shared util.
    """
    if not isinstance(content, str):
        return None
    return extract_blade_uid(content)


def _extract_blade_uid_from_messages(messages: list) -> str | None:
    """Scan messages for blade_create or kubectl exec blade output and extract uid.

    ChaosBlade `blade create` returns JSON like:
        {"code": 200, "success": true, "result": "<uid>"}

    When blade_create tool fails on the host, the LLM may bypass it by
    using kubectl exec to run blade commands directly inside a cluster pod.
    In that case, the ChaosBlade success JSON appears in a kubectl
    ToolMessage instead of a blade_create ToolMessage.

    Priority: blade_create result > kubectl exec blade result.
    Only kubectl exec calls whose v_args contain "blade create" are
    considered — other kubectl outputs (get -o json, describe, etc.)
    are NOT scanned to prevent false-positive extraction from K8s
    resource metadata.uid fields.
    """
    kubectl_uid = None  # fallback uid from kubectl exec

    # Build a set of tool_call_ids that correspond to "kubectl exec ... blade create"
    blade_exec_call_ids: set[str] = set()
    for msg in messages:
        if not hasattr(msg, "tool_calls"):
            continue
        for tc in (msg.tool_calls or []):
            name = tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            args = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
            tc_id = tc.get("id", "") if isinstance(tc, dict) else getattr(tc, "id", "")
            if name == "kubectl" and isinstance(args, dict):
                v_args = args.get("v_args", "")
                if "blade" in v_args and "create" in v_args:
                    blade_exec_call_ids.add(tc_id)

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        msg_name = getattr(msg, "name", "") or ""
        content = msg.content

        # Priority 1: blade_create ToolMessage (original path)
        if msg_name == "blade_create":
            uid = _parse_blade_uid_from_content(content)
            if uid:
                return uid

        # Priority 2: kubectl exec blade ToolMessage ONLY
        if msg_name == "kubectl" and not kubectl_uid:
            tool_call_id = getattr(msg, "tool_call_id", "") or ""
            if tool_call_id in blade_exec_call_ids:
                kubectl_uid = _parse_blade_uid_from_content(content)

    return kubectl_uid


# Regex for: blade create k8s <scope>-<target> <action>
# e.g. "blade create k8s pod-network drop --percent 100 ..."
_BLADE_CREATE_K8S_RE = re.compile(
    r"blade\s+create\s+k8s\s+(\w+)-(\w+)\s+(\w+)"
)

def _parse_blade_create_from_v_args(v_args: str) -> dict | None:
    """Parse scope/target/action/flags from kubectl exec blade create v_args.

    Returns dict with scope/target/action, plus ``flags`` if present, or None
    if v_args does not contain a ``blade create k8s`` command.
    """
    match = _BLADE_CREATE_K8S_RE.search(v_args)
    if not match:
        return None
    result = {"scope": match.group(1), "target": match.group(2), "action": match.group(3)}
    flags_str = v_args[match.end():].strip()
    if flags_str:
        result["flags"] = flags_str
    return result


def _build_replan_context(state: AgentState, error_summary: str) -> dict:
    """Extract structured error context from conversation history for Phase 1 replan."""
    messages = state.get("messages", [])
    failed_calls = []
    existing_uids = []

    for msg in reversed(messages):
        if isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or ""
            content = msg.content if isinstance(msg.content, str) else str(msg.content)

            # Collect failed and successful blade_create calls
            if name == "blade_create":
                try:
                    data = json.loads(content)
                    if not data.get("success", True):
                        failed_calls.append({"name": name, "error": content[:500]})
                    else:
                        uid = data.get("result", "")
                        if uid:
                            existing_uids.append(uid)
                except (json.JSONDecodeError, TypeError):
                    if "error" in content.lower() or "fail" in content.lower():
                        failed_calls.append({"name": name, "error": content[:500]})
            elif content.startswith("Error"):
                failed_calls.append({"name": name, "error": content[:500]})

            if len(failed_calls) >= 5:
                break

    # Extract rejected params from all error sources
    all_rejected: list[str] = extract_rejected_params(error_summary)
    failed_tool_names: set[str] = set()
    for fc in failed_calls:
        all_rejected.extend(extract_rejected_params(fc.get("error", "")))
        if fc.get("name"):
            failed_tool_names.add(fc["name"])

    return {
        "error_summary": error_summary.replace(REPLAN_MARKER, "").strip()[:1000],
        "failed_tool_calls": failed_calls,
        "existing_blade_uids": existing_uids,
        "iteration_at_failure": state.get("execute_loop_count", 0),
        "rejected_params": list(dict.fromkeys(all_rejected)),
        "failed_tool_names": sorted(failed_tool_names),
    }


def _detect_replanable_tool_error(messages: list) -> str | None:
    """Check recent ToolMessage results for auto-replanable error patterns."""
    from chaos_agent.errors import should_auto_replan
    for msg in reversed(messages[-5:]):
        if isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or ""
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            if name in ("blade_create", "blade_query_k8s") or (name == "kubectl" and "kubectl exec failed" in content):
                try:
                    data = json.loads(content)
                    if not data.get("success", True):
                        err_msg = data.get("error", "") or data.get("result", "") or content
                        if should_auto_replan(err_msg):
                            return f"{name}: {err_msg[:500]}"
                except (json.JSONDecodeError, TypeError):
                    if should_auto_replan(content):
                        return f"{name}: {content[:500]}"
    return None


_BLADE_ERROR_COOLDOWN_TURNS = 3


def _is_llm_handling_blade_error(messages: list) -> bool:
    """Check if the LLM is still within a recovery window after a blade error.

    After blade_create fails, the LLM often needs multiple steps to recover:
    e.g. ``kubectl get pods`` (find tool pods) → ``kubectl exec blade -h``
    (check flags) → ``blade_create`` (retry with correct params).

    Rather than pattern-matching each intermediate step (which is fragile
    and incomplete), we use a cooldown window: if the most recent blade
    error is within the last N AIMessage turns, the LLM is presumed to
    still be recovering and auto-replan is suppressed.

    Returns True to suppress auto-replan, False to allow it.
    """
    if not messages:
        return False

    ai_turns_since_error = 0
    found_blade_error = False

    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            ai_turns_since_error += 1
            if ai_turns_since_error > _BLADE_ERROR_COOLDOWN_TURNS:
                break
        elif isinstance(msg, ToolMessage):
            name = getattr(msg, "name", "") or ""
            if name in ("blade_create", "blade_query_k8s"):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                status = getattr(msg, "status", "") or ""
                if status == "error" or "failed" in content.lower() or "error" in content.lower():
                    found_blade_error = True
                    break

    return found_blade_error and ai_turns_since_error <= _BLADE_ERROR_COOLDOWN_TURNS


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
    max_replan_count`` the system can no longer route ``[REPLAN]`` back
    to Phase 1, so suggesting it would invite an infinite loop where
    the LLM keeps emitting ``[REPLAN]`` and the router keeps falling
    through to "continue" (the exact stuck-loop the user reports).
    With ``replan_exhausted=True`` the hint drops the ``[REPLAN]``
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
            f"**STUCK LOOP DETECTED + REPLAN EXHAUSTED**: You have produced "
            f"{threshold} consecutive responses without any tool calls AND the "
            f"system has already burned through every available replan attempt. "
            f"`[REPLAN]` is no longer a valid action — emitting it again will "
            f"have no effect.\n\n"
            f"**Required action** (choose ONE):\n"
            f"1. If there is still a viable approach you have NOT tried, use a "
            f"tool to attempt it ONCE.\n"
            f"2. Otherwise, provide a concise final conclusion stating the "
            f"specific reason why injection is impossible. Do NOT include "
            f"`[REPLAN]` in your response.\n"
            f"3. Do NOT repeat the same text you have already output in previous turns."
        )
    return (
        f"**STUCK LOOP DETECTED**: You have produced {threshold} consecutive responses "
        f"without any tool calls. You appear to be repeating the same conclusion "
        f"without making progress.\n\n"
        f"**Required action** (choose ONE):\n"
        f"1. If there is still a viable approach you have NOT tried, use a tool to attempt it.\n"
        f"2. If you have exhausted all options and the fault CANNOT be injected, "
        f"output `[REPLAN]` to request a new plan from Phase 1, "
        f"or provide a concise final conclusion stating the specific reason why "
        f"injection is impossible.\n"
        f"3. Do NOT repeat the same text you have already output in previous turns."
    )


def _detect_injection_method(messages: list, blade_uid: str | None) -> str | None:
    """Detect the injection method used based on conversation history.

    Determines how the fault was actually injected so the verifier can
    choose the correct Layer 1 verification strategy.

    Returns:
        "host_blade" | "kubectl_exec" | "kubectl_native" | None
    """
    # Scan messages in reverse to find the most recent successful injection
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            continue
        msg_name = getattr(msg, "name", "") or ""
        content = msg.content if isinstance(msg.content, str) else str(msg.content)

        # blade_create ToolMessage with success → host_blade
        if msg_name == "blade_create":
            uid = _parse_blade_uid_from_content(content)
            if uid:
                return "host_blade"

        # kubectl ToolMessage containing ChaosBlade success JSON → kubectl_exec
        if msg_name == "kubectl":
            uid = _parse_blade_uid_from_content(content)
            if uid:
                return "kubectl_exec"

    # No blade_uid found — check for kubectl-native injection
    if not blade_uid:
        from chaos_agent.agent.nodes._injection_detection import (
            _was_kubectl_injection_attempted,
        )
        if _was_kubectl_injection_attempted(messages):
            return "kubectl_native"

    return None

async def execute_loop(state: AgentState) -> dict:
    """Phase 2: ReAct loop for execution.

    The LLM follows skill instructions to call blade/kubectl tools.

    Returns updated state fields.
    """
    task_id = state.get("task_id", "unknown")
    skill_name = state.get("skill_name", "")
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
        # 0. Reset time_wait consecutive-call guard if last round included
        # any non-wait tool (allows time_wait to be called again after a
        # real tool like kubectl ran).
        from chaos_agent.tools.wait import mark_other_tool_called
        messages = state.get("messages", [])
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage):
                if getattr(msg, "name", "") != "time_wait":
                    mark_other_tool_called()
                    break
                break  # most recent ToolMessage is time_wait → don't reset

        # 1. Iteration count + limit check
        task_id = state.get("task_id", "unknown")
        skill_name = state.get("skill_name", "")
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
            result = fail_state(
                FailureCategory.EXECUTION_TIMEOUT,
                f"max_iterations={MAX_EXECUTE_LOOP}",
                state.get("messages", []),
            )
            await sync_to_store(state, result)
            return result

        # --- Zombie-replan early exit ---------------------------------
        # If the previous iteration's [REPLAN] pushed ``replan_count``
        # to ``max_replan_count`` AND the router refused to take the
        # "replan" branch (it returns False from ``_should_replan``
        # once the count cap is hit), ``state.replan_requested=True``
        # is sticky and unrelated subsequent iterations cannot escape
        # via any normal path:
        #
        #   * the gate's else branch only fires when THIS iteration's
        #     LLM emits another ``[REPLAN]`` (with the new no-[REPLAN]
        #     hint, a well-behaved LLM stops emitting it)
        #   * ``state.error`` was cleared by the prior fire so the
        #     router's error branch can't end the turn
        #   * blade_uid is empty so verifier branch can't end either
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
            and not state.get("blade_uid")
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

        # 2. Call pre_reason_hook (memory compaction)
        hook_updates = {}
        if hook:
            hook_updates = await hook(state)

        # 2b. Emit ToolMessage results from previous iteration (debug only)
        emit_debug_tool_messages(tracker, state)

        # 3. Call LLM with bound tools
        if llm is not None:
            messages = list(state.get("messages", []))

            # --- Repeated tool call detection (loop breaking) ---
            loop_hint = detect_repeated_tool_calls(messages)
            if loop_hint:
                messages.append(HumanMessage(content=loop_hint))

            # --- Action stagnation detection (tool-name level, ignores args) ---
            stagnation_hint, stagnant_tool = detect_action_stagnation(messages)
            if stagnation_hint:
                if ":" in stagnant_tool:
                    base_tool = stagnant_tool.split(":")[0]
                    exec_hint = (
                        f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
                        f"multiple consecutive times with no progress. "
                        f"Stop using this subcommand. You can still use `{base_tool}` "
                        f"with OTHER subcommands (patch, delete, scale, etc.) "
                        f"to complete remaining injection steps.\n"
                        f"Do NOT call `{stagnant_tool}` again."
                    )
                else:
                    exec_hint = (
                        f"**ACTION_STAGNATION**: You have called `{stagnant_tool}` "
                        f"multiple consecutive times with no progress. "
                        f"This tool has been temporarily removed. You MUST now either:\n"
                        f"- Use a DIFFERENT tool to achieve the injection goal.\n"
                        f"- Output your conclusion if injection already succeeded "
                        f"(include blade_uid if available).\n"
                        f"- Output [REPLAN] if the current approach is not working.\n"
                        f"Do NOT attempt to call `{stagnant_tool}` again."
                    )
                messages.append(HumanMessage(content=exec_hint))

            # --- Tool error introspection (runtime feedback > static docs) ---
            error_hint = detect_tool_error_hint(messages)
            if error_hint:
                messages.append(HumanMessage(content=error_hint))

            # --- Consecutive idle turn detection (text-only loop breaking) ---
            #
            # Pass ``replan_exhausted`` so the hint stops suggesting
            # ``[REPLAN]`` once the router can no longer act on it.
            # Without this, the LLM follows the hint, the router
            # falls through to "continue" (because ``_should_replan``
            # returns False at max), and we burn iterations on
            # identical replan-requesting responses until Esc — the
            # exact loop reported by users on environment-blocked
            # injections (DiskPressure, etc.).
            try:
                _max_replan = int(settings.max_replan_count)
            except (TypeError, ValueError):
                _max_replan = 2
            replan_exhausted = state.get("replan_count", 0) >= _max_replan
            idle_hint = _detect_consecutive_idle_turns(
                messages, replan_exhausted=replan_exhausted
            )
            if idle_hint:
                messages.append(HumanMessage(content=idle_hint))

            # --- Conflict awareness (pre-existing experiments known to be on the cluster) ---
            conflict_uids = state.get("conflict_uids", [])
            if conflict_uids:
                uids_str = ", ".join(conflict_uids[:5])
                total = len(conflict_uids)
                messages.append(HumanMessage(content=(
                    f"**Residual Experiments Detected**: {total} active ChaosBlade experiment(s) "
                    f"already exist on this cluster (UIDs: {uids_str}). "
                    f"The user was informed and chose to proceed. You MUST:\n"
                    f"1. Be aware of potential compound effects with these existing experiments.\n"
                    f"2. Include a note about existing experiments in your conclusion.\n"
                    f"3. Only destroy your own experiment (blade_uid from your blade_create call) "
                    f"during recovery. Do NOT destroy the listed residual experiments unless "
                    f"explicitly requested by the user."
                )))

            # --- Convergence hints (last-iteration conclusion prompts) ---
            remaining = MAX_EXECUTE_LOOP - count
            if MAX_EXECUTE_LOOP - 5 <= count < MAX_EXECUTE_LOOP - 1:
                # Tier 1: Soft warning — iterations running low
                messages.append(HumanMessage(content=(
                    f"**Iteration Progress**: You are on iteration {count} of max {MAX_EXECUTE_LOOP} "
                    f"({remaining} remaining). "
                    f"If fault injection has not succeeded yet, focus on completing the blade_create call "
                    f"with correct parameters. If repeated attempts fail, consider outputting [REPLAN] "
                    f"to request a revised plan from Phase 1."
                )))
            elif count == MAX_EXECUTE_LOOP - 1:
                # Tier 2: Urgent warning — second-to-last iteration
                messages.append(HumanMessage(content=(
                    f"**CRITICAL WARNING**: This is iteration {count} of max {MAX_EXECUTE_LOOP} — "
                    f"your SECOND-TO-LAST iteration.\n"
                    f"If fault injection has not succeeded:\n"
                    f"  - Make ONE final attempt with blade_create using the correct parameters.\n"
                    f"  - If you cannot succeed, output [REPLAN] to request a new plan, "
                    f"or provide a brief conclusion explaining the failure.\n"
                    f"If injection already succeeded (blade_create returned a UID), "
                    f"output a brief summary and stop calling tools."
                )))
            elif count >= MAX_EXECUTE_LOOP:
                # Tier 3: Final conclusion — tools unbound, must provide conclusion
                messages.append(HumanMessage(content=(
                    f"**FINAL ITERATION**: This is iteration {count} of max {MAX_EXECUTE_LOOP}. "
                    f"NO more iterations are available. Tools are no longer available.\n"
                    f"You MUST provide a definitive conclusion NOW:\n"
                    f"1. **If injection succeeded** (a blade_uid was obtained in earlier iterations): "
                    f"State the blade_uid and confirm what fault was injected.\n"
                    f"2. **If injection failed**: Explain the specific failure reason — "
                    f"what you attempted, what errors occurred, and why injection could not be completed.\n"
                    f"3. **If you need a new plan**: Output [REPLAN] to request Phase 1 re-planning.\n\n"
                    f"Your response will be recorded as the final execution result."
                )))

            # Build execution prompt using the modular prompt system
            # P1: Use build_system_prompt with PromptMode dispatch
            from chaos_agent.agent.prompts import build_system_prompt, PromptMode
            from chaos_agent.agent.env_info import compute_env_info
            from chaos_agent.agent.fault_spec import read_fault_spec
            plan = state.get("plan")
            plan_path = state.get("plan_path")
            # Build structured_params_hint from FaultSpec
            _spec_for_hint = read_fault_spec(state)
            structured_params_hint = ""
            if _spec_for_hint and _spec_for_hint.is_complete:
                structured_params_hint = (
                    f"scope={_spec_for_hint.scope}, "
                    f"target={_spec_for_hint.blade_target}, "
                    f"action={_spec_for_hint.blade_action}"
                )
            # Resolve env_info: prefer constructor arg, fallback to dynamic computation
            resolved_env_info = env_info or await compute_env_info(task_id)
            execute_prompt = build_system_prompt(
                PromptMode.MINIMAL,
                skill_catalog=registry.build_catalog_prompt() if registry else skill_catalog,
                skill_name=skill_name,
                plan=plan or "",
                plan_path=plan_path or "",
                structured_params_hint=structured_params_hint,
                env_info=resolved_env_info,
            )
            # On last iteration, unbind tools to force text conclusion
            if count >= MAX_EXECUTE_LOOP:
                llm_to_call = llm
            else:
                tools_this_iter = list(tools) if tools else []
                if stagnant_tool and ":" not in stagnant_tool:
                    tools_this_iter = [
                        t for t in tools_this_iter
                        if getattr(t, "name", "") != stagnant_tool
                    ]
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

        # Extract blade_uid from ToolMessages (blade_create results)
        messages = state.get("messages", [])
        blade_uid = _extract_blade_uid_from_messages(messages)
        if blade_uid and blade_uid != state.get("blade_uid"):
            result["blade_uid"] = blade_uid
            logger.info(f"Extracted blade_uid from ToolMessage: {blade_uid}")
            if not state.get("injection_start_time"):
                result["injection_start_time"] = now_iso()
                logger.info("Set injection_start_time (blade_uid first seen)")

        # Detect injection method for verifier Layer 1 strategy selection.
        # Re-detect every iteration: hybrid injections (kubectl patch + blade_create)
        # start as kubectl_native but must upgrade when blade_uid appears.
        current_injection_method = state.get("injection_method") or result.get("injection_method")
        detected_method = _detect_injection_method(messages, blade_uid)
        if detected_method and detected_method != current_injection_method:
            # blade > kubectl: if blade_uid appeared, upgrade from kubectl_native
            if current_injection_method == "kubectl_native" and detected_method in ("host_blade", "kubectl_exec"):
                result["injection_method"] = detected_method
                logger.info(f"Upgraded injection_method: {current_injection_method} → {detected_method}")
            elif not current_injection_method:
                result["injection_method"] = detected_method
                logger.info(f"Detected injection_method: {detected_method}")
            # Set injection_start_time for non-ChaosBlade methods too.
            if not state.get("injection_start_time") and "injection_start_time" not in result:
                result["injection_start_time"] = now_iso()
                logger.info("Set injection_start_time (%s detected)", detected_method or current_injection_method)

        # Extract kubectl exec injection pod name for verifier preference
        current_pod_name = state.get("kubectl_exec_pod_name")
        if not current_pod_name:
            from chaos_agent.agent.nodes._injection_detection import _extract_kubectl_exec_pod_name
            pod_name = _extract_kubectl_exec_pod_name(messages)
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

            result["messages"] = [response]

            # Immediately save AI message (including reasoning_content) to session
            record_ai_message(hook, state, response, node_name=EXECUTE_LOOP)

            # Diagnostic log for reasoning_content presence
            log_reasoning_content(response, "Execute loop", count)

            # Extract blade_uid and params from blade_create tool calls
            tool_calls = getattr(response, "tool_calls", None) or []
            for tc in tool_calls:
                tc_name, tc_args = extract_tool_call_fields(tc)

                if tc_name == "blade_create":
                    # Note: scope/target/action/params are NOT written back
                    # to state — they're already pinned on fault_spec by
                    # intent_clarification and target_guard prevents drift.
                    # Mid-loop write would create stale duplicates that
                    # mask the user-approved values.
                    # Parse key parameters from flags string for verifier consumption
                    flags_str = tc_args.get("flags", "")
                    if flags_str:
                        from chaos_agent.utils.fault_type import parse_blade_flags
                        parsed = parse_blade_flags(flags_str)
                        if parsed:
                            result["blade_parsed_flags"] = parsed
                    # Namespace harvest from LLM-emitted blade_create no longer
                    # writes to state.target — fault_spec is the source of
                    # truth and target_guard intercepts any drift. The LLM
                    # passing a different namespace than approved would have
                    # been logged by the screener.
                    logger.info(f"Blade create params: {tc_args}")

                    # FCAT P0: param safety guard for LLM mode
                    # Adjust burn --size when target pod has low memory limit.
                    target_metadata = state.get("target_metadata") or {}
                    from chaos_agent.utils.fault_context import (
                        lookup_adaptations, compute_safe_burn_size,
                    )
                    from chaos_agent.agent.fault_spec import read_fault_spec as _rfs
                    _spec_for_fcat = _rfs(state)
                    _scope = (_spec_for_fcat.scope if _spec_for_fcat else "") or tc_args.get("scope", "")
                    _target = (_spec_for_fcat.blade_target if _spec_for_fcat else "") or tc_args.get("target", "")
                    _action = (_spec_for_fcat.blade_action if _spec_for_fcat else "") or tc_args.get("action", "")
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
                            # Write FCAT P0 decision to session for audit trail
                            from chaos_agent.memory.session_store import get_global_session_store
                            from langchain_core.messages import HumanMessage as _HM
                            _fcat_store = get_global_session_store()
                            _fcat_tid = state.get("task_id", "")
                            if _fcat_store and _fcat_tid:
                                _mem_str = (
                                    "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                                    else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                                )
                                _fcat_msg = f"[FCAT P0] {adj.id}: size adjusted to {tc_args.get(key, safe_size)}MB (pod_memory_limit={_mem_str})"
                                _fcat_store.append_messages(_fcat_tid, [_HM(content=_fcat_msg)], node_name=EXECUTE_LOOP)
                            # Debug-mode CLI display (truncated)
                            if settings.is_debug and tracker:
                                _mem_str_dbg = (
                                    "unavailable" if target_metadata.get("pod_memory_limit_mb") is None
                                    else f"{target_metadata.get('pod_memory_limit_mb')}MB"
                                )
                                tracker.update(
                                    f"[FCAT P0] {adj.id}: size→{tc_args.get(key, safe_size)}MB (pod_mem={_mem_str_dbg})"[:200],
                                    {"debug": True, "fcat": True},
                                )

                # Extract params from kubectl exec blade create (fallback path)
                # When the LLM bypasses blade_create tool by using kubectl exec
                # to run blade commands inside a cluster pod, the actual fault
                # parameters are embedded in v_args.  Parse them out so that
                # fault_type, verifier hints, and status API reflect the real
                # injection — not the stale params from a prior blade_create.
                if tc_name == "kubectl" and tc_args.get("subcommand") == "exec":
                    v_args = tc_args.get("v_args", "") or ""
                    if "blade" in v_args and "create" in v_args:
                        parsed = _parse_blade_create_from_v_args(v_args)
                        if parsed:
                            # Same reasoning as the blade_create branch above:
                            # scope/target/action/params are pinned on
                            # fault_spec and target_guard prevents drift,
                            # so we don't shadow them with LLM-derived
                            # values here. Only blade_parsed_flags (a
                            # pure runtime artefact) is harvested.
                            flags_str = parsed.get("flags", "")
                            if flags_str:
                                from chaos_agent.utils.fault_type import parse_blade_flags
                                parsed_flags = parse_blade_flags(flags_str)
                                if parsed_flags:
                                    result["blade_parsed_flags"] = parsed_flags
                            logger.info(
                                f"Kubectl exec blade params: scope={parsed['scope']}, "
                                f"target={parsed['target']}, action={parsed['action']}"
                            )

                # Track kubectl scale operations to preserve original replica counts
                if tc_name == "kubectl" and tc_args.get("subcommand") == "scale":
                    v_args = tc_args.get("v_args", "")
                    # Extract resource name and new replica count from v_args
                    # e.g. "deployment accounting -n cms-demo --replicas=1"
                    import re as _re
                    replicas_match = _re.search(r"--replicas=(\d+)", v_args)
                    # Match "deployment <name>" or "statefulset <name>"
                    resource_match = _re.search(
                        r"(?:deployment|statefulset)\s+(\S+)", v_args
                    )
                    if replicas_match and resource_match:
                        new_replicas = int(replicas_match.group(1))
                        resource_name = resource_match.group(1)
                        # Only record if we don't already have the original count
                        existing = state.get("original_replicas") or {}
                        if resource_name not in existing:
                            # We need the original count from pre-injection state.
                            # Try to extract from messages (Phase 1 verification output)
                            orig_count = _extract_original_replicas_from_messages(
                                state.get("messages", []), resource_name
                            )
                            if orig_count is not None and orig_count != new_replicas:
                                existing[resource_name] = orig_count
                                result["original_replicas"] = existing
                                logger.info(
                                    f"Recorded original_replicas: {resource_name}={orig_count}"
                                )

            # Emit debug-level status event with LLM reasoning summary
            if settings.is_debug:
                debug_info, tool_names = summarize_llm_response(response)
                tracker.update(
                    f"Iteration {count} LLM:\n{debug_info}",
                    {"debug": True, "iteration": count, "tool_calls": tool_names},
                )

        from chaos_agent.memory.hook import merge_hook_updates
        merge_hook_updates(result, hook_updates)

        # --- Terminal conclusion detection ---
        # In Phase 2, the LLM has tools bound. Text-only output (no
        # tool_calls) means the LLM has concluded — it either cannot
        # inject or is summarizing a result. If no blade_uid exists,
        # the injection didn't happen; mark as failure so the router's
        # existing error-branch ends the loop. Without this, the router
        # returns "continue" and the LLM repeats the same conclusion.
        # Skip when content contains [REPLAN] — the replan logic below
        # handles that path with proper state transitions.
        if response is not None:
            _has_tool_calls = bool(getattr(response, "tool_calls", None))
            _has_uid = bool(result.get("blade_uid") or state.get("blade_uid"))
            _injection_method = result.get("injection_method") or state.get("injection_method")
            _resp_content = (getattr(response, "content", "") or "").strip()
            if (
                not _has_tool_calls
                and not _has_uid
                and not result.get("error")
                and _resp_content
                and REPLAN_MARKER not in _resp_content
                and not _injection_method
            ):
                if not state.get("_execute_text_nudged"):
                    # First text-only output: nudge LLM to execute instead
                    # of summarizing. Give it one more chance before failing.
                    result.setdefault("messages", []).append(
                        HumanMessage(content=(
                            "**EXECUTION REQUIRED**: You output text instead of "
                            "calling a tool. You are in Phase 2 (execution) — "
                            "the plan is already approved. You MUST call "
                            "`kubectl` or `blade_create` NOW to inject the "
                            "fault. Do NOT output plans, summaries, or wait "
                            "for confirmation. Execute immediately."
                        ))
                    )
                    result["_execute_text_nudged"] = True
                else:
                    result.update(fail_state(
                        FailureCategory.EXECUTION_FAILED,
                    "LLM concluded without tool use",
                    state.get("messages", []) + result.get("messages", []),
                ))

            # --- kubectl_native step completeness check ---
            # When the LLM concludes (text-only) after a kubectl_native
            # injection, verify all skill case 演练步骤 have been executed.
            # If steps are missing, nudge the LLM to continue and clear
            # injection_method so the router returns "continue".
            if (
                not _has_tool_calls
                and _injection_method == "kubectl_native"
                and not state.get("_kubectl_step_nudged")
            ):
                from chaos_agent.agent.nodes._injection_detection import (
                    check_injection_step_completeness,
                )
                _skill_case = (
                    result.get("skill_case_content")
                    or state.get("skill_case_content", "")
                )
                _all_msgs = (
                    state.get("messages", [])
                    + result.get("messages", [])
                )
                _nudge = check_injection_step_completeness(
                    _skill_case, _all_msgs,
                )
                if _nudge:
                    logger.info(
                        "kubectl_native step completeness: incomplete, "
                        "nudging LLM to continue"
                    )
                    result.setdefault("messages", []).append(
                        HumanMessage(content=_nudge)
                    )
                    result["injection_method"] = None
                    result["_kubectl_step_nudged"] = True

        # --- Last-iteration failure attribution ---
        if count >= MAX_EXECUTE_LOOP:
            existing_uid = result.get("blade_uid") or state.get("blade_uid")
            if not existing_uid:
                _fs = fail_state(
                    FailureCategory.EXECUTION_TIMEOUT,
                    f"max_iterations={MAX_EXECUTE_LOOP}",
                    state.get("messages", []) + result.get("messages", []),
                )
                result.update(_fs)

        # --- Replan detection ---
        replan_requested = False
        replan_context = None

        if response is not None:
            content = getattr(response, "content", "") or ""
            # 1. LLM explicitly outputs [REPLAN]
            if REPLAN_MARKER in content:
                replan_requested = True
                replan_context = _build_replan_context(state, content)
                logger.info(f"Phase 2 LLM requested replan: {content[:200]}")

        # 2. Auto-detect from error in blade_create ToolMessages
        #    BUT suppress if the LLM is already actively handling the error
        #    (e.g., trying kubectl exec blade or kubectl-native alternatives)
        if not replan_requested:
            all_messages = state.get("messages", [])
            error_msg = _detect_replanable_tool_error(all_messages)
            if error_msg:
                if _is_llm_handling_blade_error(all_messages):
                    logger.warning(
                        "LLM is switching injection method — safety re-evaluation "
                        "not triggered for method switch"
                    )
                else:
                    replan_requested = True
                    replan_context = _build_replan_context(state, error_msg)
                    logger.info(f"Auto-detected replanable error: {error_msg[:200]}")

        if replan_requested:
            # Gate the replan side-effects on ``replan_count <
            # max_replan_count``. Without this gate, [REPLAN]
            # detection would always (a) reset ``execute_loop_count``
            # to 0, (b) clear ``error`` to None, and (c) increment
            # ``replan_count`` past max. Once max is reached the
            # router refuses the "replan" branch (``_should_replan``
            # returns False) but the cleared error / reset count
            # mean it ALSO can't take the "end" branch — it falls
            # through to "continue", the LLM keeps following the
            # convergence-hint instruction to emit ``[REPLAN]``, and
            # the loop never terminates short of user Esc. Reported
            # symptom: identical "[REPLAN]" replies emitted forever
            # on hard-blocked injections (DiskPressure / API
            # errors). The fix splits the two branches so a
            # post-max [REPLAN] becomes an honest terminal failure
            # the router CAN end on.
            try:
                _max_replan = int(settings.max_replan_count)
            except (TypeError, ValueError):
                _max_replan = 2
            current_replan_count = state.get("replan_count", 0)
            replan_can_fire = current_replan_count < _max_replan

            if replan_can_fire:
                result["replan_requested"] = True
                result["replan_context"] = replan_context
                result["replan_count"] = current_replan_count + 1
                # Reset execute_loop_count for fresh budget on re-entry
                if settings.replan_reset_execute_count:
                    result["execute_loop_count"] = 0
                # Clear error (moved to replan_context, global error triggers "end")
                result["error"] = None
                # Clear the frozen approved_target so the next
                # confirmation_gate (after agent_loop re-plans) freezes
                # a fresh approval. Without this, the screener would
                # compare new tool_calls against the stale approval
                # whose plan we just decided to abandon.
                result["approved_target"] = None
                # Conditionally preserve blade_uid
                if not replan_context.get("existing_blade_uids"):
                    result["blade_uid"] = None
                # Update replan_history
                history = list(state.get("replan_history") or [])
                history.append({
                    "attempt": result["replan_count"],
                    "original_error": replan_context.get("error_summary", ""),
                    "action_taken": "(pending Phase 1 analysis)",
                })
                result["replan_history"] = history
                # Patch E — graph-level replan is a clear attempt boundary.
                # Begin a new pipeline attempt so the TUI / TaskStore
                # show "attempt #N · graph_replan: <error_summary>"
                # rather than the user perceiving silent retry. The
                # ``target`` is the same one Phase 1 will revisit;
                # whether Phase 1 picks a different target is recorded
                # as a refinement of the same attempt (no new
                # begin_attempt) — until LLM-internal target-switch
                # detection lands as follow-up work.
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
            else:
                # Replan exhausted — convert the [REPLAN] request into
                # a terminal failure so the router takes "end" via the
                # ``state.get("error")`` branch. Do NOT reset
                # execute_loop_count (let the loop-budget guard work
                # too) and do NOT clear blade_uid (any existing
                # injection still needs the recover path to find it).
                _fs = fail_state(
                    FailureCategory.REPLAN_EXHAUSTED,
                    f"attempts={current_replan_count}, last_error={(replan_context or {}).get('error_summary', '')[:200]}",
                    state.get("messages", []) + result.get("messages", []),
                )
                result.update(_fs)
                # Drop replan_requested so a leftover True from a
                # prior iteration can't keep the router circling.
                result["replan_requested"] = False
                logger.warning(
                    "Replan exhausted: LLM emitted [REPLAN] but "
                    "replan_count=%d already at max=%d; converting to "
                    "terminal failure",
                    current_replan_count, _max_replan,
                )

        tracker.complete(f"Execute loop iteration {count} done")
        await sync_to_store(state, result)
        # Patch C — wall-clock cause labelling. If the router is about
        # to terminate this loop due to ``settings.max_inject_seconds``,
        # stamp ``failure_reason = WALL_CLOCK_TIMEOUT`` so the result
        # envelope is honest. Only fires when budget > 0 and started.
        from chaos_agent.agent.router import mark_wall_clock_timeout
        return mark_wall_clock_timeout(state, result)

    return _execute_loop_with_llm
