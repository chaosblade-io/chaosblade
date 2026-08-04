"""Router functions: conditional edges for the inject graph."""

import logging
import time

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END

from chaos_agent.agent.nodes.verify._verifier_submit import (
    SUBMIT_RECOVER_VERIFICATION_TOOL_NAME,
    SUBMIT_VERIFICATION_TOOL_NAME,
)
from chaos_agent.agent.node_names import (
    AGENT_LOOP,
    BASELINE_CAPTURE,
    BATCH_SETUP,
    CONFIRMATION_GATE,
    DIRECT_EXECUTE,
    DIRECT_SETUP,
    EXECUTE_LOOP,
    EXTRACT_PLANNING_METADATA,
    INTENT_CLARIFICATION,
    INTENT_CONFIRM,
    PLAN_BUILDER,
    PLAN_CHANGE_CONFIRM,
    RECOVER_HANDLER,
    RECOVER_VERIFIER_LOOP,
    REJECT,
    SAFETY_CHECK,
    SAVE_MEMORY,
    SE_DETECT,
    VERIFIER_LOOP,
)
from chaos_agent.agent.result.operation_outcome import (
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
)
from chaos_agent.agent.spec.skill_identity import has_active_skill
from chaos_agent.agent.spec.fault_spec import (
    FaultSpec,
    is_full_fault_spec_proposal,
    read_fault_spec,
)
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings

logger = logging.getLogger(__name__)


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


def _is_empty_ai_turn(msg) -> bool:
    """An AI message with no tool_calls AND no text — the model said nothing.

    Not the same as a text conclusion, though both look identical to a check that
    only asks "were there tool_calls?". Measured in task-a8ad1602: the executor
    returned ``content=""`` with a ``<function=finish_execution>`` string stranded
    in ``reasoning_content`` (a tool that does not exist — it copied planning's
    ``finish_planning`` pattern into a phase whose exit protocol is plain text),
    and the verifier returned ``content=""`` with a complete, correct
    ``submit_verification`` payload stranded the same way. Both advanced: the
    executor to the verifier, and the verifier into its text fallback, which
    parsed the empty string into ``level=partial`` and shipped
    ``status=success`` — while the model's own verdict said ``verified``.

    Treating this as "keep going" costs an iteration. Treating it as a
    conclusion costs the correctness of the whole result.
    """
    if getattr(msg, "tool_calls", None):
        return False
    if getattr(msg, "type", "") != "ai":
        return False
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return not content.strip()
    # Multi-part content: empty list / all-blank blocks count as nothing said.
    return not content


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
        from chaos_agent.agent.state_mgmt.state_helpers import fail_state
        from chaos_agent.agent.result.verdict import FailureCategory
        budget = int(settings.max_inject_seconds or 0)
        _fs = fail_state(FailureCategory.WALL_CLOCK_TIMEOUT, f"budget={budget}s")
        result.setdefault("failure_detail", _fs["failure_detail"])
    return result


def mark_loop_exhausted(
    result: dict,
    count: int,
    max_loop: int,
    *,
    category=None,
    label: str = "execute loop",
) -> dict:
    """Record that a loop is stopping because its iteration budget ran out.

    The companion to :func:`mark_wall_clock_timeout`, and it exists because the
    two halves of the budget check disagreed by one. The router terminates on
    ``count >= max_loop``, while the node only stamped a cause on
    ``count > MAX_EXECUTE_LOOP`` — so a run that used its budget EXACTLY (the
    normal way to exhaust it) was terminated with ``failure_reason=""``.

    Measured in task-ff057e7f: exactly 100 of 100 iterations, router ended the
    run, ``failure_reason`` empty, and the envelope reported success. Nothing
    downstream could tell that the executor never concluded.

    Idempotent and deferential: an existing ``error`` is more specific than "we
    ran out of iterations", so it wins. Returns ``result`` for direct chaining.

    Takes no ``state``, unlike :func:`mark_wall_clock_timeout`: the counts come
    from the caller, which already has them. A ``state`` parameter kept only for
    signature symmetry would imply this reads state and does not.
    """
    if max_loop <= 0 or count < max_loop:
        return result
    from chaos_agent.agent.result.verdict import FailureCategory

    _cat = category or FailureCategory.EXECUTION_TIMEOUT
    if not result.get("error"):
        result["error"] = (
            f"{label} budget exhausted ({count}/{max_loop} iterations) "
            f"without a conclusion"
        )
    # ``failure_reason`` is its own state field, not something ``fail_state``
    # produces — that helper returns only ``error`` + ``failure_detail``. The
    # envelope reads the field directly, so it has to be set here or the result
    # shows an empty reason even though a cause is known.
    if not result.get("failure_reason"):
        result["failure_reason"] = _cat.value
    if not result.get("failure_detail"):
        from chaos_agent.agent.state_mgmt.state_helpers import fail_state

        _fs = fail_state(_cat, f"max_iterations={max_loop}")
        result.setdefault("failure_detail", _fs["failure_detail"])
    return result


def _should_replan(state: AgentState, error_msg: str | None = None) -> bool:
    """Check whether the current state qualifies for replan to Phase 1.

    Replan is allowed when:
    - replan_count < max_replan_count (loop limit)
    - Either the LLM explicitly requested structured replan, or auto-detect patterns match
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
        return REJECT
    max_loop = settings.max_agent_loop
    count = state.get("agent_loop_count", 0)

    # Check for max iterations — always reject, regardless of skill_name
    if count >= max_loop:
        return REJECT

    # If safety_status is already set to rejected, go to reject
    if state.get("safety_status") == "rejected":
        return REJECT

    # Error set by agent_loop node (terminal conclusion detection)
    if read_operation_outcome(state).error:
        return REJECT

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
                return EXTRACT_PLANNING_METADATA
            # No skill yet → might still be planning,
            # continue the loop to give LLM more turns
            return "continue"

    # Fallback: if there's a plan and skill_name from a previous iteration,
    # proceed to metadata extraction
    if state.get("plan") and has_active_skill(state):
        return EXTRACT_PLANNING_METADATA

    # Otherwise continue the ReAct loop
    return "continue"


def _tool_call_args_for_result(
    messages: list,
    *,
    tool_name: str,
    tool_call_id: str,
) -> dict | None:
    """Find the call that produced one ToolNode result in the current turn."""

    for message in reversed(messages):
        if not isinstance(message, AIMessage):
            continue
        for call in getattr(message, "tool_calls", None) or []:
            name = call.get("name") if isinstance(call, dict) else getattr(call, "name", "")
            call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", "")
            if name == tool_name and call_id == tool_call_id:
                args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
                return args if isinstance(args, dict) else None
        # The immediately preceding AI message owns every trailing ToolMessage.
        break
    return None


def _planning_contract_route(
    state: AgentState,
    *,
    tool_name: str,
    tool_call_id: str,
) -> str:
    """Route Phase 1 exit.

    ``finish_planning`` is a pure "planning complete" signal — the reviewed
    FaultSpec in state is the sole authority, immutable except through an
    explicit, user-confirmed ``propose_plan_change``. So finish_planning
    always proceeds; only ``propose_plan_change`` carries a proposed contract
    that is diffed against the reviewed FaultSpec.
    """
    if tool_name == "finish_planning":
        return EXTRACT_PLANNING_METADATA

    expected = read_fault_spec(state)
    if expected is None:
        logger.warning(
            "planning route: propose_plan_change without a reviewed "
            "FaultSpec in state; staying in the agent loop",
        )
        return AGENT_LOOP

    args = _tool_call_args_for_result(
        state.get("messages", []),
        tool_name=tool_name,
        tool_call_id=tool_call_id,
    )
    if args is None:
        logger.warning(
            "planning route: propose_plan_change ToolMessage has no owning "
            "tool_call; staying in the agent loop",
        )
        return AGENT_LOOP

    try:
        revision = int(args.get("fault_revision"))
    except (TypeError, ValueError):
        logger.warning(
            "planning route: propose_plan_change without a parseable "
            "fault_revision; staying in the agent loop",
        )
        return AGENT_LOOP
    raw_fault = args.get("proposed_fault")
    if not is_full_fault_spec_proposal(raw_fault):
        # Task-5193538b: no silent discard. The tool surface now refuses
        # partial contracts with the missing-field list (its "Error:" reply
        # keeps this branch unreachable in normal flow); if a partial
        # proposal still arrives, log it instead of dropping it quietly.
        logger.warning(
            "planning route: propose_plan_change carried a partial "
            "FaultSpec proposal; staying in the agent loop "
            "(the tool reply should have listed the missing fields)",
        )
        return AGENT_LOOP
    actual = FaultSpec.from_intent_args(raw_fault, existing=expected)
    if not actual.is_complete:
        logger.warning(
            "planning route: propose_plan_change proposal failed FaultSpec "
            "completion checks; staying in the agent loop",
        )
        return AGENT_LOOP
    if revision != expected.revision or actual.contract_dict() == expected.contract_dict():
        # Task-5193538b: used to return AGENT_LOOP silently — the model was
        # told "Plan change proposed." and nothing ever happened. Both
        # conditions are RECOVERABLE mistakes, and plan_change_confirm
        # already answers them with an actionable [PLAN CHANGE RETRY]
        # HumanMessage (stale revision / unchanged contract). Route there
        # instead of discarding.
        logger.info(
            "planning route: propose_plan_change %s; delegating to "
            "plan_change_confirm for the retry message",
            (
                f"referenced stale revision {revision} "
                f"(current {expected.revision})"
                if revision != expected.revision
                else "does not change the reviewed contract"
            ),
        )
        return PLAN_CHANGE_CONFIRM
    return PLAN_CHANGE_CONFIRM


def route_after_phase1_tools(state: AgentState) -> str:
    """Route after phase1_tools ToolNode execution.

    Detects if the just-executed tool batch contains the planning-exit
    signal (finish_planning). ``save_fault_plan`` only persists the draft;
    Phase 1 must continue so the planner can explicitly finalize it.

    Skips error ToolMessages (status="error" or an ``Error:`` result) — those
    indicate the tool invocation failed (e.g. arg validation) and the LLM
    should retry.
    """
    messages = state.get("messages", [])
    if not messages:
        return AGENT_LOOP

    for msg in reversed(messages):
        if not isinstance(msg, ToolMessage):
            break
        content = getattr(msg, "content", "")
        if getattr(msg, "status", None) == "error" or (
            isinstance(content, str) and content.startswith("Error:")
        ):
            continue
        msg_name = getattr(msg, "name", "") or ""
        if msg_name in {"finish_planning", "propose_plan_change"}:
            return _planning_contract_route(
                state,
                tool_name=msg_name,
                tool_call_id=getattr(msg, "tool_call_id", ""),
            )

    return AGENT_LOOP


def should_continue_execute_loop(state: AgentState) -> str:
    """Decide whether to continue the execute_loop or move to verifier.

    Returns:
        "continue" - more execution iterations needed (LLM output has tool_calls)
        "verifier" - execution finished OR was cut short (pure text, blade_uid
                     present, tool error, budget exhausted, wall-clock expiry)
        "replan" - error should be fed back to Phase 1 for re-planning

    There is deliberately no "end": every way this loop can stop now goes
    through verification. The graph mapping omits it too, so a future ``return
    "end"`` raises KeyError at runtime instead of silently skipping the verifier
    — the failure mode this function had in task-ff057e7f.
    """
    # Wall-clock cap and loop-budget exhaustion both mean "we stopped without
    # the model concluding", and BOTH still route to the verifier.
    #
    # This used to ``return "end"``, which sent the run straight to save_memory
    # and skipped verification entirely. Measured in task-ff057e7f: the executor
    # burned exactly 100 iterations polling pod status, the router ended the run,
    # and the envelope reported ``status=success / task_state=injected`` with
    # ``verification=null`` — while the generated postmortem said the experiment
    # "陷入无限循环，未能执行注入指令". Two parts of the same result disagreed
    # because only one of them had any idea whether the fault took effect.
    #
    # The rule already existed 20 lines below for the error branch — "error is a
    # signal, not a verdict; the verifier MUST still check" — and exhaustion is
    # the same kind of signal, only less informative: the model never said
    # anything at all. Routing it to "end" applied the opposite policy to the
    # MORE uncertain case. The verifier has its own iteration budget and its own
    # wall-clock check, so this cannot loop forever.
    if _wall_clock_exceeded(state):
        return "verifier"
    max_loop = settings.max_execute_loop
    count = state.get("execute_loop_count", 0)

    if count >= max_loop:
        if _should_replan(state):
            return "replan"
        return "verifier"

    # LLM explicitly requested replan
    if _should_replan(state):
        return "replan"

    # Error from execute_loop — the injection action may have issues,
    # but the verifier MUST still check whether the fault actually took
    # effect. Error is a signal, not a verdict; only replan short-circuits.
    outcome = read_operation_outcome(state)
    if outcome.error:
        if _should_replan(state, outcome.error):
            return "replan"
        return "verifier"

    # Check the last message for tool_calls (LLM ReAct pattern)
    # blade_uid alone does NOT mean execution is complete — hybrid injections
    # (blade_create + kubectl steps) need to continue after blade succeeds.
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        # If the last message has tool_calls, continue the ReAct loop
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
        # An empty AI turn is not a conclusion — keep the loop going rather
        # than reading "nothing" as "done". See _is_empty_ai_turn.
        if _is_empty_ai_turn(last_msg):
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
        "agent_loop" - recoverable issue (e.g. no skill activated),
                       feed back to planner for self-correction
    """
    safety_status = state.get("safety_status", "pending")

    if safety_status == "retry":
        return AGENT_LOOP

    if safety_status == "rejected":
        return REJECT

    # Dry-run requests must always pass through confirmation_gate so the
    # preview AIMessage is emitted; the gate's body short-circuits the
    # interrupt and the post-gate router sends us to END.
    if state.get("dry_run"):
        return CONFIRMATION_GATE

    if state.get("needs_confirmation", False):
        return CONFIRMATION_GATE

    if safety_status == "safe":
        return BASELINE_CAPTURE  # All modes share baseline_capture

    # confirm_required (P1): route to confirmation_gate with stricter checks
    # warning or pending: needs confirmation
    return CONFIRMATION_GATE


def route_after_confirmation(state: AgentState) -> str:
    """Decide what happens after confirmation_gate.

    Returns:
        "end" - dry_run preview: short-circuit before any side-effecting node
        "baseline_capture" - approved (all modes), collect baseline then execute
        "reject" - rejected
    """
    if state.get("safety_status") == "rejected":
        return REJECT

    # Dry-Run mode: confirmation_gate has already emitted the preview AIMessage;
    # the graph must terminate without entering baseline_capture/execute.
    if state.get("dry_run"):
        return "end"

    return BASELINE_CAPTURE  # All modes share baseline_capture


def route_after_baseline(state: AgentState) -> str:
    """Decide what happens after baseline_capture.

    baseline_capture is shared across all modes (direct and NL).
    After baseline is collected, the flow diverges by execution mode:

    Returns:
        "direct_execute" - direct mode: deterministic skill execution
        "execute_loop"   - NL mode: LLM ReAct loop for blade_create
    """
    if state.get("direct", False):
        return DIRECT_EXECUTE
    return EXECUTE_LOOP


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
        # The text fallback needs TEXT. An empty turn has none, and finalize
        # would parse "" into a verdict — see _is_empty_ai_turn.
        if _is_empty_ai_turn(last_msg):
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
    return VERIFIER_LOOP


def route_after_finalize(state: AgentState) -> str:
    """Route after finalize_verification (Scheme B).

    finalize sets ``verification`` only when it has a final verdict. When it
    instead found verification gaps with budget remaining, it leaves
    ``verification`` unset and appends a re-verify prompt → loop back to
    verifier_loop. Otherwise → se_detect.

    When finalize detects an unverified + L2-failed verdict with replan
    budget remaining, it sets ``replan_requested`` → route to agent_loop
    for re-planning with verifier feedback as context.
    """
    if state.get("replan_requested"):
        return "replan"
    if read_inject_verification(state):
        return SE_DETECT
    return VERIFIER_LOOP


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
    return RECOVER_VERIFIER_LOOP


def route_after_recover_finalize(state: AgentState) -> str:
    """Route after finalize_recover_verification (Scheme B).

    finalize sets ``recover_verification`` only when it has a final verdict.
    When it instead found a gap (no kubectl check) or retried recovery, it
    leaves recover_verification unset and appends a prompt → loop back to
    recover_verifier_loop. Otherwise → END.
    """
    if read_recover_verification(state):
        return "done"
    return RECOVER_VERIFIER_LOOP


def route_pipeline_start(state: AgentState) -> str:
    """Pipeline Graph entry routing — four paths.

    Returns:
        "direct_setup"  - CLI direct mode
        "plan_builder"  - TUI /plan dry-run
        "batch_setup"   - batch inject (from submit_batch_intent)
        "agent_loop"    - CLI NL / TUI inject
    """
    if state.get("direct", False):
        return DIRECT_SETUP
    if state.get("dry_run") and state.get("interaction_mode") == "tui":
        return PLAN_BUILDER
    if state.get("batch_submit_args"):
        return BATCH_SETUP
    return AGENT_LOOP


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
        return AGENT_LOOP
    if confirmed_intent == "recover":
        return RECOVER_HANDLER
    if confirmed_intent == "chat":
        return SAVE_MEMORY
    # Intent is unclear — continue the clarification dialogue
    return INTENT_CLARIFICATION


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
        return INTENT_CONFIRM
    if confirmed_intent == "recover":
        return RECOVER_HANDLER
    if confirmed_intent == "chat":
        return SAVE_MEMORY

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
        "continue" - has tool_calls (kubectl_read etc.), go to plan_builder_tools
        END        - pure text or submit_plan handled, graph done for this turn
    """
    messages = state.get("messages", [])
    if messages:
        last_msg = messages[-1]
        if hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
            return "continue"
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
        return BATCH_SETUP
    return END
