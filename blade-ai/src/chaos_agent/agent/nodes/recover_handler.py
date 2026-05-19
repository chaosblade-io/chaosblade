"""recover_handler node — bridge node for recover intent in inject_graph.

This node is NOT part of recover_graph. It's a bridge in inject_graph that:
1. Identifies the experiment to recover (auto-select if only one active, ask if multiple)
2. Sets operation="recover" and recover context in state
3. Routes to save_memory → END

The TUI ConversationController detects confirmed_intent="recover" in the result
event and auto-launches recover_graph independently. This keeps inject_graph
and recover_graph separate — no nested graph invocation.

CLI recover command uses a separate entry point (blade-ai recover --task-id)
and does NOT go through this node.
"""

from __future__ import annotations

import logging

from langchain_core.messages import AIMessage

from chaos_agent.agent.state import AgentState
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.persistence.task_store import get_task_store

logger = logging.getLogger(__name__)


async def recover_handler(state: AgentState) -> dict:
    """Bridge node for recover intent — prepares context for recover_graph launch.

    Scenarios:
    - No active experiments → inform user, no recovery possible
    - Exactly 1 active experiment → auto-select, set recover_task_id
    - Multiple active experiments → list them, user selects via intent_context
    """
    task_id = state.get("task_id", "unknown")

    # Manual tracker for observability
    tracker = get_tracker(task_id) if task_id else None
    if tracker:
        tracker.start(StatusCategory.NODE, "recover_handler", "查询活跃实验...")

    # Query active (injecting/injected) experiments from task_store
    try:
        store = await get_task_store()
        active_tasks = await store.query_active()

        if not active_tasks:
            msg = "当前没有活跃的故障注入实验，无需恢复。"
            if tracker:
                tracker.update("无活跃实验")
                tracker.complete()
            return {
                "operation": "recover",
                "messages": [AIMessage(content=msg)],
                "result": {"status": "completed", "message": msg},
            }

        # Enrich active tasks with full detail
        enriched = []
        for t in active_tasks:
            tid = t.get("task_id", "")
            if tid:
                detail = await store.get(tid)
                if detail:
                    enriched.append(detail)
                else:
                    enriched.append(t)
            else:
                enriched.append(t)

        if len(enriched) == 1:
            selected = enriched[0]
            tid = selected.get("task_id", "?")
            fault = selected.get("fault_type", "unknown")
            ns = (selected.get("target") or {}).get("namespace", "unknown")
            msg = f"检测到 1 个活跃实验 ({tid}，故障类型: {fault}，命名空间: {ns})，已自动选择恢复。"
            if tracker:
                tracker.update(f"自动选择实验 {tid}")
                tracker.complete()
            return {
                "operation": "recover",
                "recover_task_id": tid,
                "blade_uid": selected.get("blade_uid"),
                "messages": [AIMessage(content=msg)],
                "result": {"status": "completed", "message": msg, "recover_task_id": tid},
            }

        # Multiple active experiments — list them for user selection
        if tracker:
            tracker.update(f"检测到 {len(enriched)} 个活跃实验，等待用户选择")
            tracker.complete()
        lines = ["检测到多个活跃实验，请选择要恢复的实验：\n"]
        for i, t in enumerate(enriched[:10], 1):
            tid = t.get("task_id", "?")
            fault = t.get("fault_type", "unknown")
            target = t.get("target") or {}
            ns = target.get("namespace", "unknown")
            lines.append(f"  {i}. task_id: {tid}, 故障类型: {fault}, 命名空间: {ns}")
        lines.append("\n请回复编号或 task_id 来选择要恢复的实验。")
        msg = "\n".join(lines)

        return {
            "operation": "recover",
            "needs_task_selection": True,
            "messages": [AIMessage(content=msg)],
        }

    except Exception as e:
        logger.error(f"recover_handler failed: {e}")
        if tracker:
            tracker.fail(f"查询活跃实验失败: {e}")
        msg = f"查询活跃实验失败: {e}"
        return {
            "operation": "recover",
            "messages": [AIMessage(content=msg)],
            "result": {"status": "failed", "message": msg},
        }