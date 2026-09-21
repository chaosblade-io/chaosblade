"""StateGraph construction for inject, recover, and status graphs."""

import logging
import re

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from chaos_agent.agent.dispatch import with_phase_events, with_tool_span
from chaos_agent.agent.nodes._phase_screener import make_phase_screener
from chaos_agent.tools._strict_args import UNKNOWN_ARG_REFUSAL_MARKER
from chaos_agent.agent.nodes.recover._recover_finalize import make_finalize_recover_verification
from chaos_agent.agent.nodes.verify._verifier_finalize import make_finalize_verification
from chaos_agent.agent.nodes.execute.agent_loop import make_agent_loop
from chaos_agent.agent.nodes.baseline.baseline_capture import make_baseline_capture
from chaos_agent.agent.nodes.batch.batch_next import batch_next
from chaos_agent.agent.nodes.batch.batch_setup import batch_setup
from chaos_agent.agent.nodes.gates.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.gates.preplan_probe import preplan_probe
from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop
from chaos_agent.agent.nodes.planning.extract_planning_metadata import extract_planning_metadata
from chaos_agent.agent.nodes.planning.handoff_strip import planning_handoff
from chaos_agent.agent.nodes.planning.intent_clarification import make_intent_clarification
from chaos_agent.agent.nodes.planning.intent_confirm import intent_confirm
from chaos_agent.agent.nodes.store.memory_nodes import load_memory, save_memory
from chaos_agent.agent.nodes.store.terminal_reports import terminal_reports_node
from chaos_agent.agent.nodes.planning.phase1_screener import (
    phase1_screener,
    route_after_phase1_screener,
)
from chaos_agent.agent.nodes.planning.intent_screener import (
    INTENT_SCREENER_PASS,
    intent_screener,
)
from chaos_agent.agent.nodes.planning.plan_builder import make_plan_builder
from chaos_agent.agent.nodes.planning.plan_change_confirm import plan_change_confirm
from chaos_agent.agent.nodes.recover.recover_handler import recover_handler
from chaos_agent.agent.nodes.recover.recover_verifier import make_recover_verifier
from chaos_agent.agent.nodes.gates.reject import reject
from chaos_agent.agent.nodes.gates.safety_check import safety_check
from chaos_agent.agent.nodes.side_effect.se_detect import se_detect_node
from chaos_agent.agent.nodes.side_effect.se_snapshot import se_snapshot_node
from chaos_agent.agent.nodes.planning.tool_screener import (
    route_after_screener,
    tool_screener,
)
from chaos_agent.agent.nodes.verify.verifier import make_verifier
from chaos_agent.agent.router import (
    should_continue_agent_loop,
    should_continue_execute_loop,
    should_continue_verifier,
    should_continue_recover_verifier,
    should_continue_plan_builder,
    route_after_phase1_tools,
    route_after_safety,
    route_after_confirmation,
    route_after_intent_clarification,
    route_after_verifier_tools,
    route_after_finalize,
    route_after_recover_verifier_tools,
    route_after_recover_finalize,
    route_after_save_memory,
    route_after_batch_next,
    should_continue_intent_clarification,
)
from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)


# Regexes for extracting the offending tool name from LangGraph's
# three ToolNode error templates (see langgraph/prebuilt/tool_node.py
# constants INVALID_TOOL_NAME_ERROR_TEMPLATE / TOOL_EXECUTION_ERROR_
# TEMPLATE / TOOL_INVOCATION_ERROR_TEMPLATE). We try each in turn so
# the LLM-facing message can still name the offending tool even when
# the error is a Pydantic ValidationError (kubectl_read received a
# Literal mismatch like ``subcommand='delete'``).
_TOOL_NAME_FROM_ERROR_PATTERNS = (
    # case 1: requested tool not in this ToolNode's tool table
    re.compile(r"['\"]?(\w+)['\"]? is not a valid tool"),
    # case 2: tool body raised (TOOL_EXECUTION_ERROR_TEMPLATE)
    re.compile(r"Error executing tool ['\"](\w+)['\"]"),
    # case 3: pydantic ValidationError on tool args
    # (TOOL_INVOCATION_ERROR_TEMPLATE) — covers e.g. kubectl_read hit
    # with subcommand='delete' which violates its Literal type
    re.compile(r"Error invoking tool ['\"](\w+)['\"]"),
)


def _phase1_handle_tool_error(error: Exception) -> str:
    """Rewrite Phase 1 ToolNode errors to forbid bypass attempts.

    LangGraph's default ``handle_tool_errors=True`` returns a message
    like ``'blade_create' is not a valid tool, try one of [..., kubectl,
    ...]``. The "try one of [...]" list **actively suggests bypass
    paths** — caught in task-ce9647931ce1 where the LLM, told that
    ``blade_create`` was unavailable, immediately used ``kubectl exec
    ... blade create`` (which IS in the suggestion list). The LLM
    obediently followed the error message right past the safety
    pipeline.

    This handler returns a focused message that:
      1. Names the offending tool (so the LLM knows what was rejected)
      2. Does NOT list alternative tools (no bypass hint)
      3. Explains the restriction is intentional + enforced
      4. Points to the ONLY legitimate path forward (emit final
         summary text without tool_calls → system advances to Phase 2)

    Phase 2 has its own handler (``_phase2_handle_tool_error``) that strips
    the same "try one of [...]" list from unknown-tool errors while leaving
    genuine execution/validation errors intact — those carry the detail the
    LLM needs to fix a real typo.

    Three error shapes are handled (see ``_TOOL_NAME_FROM_ERROR_
    PATTERNS`` for the three LangGraph templates we match):
      - Unknown tool → "{tool} is not a valid tool"
      - Tool execution error → "Error executing tool '{tool}'"
      - Pydantic ValidationError on args → "Error invoking tool '{tool}'"
        (covers e.g. ``kubectl_read(subcommand='delete')`` whose Literal
        type rejects the value at validation time)
    """
    msg = str(error)
    # An argument-schema refusal is NOT a phase restriction: the tool IS bound
    # here, it just rejected a key it cannot honour (``StrictToolArgs``, e.g.
    # ``host_read(node=...)``). Its message already names the correct
    # alternative, and the phase text below would replace that with something
    # both unhelpful and false ("not available in Phase 1", "do not try
    # alternative tools") — teaching the model to abandon a legitimate tool
    # instead of dropping the bad argument. Pass it through.
    if UNKNOWN_ARG_REFUSAL_MARKER in msg:
        return f"Error: {msg}"
    tool_name = "<unknown>"
    for pattern in _TOOL_NAME_FROM_ERROR_PATTERNS:
        m = pattern.search(msg)
        if m:
            tool_name = m.group(1)
            break
    return (
        f"Tool '{tool_name}' is not available in Phase 1 (planning) — "
        f"either the tool name itself is not bound to this phase, or "
        f"the args you passed map to a mutating operation that Phase 1 "
        f"rejects.\n"
        f"\n"
        f"This is intentional — Phase 1 is read-only by design. Mutation "
        f"tools (blade_create, blade_destroy, full kubectl with exec/"
        f"delete/patch/...) and mutation-equivalent invocations "
        f"(kubectl_read with a mutating exec inner command, kubectl exec ... "
        f"blade create, kubectl create -f chaosblade.yaml) are bound "
        f"automatically in Phase 2 after your plan is approved by the user.\n"
        f"\n"
        f"DO NOT try alternative tools or alternative argument shapes to "
        f"bypass this restriction. The runtime actively enforces it via "
        f"the same classifier the Phase 2 screener uses.\n"
        f"\n"
        f"To advance to Phase 2: finish your planning observations, then "
        f"emit a final summary text WITHOUT any tool_calls. The system "
        f"will run safety_check → confirmation_gate → execute_loop "
        f"automatically once you stop calling tools."
    )


def _phase2_handle_tool_error(error: Exception) -> str:
    """Strip the bypass-suggesting tool list from Phase 2 unknown-tool errors.

    Of LangGraph's four templates only ``INVALID_TOOL_NAME_ERROR_TEMPLATE``
    enumerates the bound tools ("try one of [...]"). Phase 1 has rewritten that
    case since task-ce9647931ce1; Phase 2 kept the default and hit the same
    anti-pattern in task-c758cdbdb, where a ``save_fault_plan`` call was
    answered with ``try one of [execute_skill_script, ..., blade_create,
    blade_destroy, ...]`` — handing the model a menu to wander through instead
    of the one thing it needed to know.

    Every other template (execution error, invocation/validation error) is
    passed through untouched: those messages carry the actual failure detail,
    which is exactly what the model needs to fix a real typo or bad argument,
    and none of them list alternatives.
    """
    msg = str(error)
    if "is not a valid tool" not in msg:
        return f"Error: {msg}"
    tool_name = "<unknown>"
    for pattern in _TOOL_NAME_FROM_ERROR_PATTERNS:
        m = pattern.search(msg)
        if m:
            tool_name = m.group(1)
            break
    return (
        f"Tool '{tool_name}' does not exist in this phase. No tool ran and "
        f"nothing changed.\n"
        f"\n"
        f"Use the tools already available to you — do not guess at other tool "
        f"names. If the action you wanted has no tool, say so in plain text "
        f"and explain what you would need; do not substitute a different tool "
        f"to approximate it.\n"
        f"\n"
        f"A tool name you have only seen in the conversation history may "
        f"belong to an earlier phase's tool surface — each phase binds its "
        f"own set. The tools currently bound to you are the authority on "
        f"what exists here."
    )


async def stamp_recover_terminal(state) -> dict:
    """The recover graph's single terminal funnel — stamps ``finished_at``.

    The recover graph's cut exits (expired wall clock, exhausted loop budget)
    route ``done`` -> END from a round-trip delta carrying no verdict, so no
    node in such a run ever wrote ``finished_at``: the row kept
    ``finished_at=''`` and the derived ``duration_ms=0`` (W-56-8 review round
    3, probe K1-K3), violating the terminal-node contract ``sync_to_store``
    documents ("Terminal nodes put finished_at into updated_fields"). The
    inject graph has no such hole — its ``done`` funnels through
    ``save_memory``, which stamps.

    Skipping an existing stamp is deliberate, not an optimisation:
    ``finalize_recover_verification`` already stamps its verdict time, and
    re-stamping here would date the run's end after the judgement that ended
    it. No ``with_phase_events`` wrapper either — the intent graph's
    ``save_dialogue`` sets the precedent for timestamps-only end nodes, and
    this one runs only after the run has been judged, so it has no phase of
    its own to emit.
    """
    if state.get("finished_at"):
        return {}
    from chaos_agent.agent.nodes.store._store_sync import sync_to_store
    from chaos_agent.utils.time import now_iso

    result = {"finished_at": now_iso()}
    await sync_to_store(state, result)
    return result


def build_recover_graph(
    verifier_tools: list = None,
    pre_reason_hook=None,
    llm=None,
    registry=None,
) -> StateGraph:
    """Build the recover graph with two-layer verification.

    Flow:
        START → recover_verifier_loop ⇄ (recover_verifier_screener →
        recover_verifier_tools) → finalize_recover_verification →
        stamp_recover_terminal → END

    Layer 1: Execute blade_destroy + verify via blade_status (deterministic)
    Layer 2: LLM reads skill's "恢复验证" section and verifies (ReAct loop)

    Args:
        verifier_tools: Tools for Layer 2 verification (kubectl_*, blade_status)
        pre_reason_hook: Optional PreReasoningHook for memory compaction and session recording
        llm: LangChain LLM instance for ReAct reasoning
        registry: SkillRegistry for reading skill recovery instructions
    """
    graph = StateGraph(AgentState)

    # Build recover verifier with LLM support
    recover_verifier_node = make_recover_verifier(hook=pre_reason_hook, llm=llm, tools=verifier_tools, registry=registry)
    # Scheme B: finalize_recover_verification node owns Layer 2 finalization
    # (parse verdict + guard + retry + cleanup).
    finalize_recover_node = make_finalize_recover_verification(registry=registry)

    # Nodes
    graph.add_node("recover_verifier_loop", with_phase_events("recover_verifier_loop", "recovery", recover_verifier_node))
    graph.add_node("finalize_recover_verification", with_phase_events("finalize_recover_verification", "recovery", finalize_recover_node))
    # Single terminal funnel — mirrors the inject graph's terminal_reports
    # contract (task-349ccf5d): every "done" below lands here before END.
    graph.add_node("stamp_recover_terminal", stamp_recover_terminal)
    if verifier_tools:
        # Unified screener edge node — capability verdict + read-only
        # discipline between the loop and its ToolNode, mirroring
        # phase1_screener / tool_screener. Read-only gating applies to
        # Layer 2 only: Layer 1 repairs (recover_phase=layer1_recovery),
        # Layer 2 only verifies (recover_phase=layer2_verification).
        _recover_screener, _route_after_recover_screener = make_phase_screener(
            capability_phase="recover_verify",
            readonly=lambda s: s.get("recover_phase", "layer1_recovery") == "layer2_verification",
            phase_duty=(
                "Layer 2 verifies recovery outcome with read-only observations "
                "only — it never repairs. Recovery actions belong to Layer 1, "
                "which has already run."
            ),
            verdict_guidance=(
                "If your observations show residual fault effects, submit your "
                "verdict as `unrecovered` and describe in details exactly which "
                "recovery action is needed. If the residual matches a recorded "
                "side effect, report it as a warning. Do not re-attempt the "
                "refused call in any form."
            ),
        )
        graph.add_node("recover_verifier_screener", _recover_screener)
        # Same unknown-tool rewrite as Phase 2: the LangGraph default lists
        # every bound tool ("try one of [...]"), which hands the model a menu
        # instead of the one fact it needs (task-ce9647931ce1 pattern).
        graph.add_node("recover_verifier_tools", with_tool_span("recover_verifier_tools", ToolNode(verifier_tools, handle_tool_errors=_phase2_handle_tool_error)))

    graph.set_entry_point("recover_verifier_loop")

    # recover_verifier_loop ⇄ recover_verifier_tools → finalize_recover_verification (Scheme B).
    # Mirrors the inject verifier wiring: tool_calls run in recover_verifier_tools;
    # route_after_recover_verifier_tools sends submit_recover_verification to finalize
    # (verdict) or other tools back to the loop. A Layer 2 verdict text routes straight
    # to finalize (fallback). finalize either loops back (guard/retry) or → END.
    if verifier_tools:
        graph.add_conditional_edges(
            "recover_verifier_loop",
            should_continue_recover_verifier,
            {
                "continue": "recover_verifier_screener",
                "finalize": "finalize_recover_verification",
                "done": "stamp_recover_terminal",
            },
        )
        graph.add_conditional_edges(
            "recover_verifier_screener",
            _route_after_recover_screener,
            {
                "pass": "recover_verifier_tools",
                "retry": "recover_verifier_loop",
            },
        )
        graph.add_conditional_edges(
            "recover_verifier_tools",
            route_after_recover_verifier_tools,
            {
                "recover_verifier_loop": "recover_verifier_loop",
                "finalize": "finalize_recover_verification",
            },
        )
    else:
        # No verifier tools: LLM can only emit text → finalize, or early-exit.
        graph.add_conditional_edges(
            "recover_verifier_loop",
            should_continue_recover_verifier,
            {
                "continue": "finalize_recover_verification",
                "finalize": "finalize_recover_verification",
                "done": "stamp_recover_terminal",
            },
        )
    # finalize_recover_verification → terminal funnel (done), or back to
    # recover_verifier_loop (guard/retry).
    graph.add_conditional_edges(
        "finalize_recover_verification",
        route_after_recover_finalize,
        {
            "recover_verifier_loop": "recover_verifier_loop",
            "done": "stamp_recover_terminal",
        },
    )
    # The funnel's ONLY edge — the single path to END. Routing every "done"
    # through it makes the finished_at stamp a property of the topology, so no
    # future exit (or a missed one today) can end a recover run unstamped.
    graph.add_edge("stamp_recover_terminal", END)

    return graph


# ---------------------------------------------------------------------------
# Intent Graph
# ---------------------------------------------------------------------------

async def save_dialogue(state) -> dict:
    """Lightweight end node for Intent Graph — timestamps only."""
    from chaos_agent.utils.time import now_iso
    return {"finished_at": now_iso()}


def build_intent_graph(
    clarification_tools: list = None,
    llm=None,
    registry=None,
    pre_reason_hook=None,
) -> StateGraph:
    """Build the Intent Graph for TUI conversational intent recognition.

    Nodes: load_memory → intent_clarification ⇄ clarification_tools
           → intent_confirm → save_dialogue → END

    This graph handles ONLY dialogue — no execution (no agent_loop,
    safety_check, execute_loop, etc.). When intent is confirmed as
    "inject", the Runner reads state.handoff_summary + state.fault_spec
    and launches Pipeline Graph separately.
    """
    from chaos_agent.agent.state import IntentState

    graph = StateGraph(IntentState)

    intent_clarification_node = make_intent_clarification(
        llm=llm, tools=clarification_tools, hook=pre_reason_hook, registry=registry,
    )

    graph.add_node("load_memory", load_memory)
    graph.add_node(
        "intent_clarification",
        with_phase_events("intent_clarification", "intent", intent_clarification_node),
    )
    if clarification_tools:
        graph.add_node("clarification_tools", with_tool_span("clarification_tools", ToolNode(clarification_tools)))
        graph.add_node("intent_screener", intent_screener)
    graph.add_node(
        "intent_confirm",
        with_phase_events("intent_confirm", "safety", intent_confirm),
    )
    graph.add_node("recover_handler", recover_handler)
    graph.add_node("save_dialogue", save_dialogue)

    graph.set_entry_point("load_memory")
    graph.add_edge("load_memory", "intent_clarification")

    if clarification_tools:
        graph.add_conditional_edges(
            "intent_clarification",
            should_continue_intent_clarification,
            {
                "continue": "intent_screener",
                "intent_confirm": "intent_confirm",
                "recover_handler": "recover_handler",
                "save_memory": "save_dialogue",
                END: END,
            },
        )
        graph.add_conditional_edges(
            "intent_screener",
            lambda state: state.get("intent_screener_route", INTENT_SCREENER_PASS),
            {INTENT_SCREENER_PASS: "clarification_tools", "retry": "intent_clarification"},
        )
        graph.add_edge("clarification_tools", "intent_clarification")
    else:
        graph.add_conditional_edges(
            "intent_clarification",
            route_after_intent_clarification,
            {
                "agent_loop": "intent_confirm",
                "recover_handler": "recover_handler",
                "save_memory": "save_dialogue",
                "intent_clarification": "intent_clarification",
            },
        )

    graph.add_conditional_edges(
        "intent_confirm",
        lambda s: "save_dialogue" if s.get("confirmed_intent") in ("inject", "batch_inject") and s.get("fault_spec") else END,
        {"save_dialogue": "save_dialogue", END: END},
    )

    graph.add_edge("recover_handler", "save_dialogue")
    graph.add_edge("save_dialogue", END)

    return graph


# ---------------------------------------------------------------------------
# Pipeline Graph
# ---------------------------------------------------------------------------

def build_pipeline_graph(
    phase1_tools: list,
    phase2_tools: list,
    verifier_tools: list = None,
    clarification_tools: list = None,
    pre_reason_hook=None,
    llm=None,
    registry=None,
) -> StateGraph:
    """Build the Pipeline Graph for fault injection execution.

    Entry paths via pipeline_init:
      - agent_loop: CLI structured / NL / TUI inject (after Intent Graph confirms)
      - plan_builder: TUI /plan dry-run

    Shared pipeline: safety_check → confirmation_gate → baseline_capture
    → se_snapshot → execute_loop → verifier_loop → terminal_reports → save_memory → END
    """
    from chaos_agent.agent.nodes.store.memory_nodes import pipeline_init
    from chaos_agent.agent.router import route_pipeline_start

    graph = StateGraph(AgentState)

    agent_loop_node = make_agent_loop(hook=pre_reason_hook, llm=llm, tools=phase1_tools, registry=registry)
    execute_loop_node = make_execute_loop(hook=pre_reason_hook, llm=llm, tools=phase2_tools, registry=registry)
    verifier_node = make_verifier(hook=pre_reason_hook, llm=llm, tools=verifier_tools, registry=registry)
    finalize_verification_node = make_finalize_verification(registry=registry)
    baseline_capture_node = make_baseline_capture(llm=llm, registry=registry)
    plan_builder_node = make_plan_builder(llm=llm, tools=clarification_tools, hook=pre_reason_hook, registry=registry)

    # Entry
    graph.add_node("pipeline_init", pipeline_init)
    # Fresh pre-task probes (operator status, target health, headroom,
    # conflicts, ...) collected once at task start and injected into the
    # Phase 1 prompt so the planner reuses them instead of re-probing.
    # Deterministic, read-only, never blocks; the Phase 2 safety_check gate
    # re-runs the same probes authoritatively (verdicts stay there).
    graph.add_node("preplan_probe", with_phase_events("preplan_probe", "inject", preplan_probe))

    # Plan builder (TUI /plan)
    graph.add_node("plan_builder", with_phase_events("plan_builder", "intent", plan_builder_node))
    if clarification_tools:
        # plan_builder binds through ``build_capability_context(state, "plan")``,
        # so its ToolNode needs the matching runtime screen — the /plan path has
        # no phase1_screener/tool_screener equivalent, and ``clarification_tools``
        # includes provider discovery tools (``host_read`` is HostShell's PLAN
        # tool), i.e. exactly the shape of task-46317228. Unified screener
        # edge node; ``stop_retry_hint`` because should_continue_plan_builder
        # has no iteration bound.
        _plan_builder_screener, _route_after_plan_builder_screener = make_phase_screener(
            capability_phase="plan",
            readonly=False,
            stop_retry_hint=True,
        )
        graph.add_node("plan_builder_screener", _plan_builder_screener)
        graph.add_node("plan_builder_tools", with_tool_span("plan_builder_tools", ToolNode(
            clarification_tools,
            handle_tool_errors=_phase1_handle_tool_error,
        )))

    # Batch execution (loop-back)
    graph.add_node("batch_setup", with_phase_events("batch_setup", "inject", batch_setup))
    graph.add_node("batch_next", batch_next)

    # Phase 1 (planning)
    graph.add_node("agent_loop", with_phase_events("agent_loop", "inject", agent_loop_node))
    graph.add_node("phase1_screener", phase1_screener)
    graph.add_node("phase1_tools", with_tool_span("phase1_tools", ToolNode(
        phase1_tools,
        handle_tool_errors=_phase1_handle_tool_error,
    )))
    graph.add_node("extract_planning_metadata", extract_planning_metadata)
    # Planning → execution handoff strip: the deterministic slimming point
    # on the edge every finalized plan crosses (first pass AND every replan
    # round). Sits AFTER extract_planning_metadata (which reverse-scans
    # AIMessage tool_calls the strip would remove) and BEFORE the safety
    # gates (deterministic nodes that do not consume the stripped context).
    graph.add_node("planning_handoff", planning_handoff)
    graph.add_node("plan_change_confirm", plan_change_confirm)

    # Safety + confirm
    graph.add_node("safety_check", with_phase_events("safety_check", "safety", safety_check))
    graph.add_node("confirmation_gate", with_phase_events("confirmation_gate", "safety", confirmation_gate))
    graph.add_node("baseline_capture", with_phase_events("baseline_capture", "inject", baseline_capture_node))
    graph.add_node("se_snapshot", with_phase_events("se_snapshot", "inject", se_snapshot_node))

    # Phase 2 (execution)
    graph.add_node("execute_loop", with_phase_events("execute_loop", "inject", execute_loop_node))
    graph.add_node("tool_screener", tool_screener)
    graph.add_node("phase2_tools", with_tool_span("phase2_tools", ToolNode(phase2_tools, handle_tool_errors=_phase2_handle_tool_error)))

    # Verification
    graph.add_node("verifier_loop", with_phase_events("verifier_loop", "verify", verifier_node))
    graph.add_node("finalize_verification", with_phase_events("finalize_verification", "verify", finalize_verification_node))
    if verifier_tools:
        # Unified screener edge node — capability verdict + read-only
        # discipline between the loop and its ToolNode, mirroring
        # phase1_screener / tool_screener (fabricated ToolMessage
        # pairing, screener_route retry).
        _verifier_screener, _route_after_verifier_screener = make_phase_screener(
            capability_phase="verify",
            readonly=True,
            phase_duty=(
                "The verification phase judges whether the injected fault "
                "landed, using read-only observations only — it never "
                "injects, repairs, or alters cluster state. Injection "
                "actions belong to the execute phase, which has already run."
            ),
            verdict_guidance=(
                "If your observations show the fault did not land (or only "
                "partially landed), submit your verdict as `unverified` and "
                "describe exactly what is missing. Do not re-attempt the "
                "refused call in any form."
            ),
        )
        graph.add_node("verifier_screener", _verifier_screener)
        # Same unknown-tool rewrite as Phase 2 — the LangGraph default's
        # "try one of [...]" list is the anti-pattern Layer D removed.
        graph.add_node("verifier_tools", with_tool_span("verifier_tools", ToolNode(verifier_tools, handle_tool_errors=_phase2_handle_tool_error)))
    graph.add_node("se_detect", with_phase_events("se_detect", "verify", se_detect_node))

    # End
    # terminal_reports produces the postmortem / issue-report artifacts on
    # EVERY experiment terminal path (se_detect, reject) ahead of
    # persistence; wrapped with
    # phase="postmortem" so the TUI stepper ignores it (unknown phase) while
    # L4 still surfaces it via _PHASE_STEP_MAP ("postmortem" step).
    graph.add_node("terminal_reports", with_phase_events("terminal_reports", "postmortem", terminal_reports_node))
    graph.add_node("save_memory", save_memory)
    graph.add_node("reject", reject)

    # --- Entry routing ---
    # pipeline_init → preplan_probe → the three-way pipeline routing. The
    # probe node sits on EVERY entry path but skips itself when there is
    # nothing to probe for (no spec); replan re-entries into
    # agent_loop deliberately bypass it — probes run once at task start.
    graph.set_entry_point("pipeline_init")
    graph.add_edge("pipeline_init", "preplan_probe")
    graph.add_conditional_edges(
        "preplan_probe",
        route_pipeline_start,
        {
            "agent_loop": "agent_loop",
            "plan_builder": "plan_builder",
            "batch_setup": "batch_setup",
        },
    )

    # --- Plan builder ⇄ tools ---
    # plan_confirmed → batch_setup (enters batch execution loop)
    if clarification_tools:
        graph.add_conditional_edges(
            "plan_builder",
            should_continue_plan_builder,
            {"continue": "plan_builder_screener", END: END},
        )
        graph.add_conditional_edges(
            "plan_builder_screener",
            _route_after_plan_builder_screener,
            {"pass": "plan_builder_tools", "retry": "plan_builder"},
        )
        graph.add_edge("plan_builder_tools", "plan_builder")
    else:
        graph.add_edge("plan_builder", END)

    # batch_setup → agent_loop (full per-fault planning)
    graph.add_edge("batch_setup", "agent_loop")

    # --- Agent loop ⇄ phase1 tools ---
    graph.add_conditional_edges(
        "agent_loop",
        should_continue_agent_loop,
        {
            "continue": "phase1_screener",
            "extract_planning_metadata": "extract_planning_metadata",
            "reject": "reject",
        },
    )
    graph.add_conditional_edges(
        "phase1_screener",
        route_after_phase1_screener,
        {"pass": "phase1_tools", "retry": "agent_loop"},
    )
    graph.add_conditional_edges(
        "phase1_tools",
        route_after_phase1_tools,
        {
            "agent_loop": "agent_loop",
            "extract_planning_metadata": "extract_planning_metadata",
            "plan_change_confirm": "plan_change_confirm",
        },
    )
    graph.add_edge("plan_change_confirm", "agent_loop")

    graph.add_conditional_edges(
        "extract_planning_metadata",
        lambda s: "reject" if s.get("error") else ("agent_loop" if s.get("planning_rejected") else "planning_handoff"),
        {"agent_loop": "agent_loop", "planning_handoff": "planning_handoff", "reject": "reject"},
    )
    graph.add_edge("planning_handoff", "safety_check")

    # --- Safety + confirmation ---
    graph.add_conditional_edges(
        "safety_check",
        route_after_safety,
        {
            "confirmation_gate": "confirmation_gate",
            "baseline_capture": "baseline_capture",
            "reject": "reject",
            "agent_loop": "agent_loop",
        },
    )
    graph.add_conditional_edges(
        "confirmation_gate",
        route_after_confirmation,
        {"baseline_capture": "baseline_capture", "reject": "reject", "end": END},
    )

    # --- Baseline + execution ---
    graph.add_edge("baseline_capture", "se_snapshot")
    graph.add_edge("se_snapshot", "execute_loop")
    graph.add_conditional_edges(
        "execute_loop",
        should_continue_execute_loop,
        # No "end": every exit from execute_loop goes through verification now
        # (budget exhaustion and wall-clock expiry included). Omitting the key
        # makes a stray ``return "end"`` raise KeyError rather than silently
        # bypass the verifier — see should_continue_execute_loop.
        {"continue": "tool_screener", "verifier": "verifier_loop", "replan": "agent_loop"},
    )
    graph.add_conditional_edges(
        "tool_screener",
        route_after_screener,
        # "reject": the screener's hard-termination route (SCREENER_ROUTE_FAIL)
        # ends at the terminal node — the W-56-5 fix; a hard stop that looped
        # as "retry" kept the run alive and leaked stale fail state (#56).
        {
            "pass": "phase2_tools",
            "replan": "agent_loop",
            "retry": "execute_loop",
            "reject": "reject",
        },
    )
    graph.add_edge("phase2_tools", "execute_loop")

    # --- Verification ---
    if verifier_tools:
        graph.add_conditional_edges(
            "verifier_loop",
            should_continue_verifier,
            {"continue": "verifier_screener", "finalize": "finalize_verification", "done": "se_detect"},
        )
        graph.add_conditional_edges(
            "verifier_screener",
            _route_after_verifier_screener,
            {"pass": "verifier_tools", "retry": "verifier_loop"},
        )
        graph.add_conditional_edges(
            "verifier_tools",
            route_after_verifier_tools,
            {"verifier_loop": "verifier_loop", "finalize": "finalize_verification"},
        )
    else:
        graph.add_conditional_edges(
            "verifier_loop",
            should_continue_verifier,
            {"continue": "finalize_verification", "finalize": "finalize_verification", "done": "se_detect"},
        )
    graph.add_conditional_edges(
        "finalize_verification",
        route_after_finalize,
        # "replan" is REQUIRED: route_after_finalize returns it on a
        # verify-replan (unverified + L2 failed, budget remaining). Omitting
        # it makes LangGraph raise KeyError: 'replan' when finalize triggers a
        # replan (regression: task-edfed134).
        {"verifier_loop": "verifier_loop", "se_detect": "se_detect", "replan": "agent_loop"},
    )

    # --- Post-verification ---
    # Every experiment terminal path funnels through terminal_reports
    # (postmortem + issue-report artifacts) before persistence.
    graph.add_edge("se_detect", "terminal_reports")
    graph.add_edge("terminal_reports", "save_memory")

    # save_memory → batch_next (batch in progress) or END
    graph.add_conditional_edges(
        "save_memory", route_after_save_memory,
        {"batch_next": "batch_next", END: END},
    )

    # batch_next → batch_setup (more faults) or END
    graph.add_conditional_edges(
        "batch_next", route_after_batch_next,
        {"batch_setup": "batch_setup", END: END},
    )

    # reject → terminal_reports → save_memory → batch_next (batch: collect
    # failed result) or END. Rejections go through the SAME terminal funnel
    # as executed failures (task-349ccf5d): the postmortem gate skips
    # pre-execution categories (user/safety_rejected) without an LLM call,
    # while planning_rejected / planning_timeout DO produce a report.
    graph.add_edge("reject", "terminal_reports")

    return graph
