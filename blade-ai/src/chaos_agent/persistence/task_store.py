"""Async persistent task store with pluggable storage backend.

The store holds task lifecycle state and execution metrics across three
normalised tables:

- **tasks** – narrow, hot-path row per ``task_id`` (state, stage, phase …)
- **task_details** – wide, cold-path row (target JSON, params, metrics …)
- **task_spans** – one row per graph-node execution (timing / tokens / tools)

All public methods are ``async``; the actual I/O is delegated to a
``StorageBackend`` protocol implementation (SQLite via *aiosqlite* or
PostgreSQL via *asyncpg*).

Usage::

    from chaos_agent.persistence.task_store import get_task_store

    store = await get_task_store()
    await store.upsert("task-1", skill_name="pod-kill", blade_uid="abc")
    data = await store.get("task-1")
    metrics = await store.get_metric("task-1")
"""

import atexit
import json
import logging
from typing import Optional

from chaos_agent.config.settings import settings
from chaos_agent.persistence.task_store_backend import (
    StorageBackend,
    _DETAIL_COLUMNS,
    _JSON_COLUMNS,
    _TASK_COLUMNS,
    _extract_index_fields,
    _set_timestamps,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TaskStore — business logic layer
# ---------------------------------------------------------------------------

class TaskStore:
    """Async persistent store for task state and execution metrics.

    Delegates all SQL I/O to a ``StorageBackend`` implementation.
    """

    def __init__(self, backend: StorageBackend) -> None:
        self._backend = backend

    # -- upsert --------------------------------------------------------------

    async def upsert(self, task_id: str, **fields: object) -> None:
        """Insert a new task or update specific columns on an existing row.

        Automatically:
        1. Reads the current ``tasks`` row (if any) and merges with *fields*.
        2. Extracts ``namespace`` / ``target_name`` from the ``target`` JSON.
        3. Infers ``task_state`` / ``stage`` / ``phase``.
        4. Sets ``gmt_create`` (preserved on update) / ``gmt_modified``.
        5. Splits merged fields into tasks + task_details and writes both.
        """
        if not task_id:
            return

        # 1. Read current tasks row
        row = await self._backend.select_task(task_id)

        # 2. Merge: current DB values + incoming fields
        merged: dict = dict(row) if row else {"task_id": task_id}
        for k, v in fields.items():
            if k in _JSON_COLUMNS and v is not None:
                v = json.dumps(v, ensure_ascii=False, default=str)
            merged[k] = v

        # 3. Extract index fields (namespace, target_name) from target JSON
        merged = _extract_index_fields(merged)

        # 4. Infer task_state / stage / phase
        merged.update(self._infer_fields(merged))
        merged["task_id"] = task_id

        # 5. Set gmt_create / gmt_modified
        _set_timestamps(merged, row)

        # 6. Split fields into two tables (exclude auto-increment `id`)
        task_cols = [c for c in _TASK_COLUMNS if c in merged and c != "id"]
        task_vals = [merged[c] for c in task_cols]

        detail_cols = [c for c in _DETAIL_COLUMNS if c in merged and c != "id"]
        detail_vals = [merged[c] for c in detail_cols]

        # 7. Write both tables (ON CONFLICT(task_id) DO UPDATE SET)
        await self._backend.upsert_task(task_id, task_cols, task_vals)
        if detail_cols:
            await self._backend.upsert_details(task_id, detail_cols, detail_vals)

    # -- read ----------------------------------------------------------------

    async def get(self, task_id: str) -> Optional[dict]:
        """Return the full task data (tasks + task_details merged).

        Returns ``None`` if not found.
        """
        task_row = await self._backend.select_task(task_id)
        if task_row is None:
            return None
        detail_row = await self._backend.select_details(task_id)
        # Merge: detail_columns first, then task_columns override (e.g. task_id)
        merged = {**(detail_row or {}), **task_row}
        return self._row_to_dict(merged)

    async def list(self, task_state: str = None, limit: int = 50, offset: int = 0) -> list[dict]:
        """Return tasks from the narrow table, ordered by ``gmt_create`` DESC.

        No large JSON fields are included (only the hot-path columns).
        """
        if task_state:
            rows = await self._backend.select_tasks_by_state(task_state, limit, offset)
        else:
            rows = await self._backend.select_tasks_ordered(limit, offset)
        return [self._row_to_dict(r) for r in rows]

    async def query_active(self, namespace: str = "", target_name: str = "") -> list[dict]:
        """Return active tasks (``injecting`` / ``injected``) as
        ExperimentStore-compatible dicts.

        Filtering is done at the SQL level using the ``namespace`` /
        ``target_name`` indexed columns (no Python-side JSON filtering).
        """
        rows = await self._backend.select_active_tasks(namespace, target_name)
        results = []
        for d in (self._row_to_dict(r) for r in rows):
            # Need target JSON from task_details for compatibility
            detail = await self._backend.select_details(d["task_id"])
            target = (detail or {}).get("target") or {}
            if isinstance(target, str):
                try:
                    target = json.loads(target)
                except (json.JSONDecodeError, TypeError):
                    target = {}
            results.append({
                "task_id": d["task_id"],
                "operation": d.get("operation", "inject"),
                "skill": d.get("skill_name", ""),
                "target": target,
                "params": (detail or {}).get("params") or {},
                "blade_uid": d.get("blade_uid", ""),
                "status": "success" if not d.get("error") else "failed",
                "error": d.get("error"),
            })
        return results

    async def delete(self, task_id: str) -> bool:
        """Delete a task and its associated details + spans.

        Deletion order: spans → details → tasks (code-maintained consistency).
        """
        await self._backend.delete_spans_by_task(task_id)
        await self._backend.delete_details(task_id)
        return await self._backend.delete_task(task_id)

    async def count(self, task_state: str = None) -> int:
        """Count tasks, optionally filtered by state."""
        return await self._backend.count_tasks(task_state)

    # -- span methods --------------------------------------------------------

    async def append_span(
        self,
        task_id: str,
        node_name: str,
        start_time: float,
        end_time: float,
        duration_ms: float,
        token_input: int = 0,
        token_output: int = 0,
        tool_calls: list[str] | None = None,
        error: str | None = None,
    ) -> None:
        """Append a span row and update task_details summary fields."""
        now = now_iso()
        await self._backend.insert_span(
            task_id, node_name, start_time, end_time, duration_ms,
            token_input, token_output,
            json.dumps(tool_calls or [], ensure_ascii=False),
            error, now, now,  # gmt_create, gmt_modified
        )
        await self._backend.update_task_summary(
            task_id, token_input, token_output, int(duration_ms),
            len(tool_calls) if tool_calls else 0,
            1 if token_input > 0 else 0,  # heuristic: tokens consumed → LLM call
            now,  # gmt_modified
        )

    async def get_spans(self, task_id: str) -> list[dict]:
        """Return all spans for a task, ordered by ``id``."""
        rows = await self._backend.select_spans(task_id)
        result = []
        for d in rows:
            d["tool_calls"] = json.loads(d.get("tool_calls", "[]"))
            result.append(d)
        return result

    async def get_summary(self, task_id: str) -> Optional[dict]:
        """Return summary metrics from the ``task_details`` row."""
        detail = await self._backend.select_details(task_id)
        if detail is None:
            return None
        return {k: detail[k] for k in (
            "total_token_input", "total_token_output",
            "total_llm_calls", "total_tool_calls", "total_duration_ms",
        ) if k in detail}

    # -- metric methods ------------------------------------------------------

    async def get_metric(self, task_id: str) -> Optional[dict]:
        """Return combined metric data (status + spans + summary) for a task.

        This is the primary method for the ``metric --task-id`` command.
        """
        task = await self.get(task_id)
        if task is None:
            return None

        spans = await self.get_spans(task_id)
        summary = await self.get_summary(task_id) or {
            "total_token_input": 0,
            "total_token_output": 0,
            "total_llm_calls": 0,
            "total_tool_calls": 0,
            "total_duration_ms": 0,
        }

        fault_type = self._compute_fault_type(task)
        from chaos_agent.agent.state import infer_status
        task_state = task.get("task_state", "injecting")
        operation = task.get("operation", "")
        stage = task.get("stage", "injection")

        # Compute duration_ms from timestamps if not already set
        duration_ms = task.get("duration_ms", 0)
        if not duration_ms:
            gmt_create = task.get("gmt_create", "")
            finished_at = task.get("finished_at", "")
            if gmt_create and finished_at:
                try:
                    from chaos_agent.utils.time import now_iso, parse_iso_timestamp
                    ct = parse_iso_timestamp(gmt_create)
                    ft = parse_iso_timestamp(finished_at)
                    duration_ms = int((ft - ct).total_seconds() * 1000)
                except (ValueError, TypeError):
                    pass

        # Merge failure_reason into error
        merged_error = task.get("failure_reason") or task.get("error") or ""

        return {
            "task_id": task_id,
            # See ``get_all_metrics`` for why both ``task_state`` and
            # ``status`` are exposed (raw lifecycle vs derived rollup).
            "task_state": task_state,
            "operation": operation,
            "stage": stage,
            "status": infer_status(stage, task_state, operation),
            "phase": task.get("phase", "planning"),
            "fault_type": fault_type,
            "skill_name": task.get("skill_name", ""),
            "target": task.get("target"),
            "params": task.get("params"),
            "blade_uid": task.get("blade_uid", ""),
            "safety_status": task.get("safety_status", "pending"),
            "safety_reason": task.get("safety_reason"),
            "needs_confirm": bool(task.get("needs_confirm", 0)),
            "verification": task.get("verification"),
            "recover_verification": task.get("recover_verification"),
            "plan_summary": task.get("plan_summary", ""),
            "error": merged_error,
            "gmt_create": task.get("gmt_create", ""),
            "gmt_modified": task.get("gmt_modified", ""),
            "finished_at": task.get("finished_at", ""),
            "duration_ms": duration_ms,
            "spans": spans,
            "summary": summary,
        }

    async def get_all_metrics(self, task_state: str = None, limit: int = 200) -> dict:
        """Return metric data for all tasks.

        Uses a single batch read of ``task_details`` to avoid N+1 queries.
        This is the primary method for the ``metric`` (no task-id) command.
        """
        tasks = await self.list(task_state=task_state, limit=limit)
        if not tasks:
            return {"total": 0, "tasks": []}

        # Batch-read details (eliminates N+1)
        task_ids = [t["task_id"] for t in tasks]
        details_rows = await self._backend.select_details_batch(task_ids)
        details_map = {r["task_id"]: r for r in details_rows}

        from chaos_agent.agent.state import infer_status
        task_list = []
        for task in tasks:
            detail = details_map.get(task["task_id"], {})
            summary = {k: detail.get(k, 0) for k in (
                "total_token_input", "total_token_output",
                "total_llm_calls", "total_tool_calls", "total_duration_ms",
            )}
            _ts = task.get("task_state", "injecting")
            _op = task.get("operation", "")
            _stage = task.get("stage", "injection")

            # Fix: target is stored in task_details, not tasks table
            target_raw = detail.get("target")
            if isinstance(target_raw, str) and target_raw:
                try:
                    target = json.loads(target_raw)
                except (json.JSONDecodeError, TypeError):
                    target = None
            else:
                target = target_raw

            # Merge failure_reason into error
            merged_error = detail.get("failure_reason") or task.get("error") or ""

            task_list.append({
                "task_id": task["task_id"],
                # ``task_state`` is the raw lifecycle field
                # (injecting / injected / recovering / recovered /
                # partial_recovered / failed / rejected / completed)
                # — clients that need to gate "is this still in
                # flight" reach for it directly. ``status`` below is
                # the derived success/failed/in_progress/pending
                # rollup; both are exposed because they answer
                # different questions and the prior rollup-only shape
                # silently broke the TS TUI's PendingTasksCard, which
                # filters on ``task_state in {"injecting","injected"}``
                # but was reading ``undefined`` on every row.
                "task_state": _ts,
                "operation": _op,
                "stage": _stage,
                "status": infer_status(_stage, _ts, _op),
                "phase": task.get("phase", "planning"),
                "skill_name": task.get("skill_name", ""),
                "blade_uid": task.get("blade_uid", ""),
                "target": target,
                "gmt_create": task.get("gmt_create", ""),
                "gmt_modified": task.get("gmt_modified", ""),
                "finished_at": task.get("finished_at", ""),
                "error": merged_error,
                "summary": summary,
            })
        return {
            "total": len(task_list),
            "tasks": task_list,
        }

    # -- internal helpers (pure Python, sync) --------------------------------

    @staticmethod
    def _row_to_dict(row: dict) -> dict:
        """Deserialize JSON columns in a row dict."""
        d = dict(row)
        for col in _JSON_COLUMNS:
            val = d.get(col)
            if isinstance(val, str) and val:
                try:
                    d[col] = json.loads(val)
                except json.JSONDecodeError:
                    pass
        return d

    @staticmethod
    def _infer_fields(merged: dict) -> dict:
        """Run infer_task_state / infer_stage / infer_phase on merged values.

        Returns a dict with ``task_state``, ``stage``, ``phase``.
        Inference is based on the full merged data (DB row + new fields),
        so the result always reflects the current state of all fields.

        Additionally detects ``waiting_input`` state: when a task is paused
        at an interrupt point (confirmation_gate or ask_human), waiting for
        user input. This is used by TUI crash recovery to discover tasks
        that need to be resumed.
        """
        try:
            from chaos_agent.agent.state import infer_task_state, infer_stage, infer_phase

            values = dict(merged)
            # Deserialize JSON fields for inference
            for col in _JSON_COLUMNS:
                val = values.get(col)
                if isinstance(val, str) and val:
                    try:
                        values[col] = json.loads(val)
                    except json.JSONDecodeError:
                        pass
            # Convert DB column names to AgentState names where they differ
            if "needs_confirm" in values:
                values["needs_confirmation"] = values["needs_confirm"]

            task_state = infer_task_state(values)
            stage = infer_stage(values)
            phase = infer_phase(values)

            # Non-injection intents (chat, recover) are completed immediately.
            # Older sessions may still carry "query"/"explore" as confirmed_intent;
            # treat them the same way to stay backward-compatible with persisted state.
            if values.get("confirmed_intent") in ("chat", "recover", "query", "explore"):
                task_state = "completed"
                # DB columns stage/phase are NOT NULL — use descriptive defaults
                # instead of None which would violate the constraint
                if stage is None:
                    stage = "injection"  # DB default; non-injection has no meaningful stage
                if phase is None:
                    phase = "completed"  # More descriptive than "planning" for a completed task

            # Detect waiting_input: task is paused at an interrupt point
            # (needs confirmation but no blade_uid yet, or interaction_mode=tui
            # with confirmed_intent still None)
            if task_state == "injecting" and values.get("needs_confirmation") and not values.get("blade_uid"):
                task_state = "waiting_input"
            elif values.get("interaction_mode") == "tui" and not values.get("confirmed_intent") and not values.get("blade_uid"):
                task_state = "waiting_input"

            return {
                "task_state": task_state,
                "stage": stage,
                "phase": phase,
            }
        except Exception as e:
            logger.warning(f"TaskStore infer failed: {e}")
            return {}

    @staticmethod
    def _compute_fault_type(task: dict) -> str:
        """Infer fault_type from params."""
        params = task.get("params") or {}
        if params:
            scope = params.get("scope", "")
            action = params.get("action", "")
            target_action = params.get("target", "")
            if scope and target_action and action:
                return f"{scope}-{target_action}-{action}"
        return task.get("skill_name", "")


# ---------------------------------------------------------------------------
# Singleton / global instance
# ---------------------------------------------------------------------------

_store: Optional[TaskStore] = None


async def get_task_store() -> TaskStore:
    """Get or create the global async TaskStore instance.

    Backend selection is driven by ``settings.tasks_db_backend``
    (``"sqlite"`` or ``"postgresql"``).
    """
    global _store
    if _store is not None:
        return _store

    backend_type = getattr(settings, "tasks_db_backend", "sqlite")

    backend: StorageBackend
    try:
        if backend_type == "postgresql":
            from chaos_agent.persistence.task_store_postgresql import PostgreSQLBackend
            dsn = getattr(settings, "tasks_pg_dsn", "")
            if not dsn:
                raise ValueError("tasks_pg_dsn must be set when tasks_db_backend=postgresql")
            backend = await PostgreSQLBackend.create(dsn)
        else:
            from chaos_agent.persistence.task_store_sqlite import SQLiteBackend
            backend = await SQLiteBackend.create(db_path=settings.resolved_tasks_db_path)
    except Exception:
        # Backend created a connection but schema init failed — close to avoid leak
        try:
            await backend.close()  # type: ignore[possibly-undefined]
        except Exception:
            pass
        raise

    _store = TaskStore(backend=backend)
    return _store


async def reset_task_store() -> None:
    """Close and reset the global TaskStore instance."""
    global _store
    if _store is not None:
        try:
            await _store._backend.close()
        except Exception:
            pass
    _store = None


def _sync_close_store() -> None:
    """atexit callback: synchronously close the TaskStore.

    For aiosqlite: the underlying ``sqlite3.Connection`` (``_conn._conn``)
    can be closed synchronously and safely.  Calling ``_conn.close()``
    directly would produce an unawaited-coroutine warning because
    aiosqlite's ``Connection.close()`` is async.

    For asyncpg: ``pool.close()`` is a coroutine that atexit cannot await,
    but asyncpg's ``__del__`` handles cleanup.
    """
    global _store
    if _store is not None and _store._backend is not None:
        try:
            # SQLite: close the underlying sqlite3 connection synchronously
            if hasattr(_store._backend, "_conn") and _store._backend._conn is not None:
                raw_conn = getattr(_store._backend._conn, "_conn", None)
                if raw_conn is not None:
                    raw_conn.close()  # sqlite3.Connection.close() is sync
        except Exception:
            pass
    _store = None


atexit.register(_sync_close_store)
