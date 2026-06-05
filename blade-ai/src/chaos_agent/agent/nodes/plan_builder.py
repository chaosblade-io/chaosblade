"""Plan builder node — interactive guided plan construction via interrupt().

The LLM drives a conversation that:
1. Discovers targets (kubectl_ro) — routes to ToolNode
2. Asks user structured questions via present_options → interrupt()
3. Reads skill references for parameter recommendations — routes to ToolNode
4. Generates final plan via submit_plan

Architecture: single graph invocation with internal loop.
- present_options / pure text → interrupt() (pause, wait for user selection)
- tool_calls (kubectl_ro etc.) → RETURN to graph (ToolNode executes, then loops back)
- submit_plan → finalize and RETURN
"""

from __future__ import annotations

import logging
import uuid

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from chaos_agent.config.settings import settings
from chaos_agent.agent.fault_spec import FaultSpec, read_fault_spec
from chaos_agent.agent.plan_generator import generate_injection_plan
from chaos_agent.agent.prompts.builders import build_system_prompt
from chaos_agent.agent.prompts.modes import PromptMode
from chaos_agent.agent.state import AgentState
from chaos_agent.memory.hook import merge_hook_updates
from chaos_agent.memory.session_store import NO_SESSION_MARKER
from chaos_agent.observability.status_tracker import StatusCategory, get_tracker

logger = logging.getLogger(__name__)

MAX_PLAN_BUILDER_ROUNDS = settings.max_plan_builder_rounds

SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": (
        "Generate the final injection plan. Call this ONLY after ALL "
        "decisions are confirmed by the user (target, fault type, "
        "parameters for every fault). Do NOT call this prematurely."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "faults": {
                "type": "array",
                "description": "List of faults to inject (order matters for serial execution)",
                "items": {
                    "type": "object",
                    "properties": {
                        "scope": {"type": "string", "description": "pod/node/container"},
                        "target": {"type": "string", "description": "cpu/mem/network/disk/process"},
                        "action": {"type": "string", "description": "fullload/delay/loss/fill/kill..."},
                        "namespace": {"type": "string"},
                        "names": {"type": "array", "items": {"type": "string"}},
                        "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                        "params": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                    "required": ["scope", "target", "action"],
                },
            },
            "execution_order": {
                "type": "string",
                "enum": ["serial", "parallel"],
                "description": "How to execute multiple faults",
            },
            "interval_seconds": {
                "type": "integer",
                "description": "Interval between serial faults (seconds)",
            },
        },
        "required": ["faults"],
    },
}

PRESENT_OPTIONS_TOOL = {
    "name": "present_options",
    "description": (
        "Present structured options to the user for selection. "
        "ALWAYS use this tool to ask questions — never output options as plain text. "
        "Options: 1-3 real choices + 1 free-input (last). Total 2-4."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "简洁的中文问题",
            },
            "options": {
                "type": "array",
                "description": "1-3 real options + 1 free-input (last). Min 2, max 4.",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {
                            "type": "string",
                            "description": "A/B/C for real options, free_input for last",
                        },
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                        "recommended": {"type": "boolean"},
                    },
                    "required": ["key", "label"],
                },
            },
        },
        "required": ["question", "options"],
    },
}

# Internal tool names that the node handles directly (not via ToolNode)
_INTERNAL_TOOLS = frozenset(("submit_plan", "present_options"))


def make_plan_builder(llm=None, tools: list = None, hook=None, registry=None):
    """Create the plan_builder node function.

    Args:
        llm: LangChain LLM instance.
        tools: Phase1 tools (kubectl_ro, activate_skill, read_skill_resource).
        hook: Optional PreReasoningHook for memory compaction.
        registry: SkillRegistry for dynamic skill catalog in system prompts.
    """

    async def plan_builder(state: AgentState) -> dict:
        messages = list(state.get("messages", []))
        plan_builder_round = state.get("plan_builder_round", 0)
        task_id = state.get("task_id", "")
        tui_session_id = state.get("tui_session_id", "")

        tracker = get_tracker(task_id) if task_id else None
        if tracker:
            tracker.start(StatusCategory.NODE, "plan_builder", "方案设计对话中...")

        if llm is None:
            if tracker:
                tracker.complete("LLM 不可用")
            return {"messages": [AIMessage(content="抱歉，LLM 不可用，无法构建方案。")]}

        hook_updates = {}
        if hook:
            hook_updates = await hook(state) or {}

        existing_spec = read_fault_spec(state)

        system_msg = SystemMessage(
            content=build_system_prompt(
                PromptMode.PLAN_BUILDER,
                fault_spec=existing_spec,
                skill_catalog=registry.build_catalog_prompt() if registry else "",
            )
        )

        function_schemas = [SUBMIT_PLAN_TOOL, PRESENT_OPTIONS_TOOL]
        llm_bound = llm.bind_tools(function_schemas + (tools or []))

        # Accumulated messages from interrupt/resume cycles within this
        # single node invocation. Returned as part of state update so
        # the checkpointer persists them for subsequent ToolNode → plan_builder calls.
        accumulated: list = []
        rounds_this_invoke = 0

        while plan_builder_round + rounds_this_invoke < MAX_PLAN_BUILDER_ROUNDS:
            if tracker:
                tracker.update("调用 LLM...")
            try:
                response = await llm_bound.ainvoke(
                    [system_msg] + messages[-30:] + accumulated
                )
            except Exception as e:
                logger.error("Plan builder LLM failed: %s", e)
                if tracker:
                    tracker.fail(f"LLM 调用失败: {e}")
                return merge_hook_updates({
                    "messages": accumulated + [AIMessage(content="抱歉，遇到了一些问题，请稍后再试。")],
                    "plan_builder_round": plan_builder_round + rounds_this_invoke + 1,
                }, hook_updates)

            tool_calls = getattr(response, "tool_calls", None) or []

            # ── Priority 1: submit_plan ──
            submit_args = _extract_submit_plan(tool_calls)
            if submit_args:
                submit_args = _validate_submit_plan(submit_args)
                new_spec = _build_spec_from_submit(submit_args, existing_spec)
                plan_text = _format_final_plan(submit_args, state, new_spec)
                if tracker:
                    tracker.complete("方案设计完成")
                _persist_dialogue(tui_session_id, _build_dialogue_persist_list(
                    messages, accumulated, response, system_msg, plan_builder_round,
                ))
                result_dict = {
                    "messages": accumulated + [AIMessage(content=plan_text)],
                    "fault_spec": new_spec.to_dict(),
                    "plan_confirmed": True,
                    "plan_builder_round": plan_builder_round + rounds_this_invoke + 1,
                }
                # Store full batch args when multiple faults submitted
                faults = submit_args.get("faults", [])
                if len(faults) > 1:
                    result_dict["batch_submit_args"] = {
                        "faults": faults,
                        "execution_order": submit_args.get("execution_order", "serial"),
                        "interval_seconds": submit_args.get("interval_seconds", 0),
                    }
                return merge_hook_updates(result_dict, hook_updates)

            # ── Priority 2: present_options → interrupt() ──
            options_args = _extract_present_options(tool_calls)
            if options_args:
                if tracker:
                    tracker.complete("等待用户选择")
                _persist_dialogue(tui_session_id, _build_dialogue_persist_list(
                    messages, accumulated, response, system_msg, plan_builder_round,
                ))

                answer = interrupt({
                    "type": "plan_selection",
                    "question": options_args["question"],
                    "options": options_args["options"],
                })

                # User cancelled (Esc) — exit plan_builder cleanly.
                if answer == "rejected":
                    if tracker:
                        tracker.complete("用户取消")
                    return merge_hook_updates({
                        "messages": accumulated + [AIMessage(content="已取消方案构建。")],
                        "plan_builder_round": plan_builder_round + rounds_this_invoke + 1,
                    }, hook_updates)

                # Resume: user selected. Add AI response + user answer to accumulated.
                # Keep ONLY the present_options call (to pair with the ToolMessage below);
                # strip real tools — they won't be executed since we're interrupting.
                tc_id = _get_tool_call_id(tool_calls, "present_options")
                options_tc = [tc for tc in tool_calls if tc.get("name") == "present_options"]
                accumulated.append(AIMessage(
                    content=getattr(response, "content", "") or "",
                    tool_calls=options_tc,
                    id=f"pb-opt-{uuid.uuid4().hex[:8]}",
                ))
                accumulated.append(ToolMessage(
                    content=f"用户选择: {answer}",
                    tool_call_id=tc_id,
                ))
                rounds_this_invoke += 1
                if tracker:
                    tracker.start(StatusCategory.NODE, "plan_builder", "方案设计对话中...")
                continue

            # ── Priority 3: real tool_calls (kubectl_ro etc.) → return to ToolNode ──
            has_real_tools = any(
                tc.get("name") not in _INTERNAL_TOOLS for tc in tool_calls
            )
            if has_real_tools:
                if tracker:
                    tracker.complete("等待工具执行")
                filtered = _filter_internal_from_response(response)
                _persist_dialogue(tui_session_id, _build_dialogue_persist_list(
                    messages, accumulated, response, system_msg, plan_builder_round,
                ))
                state_update: dict = {
                    "messages": accumulated + [filtered],
                    "plan_builder_round": plan_builder_round + rounds_this_invoke + 1,
                }
                for tc in tool_calls:
                    if tc.get("name") == "activate_skill":
                        tc_args = tc.get("args") or {}
                        if tc_args.get("skill_name"):
                            state_update["skill_name"] = tc_args["skill_name"]
                            break
                return merge_hook_updates(state_update, hook_updates)

            # ── Priority 4: pure text fallback → interrupt with raw text ──
            if tracker:
                tracker.complete("等待用户输入")
            _persist_dialogue(tui_session_id, _build_dialogue_persist_list(
                messages, accumulated, response, system_msg, plan_builder_round,
            ))

            answer = interrupt({
                "type": "plan_selection",
                "question": getattr(response, "content", "") or "",
                "options": [],
            })

            # User cancelled (Esc) — exit plan_builder cleanly.
            if answer == "rejected":
                if tracker:
                    tracker.complete("用户取消")
                return merge_hook_updates({
                    "messages": accumulated + [AIMessage(content="已取消方案构建。")],
                    "plan_builder_round": plan_builder_round + rounds_this_invoke + 1,
                }, hook_updates)

            accumulated.append(response)
            accumulated.append(HumanMessage(content=answer))
            rounds_this_invoke += 1
            if tracker:
                tracker.start(StatusCategory.NODE, "plan_builder", "方案设计对话中...")
            continue

        # Max rounds exceeded
        if tracker:
            tracker.complete("方案构建轮数超限")
        return merge_hook_updates({
            "messages": accumulated + [AIMessage(content="方案构建对话轮数已达上限，请使用 /plan 重新开始。")],
            "plan_builder_round": plan_builder_round + rounds_this_invoke,
        }, hook_updates)

    return plan_builder


# ── Helpers ──


def _extract_submit_plan(tool_calls: list) -> dict | None:
    for tc in tool_calls:
        if tc.get("name") == "submit_plan":
            return tc.get("args", {})
    return None


def _validate_submit_plan(args: dict) -> dict:
    """Validate and sanitize submit_plan arguments.

    Ensures faults array is non-empty and each fault has scope/target/action.
    Drops invalid entries and logs warnings.
    """
    faults = args.get("faults", [])
    if not isinstance(faults, list) or not faults:
        logger.warning("submit_plan: empty or invalid faults array")
        return args

    valid = []
    for i, f in enumerate(faults):
        if not isinstance(f, dict):
            logger.warning("submit_plan: fault[%d] is not a dict, skipping", i)
            continue
        if not (f.get("scope") and f.get("target") and f.get("action")):
            logger.warning(
                "submit_plan: fault[%d] missing scope/target/action: %s, skipping",
                i, {k: f.get(k) for k in ("scope", "target", "action")},
            )
            continue
        valid.append(f)

    if not valid:
        logger.warning("submit_plan: no valid faults after validation, keeping originals")
        return args

    args = dict(args)
    args["faults"] = valid
    return args


def _extract_present_options(tool_calls: list) -> dict | None:
    for tc in tool_calls:
        if tc.get("name") == "present_options":
            return tc.get("args", {})
    return None


def _get_tool_call_id(tool_calls: list, name: str) -> str:
    for tc in tool_calls:
        if tc.get("name") == name:
            return tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
    return f"call_{uuid.uuid4().hex[:8]}"


def _filter_internal_from_response(response) -> AIMessage:
    """Remove internal tool_calls (submit_plan, present_options) from response.
    Real tools (kubectl_ro etc.) preserved for ToolNode routing.
    """
    filtered_tc = [
        tc for tc in (getattr(response, "tool_calls", None) or [])
        if tc.get("name") not in _INTERNAL_TOOLS
    ]
    kwargs = {}
    original_id = getattr(response, "id", None)
    if original_id:
        kwargs["id"] = original_id
    return AIMessage(
        content=getattr(response, "content", "") or "",
        tool_calls=filtered_tc,
        response_metadata=getattr(response, "response_metadata", {}) or {},
        **kwargs,
    )


def _build_spec_from_submit(submit_args: dict, existing_spec: FaultSpec | None) -> FaultSpec:
    faults = submit_args.get("faults", [])
    if not faults:
        return existing_spec or FaultSpec()

    first = faults[0]
    spec = existing_spec or FaultSpec()
    return FaultSpec(
        namespace=first.get("namespace") or spec.namespace,
        scope=first.get("scope") or spec.scope,
        names=tuple(first.get("names") or list(spec.names)),
        labels=dict(first.get("labels") or dict(spec.labels)),
        blade_target=first.get("target") or spec.blade_target,
        blade_action=first.get("action") or spec.blade_action,
        params=dict(first.get("params") or dict(spec.params)),
        params_flags=spec.params_flags,
        duration_seconds=spec.duration_seconds,
        source=spec.source or "tui",
        user_description=spec.user_description,
    )


def _format_final_plan(submit_args: dict, state: dict, spec: FaultSpec) -> str:
    try:
        patched_state = dict(state)
        patched_state["fault_spec"] = spec.to_dict()
        return generate_injection_plan(patched_state)
    except Exception as e:
        logger.warning("plan_generator failed, using fallback: %s", e)
        faults = submit_args.get("faults", [])
        lines = ["# 故障注入计划\n"]
        for i, f in enumerate(faults, 1):
            lines.append(f"## 故障 {i}: {f.get('scope')}-{f.get('target')} {f.get('action')}")
            if f.get("namespace"):
                lines.append(f"- Namespace: `{f['namespace']}`")
            if f.get("names"):
                lines.append(f"- Names: {', '.join(f['names'])}")
            if f.get("params"):
                lines.append(f"- Params: {f['params']}")
            lines.append("")
        lines.append("---\n确认执行: `/run` | 调整: `/plan <修改建议>`")
        return "\n".join(lines)


def _build_dialogue_persist_list(
    messages: list,
    accumulated: list,
    response=None,
    system_msg: SystemMessage | None = None,
    dialogue_round: int = 0,
) -> list:
    persist = []
    if system_msg is not None:
        pb_system = SystemMessage(
            content=f"[Plan Builder Prompt]\n{system_msg.content}",
            id=f"pb-system-round-{dialogue_round}",
        )
        persist.append(pb_system)

    for msg in messages:
        if isinstance(msg, SystemMessage):
            continue
        _kw = getattr(msg, "additional_kwargs", None) or {}
        if _kw.get(NO_SESSION_MARKER):
            continue
        persist.append(msg)

    persist.extend(accumulated)

    if response is not None:
        filtered = _filter_internal_from_response(response)
        if filtered.content or filtered.tool_calls:
            persist.append(filtered)

    return persist


def _persist_dialogue(tui_session_id: str, messages: list) -> None:
    if not tui_session_id:
        return
    try:
        from chaos_agent.memory.tui_session_store import (
            get_global_tui_session_store,
        )
        store = get_global_tui_session_store()
        if store is not None:
            store.append_dialogue(tui_session_id, messages)
    except Exception as e:
        logger.debug(f"Plan builder dialogue persistence skipped: {e}")
