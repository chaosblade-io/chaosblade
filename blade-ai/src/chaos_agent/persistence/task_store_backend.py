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
    {"fault_spec", "target", "params", "verification",
     "recover_verification", "result",
     "baseline_data", "execution_artifacts",
     # R18 — postmortem dict (path/markdown/summary) JSON-serialised.
     "postmortem",
     # E18 — safety pre-check report dicts.
     "target_health_report", "feasibility_report",
     # Round-32 — the row-level liability ledger (agent/state.py
     # owned_experiment_uids / retired_experiment_uids, the two wings
     # behind ``live_liability_uids``). JSON arrays of experiment UIDs:
     # birth wing monotonic by append, death wing extended by proven
     # destroys and framework-side cleanup. Consumed by
     # ``may_carry_live_fault`` to materialise tasks.liability_live.
     "owned_experiment_uids", "retired_experiment_uids",
     # Round-32b — combo discriminator (agent/state.py
     # ``combo_native_issued``): True = a native mutation was issued
     # alongside a live experiment (the combo shape whose native half
     # survives experiment-wing balance). Persisted as JSON true/false so
     # the tri-state survives the round trip: NULL = never asserted
     # (legacy rows — keep the committed fallback), false = the execute
     # side asserted experiments-only at birth, true = combo. The upsert
     # latch (task_store.py) keeps true sticky and lets None flushes
     # never erase it — same hydration-gap discipline as the wings.
     "combo_native_issued"}
)

# tasks table — narrow, hot path (16 columns + tenant_id + workspace_id)
_TASK_COLUMNS: list[str] = [
    "id", "task_id", "task_state", "stage", "phase", "operation",
    "skill_name", "experiment_uid", "namespace", "target_name",
    "tenant_id",
    # Workspace-scoped isolation (platform mode): the home workspace of
    # the task — a durable ownership fact, unlike connection credentials.
    # Empty on local CLI / bare SDK entries; empty means UNFILTERED in
    # select_active_tasks, exactly like tenant_id's contract.
    "workspace_id",
    # Round-32 — materialised liability verdict: MAY this row still carry
    # a live fault on the cluster? Recomputed on every upsert /
    # update_task_state write from ``may_carry_live_fault`` (ledger first,
    # clearing-word proof, monotonic guard). This column — not the
    # task_state word — is what select_active_tasks keys on: words answer
    # display questions, the ledger answers the recovery question.
    "liability_live",
    "error", "finished_at", "duration_ms",
    "gmt_create", "gmt_modified",
]

# task_details table — wide, cold path
_DETAIL_COLUMNS: list[str] = [
    "id", "task_id", "fault_spec", "target", "params", "input",
    "safety_status", "safety_reason", "needs_confirm",
    "plan_summary", "kubeconfig", "kube_context",
    "verification", "recover_verification", "result",
    "failure_reason",
    # Round-32 — liability ledger wings (see _JSON_COLUMNS).
    "owned_experiment_uids", "retired_experiment_uids",
    # Round-32b — combo discriminator (see _JSON_COLUMNS): the DB-side
    # gate that lets a BALANCED wing settle an experiments-only row dead
    # (may_carry_live_fault branch A2) — combo rows keep the committed
    # fallback because their native half may still be owed.
    "combo_native_issued",
    "baseline_data", "inject_context", "skill_use_case",
    "injection_method", "execution_artifacts", "kubectl_exec_pod_name",
    "injection_start_time",
    # LLM model frozen at task finalize time (snapshot, NOT live config —
    # config can change mid-investigation; the session/task row is the only
    # place the at-run fact survives). Synced from the session JSON by
    # ``_finalize_session_store``.
    "model_name",
    # R18 — postmortem dict (JSON-serialised), see save_memory.
    "postmortem",
    # E18 — safety pre-check reports (JSON-serialised).
    "target_health_report", "feasibility_report",
    # total_token_cached is a SUBSET of total_token_input (prompt-cache
    # hits, not additive) — persisted so per-task hit rate survives a
    # restart and is queryable/aggregatable in SQL. Written absolutely by
    # tracer._persist_summary at finalize; see design D4.
    "total_token_input", "total_token_output", "total_token_cached",
    "total_llm_calls", "total_tool_calls", "total_duration_ms",
    "gmt_create", "gmt_modified",
]

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _decode_json_mapping(value: object) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            value = None
    return value if isinstance(value, dict) else {}


def _extract_index_fields(fields: dict) -> dict:
    """Extract *namespace* and *target_name* from task target fields into
    independent, indexable columns.

    This enables SQL-level filtering in ``query_active()`` without scanning
    and parsing JSON at the Python layer. ``target`` is the legacy detail
    shape; ``fault_spec`` is the canonical shape and is used as a fallback
    when no legacy target has been projected yet.
    """
    target = _decode_json_mapping(fields.get("target"))
    if target:
        fields.setdefault("namespace", target.get("namespace", ""))
        names = target.get("names", [])
        if names:
            fields.setdefault("target_name", names[0])
        return fields

    fault_spec = _decode_json_mapping(fields.get("fault_spec"))
    if fault_spec:
        fields.setdefault("namespace", fault_spec.get("namespace", ""))
        names = fault_spec.get("names", [])
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

    async def select_active_tasks(self, namespace: str = "", target_name: str = "", tenant_id: str = "", workspace_id: str = "") -> list[dict]:
        """SELECT * FROM tasks WHERE liability_live = 1 [AND namespace=?] [AND target_name=?] [AND tenant_id=?] [AND workspace_id=?] ORDER BY gmt_create DESC

        Round-32 root-cause fix: the recoverable set keys on the
        materialised ``tasks.liability_live`` verdict (ledger-first,
        rendered by ``may_carry_live_fault`` in task_store.py), not on
        the task_state word. The word-guessing predicate
        (task_state IN TASK_STATE_ACTIVE_VALUES …) answered "which
        lifecycle words sound unfinished" — every new word
        (recovering, failed-with-experiment, partial_recovered) then
        needed a fresh human re-derivation, and the two that lost
        (round-32 K1/K2) permanently blinded recovery to live faults.
        The ledger column absorbs the injection_start_time / target /
        fault_spec joins of the old predicate: "may still owe a
        destroy" is computed once, at write time, from evidence.
        """
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
