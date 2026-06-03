"""Storage backend protocol and shared constants for TaskStore persistence.

Defines the async ``StorageBackend`` protocol that both SQLite and PostgreSQL
backends must implement, along with column definitions and helper functions
shared across backends and the TaskStore business-logic layer.
"""

import json
from typing import Optional, Protocol, runtime_checkable

from chaos_agent.utils.time import now_iso

# ---------------------------------------------------------------------------
# Shared column definitions
# ---------------------------------------------------------------------------

# Columns stored as JSON strings in the DB
_JSON_COLUMNS: frozenset[str] = frozenset(
    {"target", "params", "verification", "recover_verification", "result",
     "baseline_data",
     # R18 — postmortem dict (path/markdown/summary) JSON-serialised.
     "postmortem",
     # E18 — safety pre-check report dicts.
     "target_health_report", "feasibility_report"}
)

# tasks table — narrow, hot path (16 columns)
_TASK_COLUMNS: list[str] = [
    "id", "task_id", "task_state", "stage", "phase", "operation",
    "skill_name", "blade_uid", "namespace", "target_name",
    "error", "finished_at", "duration_ms",
    "gmt_create", "gmt_modified",
]

# task_details table — wide, cold path (25 columns)
_DETAIL_COLUMNS: list[str] = [
    "id", "task_id", "target", "params", "input",
    "safety_status", "safety_reason", "needs_confirm",
    "plan_summary", "kubeconfig", "kube_context",
    "verification", "recover_verification", "result",
    "failure_reason",
    "baseline_data", "inject_context", "skill_use_case",
    "injection_method", "kubectl_exec_pod_name",
    # R18 — postmortem dict (JSON-serialised), see save_memory.
    "postmortem",
    # E18 — safety pre-check reports (JSON-serialised).
    "target_health_report", "feasibility_report",
    "total_token_input", "total_token_output",
    "total_llm_calls", "total_tool_calls", "total_duration_ms",
    "gmt_create", "gmt_modified",
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _extract_index_fields(fields: dict) -> dict:
    """Extract *namespace* and *target_name* from the ``target`` JSON field
    into independent, indexable columns.

    This enables SQL-level filtering in ``query_active()`` without scanning
    and parsing JSON at the Python layer.
    """
    target = fields.get("target")
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except (json.JSONDecodeError, TypeError):
            target = None
    if isinstance(target, dict):
        fields.setdefault("namespace", target.get("namespace", ""))
        names = target.get("names", [])
        if names:
            fields.setdefault("target_name", names[0])
    return fields


def _set_timestamps(fields: dict, existing: Optional[dict]) -> dict:
    """Set ``gmt_create`` / ``gmt_modified`` on *fields*.

    - ``gmt_create``: set on INSERT only; preserved from *existing* on UPDATE.
    - ``gmt_modified``: always updated to the current UTC time.
    """
    now = now_iso()
    if existing and existing.get("gmt_create"):
        fields["gmt_create"] = existing["gmt_create"]  # preserve from DB
    elif not fields.get("gmt_create"):
        fields["gmt_create"] = now  # new row, no explicit value → auto-generate
    fields["gmt_modified"] = now
    return fields


# ---------------------------------------------------------------------------
# StorageBackend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageBackend(Protocol):
    """Async protocol that every persistence backend must implement.

    All methods are coroutine functions.  Backends are responsible for their
    own connection lifecycle (lazy init on first use, explicit ``close()``).
    """

    # -- schema --------------------------------------------------------------

    async def ensure_schema(self) -> None:
        """Create tables / indexes if they do not yet exist."""
        ...

    # -- tasks (narrow, hot) -------------------------------------------------

    async def select_task(self, task_id: str) -> Optional[dict]:
        """SELECT * FROM tasks WHERE task_id = ?"""
        ...

    async def upsert_task(self, task_id: str, columns: list[str], values: list) -> None:
        """INSERT … ON CONFLICT(task_id) DO UPDATE SET … for the *tasks* table."""
        ...

    async def select_tasks_ordered(self, limit: int, offset: int) -> list[dict]:
        """SELECT * FROM tasks ORDER BY gmt_create DESC LIMIT ? OFFSET ?"""
        ...

    async def select_tasks_by_state(self, task_state: str, limit: int, offset: int) -> list[dict]:
        """SELECT * FROM tasks WHERE task_state = ? ORDER BY gmt_create DESC LIMIT ? OFFSET ?"""
        ...

    async def select_active_tasks(self, namespace: str = "", target_name: str = "") -> list[dict]:
        """SELECT * FROM tasks WHERE task_state IN ('injecting','injected') [AND namespace=?] [AND target_name=?] ORDER BY gmt_create DESC"""
        ...

    async def delete_task(self, task_id: str) -> bool:
        """DELETE FROM tasks WHERE task_id = ?.  Return True if a row was deleted."""
        ...

    async def count_tasks(self, task_state: str = None) -> int:
        """COUNT(*) with optional task_state filter."""
        ...

    # -- task_details (wide, cold) -------------------------------------------

    async def select_details(self, task_id: str) -> Optional[dict]:
        """SELECT * FROM task_details WHERE task_id = ?"""
        ...

    async def upsert_details(self, task_id: str, columns: list[str], values: list) -> None:
        """INSERT … ON CONFLICT(task_id) DO UPDATE SET … for the *task_details* table."""
        ...

    async def select_details_batch(self, task_ids: list[str]) -> list[dict]:
        """SELECT * FROM task_details WHERE task_id IN (…)."""
        ...

    async def delete_details(self, task_id: str) -> None:
        """DELETE FROM task_details WHERE task_id = ?"""
        ...

    # -- task_spans ----------------------------------------------------------

    async def delete_spans_by_task(self, task_id: str) -> None:
        """DELETE FROM task_spans WHERE task_id = ?"""
        ...

    async def insert_span(
        self,
        task_id: str,
        node_name: str,
        start_time: float,
        end_time: float,
        duration_ms: float,
        token_input: int,
        token_output: int,
        tool_calls_json: str,
        error: Optional[str],
        gmt_create: str,
        gmt_modified: str,
    ) -> None:
        """INSERT a single span row."""
        ...

    async def update_task_summary(
        self,
        task_id: str,
        token_input: int,
        token_output: int,
        duration_ms: int,
        tool_calls: int,
        llm_calls: int,
        gmt_modified: str,
    ) -> None:
        """UPDATE task_details SET total_token_* = total_token_* + ?, gmt_modified = ? WHERE task_id = ?"""
        ...

    async def select_spans(self, task_id: str) -> list[dict]:
        """SELECT * FROM task_spans WHERE task_id = ? ORDER BY id"""
        ...

    # -- sessions ------------------------------------------------------------

    async def upsert_session(self, session_id: str, columns: list[str], values: list) -> None:
        """INSERT … ON CONFLICT(session_id) DO UPDATE SET … for the *sessions* table."""
        ...

    async def select_session(self, session_id: str) -> Optional[dict]:
        """SELECT * FROM sessions WHERE session_id = ?"""
        ...

    async def select_sessions_ordered(self, limit: int, offset: int, status: str = "") -> list[dict]:
        """SELECT * FROM sessions [WHERE status=?] ORDER BY gmt_create DESC LIMIT ? OFFSET ?"""
        ...

    # -- lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        """Release all resources (connections, pools)."""
        ...
