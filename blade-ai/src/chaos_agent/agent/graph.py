"""StateGraph construction for inject, recover, and status graphs."""

import logging
import re

from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

from chaos_agent.agent.dispatch import with_phase_events
from chaos_agent.agent.nodes.agent_loop import make_agent_loop
from chaos_agent.agent.nodes.baseline_capture import make_baseline_capture
from chaos_agent.agent.nodes.confirmation_gate import confirmation_gate
from chaos_agent.agent.nodes.direct_execute import direct_execute
from chaos_agent.agent.nodes.direct_setup import make_direct_setup
from chaos_agent.agent.nodes.execute_loop import make_execute_loop
from chaos_agent.agent.nodes.extract_planning_metadata import extract_planning_metadata
from chaos_agent.agent.nodes.intent_clarification import make_intent_clarification
from chaos_agent.agent.nodes.intent_confirm import intent_confirm
from chaos_agent.agent.nodes.memory_nodes import load_memory, save_memory
from chaos_agent.agent.nodes.recover_handler import recover_handler
from chaos_agent.agent.nodes.recover_verifier import make_recover_verifier
from chaos_agent.agent.nodes.reject import reject
from chaos_agent.agent.nodes.safety_check import safety_check
from chaos_agent.agent.nodes.phase1_screener import (
    phase1_screener,
    route_after_phase1_screener,
)
from chaos_agent.agent.nodes.tool_screener import (
    route_after_screener,
    tool_screener,
)
from chaos_agent.agent.nodes.verifier import make_verifier
from chaos_agent.agent.router import (
    should_continue_agent_loop,
    should_continue_execute_loop,
    should_continue_verifier,
    should_continue_recover_verifier,
    route_after_load_memory,
    route_after_safety,
    route_after_confirmation,
    route_after_baseline,
    route_after_direct_execute,
    route_after_intent_clarification,
    route_after_intent_confirm,
    should_continue_intent_clarification,
)
from chaos_agent.agent.state import AgentState

logger = logging.getLogger(__name__)


# Regexes for extracting the offending tool name from LangGraph's
# three ToolNode error templates (see langgraph/prebuilt/tool_node.py
# constants INVALID_TOOL_NAME_ERROR_TEMPLATE / TOOL_EXECUTION_ERROR_
# TEMPLATE / TOOL_INVOCATION_ERROR_TEMPLATE). We try each in turn so
# the LLM-facing message can still name the offending tool even when
# the error is a Pydantic ValidationError (kubectl_ro received a
# Literal mismatch like ``subcommand='exec'``).
_TOOL_NAME_FROM_ERROR_PATTERNS = (
    # case 1: requested tool not in this ToolNode's tool table
    re.compile(r"['\"]?(\w+)['\"]? is not a valid tool"),
    # case 2: tool body raised (TOOL_EXECUTION_ERROR_TEMPLATE)
    re.compile(r"Error executing tool ['\"](\w+)['\"]"),
    # case 3: pydantic ValidationError on tool args
    # (TOOL_INVOCATION_ERROR_TEMPLATE) — covers e.g. kubectl_ro hit
    # with subcommand='exec' which violates its Literal type
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

    Phase 2's ToolNode keeps the LangChain default — the "try one of
    [...]" hint is appropriate there because (a) the screener already
    blocks target drift / banned ops, and (b) listing alternatives
    helps the LLM recover from real typos in execution.

    Three error shapes are handled (see ``_TOOL_NAME_FROM_ERROR_
    PATTERNS`` for the three LangGraph templates we match):
      - Unknown tool → "{tool} is not a valid tool"
      - Tool execution error → "Error executing tool '{tool}'"
      - Pydantic ValidationError on args → "Error invoking tool '{tool}'"
        (covers e.g. ``kubectl_ro(subcommand='exec')`` whose Literal
        type rejects the value at validation time)
    """
    msg = str(error)
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
        f"(kubectl_ro with a mutating subcommand, kubectl exec ... "
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


def build_inject_graph(phase1_tools: list, phase2_tools: list, verifier_tools: list = None, pre_reason_hook=None, llm=None, skill_catalog: str = "", registry=None, clarification_tools: list = None) -> StateGraph:
    """Build the inject fault injection graph.

    Flow (NL mode):
        START → load_memory → [route] → intent_clarification ⇄ clarification_tools → agent_loop ⇄ tools(phase1)
              → safety_check → [confirmation_gate] → baseline_capture → execute_loop ⇄ tools(phase2)
              → verifier_loop ⇄ verifier_tools → save_memory → END

    Flow (Direct mode):
        START → load_memory → direct_setup → safety_check → [confirmation_gate]
              → baseline_capture → direct_execute → verifier_loop ⇄ verifier_tools → save_memory → END

    Args:
        phase1_tools: Tools for the planning phase (activate_skill, kubectl, read_skill_resource)
        phase2_tools: Tools for the execution phase (blade_*, kubectl)
        verifier_tools: Tools for the verification phase (blade_status, kubectl_*)
        pre_reason_hook: Optional PreReasoningHook for memory compaction before LLM steps
        llm: LangChain LLM instance for ReAct reasoning
        skill_catalog: Skill catalog prompt string for system prompt
        clarification_tools: Tools for intent_clarification node (ask_human, activate_skill, read_skill_resource)
    """
    graph = StateGraph(AgentState)

    # Create loop nodes with hook and LLM injection
    agent_loop_node = make_agent_loop(hook=pre_reason_hook, llm=llm, tools=phase1_tools, skill_catalog=skill_catalog)
    execute_loop_node = make_execute_loop(hook=pre_reason_hook, llm=llm, tools=phase2_tools, skill_catalog=skill_catalog, env_info=None)

    # Build verifier with LLM support and verifier tools
    verifier_node = make_verifier(hook=pre_reason_hook, llm=llm, tools=verifier_tools, registry=registry)

    # Direct path node: deterministic skill activation (no LLM)
    direct_setup_node = make_direct_setup(registry=registry)

    # Baseline capture node: pre-injection metrics for direct mode
    baseline_capture_node = make_baseline_capture(llm=llm, registry=registry)

    # Intent clarification node (TUI mode only)
    intent_clarification_node = make_intent_clarification(llm=llm, tools=clarification_tools, hook=pre_reason_hook)

    # Add nodes (pipeline nodes wrapped with phase events for TUI stepper)
    graph.add_node("load_memory", load_memory)
    graph.add_node("intent_clarification", with_phase_events("intent_clarification", "intent", intent_clarification_node))
    if clarification_tools:
        graph.add_node("clarification_tools", ToolNode(clarification_tools))
    graph.add_node("agent_loop", with_phase_events("agent_loop", "inject", agent_loop_node))
    # phase1_screener sits between agent_loop and phase1_tools — the
    # planning-phase analog of the phase 2 tool_screener. Reuses the
    # shared classifier so any mutation tool_call (direct blade_create,
    # kubectl exec ... blade create, kubectl create -f chaosblade.yaml,
    # etc.) gets the same verdict. See
    # ``chaos_agent.agent.nodes.phase1_screener`` for the routing
    # contract and operating modes.
    graph.add_node("phase1_screener", phase1_screener)
    # phase1_tools uses a custom error handler that refuses to leak
    # alternative-tool hints — see ``_phase1_handle_tool_error`` for
    # the rationale (LangChain's default "try one of [...]" message
    # actively trained the LLM to bypass Phase 1 via kubectl exec).
    graph.add_node("phase1_tools", ToolNode(
        phase1_tools,
        handle_tool_errors=_phase1_handle_tool_error,
    ))
    graph.add_node("extract_planning_metadata", extract_planning_metadata)
    graph.add_node("direct_setup", direct_setup_node)
    graph.add_node("baseline_capture", with_phase_events("baseline_capture", "inject", baseline_capture_node))
    graph.add_node("safety_check", with_phase_events("safety_check", "safety", safety_check))
    graph.add_node("confirmation_gate", with_phase_events("confirmation_gate", "safety", confirmation_gate))
    graph.add_node("execute_loop", with_phase_events("execute_loop", "inject", execute_loop_node))
    graph.add_node("direct_execute", with_phase_events("direct_execute", "inject", direct_execute))
    # Tool screener sits between execute_loop's LLM and phase2_tools.
    # Compares each tool_call's effective target against state.approved_target
    # (see chaos_agent.agent.target_guard). In log-only mode (default)
    # it always passes through; in enforcing mode it can route back to
    # agent_loop on drift, or back to execute_loop on banned/unknown.
    graph.add_node("tool_screener", tool_screener)
    graph.add_node("phase2_tools", ToolNode(phase2_tools))
    graph.add_node("verifier_loop", with_phase_events("verifier_loop", "verify", verifier_node))
    if verifier_tools:
        graph.add_node("verifier_tools", ToolNode(verifier_tools))
    graph.add_node("save_memory", save_memory)
    graph.add_node("reject", reject)
    graph.add_node("recover_handler", recover_handler)
    # Wrap intent_confirm in phase events so the TUI stepper shows a
    # ``◉ 安全检查`` indicator while the user reads the confirm card.
    # Without this wrapper, the gap between the user typing "开始" and
    # the confirm panel rendering looked like a stuck terminal — there
    # was no phase signal to anchor the wait. Tagging it under "safety"
    # (same family as confirmation_gate / safety_check) keeps the 5-stage
    # stepper to its existing 5 buckets while still emitting a paint.
    graph.add_node(
        "intent_confirm",
        with_phase_events("intent_confirm", "safety", intent_confirm),
    )

    # Set entry point
    graph.set_entry_point("load_memory")

    # load_memory → conditional routing (direct_setup, intent_clarification, or agent_loop)
    graph.add_conditional_edges(
        "load_memory",
        route_after_load_memory,
        {"agent_loop": "agent_loop", "direct_setup": "direct_setup", "intent_clarification": "intent_clarification"},
    )

    # intent_clarification ⇄ clarification_tools (ReAct loop for TUI intent recognition)
    # Multi-invocation model: pure text → END (turn done), inject → intent_confirm
    if clarification_tools:
        graph.add_conditional_edges(
            "intent_clarification",
            should_continue_intent_clarification,
            {
                "continue": "clarification_tools",
                "intent_confirm": "intent_confirm",
                "recover_handler": "recover_handler",
                "save_memory": "save_memory",
                END: END,
            },
        )
        graph.add_edge("clarification_tools", "intent_clarification")
    else:
        # No clarification tools: intent_clarification routes directly
        graph.add_conditional_edges(
            "intent_clarification",
            route_after_intent_clarification,
            {
                "agent_loop": "agent_loop",
                "recover_handler": "recover_handler",
                "save_memory": "save_memory",
                "intent_clarification": "intent_clarification",
            },
        )

    # intent_confirm → agent_loop (approved) or END (rejected/modified)
    graph.add_conditional_edges(
        "intent_confirm",
        route_after_intent_confirm,
        {
            "agent_loop": "agent_loop",
            END: END,
        },
    )

    # direct_setup → safety_check
    graph.add_edge("direct_setup", "safety_check")

    # agent_loop ⇄ phase1_screener ⇄ phase1_tools (ReAct loop with guard)
    #
    # The screener intercepts the "continue" branch so every tool_call
    # the planner LLM emits is classified before reaching ToolNode.
    # On rejection the screener appends synthetic ToolMessages and
    # routes back to agent_loop (the LLM then sees the error and
    # self-corrects). On pass the call flows through to ToolNode
    # exactly as before, with no observable latency in normal traffic.
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
        {
            "pass": "phase1_tools",
            # Retry — let the LLM see the rejection ToolMessages the
            # screener appended and pick a different action next turn.
            "retry": "agent_loop",
        },
    )
    graph.add_edge("phase1_tools", "agent_loop")

    # extract_planning_metadata → safety_check
    # (fills State gap for NL mode before baseline_capture)
    graph.add_edge("extract_planning_metadata", "safety_check")

    # safety_check → confirmation_gate or baseline_capture or reject
    # (execute_loop is no longer a direct destination from safety_check;
    #  all modes go through baseline_capture first)
    graph.add_conditional_edges(
        "safety_check",
        route_after_safety,
        {
            "confirmation_gate": "confirmation_gate",
            "baseline_capture": "baseline_capture",
            "reject": "reject",
        },
    )

    # confirmation_gate → baseline_capture / end / reject
    # (execute_loop is no longer a direct destination from confirmation_gate;
    #  all modes go through baseline_capture first. Dry-Run takes the END path.)
    graph.add_conditional_edges(
        "confirmation_gate",
        route_after_confirmation,
        {
            "baseline_capture": "baseline_capture",
            "reject": "reject",
            "end": END,
        },
    )

    # execute_loop ⇄ tool_screener ⇄ phase2_tools (ReAct loop)
    # The screener intercepts the "continue" branch so every tool_call
    # is target-guard checked before reaching ToolNode. See
    # chaos_agent.agent.nodes.tool_screener for verdict-to-route logic.
    graph.add_conditional_edges(
        "execute_loop",
        should_continue_execute_loop,
        {
            "continue": "tool_screener",
            "verifier": "verifier_loop",
            "end": "save_memory",
            "replan": "agent_loop",
        },
    )
    graph.add_conditional_edges(
        "tool_screener",
        route_after_screener,
        {
            # All tool_calls cleared the guard → run them as usual.
            "pass": "phase2_tools",
            # Target drift detected → re-plan from agent_loop (next
            # confirmation_gate refreezes a fresh approval).
            "replan": "agent_loop",
            # Banned / unknown call → LLM retries with the rejection
            # ToolMessages already appended by the screener.
            "retry": "execute_loop",
        },
    )
    graph.add_edge("phase2_tools", "execute_loop")

    # baseline_capture → conditional routing by execution mode
    # direct mode → direct_execute (deterministic skill execution)
    # NL mode → execute_loop (LLM ReAct loop)
    graph.add_conditional_edges(
        "baseline_capture",
        route_after_baseline,
        {
            "direct_execute": "direct_execute",
            "execute_loop": "execute_loop",
        },
    )

    # direct_execute → verifier_loop or save_memory
    graph.add_conditional_edges(
        "direct_execute",
        route_after_direct_execute,
        {
            "verifier": "verifier_loop",
            "end": "save_memory",
        },
    )

    # verifier_loop ⇄ verifier_tools (ReAct loop for Layer 2 verification)
    if verifier_tools:
        graph.add_conditional_edges(
            "verifier_loop",
            should_continue_verifier,
            {
                "continue": "verifier_tools",
                "done": "save_memory",
            },
        )
        graph.add_edge("verifier_tools", "verifier_loop")
    else:
        # No verifier tools: verifier_loop goes straight to save_memory
        graph.add_edge("verifier_loop", "save_memory")

    # save_memory → END
    graph.add_edge("save_memory", END)

    # Handler nodes → save_memory
    graph.add_edge("recover_handler", "save_memory")

    # reject → END
    graph.add_edge("reject", END)

    return graph


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

    # Nodes
    graph.add_node("recover_verifier_loop", with_phase_events("recover_verifier_loop", "recovery", recover_verifier_node))
    if verifier_tools:
        graph.add_node("recover_verifier_tools", ToolNode(verifier_tools))

    graph.set_entry_point("recover_verifier_loop")

    # recover_verifier_loop ⇄ recover_verifier_tools (ReAct loop)
    if verifier_tools:
        graph.add_conditional_edges(
            "recover_verifier_loop",
            should_continue_recover_verifier,
            {
                "continue": "recover_verifier_tools",
                "done": END,
            },
        )
        graph.add_edge("recover_verifier_tools", "recover_verifier_loop")
    else:
        # No verifier tools: goes straight to END
        graph.add_edge("recover_verifier_loop", END)

    return graph



