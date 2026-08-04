"""Normalize LangGraph streaming events for L4 runtime consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable


def _extract_pending_interrupt_payload(graph_state) -> object | None:
    """Return the first unresolved interrupt's payload, or None."""
    if not graph_state or not graph_state.tasks:
        return None
    for task in graph_state.tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for it in interrupts:
            value = getattr(it, "value", None)
            if value is not None:
                return value
    return None


# ---------------------------------------------------------------------------
# Progress event forwarding — astream_events v2 → on_event callback
# ---------------------------------------------------------------------------

# Tool names to surface as progress events (user-facing).
_TOOL_DISPLAY_NAMES: dict[str, str] = {
    "kubectl_read": "Querying cluster resources",
    "host_read": "Querying host state",
    "read_skill_resource": "Reading a fault scenario",
    "activate_skill": "Activating a fault skill",
}


def _extract_aimessage(output: Any) -> Any | None:
    """Pull the AIMessage from a LangGraph ``on_chat_model_end`` ``data.output``.

    The output may be:
      * an ``LLMResult`` with ``.generations[0][0].message``
      * an ``AIMessage`` directly
      * ``None`` / something else
    """
    if output is None:
        return None
    if hasattr(output, "generations"):
        gens = output.generations
        if gens and gens[0]:
            first = gens[0][0]
            return getattr(first, "message", None)
    if hasattr(output, "additional_kwargs"):
        return output
    return None


# 与 TUI streaming.py 的 _SILENT_TOKEN_NODES 保持一致：
# 这些节点的 LLM 输出以专用卡片展示（如 postmortem），流式 token 会重复。
_SILENT_TOKEN_NODES: frozenset = frozenset({"save_memory"})


def _is_silent_node(event: dict) -> bool:
    """Check if the event originates from a node whose tokens should not stream."""
    tags = event.get("tags", [])
    for tag in tags:
        if tag.startswith("langsmith:nodes:"):
            return tag.split(":")[-1] in _SILENT_TOKEN_NODES
    metadata = event.get("metadata", {})
    node = metadata.get("langgraph_node", "")
    return node in _SILENT_TOKEN_NODES


def _normalize_langgraph_event(event: dict) -> list[dict]:
    """Single source of truth for parsing LangGraph ``astream_events(v2)``
    events into normalized progress dicts.

    Shared by both progress channels:
      * clarify path → ``_forward_progress_event`` → ``on_event(dict)`` callback
      * inject/recover path → ``_process_event`` → ``runtime.emit_event(kind, dict)``

    Adding a new event surface (or changing field shape) only requires
    touching this function — both channels stay in lockstep.

    Returns a list because some upstream events expand into multiple
    progress events (notably ``on_chat_model_end`` → ``llm_thought`` +
    ``llm_end``). Returns ``[]`` when the event is intentionally dropped
    (e.g. ``on_chain_start`` outside the small whitelist, ``on_chain_end``,
    retriever / parser / prompt events, unknown custom events).
    """
    kind = event.get("event", "")
    name = event.get("name", "")
    data = event.get("data") or {}

    if kind == "on_tool_start":
        display = _TOOL_DISPLAY_NAMES.get(name, name)
        ev: dict = {
            "kind": "tool_start",
            "tool_name": name,
            "message": f"Calling: {display}",
            # astream_events 中同一次工具调用的 on_tool_start / on_tool_end
            # 共享同一 run_id，作为前端精确配对并发同名工具的唯一键。
            "call_id": event.get("run_id", "") or "",
        }
        tool_input = data.get("input")
        if tool_input is not None:
            ev["input"] = tool_input
        return [ev]

    if kind == "on_tool_end":
        display = _TOOL_DISPLAY_NAMES.get(name, name)
        ev = {
            "kind": "tool_end",
            "tool_name": name,
            "message": f"Finished: {display}",
            "level": "ok",
            "call_id": event.get("run_id", "") or "",
        }
        output = data.get("output")
        if output is not None:
            content = getattr(output, "content", None)
            if content is None and isinstance(output, dict):
                content = output.get("content")
            if content is None:
                content = str(output)
            if isinstance(content, str) and len(content) > 2000:
                content = content[:2000] + "...(truncated)"
            ev["output"] = content
        return [ev]

    if kind == "on_tool_error":
        # A tool raised — most often an arg-schema ValidationError (the LLM
        # sent a dict/list field as a JSON string, e.g. update_progress's
        # ``state_update``), but also any exception mid-execution. LangChain
        # fires ``on_tool_error`` here, NOT ``on_tool_end``. Without a terminal
        # event carrying the SAME ``run_id`` as the preceding ``on_tool_start``,
        # the platform timeline pairs by ``call_id`` and leaves the tool card
        # stuck on 正在调用 forever. The TUI path (streaming.py) already
        # synthesises this; this channel was missing it. ToolNode's
        # ``handle_tool_errors`` separately feeds the error back to the LLM as a
        # ToolMessage — that is graph state, not a stream event.
        display = _TOOL_DISPLAY_NAMES.get(name, name)
        err = data.get("error")
        content = (str(err).strip() if err is not None else "") or "tool error"
        if len(content) > 2000:
            content = content[:2000] + "...(truncated)"
        return [{
            "kind": "tool_end",
            "tool_name": name,
            "message": f"Failed: {display}",
            "level": "error",
            "is_error": True,
            "call_id": event.get("run_id", "") or "",
            "output": content,
        }]

    if kind == "on_chain_start" and name in (
        "load_memory", "intent_confirm", "save_memory",
    ):
        return [{
            "kind": "node_start",
            "node_name": name,
            "message": f"Entering node: {name}",
        }]

    if kind == "on_chat_model_start":
        # save_memory 节点的输出以 postmortem 卡片展示，流式 token 会重复。
        if _is_silent_node(event):
            return []
        # 发送 llm_start 让前端创建 thinking 节点，后续 llm_token 会累积到该节点。
        return [{"kind": "llm_start", "message": ""}]

    if kind == "on_chat_model_stream":
        # save_memory 节点静默（见上）
        if _is_silent_node(event):
            return []
        # 流式 token chunk：前端已实现 token 聚合（按 llm_start → llm_thought
        # 聚到同一 thinking 卡片），恢复逐 token 下发以支持流式展示。
        chunk = data.get("chunk")
        if chunk is None:
            return []

        # enable_thinking 模式（Qwen 等）：思考内容在 additional_kwargs.reasoning_content
        additional_kwargs = getattr(chunk, "additional_kwargs", {}) or {}
        reasoning_content = additional_kwargs.get("reasoning_content", "")
        if reasoning_content:
            return [{"kind": "llm_token", "message": reasoning_content}]

        content = getattr(chunk, "content", "") or ""
        if isinstance(content, list):
            text_parts = [
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            ]
            content = "".join(text_parts)
        if not content:
            return []
        return [{"kind": "llm_token", "message": content}]

    if kind == "on_chat_model_end":
        msg = _extract_aimessage(data.get("output"))
        if msg is None:
            return []
        content = ""
        if hasattr(msg, "content"):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
        rc = ""
        if hasattr(msg, "additional_kwargs"):
            rc = msg.additional_kwargs.get("reasoning_content") or ""
        usage = getattr(msg, "usage_metadata", None)

        events: list[dict] = []
        # ``llm_thought``: surfaces final answer or chain-of-thought. Thinking
        # models (Qwen enable_thinking, etc.) put CoT in ``reasoning_content``
        # with empty ``content`` on tool-call turns — fall back to ``rc`` so
        # the live stream still shows reasoning on those turns.
        # 同时把 token usage / reasoning 原文挂到 llm_thought 上，避免再
        # 单独发一帧 ``llm_end``（"AI 思考完成"卡片相对 llm_thought 整段
        # 是冗余收尾，对用户无信息增量）。
        # message 统一前缀 ``💭 模型思考：``，让前端 timeline 一眼分辨
        # 这是模型推理而非工具调用 / 节点提示。完整内容仍在 ``content``
        # 字段（截 3000 字），前端可基于 content 做展开式展示。
        _THOUGHT_PREFIX = "💭 Model reasoning: "
        if content:
            ev: dict = {
                "kind": "llm_thought",
                "message": f"{_THOUGHT_PREFIX}{content[:500]}",
                "content": content[:3000],
            }
            if isinstance(usage, dict):
                ev["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            if isinstance(rc, str) and rc:
                ev["reasoning"] = rc[:3000]
            events.append(ev)
        elif rc:
            ev = {
                "kind": "llm_thought",
                "message": f"{_THOUGHT_PREFIX}{rc[:500]}",
                "content": rc[:5000],
                "reasoning": rc[:5000],
            }
            if isinstance(usage, dict):
                ev["usage"] = {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "total_tokens": usage.get("total_tokens", 0),
                }
            events.append(ev)
        # 不再发独立的 ``llm_end`` 卡片；如未来前端需要"思考完成"语义
        # 标记，可改为读 ``llm_thought`` 的存在与否做边界推断。
        return events

    if kind == "on_custom_event":
        # phase_started / phase_completed 仅在 ReAct 循环的 intent 阶段
        # 出现重复噪声（每次工具调用回到 intent node 都会再发一次）。
        # inject / recover 状态机里 baseline_capture / inject / verify /
        # recover / cleanup / postmortem 等 phase 每阶段只发 1 次，是
        # 真实的状态切换提示，必须保留以便前端展示节奏。
        if name in ("phase_started", "phase_completed"):
            phase_name = data.get("phase", "") or ""
            if phase_name == "intent":
                return []
            # phase_completed 不在此处 emit 给平台——由 _process_event /
            # _process_recover_event 在容器真正关闭时补发。这样避免
            # verifier_loop 等多次进出同一 phase 的节点在中间迭代发出
            # phase_completed 导致前端提前关闭容器、后续工具事件逃逸到顶层。
            if name == "phase_completed":
                return []
            label = "Phase started"
            return [{
                "kind": name,
                "node": data.get("node", ""),
                "phase": phase_name,
                "message": f"{label}: {phase_name or data.get('node', '')}",
            }]
        if name == "node_message":
            content = data.get("content", "")
            if isinstance(content, str) and content:
                return [{
                    "kind": "node_message",
                    "node": data.get("node", ""),
                    "message": content,
                }]
            return []
        if name == "batch_fault_result":
            return [{
                "kind": "batch_fault_result",
                "message": "Batch fault results",
                "detail": data,
            }]
        return []

    return []


def _forward_progress_event(
    event: dict,
    on_event: "Callable[[dict], None]",
) -> None:
    """Thin wrapper used by the clarify path (lightweight ``on_event``
    callback). All parsing lives in :func:`_normalize_langgraph_event` so
    this channel stays byte-for-byte aligned with the inject/recover
    ``runtime.emit_event`` channel.
    """
    for ev in _normalize_langgraph_event(event):
        on_event(ev)


def _last_ai_message_text(values: dict) -> str:
    """Pull the last AIMessage content from graph state.messages."""
    msgs = values.get("messages") or []
    for msg in reversed(msgs):
        cls_name = type(msg).__name__
        if cls_name == "AIMessage":
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                return content
            return str(content) if content else ""
    return ""


def _conn_to_state_patch(conn: dict) -> dict:
    """Translate a ``conn`` dict (from platform) into LangGraph state patch.

    Only includes keys present in ``conn`` to avoid clobbering existing
    state values with empty strings. This is the wire-format used by both
    ``clarify(conn=...)`` (initial inject) and ``update_connection``
    (mid-conversation env switch).
    """
    patch: dict = {}
    for key in ("kubeconfig", "kube_context",
                "kubewiz_cluster_uuid", "kubewiz_profile"):
        if key in conn:
            patch[key] = conn.get(key) or ""
    return patch

# phase_started node → runtime.step() name mapping.
# Only nodes wrapped with with_phase_events() emit events.
# direct_setup, load_memory do NOT emit phase events.
# save_memory IS wrapped (phase="postmortem") — see graph.py.
_PHASE_STEP_MAP: dict[str, str] = {
    "intent_clarification": "planning",
    "plan_builder": "planning",
    "agent_loop": "planning",
    "safety_check": "safety_check",
    "confirmation_gate": "approval_gate",
    "intent_confirm": "approval_gate",
    "baseline_capture": "baseline_capture",
    "execute_loop": "fault_injection",
    "direct_execute": "fault_injection",
    "verifier_loop": "verification",
    "finalize_verification": "verification",
    "save_memory": "postmortem",
    # Recover graph phases
    "recover_verifier_loop": "recovery",
    "finalize_recover_verification": "recovery",
}


_logging_configured = False
_log_dir: "Path | None" = None

