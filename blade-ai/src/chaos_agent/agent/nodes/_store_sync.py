"""Helper to sync graph-node state updates to the persistent TaskStore.

This module provides the ``sync_to_store`` function that each graph node
calls (as a side-effect) before returning its result dict.  The function
is fire-and-forget: exceptions are logged but never propagated to the
graph pipeline.
"""

import logging
from typing import Any

from chaos_agent.persistence.task_store_backend import (
    _DETAIL_COLUMNS,
    _TASK_COLUMNS,
)

logger = logging.getLogger(__name__)

# Mapping from AgentState field names to DB column names where they differ
_STATE_TO_DB_MAP = {
    "needs_confirmation": "needs_confirm",
    "plan_summary": "plan_summary",
    "created_at": "gmt_create",  # AgentState uses created_at, DB uses gmt_create
    "skill_case_content": "skill_use_case",  # Store reference (skill + use-case name), not full content
}


def _extract_db_fields(merged: dict) -> tuple[dict, dict]:
    """Split AgentState fields into (task_fields, detail_fields).

    AgentState field ``created_at`` maps to DB column ``gmt_create``.
    JSON columns are left as-is (dicts) — serialization is handled by
    ``TaskStore.upsert`` which knows how to merge and serialize correctly.

    ``skill_case_content`` (full SKILL.md text) is mapped to ``skill_use_case``
    as a *reference* — we store the skill_name instead of the full content,
    because the full content can be rebuilt from the skills/ directory.
    """
    task_fields: dict[str, Any] = {}
    detail_fields: dict[str, Any] = {}
    for key, value in merged.items():
        db_key = _STATE_TO_DB_MAP.get(key, key)
        if db_key == "task_id":
            continue
        # skill_case_content → skill_use_case: store skill_name reference, not full text
        if key == "skill_case_content":
            skill_name = merged.get("skill_name", "")
            db_key = "skill_use_case"
            value = skill_name  # Reference: can rebuild full content from skills/ dir
        if db_key in _TASK_COLUMNS:
            task_fields[db_key] = value
        elif db_key in _DETAIL_COLUMNS:
            detail_fields[db_key] = value
    # bool → int
    if "needs_confirm" in detail_fields and isinstance(detail_fields["needs_confirm"], bool):
        detail_fields["needs_confirm"] = int(detail_fields["needs_confirm"])
    return task_fields, detail_fields


async def sync_to_store(state: dict, updated_fields: dict) -> None:
    """Sync node state updates to TaskStore (fire-and-forget).

    Merges the full AgentState (``state``) with the node's return dict
    (``updated_fields``) to produce a complete post-node snapshot, then
    upserts the relevant fields into the TaskStore.

    Must **not** raise exceptions – errors are logged and swallowed so
    the graph pipeline is never disrupted.
    """
    task_id = state.get("task_id", "") or updated_fields.get("task_id", "")
    if not task_id:
        return
    try:
        from chaos_agent.persistence.task_store import get_task_store

        merged = dict(state)
        merged.update(updated_fields)
        task_fields, detail_fields = _extract_db_fields(merged)
        # upsert handles the field splitting internally
        all_fields = {**task_fields, **detail_fields}
        store = await get_task_store()
        await store.upsert(task_id, **all_fields)
    except Exception:
        logger.exception(f"TaskStore sync failed for {task_id}")


def sync_node_status_to_session(
    state: dict,
    node_name: str,
    message: str,
    detail: dict | None = None,
) -> None:
    """Record node execution status to session (aligned with StatusTracker events).

    Fire-and-forget: exceptions are logged but never propagated to the
    graph pipeline, matching the same safety guarantee as ``sync_to_store``.
    """
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        _session_store = get_global_session_store()
        _task_id = state.get("task_id", "")
        if _session_store and _task_id:
            _session_store.append_raw_message(_task_id, {
                "type": "system",
                "content": f"[{node_name}] {message}",
                "detail": detail or {},
            })
    except Exception:
        logger.exception(f"Session status sync failed for node {node_name}")
