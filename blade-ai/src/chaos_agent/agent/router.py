"""Router functions: conditional edges for the inject graph."""

import time

from langchain_core.messages import ToolMessage
from langgraph.graph import END

from chaos_agent.agent.nodes._verifier_submit import (
    SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
    SUBMIT_VERIFICATION_TOOL_NAME,
)
from chaos_agent.agent.operation_outcome import (
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
)
from chaos_agent.agent.skill_identity import has_active_skill
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
    wall-clock budget is exceeded, write ``error`` + ``failure_detail``
    so the eventual result envelope says **why** it ended.

    Idempotent: existing ``error`` / ``failure_detail`` values win
    (an LLM-detected error is more specific than "we ran out of
    time"). Returns ``result`` unchanged for direct chaining.
    """
    if not _wall_clock_exceeded(state):
        return result
    # Prefer pre-existing causes — wall-clock is the catch-all.
    if not result.get("error"):
        budget = int(settings.max_inject_seconds or 0)
        result["error"] = f"wall-clock timeout ({budget}s)"
    if not result.get("failure_detail"):
        from chaos_agent.agent.state_helpers import fail_state
        from chaos_agent.agent.verdict import FailureCategory
        budget = int(settings.max_inject_seconds or 0)
        _fs = fail_state(FailureCategory.WALL_CLOCK_TIMEOUT, f"budget={budget}s")
        result.setdefault("failure_detail", _fs["failure_detail"])
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

    # Error set by agent_loop node (terminal conclusion detection)
    if read_operation_outcome(state).error:
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
            # If a skill was activated → planning complete, proceed to metadata extraction
            if has_active_skill(state):
                return "extract_planning_metadata"
            # No skill yet → might still be planning,
            # continue the loop to give LLM more turns
            return "continue"

    # Fallback: if there's a plan and skill_name from a previous iteration,
    # proceed to metadata extraction
    if state.get("plan") and has_active_skill(state):
        return "extract_planning_metadata"

    # Otherwise continue the ReAct loop
    return "continue"


def route_after_phase1_tools(state: AgentState) -> str:
    """Route after phase1_tools ToolNode execution.

    Detects if the just-executed tool batch contains a planning-exit
    signal (finish_planning or save_fault_plan). If so, skip the extra
    agent_loop iteration and go directly to extract_planning_metadata.

    Skips error ToolMessages (status="error") — those indicate the tool
    invocation failed (e.g. arg validation) and the LLM should retry.
    """
    messages = state.get("messages", [])
    if not messages:
        return "agent_loop"

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break
        if getattr(msg, "status", None) == "error":
            continue
        msg_name = getattr(msg, "name", "") or ""
        if msg_name in ("finish_planning", "save_fault_plan"):
            return "extract_planning_metadata"
        if msg_name == "propose_plan_change" and state.get("replan_context"):
            return "plan_change_confirm"

    return "agent_loop"


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
    outcome = read_operation_outcome(state)
    if outcome.error:
        if _should_replan(state, outcome.error):
            return "replan"
        return "end"

    # Check the last message for tool_calls (LLM ReAct pattern)
    # blade_uid alone does NOT mean execution is complete — hybrid injections
    # (blade_create + kubectl steps) need to continue after blade succeeds.
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # If the last message is an AI message without tool_calls,
        # check whether execution actually succeeded before routing to verifier.
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            if state.get("blade_uid"):
                return "verifier"
            if state.get("injection_method"):
                return "verifier"
            # Text-only without blade_uid: the execute_loop node's
            # terminal-conclusion detection normally sets error (caught
            # by the error check above → "end"). This "continue" is a
            # fallback for edge cases (empty content, replan cleared
            # the error, etc.).
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
    """Decide what happens after the verifier_loop LLM step (Scheme B).

    verifier_loop is now a pure ReAct step; finalization lives in the
    finalize_verification node.

    Returns:
        "continue" - LLM emitted tool_calls (incl. submit_verification) →
                     run them in verifier_tools, then route_after_verifier_tools
                     decides finalize vs continue.
        "finalize" - LLM emitted text without tool_calls → hand the text
                     verdict to finalize_verification (text fallback).
        "done"     - early-exit terminal: wall-clock / max iterations, or
                     verification already set inline by verifier_loop
                     (max-guard or Layer 1 failure) → straight to se_detect.

    Re-verification is NOT handled here anymore — finalize_verification sets
    the reverify prompt and route_after_finalize loops back to verifier_loop.
    """
    # Early-exit terminals set verification inline (node max-guard at
    # count>max, or Layer 1 failure). Those are truly done.
    if read_inject_verification(state):
        return "done"

    # Patch C — wall-clock cap. A timeout is an ABNORMAL cutoff: the node
    # already stamped a failure (mark_wall_clock_timeout); give up cleanly.
    if _wall_clock_exceeded(state):
        return "done"

    # Max-iteration cap. On the final allowed iteration (count==max) the node
    # forces a text-only verdict (JSON mode / unbound tools) — a NORMAL forced
    # completion. That verdict must still be PROCESSED, so route to
    # finalize_verification rather than dropping it via "done" (which would
    # leave verification unset and lose the verdict).
    max_loop = settings.max_verifier_loop
    count = state.get("verifier_loop_count", 0)
    if count >= max_loop:
        return "finalize"

    # Check the last message for tool_calls
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # tool_calls (incl. submit_verification) → run them; routing after
        # verifier_tools decides finalize vs continue.
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # AI text without tool_calls → finalize from text (fallback path).
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            return "finalize"

    # Default: continue the loop.
    return "continue"


def route_after_verifier_tools(state: AgentState) -> str:
    """Route after the verifier_tools ToolNode (Scheme B).

    Mirrors ``route_after_phase1_tools``: scan the just-executed
    ToolMessages; if ``submit_verification`` ran, the verifier declared its
    verdict → go to finalize_verification. Otherwise it was ordinary
    evidence-gathering (kubectl/...) → back to verifier_loop for the next
    ReAct turn. Error ToolMessages are skipped so a failed call doesn't
    masquerade as a submit.
    """
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break
        if getattr(msg, "status", None) == "error":
            continue
        if getattr(msg, "name", "") == SUBMIT_VERIFICATION_TOOL_NAME:
            return "finalize"
    return "verifier_loop"


def route_after_finalize(state: AgentState) -> str:
    """Route after finalize_verification (Scheme B).

    finalize sets ``verification`` only when it has a final verdict. When it
    instead found verification gaps with budget remaining, it leaves
    ``verification`` unset and appends a re-verify prompt → loop back to
    verifier_loop. Otherwise → se_detect.
    """
    if read_inject_verification(state):
        return "se_detect"
    return "verifier_loop"


def should_continue_recover_verifier(state: AgentState) -> str:
    """Decide what happens after the recover_verifier_loop step (Scheme B).

    recover_verifier_loop is now a pure ReAct step; Layer 2 finalization lives
    in the finalize_recover_verification node.

    Returns:
        "continue" - LLM emitted tool_calls (incl. submit_recover_verification),
                     OR a Layer 1 → Layer 2 transition text (RECOVERY_EXECUTION_RESULT
                     before Layer 2 has built its context).
        "finalize" - a Layer 2 verdict text (no tool_calls, Layer 2 context built)
                     → finalize_recover_verification (text fallback).
        "done"     - early-exit terminal: wall-clock / max iterations, or
                     recover_verification already set inline (max-guard or
                     Layer 1 failure) → END.

    The Layer 1 → Layer 2 transition is distinguished from a Layer 2 verdict by
    ``layer2_context_added``: it's only True once Layer 2 has run, so transition
    text (before Layer 2) routes "continue", while verdict text routes "finalize".
    """
    # Early-exit terminals set recover_verification inline (node max-guard at
    # count>max, or Layer 1 failure). Those are truly done.
    if read_recover_verification(state):
        return "done"

    # Patch C — wall-clock cap: abnormal cutoff, the node stamped a failure.
    if _wall_clock_exceeded(state):
        return "done"

    # Max-iteration cap. On the final allowed iteration the node forces a
    # text-only Layer 2 verdict, which must still be processed → route to
    # finalize_recover_verification. BUT only when we're actually in Layer 2
    # (layer2_context_added): the recover node's Layer 1 recovery-execution
    # sub-loop also consumes verifier_loop_count, and a Layer-1 transition text
    # is NOT a verdict. If the budget ran out still in Layer 1, we're done
    # (Layer 2 never reached) — matching the pre-Scheme-B behaviour.
    max_loop = settings.max_recover_verifier_loop
    count = state.get("verifier_loop_count", 0)
    if count >= max_loop:
        return "finalize" if state.get("layer2_context_added") else "done"

    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # tool_calls (incl. submit_recover_verification) → run them; routing
        # after recover_verifier_tools decides finalize vs continue.
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        if hasattr(last_msg, "type") and last_msg.type == "ai":
            # AI text: a Layer 2 verdict (context built) → finalize;
            # a Layer 1 → Layer 2 transition (context not yet built) → continue.
            if state.get("layer2_context_added"):
                return "finalize"
            return "continue"

    # Default: continue
    return "continue"


def route_after_recover_verifier_tools(state: AgentState) -> str:
    """Route after the recover_verifier_tools ToolNode (Scheme B).

    Mirrors route_after_verifier_tools: if submit_recover_verification ran, the
    verifier declared its verdict → finalize_recover_verification. Otherwise it
    was ordinary evidence-gathering / recovery actions → back to
    recover_verifier_loop.
    """
    messages = state.get("messages", [])
    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break
        if getattr(msg, "status", None) == "error":
            continue
        if getattr(msg, "name", "") == SUBMIT_RECOVER_VERIFICATION_TOOL_NAME:
            return "finalize"
    return "recover_verifier_loop"


def route_after_recover_finalize(state: AgentState) -> str:
    """Route after finalize_recover_verification (Scheme B).

    finalize sets ``recover_verification`` only when it has a final verdict.
    When it instead found a gap (no kubectl check) or retried recovery, it
    leaves recover_verification unset and appends a prompt → loop back to
    recover_verifier_loop. Otherwise → END.
    """
    if read_recover_verification(state):
        return "done"
    return "recover_verifier_loop"


def route_pipeline_start(state: AgentState) -> str:
    """Pipeline Graph entry routing — four paths.

    Returns:
        "direct_setup"  - CLI direct mode
        "plan_builder"  - TUI /plan dry-run
        "batch_setup"   - batch inject (from submit_batch_intent)
        "agent_loop"    - CLI NL / TUI inject
    """
    if state.get("direct", False):
        return "direct_setup"
    if state.get("dry_run") and state.get("interaction_mode") == "tui":
        return "plan_builder"
    if state.get("batch_submit_args"):
        return "batch_setup"
    return "agent_loop"


def route_after_load_memory(state: AgentState) -> str:
    """Decide which path to take after load_memory.

    Returns:
        "direct_setup" - direct mode: skip LLM, go to deterministic setup
        "plan_builder" - TUI /plan mode: guided plan construction
        "safety_check" - /run after plan_builder (spec + skill_name ready)
        "intent_clarification" - TUI mode: go to intent recognition first
        "agent_loop" - CLI mode: normal ReAct planning loop
    """
    if state.get("direct", False):
        return "direct_setup"
    # TUI /plan mode: guided plan construction
    if state.get("dry_run") and state.get("interaction_mode") == "tui":
        return "plan_builder"
    # /run after plan_builder completed: skip directly to safety_check.
    # plan_builder has already set skill_name (via activate_skill interception)
    # and fault_spec (via submit_plan). All safety_check prerequisites met.
    if (
        not state.get("dry_run")
        and state.get("interaction_mode") == "tui"
        and _spec_ready_for_execute(state)
    ):
        return "safety_check"
    # TUI mode: route to intent_clarification for guided conversation
    if state.get("interaction_mode") == "tui":
        return "intent_clarification"
    return "agent_loop"


def _spec_ready_for_execute(state: AgentState) -> bool:
    """Check if plan_builder has completed and spec is ready for execution.

    Requirements for safety_check:
      - plan_confirmed: submit_plan was called successfully
      - fault_spec.is_complete: scope/target/action/namespace all filled
      - skill_name: activate_skill was called during plan building
    """
    if not state.get("plan_confirmed"):
        return False
    if not has_active_skill(state):
        return False
    from chaos_agent.agent.fault_spec import read_fault_spec
    spec = read_fault_spec(state)
    return spec is not None and getattr(spec, "is_complete", False)


def route_after_intent_clarification(state: AgentState) -> str:
    """Decide what happens after intent_clarification.

    Returns:
        "agent_loop"       - user confirmed fault injection intent (inject or batch_inject)
        "recover_handler"  - user wants to recover a previous injection
        "save_memory"      - chat intent (direct end, no special handler)
        "intent_clarification" - intent still unclear, continue dialogue
    """
    confirmed_intent = state.get("confirmed_intent")
    if confirmed_intent in ("inject", "batch_inject"):
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
    - batch_inject → "intent_confirm" (user confirms batch intent before execution)
    - has tool_calls (kubectl, etc.) → "continue" (ReAct within single invocation)
    - pure text → END (conversation turn done, TUI waits for next input)

    Returns:
        "continue"         - LLM has tool_calls (kubectl, etc.), continue the loop
        "intent_confirm"   - intent confirmed as inject or batch_inject
        "recover_handler"  - intent confirmed as recover
        "save_memory"      - chat intent (direct end)
        END                - conversation turn done, wait for next user input
    """
    # Check confirmed_intent first
    confirmed_intent = state.get("confirmed_intent")
    if confirmed_intent in ("inject", "batch_inject"):
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


def should_continue_plan_builder(state: AgentState) -> str:
    """Decide whether to continue the plan_builder ReAct loop.

    Returns:
        "continue" - has tool_calls (kubectl_ro etc.), go to plan_builder_tools
        END        - pure text or submit_plan handled, graph done for this turn
    """
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
    return END


def route_after_intent_confirm(state: AgentState) -> str:
    """Route after intent confirmation gate.

    If user approved (confirmed_intent still "inject" + fault_spec exists),
    proceed to agent_loop. Otherwise user rejected/modified — graph ends,
    TUI waits for next input to continue the conversation.

    Returns:
        "agent_loop" - user confirmed, proceed to planning/execution
        END          - user rejected, wait for next input
    """
    if state.get("confirmed_intent") in ("inject", "batch_inject") and state.get("fault_spec"):
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
    if read_operation_outcome(state).error:
        return "end"
    return "verifier"


def route_after_save_memory(state: AgentState) -> str:
    """Decide what happens after save_memory.

    Returns:
        "batch_next" - batch in progress, collect result and advance index
        END          - non-batch path (single inject, recover, chat)

    Always routes to batch_next when batch_submit_args has faults —
    including the last fault. batch_next appends the result, then
    route_after_batch_next decides whether to loop or END.
    """
    batch_args = state.get("batch_submit_args")
    if batch_args and isinstance(batch_args, dict) and batch_args.get("faults"):
        return "batch_next"
    return END


def route_after_batch_next(state: AgentState) -> str:
    """Decide what happens after batch_next.

    Returns:
        "batch_setup" - more faults to execute
        END           - all faults completed
    """
    batch_args = state.get("batch_submit_args")
    if not batch_args or not isinstance(batch_args, dict):
        return END
    faults = batch_args.get("faults", [])
    current = state.get("current_fault_index", 0)
    if current < len(faults):
        return "batch_setup"
    return END
