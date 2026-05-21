"""Router functions: conditional edges for the inject graph."""

import time

from langgraph.graph import END

from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings


def _wall_clock_exceeded(state: AgentState) -> bool:
    """Patch C — has the inject turn run past ``settings.max_inject_seconds``?

    Reads ``state.pipeline_started_at`` (stamped on first agent_loop
    entry). Returns ``False`` when the budget is disabled (``0``) or
    the timestamp hasn't been stamped yet — this is intentional so the
    guard never fires on the very first node before instrumentation
    has had a chance to run.

    Used by every ``should_continue_*`` so a single setting governs
    inject / execute / verifier / recover loops uniformly.

    Note on observability: this is a **read-only** check. The router
    can't write state (LangGraph conditional edges are pure routing
    functions). The companion helper ``mark_wall_clock_timeout`` (in
    each node) writes ``state.error`` + ``state.failure_reason`` so
    the user-facing result envelope honestly reports
    ``WALL_CLOCK_TIMEOUT`` instead of an empty failure.
    """
    budget = int(settings.max_inject_seconds or 0)
    if budget <= 0:
        return False
    started = float(state.get("pipeline_started_at", 0.0) or 0.0)
    if started <= 0.0:
        return False
    return (time.time() - started) > budget


def mark_wall_clock_timeout(state: AgentState, result: dict) -> dict:
    """Patch C — mutate ``result`` to record wall-clock timeout cause.

    Each LLM-loop node (``agent_loop``, ``execute_loop``, ``verifier``,
    ``recover_verifier``) calls this just before returning. If the
    wall-clock budget is exceeded, write ``error`` + ``failure_reason``
    so the eventual result envelope says **why** it ended.

    Idempotent: existing ``error`` / ``failure_reason`` values win
    (an LLM-detected error is more specific than "we ran out of
    time"). Returns ``result`` unchanged for direct chaining.
    """
    if not _wall_clock_exceeded(state):
        return result
    # Prefer pre-existing causes — wall-clock is the catch-all.
    if not result.get("error"):
        budget = int(settings.max_inject_seconds or 0)
        result["error"] = f"wall-clock timeout ({budget}s)"
    if not result.get("failure_reason"):
        # Late import to avoid module-load circularity (errors imports
        # nothing from agent, but agent.router is imported during
        # graph build before errors module finishes loading).
        from chaos_agent.errors import FailureReason
        result["failure_reason"] = FailureReason.WALL_CLOCK_TIMEOUT.value
    return result


def _should_replan(state: AgentState, error_msg: str | None = None) -> bool:
    """Check whether the current state qualifies for replan to Phase 1.

    Replan is allowed when:
    - replan_count < max_replan_count (loop limit)
    - Either the LLM explicitly requested [REPLAN], or auto-detect patterns match
    """
    replan_count = state.get("replan_count", 0)
    try:
        max_replan = int(settings.max_replan_count)
    except (TypeError, ValueError):
        max_replan = 2

    if replan_count >= max_replan:
        return False

    # LLM explicitly requested replan
    if state.get("replan_requested"):
        return True

    # Auto-detect from error message patterns
    if error_msg and settings.replan_auto_trigger:
        from chaos_agent.errors import should_auto_replan
        return should_auto_replan(error_msg)

    return False


def should_continue_agent_loop(state: AgentState) -> str:
    """Decide whether to continue the agent_loop or proceed to extract_planning_metadata.

    Returns:
        "continue" - more ReAct iterations needed (LLM output has tool_calls, or no skill yet)
        "extract_planning_metadata" - planning complete (LLM output is pure text + skill activated)
        "reject" - max iterations exceeded OR wall-clock timeout reached
    """
    # Patch C — wall-clock cap. Treat the timeout as "reject" because
    # planning never completed; saving an incomplete plan is worse
    # than a clean reject signal that the caller can surface.
    if _wall_clock_exceeded(state):
        return "reject"
    max_loop = settings.max_agent_loop
    count = state.get("agent_loop_count", 0)

    # Check for max iterations — always reject, regardless of skill_name
    if count >= max_loop:
        return "reject"

    # If safety_status is already set to rejected, go to reject
    if state.get("safety_status") == "rejected":
        return "reject"

    # Check the last message for tool_calls (LLM ReAct pattern)
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # If the last message is an AI message without tool_calls,
        # the LLM has finished its turn
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            content = getattr(last_msg, "content", "") or ""
            # If a skill was activated → planning complete, proceed to metadata extraction
            if state.get("skill_name"):
                return "extract_planning_metadata"
            # No skill yet → might still be planning,
            # continue the loop to give LLM more turns
            return "continue"

    # Fallback: if there's a plan and skill_name from a previous iteration,
    # proceed to metadata extraction
    if state.get("plan") and state.get("skill_name"):
        return "extract_planning_metadata"

    # Otherwise continue the ReAct loop
    return "continue"


def should_continue_execute_loop(state: AgentState) -> str:
    """Decide whether to continue the execute_loop or move to verifier.

    Returns:
        "continue" - more execution iterations needed (LLM output has tool_calls)
        "verifier" - execution complete (LLM output is pure text or blade_uid present)
        "end" - max iterations exceeded, error, or wall-clock timeout
        "replan" - error should be fed back to Phase 1 for re-planning
    """
    # Patch C — wall-clock cap. End with whatever progress has been
    # made (preserving any blade_uid the LLM did manage to land before
    # the budget ran out). The downstream end-handler will set
    # ``failure_reason = WALL_CLOCK_TIMEOUT`` so the result envelope
    # is honest about why we stopped.
    if _wall_clock_exceeded(state):
        return "end"
    max_loop = settings.max_execute_loop
    count = state.get("execute_loop_count", 0)

    if count >= max_loop:
        if _should_replan(state):
            return "replan"
        return "end"

    # LLM explicitly requested replan
    if _should_replan(state):
        return "replan"

    # Error with auto-replan detection
    if state.get("error"):
        if _should_replan(state, state["error"]):
            return "replan"
        return "end"

    # If we have a blade_uid, execution likely succeeded
    if state.get("blade_uid"):
        return "verifier"

    # Check the last message for tool_calls (LLM ReAct pattern)
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # If the last message is an AI message without tool_calls,
        # check whether execution actually succeeded before routing to verifier.
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            # Only route to verifier if injection succeeded (blade_uid present).
            # Without blade_uid, the LLM may be proposing a correction plan
            # or preparing alternative approaches — keep executing.
            if state.get("blade_uid"):
                return "verifier"
            # No blade_uid: continue the loop to give LLM more turns.
            # Safety: max_loop guard at the top of this function prevents
            # unbounded execution.
            return "continue"

    return "continue"


def route_after_safety(state: AgentState) -> str:
    """Decide what happens after safety_check.

    Returns:
        "confirmation_gate" - needs confirmation before execution
        "baseline_capture" - safe (all modes), collect baseline metrics then execute
        "reject" - unsafe, reject the request
    """
    safety_status = state.get("safety_status", "pending")

    if safety_status == "rejected":
        return "reject"

    # Dry-run requests must always pass through confirmation_gate so the
    # preview AIMessage is emitted; the gate's body short-circuits the
    # interrupt and the post-gate router sends us to END.
    if state.get("dry_run"):
        return "confirmation_gate"

    if state.get("needs_confirmation", False):
        return "confirmation_gate"

    if safety_status == "safe":
        return "baseline_capture"  # All modes share baseline_capture

    # confirm_required (P1): route to confirmation_gate with stricter checks
    # warning or pending: needs confirmation
    return "confirmation_gate"


def route_after_confirmation(state: AgentState) -> str:
    """Decide what happens after confirmation_gate.

    Returns:
        "end" - dry_run preview: short-circuit before any side-effecting node
        "baseline_capture" - approved (all modes), collect baseline then execute
        "reject" - rejected
    """
    if state.get("safety_status") == "rejected":
        return "reject"

    # Dry-Run mode: confirmation_gate has already emitted the preview AIMessage;
    # the graph must terminate without entering baseline_capture/execute.
    if state.get("dry_run"):
        return "end"

    return "baseline_capture"  # All modes share baseline_capture


def route_after_baseline(state: AgentState) -> str:
    """Decide what happens after baseline_capture.

    baseline_capture is shared across all modes (direct and NL).
    After baseline is collected, the flow diverges by execution mode:

    Returns:
        "direct_execute" - direct mode: deterministic skill execution
        "execute_loop"   - NL mode: LLM ReAct loop for blade_create
    """
    if state.get("direct", False):
        return "direct_execute"
    return "execute_loop"


def should_continue_verifier(state: AgentState) -> str:
    """Decide whether to continue the inject verifier ReAct loop or finish.

    Checks the ``verification`` state key (inject verifier's output).

    P2: Also checks reverify_gaps — if gaps were detected and reverify_count
    hasn't exceeded max_reverify_attempts, forces another verification round.

    Returns:
        "continue" - more verification iterations needed (LLM has tool_calls)
        "done" - verification complete (LLM output is pure text, no tool_calls)
                 OR wall-clock timeout reached
    """
    # Patch C — wall-clock cap. Verifier is best-effort: stopping
    # early with whatever evidence has accumulated is preferable to
    # spinning Layer 2 indefinitely on an unstable cluster.
    if _wall_clock_exceeded(state):
        return "done"
    max_loop = settings.max_verifier_loop
    count = state.get("verifier_loop_count", 0)

    if count >= max_loop:
        return "done"

    # P2: re-verification triggered by verification gaps
    reverify_gaps = state.get("reverify_gaps")
    if reverify_gaps:
        reverify_count = state.get("reverify_count", 0)
        from chaos_agent.utils.fault_context import lookup_adaptations
        adaptations = lookup_adaptations(
            state.get("blade_scope", ""),
            state.get("blade_target", ""),
            state.get("blade_action", ""),
            state.get("target_metadata") or {},
            rule_type="verification_integrity_guard",
        )
        max_attempts = 1  # default
        if adaptations:
            max_attempts = adaptations[0].action.get("max_reverify_attempts", 1)
        if reverify_count <= max_attempts:
            return "continue"

    # If verification result is already set, we're done
    if state.get("verification"):
        return "done"

    # Check the last message for tool_calls
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # If the last message is an AI message without tool_calls,
        # verification is complete
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            return "done"

    # Default: continue
    return "continue"


def should_continue_recover_verifier(state: AgentState) -> str:
    """Decide whether to continue the recover verifier ReAct loop or finish.

    Checks the ``recover_verification`` state key (recover verifier's output).
    Separate from should_continue_verifier to avoid false "done" caused by
    the inject graph's ``verification`` key leaking through shared checkpoints.

    Returns:
        "continue" - more verification iterations needed (LLM has tool_calls)
        "done" - recovery verification complete OR wall-clock timeout
    """
    # Patch C — wall-clock cap (same rationale as inject verifier).
    if _wall_clock_exceeded(state):
        return "done"
    max_loop = settings.max_recover_verifier_loop
    count = state.get("verifier_loop_count", 0)

    if count >= max_loop:
        return "done"

    # If recover verification result is already set, we're done
    if state.get("recover_verification"):
        return "done"

    # If Layer 1 passed and we're transitioning to Layer 2, continue the loop
    # even though the last message is an AI message without tool_calls.
    # Without this check, the router would see the Layer 1 AI final result
    # (RECOVERY_EXECUTION_RESULT) and incorrectly return "done", skipping Layer 2.
    if state.get("recover_phase") == "layer2_verification":
        return "continue"

    # Check the last message for tool_calls
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # If the last message is an AI message without tool_calls,
        # verification is complete
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            return "done"

    # Default: continue
    return "continue"


def route_after_load_memory(state: AgentState) -> str:
    """Decide which path to take after load_memory.

    Returns:
        "direct_setup" - direct mode: skip LLM, go to deterministic setup
        "intent_clarification" - TUI mode: go to intent recognition first
        "agent_loop" - CLI mode: normal ReAct planning loop (skip intent_clarification)
    """
    if state.get("direct", False):
        return "direct_setup"
    # TUI mode: route to intent_clarification for guided conversation
    if state.get("interaction_mode") == "tui":
        return "intent_clarification"
    return "agent_loop"


def route_after_intent_clarification(state: AgentState) -> str:
    """Decide what happens after intent_clarification.

    Returns:
        "agent_loop"       - user confirmed fault injection intent
        "recover_handler"  - user wants to recover a previous injection
        "save_memory"      - chat intent (direct end, no special handler)
        "intent_clarification" - intent still unclear, continue dialogue
    """
    confirmed_intent = state.get("confirmed_intent")
    if confirmed_intent == "inject":
        return "agent_loop"
    if confirmed_intent == "recover":
        return "recover_handler"
    if confirmed_intent == "chat":
        return "save_memory"
    # Intent is unclear — continue the clarification dialogue
    return "intent_clarification"


def should_continue_intent_clarification(state: AgentState) -> str:
    """Decide whether to continue the intent_clarification ReAct loop.

    Multi-invocation model:
    - inject → "intent_confirm" (user must confirm intent before execution)
    - has tool_calls (kubectl, etc.) → "continue" (ReAct within single invocation)
    - pure text → END (conversation turn done, TUI waits for next input)

    Returns:
        "continue"         - LLM has tool_calls (kubectl, etc.), continue the loop
        "intent_confirm"   - intent confirmed as inject, needs user confirmation
        "recover_handler"  - intent confirmed as recover
        "save_memory"      - chat intent (direct end)
        END                - conversation turn done, wait for next user input
    """
    # Check confirmed_intent first
    confirmed_intent = state.get("confirmed_intent")
    if confirmed_intent == "inject":
        return "intent_confirm"
    if confirmed_intent == "recover":
        return "recover_handler"
    if confirmed_intent == "chat":
        return "save_memory"

    # No confirmed_intent — intent_clarification returns without
    # confirmed_intent when tool_calls are present (kubectl,
    # activate_skill, read_skill_resource). ToolNode must process them.
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"

    # No confirmed_intent and no tool_calls — LLM produced pure text.
    # Conversation turn is complete; graph ends, TUI waits for next input.
    return END


def route_after_intent_confirm(state: AgentState) -> str:
    """Route after intent confirmation gate.

    If user approved (confirmed_intent still "inject" + fault_intent exists),
    proceed to agent_loop. Otherwise user rejected/modified — graph ends,
    TUI waits for next input to continue the conversation.

    Returns:
        "agent_loop" - user confirmed, proceed to planning/execution
        END          - user rejected, wait for next input
    """
    if state.get("confirmed_intent") == "inject" and state.get("fault_intent"):
        return "agent_loop"
    return END


def route_after_direct_execute(state: AgentState) -> str:
    """Decide what happens after direct_execute.

    Returns:
        "verifier" - blade_uid present, proceed to verification
        "end" - error occurred, skip verification
    """
    if state.get("blade_uid"):
        return "verifier"
    if state.get("error"):
        return "end"
    return "verifier"
