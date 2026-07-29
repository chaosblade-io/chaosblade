"""StateGraph construction for inject, recover, and status graphs."""

import logging
import re

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from chaos_agent.agent.dispatch import with_phase_events
from chaos_agent.agent.nodes._capability_screen import with_capability_screen
from chaos_agent.tools._strict_args import UNKNOWN_ARG_REFUSAL_MARKER
from chaos_agent.agent.nodes.recover._recover_finalize import make_finalize_recover_verification
from chaos_agent.agent.nodes.verify._verifier_finalize import make_finalize_verification
from chaos_agent.agent.nodes.execute.agent_loop import make_agent_loop
from chaos_agent.agent.nodes.baseline.baseline_capture import make_baseline_capture
from chaos_agent.agent.nodes.batch.batch_next import batch_next
from chaos_agent.agent.nodes.batch.batch_setup import batch_setup
from chaos_agent.agent.nodes.gates.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.execute.direct_execute import direct_execute
from chaos_agent.agent.nodes.execute.direct_setup import make_direct_setup
from chaos_agent.agent.nodes.execute.execute_loop import make_execute_loop
from chaos_agent.agent.nodes.planning.extract_planning_metadata import extract_planning_metadata
from chaos_agent.agent.nodes.planning.intent_clarification import make_intent_clarification
from chaos_agent.agent.nodes.planning.intent_confirm import intent_confirm
from chaos_agent.agent.nodes.store.memory_nodes import load_memory, save_memory
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
    route_after_baseline,
    route_after_direct_execute,
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
        f"to approximate it."
    )


def build_recover_graph(
    verifier_tools: list = None,
    pre_reason_hook=None,
    llm=None,
    registry=None,
) -> StateGraph:
    """Build the recover graph with two-layer verification.

    Flow:
        START → execute_destroy → recover_verifier_loop ⇄ verifier_tools → END

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
    if verifier_tools:
        # Same gap as the inject verifier — recovery observes with the same
        # read-only tool surface and needs the same runtime screen.
        graph.add_node("recover_verifier_tools", with_capability_screen(
            ToolNode(verifier_tools, handle_tool_errors=True), "recover_verify",
        ))

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
                "continue": "recover_verifier_tools",
                "finalize": "finalize_recover_verification",
                "done": END,
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
                "done": END,
            },
        )
    # finalize_recover_verification → END (done) or back to recover_verifier_loop (guard/retry).
    graph.add_conditional_edges(
        "finalize_recover_verification",
        route_after_recover_finalize,
        {
            "recover_verifier_loop": "recover_verifier_loop",
            "done": END,
        },
    )

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
        graph.add_node("clarification_tools", ToolNode(clarification_tools))
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

    Three entry paths via pipeline_init:
      - agent_loop: CLI NL / TUI inject (after Intent Graph confirms)
      - direct_setup: CLI direct mode
      - plan_builder: TUI /plan dry-run

    Shared pipeline: safety_check → confirmation_gate → baseline_capture
    → execute_loop → verifier_loop → save_memory → END
    """
    from chaos_agent.agent.nodes.store.memory_nodes import pipeline_init
    from chaos_agent.agent.router import route_pipeline_start

    graph = StateGraph(AgentState)

    agent_loop_node = make_agent_loop(hook=pre_reason_hook, llm=llm, tools=phase1_tools, registry=registry)
    execute_loop_node = make_execute_loop(hook=pre_reason_hook, llm=llm, tools=phase2_tools, registry=registry)
    verifier_node = make_verifier(hook=pre_reason_hook, llm=llm, tools=verifier_tools, registry=registry)
    finalize_verification_node = make_finalize_verification(registry=registry)
    direct_setup_node = make_direct_setup(registry=registry)
    baseline_capture_node = make_baseline_capture(llm=llm, registry=registry)
    plan_builder_node = make_plan_builder(llm=llm, tools=clarification_tools, hook=pre_reason_hook, registry=registry)

    # Entry
    graph.add_node("pipeline_init", pipeline_init)

    # Plan builder (TUI /plan)
    graph.add_node("plan_builder", with_phase_events("plan_builder", "intent", plan_builder_node))
    if clarification_tools:
        # plan_builder binds through ``build_capability_context(state, "plan")``,
        # so its ToolNode needs the matching runtime screen — the /plan path has
        # no phase1_screener/tool_screener equivalent, and ``clarification_tools``
        # includes provider discovery tools (``host_read`` is HostShell's PLAN
        # tool), i.e. exactly the shape of task-46317228.
        graph.add_node("plan_builder_tools", with_capability_screen(
            ToolNode(
                clarification_tools,
                handle_tool_errors=_phase1_handle_tool_error,
            ),
            "plan",
        ))

    # Batch execution (loop-back)
    graph.add_node("batch_setup", with_phase_events("batch_setup", "inject", batch_setup))
    graph.add_node("batch_next", batch_next)

    # Phase 1 (planning)
    graph.add_node("agent_loop", with_phase_events("agent_loop", "inject", agent_loop_node))
    graph.add_node("phase1_screener", phase1_screener)
    graph.add_node("phase1_tools", ToolNode(
        phase1_tools,
        handle_tool_errors=_phase1_handle_tool_error,
    ))
    graph.add_node("extract_planning_metadata", extract_planning_metadata)
    graph.add_node("plan_change_confirm", plan_change_confirm)

    # Direct
    graph.add_node("direct_setup", direct_setup_node)

    # Safety + confirm
    graph.add_node("safety_check", with_phase_events("safety_check", "safety", safety_check))
    graph.add_node("confirmation_gate", with_phase_events("confirmation_gate", "safety", confirmation_gate))
    graph.add_node("baseline_capture", with_phase_events("baseline_capture", "inject", baseline_capture_node))
    graph.add_node("se_snapshot", with_phase_events("se_snapshot", "inject", se_snapshot_node))

    # Phase 2 (execution)
    graph.add_node("execute_loop", with_phase_events("execute_loop", "inject", execute_loop_node))
    graph.add_node("direct_execute", with_phase_events("direct_execute", "inject", direct_execute))
    graph.add_node("tool_screener", tool_screener)
    graph.add_node("phase2_tools", ToolNode(phase2_tools, handle_tool_errors=_phase2_handle_tool_error))

    # Verification
    graph.add_node("verifier_loop", with_phase_events("verifier_loop", "verify", verifier_node))
    graph.add_node("finalize_verification", with_phase_events("finalize_verification", "verify", finalize_verification_node))
    if verifier_tools:
        # Runtime capability screen: the read-only phases had no equivalent of
        # phase1_screener / tool_screener, so a cross-profile read (task-46317228:
        # host_read during verification on a k8s session) reached the ToolNode.
        graph.add_node("verifier_tools", with_capability_screen(
            ToolNode(verifier_tools, handle_tool_errors=True), "verify",
        ))
    graph.add_node("se_detect", with_phase_events("se_detect", "verify", se_detect_node))

    # End
    graph.add_node("save_memory", save_memory)
    graph.add_node("reject", reject)

    # --- Entry routing ---
    graph.set_entry_point("pipeline_init")
    graph.add_conditional_edges(
        "pipeline_init",
        route_pipeline_start,
        {
            "agent_loop": "agent_loop",
            "direct_setup": "direct_setup",
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
            {"continue": "plan_builder_tools", END: END},
        )
        graph.add_edge("plan_builder_tools", "plan_builder")
    else:
        graph.add_edge("plan_builder", END)

    # batch_setup → agent_loop (full per-fault planning)
    graph.add_edge("batch_setup", "agent_loop")

    # --- Direct path ---
    graph.add_edge("direct_setup", "safety_check")

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
        lambda s: "reject" if s.get("error") else ("agent_loop" if s.get("planning_rejected") else "safety_check"),
        {"agent_loop": "agent_loop", "safety_check": "safety_check", "reject": "reject"},
    )

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
    graph.add_conditional_edges(
        "se_snapshot",
        route_after_baseline,
        {"direct_execute": "direct_execute", "execute_loop": "execute_loop"},
    )
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
        {"pass": "phase2_tools", "replan": "agent_loop", "retry": "execute_loop"},
    )
    graph.add_edge("phase2_tools", "execute_loop")

    graph.add_conditional_edges(
        "direct_execute",
        route_after_direct_execute,
        {"verifier": "verifier_loop", "end": "save_memory"},
    )

    # --- Verification ---
    if verifier_tools:
        graph.add_conditional_edges(
            "verifier_loop",
            should_continue_verifier,
            {"continue": "verifier_tools", "finalize": "finalize_verification", "done": "se_detect"},
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
    graph.add_edge("se_detect", "save_memory")

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

    # reject → batch_next (batch: collect failed result) or END
    graph.add_conditional_edges(
        "reject", route_after_save_memory,
        {"batch_next": "batch_next", END: END},
    )

    return graph
