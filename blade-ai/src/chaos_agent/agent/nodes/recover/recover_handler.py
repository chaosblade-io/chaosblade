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
from chaos_agent.memory.tui_session_store import persist_node_dialogue
from chaos_agent.observability.status_tracker import get_tracker, StatusCategory
from chaos_agent.persistence.task_store import get_task_store

logger = logging.getLogger(__name__)


async def _announce(state: AgentState, msg: str) -> AIMessage:
    """Show *msg* in the TUI, record it on disk, and return it for graph state.

    This node speaks to the user directly instead of through an LLM reply, so it
    owns all three outlets. Persistence is its own job: the next
    ``intent_clarification`` turn rebuilds its persist list from scratch and only
    back-fills ToolMessages from history, and the turn-level fallback in
    ``session_finalizer`` reaches the session file only for the CLI runner (the
    server route passes no ``tui_session_store`` and is gated on an operational
    ``task_id`` that a recovery lookup turn does not have).

    Display failures stay non-fatal — the verdict is already decided.
    """
    try:
        from chaos_agent.agent.dispatch import dispatch_node_message
        await dispatch_node_message("recover_handler", msg)
    except Exception:  # noqa: BLE001
        pass
    message = AIMessage(content=msg)
    persist_node_dialogue(state.get("tui_session_id", ""), [message])
    return message


async def recover_handler(state: AgentState) -> dict:
    """Bridge node for recover intent — prepares context for recover_graph launch.

    If intent_clarification already set recover_task_id (LLM guided the user
    through query_active_experiments), this node passes through without
    redundant queries. Only runs the full lookup as fallback when
    recover_task_id is missing.
    """
    task_id = state.get("task_id", "") or ""

    # If intent_clarification already resolved the target, pass through.
    existing_recover_tid = state.get("recover_task_id", "")
    if existing_recover_tid:
        tracker = get_tracker(task_id) if task_id else None
        if tracker:
            tracker.start(StatusCategory.NODE, "recover_handler", "Recovery target already set")
            tracker.complete(f"pass-through → {existing_recover_tid}")
        return {
            "operation": "recover",
            "recover_task_id": existing_recover_tid,
        }

    # Manual tracker for observability
    tracker = get_tracker(task_id) if task_id else None
    if tracker:
        tracker.start(StatusCategory.NODE, "recover_handler", "Querying active experiments...")

    # Query active (injecting/injected) experiments from task_store
    try:
        store = await get_task_store()
        # Multi-tenant isolation: only query the current tenant's active experiments
        _tenant_id = state.get("tenant_id", "") or ""
        active_tasks = await store.query_active(tenant_id=_tenant_id)

        if not active_tasks:
            msg = "There are no active fault-injection experiments, so there is nothing to recover."
            if tracker:
                tracker.update("No active experiments")
                tracker.complete()
            return {
                "operation": "recover",
                "messages": [await _announce(state, msg)],
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
            msg = f"Found 1 active experiment ({tid}, fault type: {fault}, namespace: {ns}); selected it for recovery automatically."
            if tracker:
                tracker.update(f"Auto-selected experiment {tid}")
                tracker.complete()
            return {
                "operation": "recover",
                "recover_task_id": tid,
                "blade_uid": selected.get("blade_uid"),
                "messages": [await _announce(state, msg)],
                "result": {"status": "completed", "message": msg, "recover_task_id": tid},
            }

        # Multiple active experiments — list them for user selection
        if tracker:
            tracker.update(f"Found {len(enriched)} active experiments, waiting for the user to choose")
            tracker.complete()
        lines = ["Found multiple active experiments. Choose which one to recover:\n"]
        from chaos_agent.agent.experiment_display import format_experiment_line
        for i, t in enumerate(enriched[:10], 1):
            lines.append(format_experiment_line(i, t))
        lines.append("\nReply with the number or the task_id of the experiment to recover.")
        msg = "\n".join(lines)

        return {
            "operation": "recover",
            "needs_task_selection": True,
            "messages": [await _announce(state, msg)],
        }

    except Exception as e:
        logger.error(f"recover_handler failed: {e}")
        if tracker:
            tracker.fail(f"Failed to query active experiments: {e}")
        msg = f"Failed to query active experiments: {e}"
        return {
            "operation": "recover",
            "messages": [await _announce(state, msg)],
            "result": {"status": "failed", "message": msg},
        }