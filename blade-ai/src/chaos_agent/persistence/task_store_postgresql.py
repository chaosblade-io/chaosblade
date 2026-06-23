"""PostgreSQL async backend for TaskStore, powered by *asyncpg*.

Implements the ``StorageBackend`` protocol with an ``asyncpg.Pool`` and
the 3-table DDL (``tasks``, ``task_details``, ``task_spans``) following
MySQL design conventions adapted for PostgreSQL:

- ``id BIGSERIAL PRIMARY KEY`` on every table.
- ``gmt_create`` / ``gmt_modified`` use ``TIMESTAMPTZ``.
- Unique indexes: ``uk_{table}_{field}``; normal indexes: ``idx_{table}_{field}``.
- ``task_id`` is a UNIQUE INDEX (not PK); upserts use
  ``INSERT … ON CONFLICT(task_id) DO UPDATE SET …``.
- Positional parameters: ``$1``, ``$2``, … (asyncpg native).

``asyncpg`` is lazy-imported at instantiation time so that the core
package does not require it when using the SQLite backend.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_TASKS_DDL = """\
CREATE TABLE IF NOT EXISTS tasks (
    id              BIGSERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL,
    task_state      TEXT NOT NULL DEFAULT 'injecting',
    stage           TEXT NOT NULL DEFAULT 'injection',
    phase           TEXT NOT NULL DEFAULT 'planning',
    operation       TEXT NOT NULL DEFAULT 'inject',
    skill_name      TEXT,
    blade_uid       TEXT,
    namespace       TEXT,
    target_name     TEXT,
    error           TEXT,
    finished_at     TEXT,
    duration_ms     INTEGER DEFAULT 0,
    gmt_create      TIMESTAMPTZ,
    gmt_modified    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_tasks_task_id ON tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_state ON tasks(task_state);
CREATE INDEX IF NOT EXISTS idx_tasks_namespace ON tasks(namespace);
CREATE INDEX IF NOT EXISTS idx_tasks_gmt_create ON tasks(gmt_create);
"""

_DETAILS_DDL = """\
CREATE TABLE IF NOT EXISTS task_details (
    id                  BIGSERIAL PRIMARY KEY,
    task_id             TEXT NOT NULL,
    fault_spec          TEXT,
    target              TEXT,
    params              TEXT,
    input               TEXT,
    safety_status       TEXT NOT NULL DEFAULT 'pending',
    safety_reason       TEXT,
    needs_confirm       INTEGER NOT NULL DEFAULT 0,
    plan_summary        TEXT DEFAULT '',
    kubeconfig          TEXT,
    kube_context        TEXT,
    verification        TEXT,
    recover_verification TEXT,
    result              TEXT,
    failure_reason      TEXT,
    postmortem          TEXT,
    target_health_report TEXT,
    feasibility_report  TEXT,
    total_token_input   INTEGER NOT NULL DEFAULT 0,
    total_token_output  INTEGER NOT NULL DEFAULT 0,
    total_llm_calls     INTEGER NOT NULL DEFAULT 0,
    total_tool_calls    INTEGER NOT NULL DEFAULT 0,
    total_duration_ms   INTEGER NOT NULL DEFAULT 0,
    gmt_create          TIMESTAMPTZ,
    gmt_modified        TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_task_details_task_id ON task_details(task_id);
"""

_SPANS_DDL = """\
CREATE TABLE IF NOT EXISTS task_spans (
    id              BIGSERIAL PRIMARY KEY,
    task_id         TEXT NOT NULL,
    node_name       TEXT NOT NULL,
    start_time      DOUBLE PRECISION NOT NULL,
    end_time        DOUBLE PRECISION NOT NULL,
    duration_ms     DOUBLE PRECISION NOT NULL,
    token_input     INTEGER NOT NULL DEFAULT 0,
    token_output    INTEGER NOT NULL DEFAULT 0,
    tool_calls      TEXT DEFAULT '[]',
    error           TEXT,
    gmt_create      TIMESTAMPTZ,
    gmt_modified    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_spans_task_id ON task_spans(task_id);
CREATE INDEX IF NOT EXISTS idx_task_spans_gmt_create ON task_spans(gmt_create);
"""

_SESSIONS_DDL = """\
CREATE TABLE IF NOT EXISTS sessions (
    id              BIGSERIAL PRIMARY KEY,
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    cluster_name    TEXT DEFAULT '',
    namespace       TEXT DEFAULT '',
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    gmt_create      TIMESTAMPTZ,
    gmt_modified    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_gmt_create ON sessions(gmt_create);
"""

_SCHEMA_DDL = _TASKS_DDL + _DETAILS_DDL + _SPANS_DDL + _SESSIONS_DDL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_upsert_sql(table: str, columns: list[str], conflict_col: str = "task_id") -> tuple[str, int]:
    """Build an ``INSERT … ON CONFLICT(<col>) DO UPDATE SET`` statement.

    Returns ``(sql, param_count)`` where *param_count* is the number of
    positional ``$N`` placeholders used (for validation purposes).

    asyncpg uses ``$1``, ``$2``, … positional parameters.
    """
    col_names = ", ".join(columns)
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    update_clause = ", ".join(
        f"{c}=EXCLUDED.{c}" for c in columns if c != conflict_col
    )
    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_col}) DO UPDATE SET {update_clause}"
    )
    return sql, len(columns)


def _record_to_dict(record) -> dict:
    """Convert an asyncpg Record to a plain dict."""
    return dict(record)


# ---------------------------------------------------------------------------
# PostgreSQLBackend
# ---------------------------------------------------------------------------

class PostgreSQLBackend:
    """Async PostgreSQL backend using an *asyncpg* connection pool."""

    def __init__(self, pool) -> None:  # pool: asyncpg.Pool
        self._pool = pool

    # -- factory -------------------------------------------------------------

    @classmethod
    async def create(cls, dsn: str) -> "PostgreSQLBackend":
        """Factory: create a ``PostgreSQLBackend`` from a DSN string.

        ``asyncpg`` is lazy-imported here so the module can be loaded
        even when asyncpg is not installed (e.g. SQLite-only setups).
        """
        import asyncpg  # lazy import

        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5)
        backend = cls(pool)
        try:
            await backend.ensure_schema()
        except Exception:
            # Pool was created but schema init failed — close to avoid leak
            await backend.close()
            raise
        return backend

    # -- schema --------------------------------------------------------------

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            # asyncpg does not support executescript; run statements individually
            # Split by semicolons, filter empty lines
            statements = [s.strip() for s in _SCHEMA_DDL.split(";") if s.strip()]
            for stmt in statements:
                await conn.execute(stmt)
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN fault_spec TEXT")
            except Exception:
                pass
            # Migration: add failure_reason column if not exists
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN failure_reason TEXT")
            except Exception:
                pass  # Column already exists
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN baseline_data TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN inject_context TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN skill_use_case TEXT")
            except Exception:
                pass
            try:
                # R18 — postmortem JSON column (path/markdown/summary).
                await conn.execute("ALTER TABLE task_details ADD COLUMN postmortem TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN target_health_report TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN feasibility_report TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN injection_method TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN kubectl_exec_pod_name TEXT")
            except Exception:
                pass

    # -- tasks (narrow, hot) -------------------------------------------------

    async def select_task(self, task_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE task_id = $1", task_id
            )
            return _record_to_dict(row) if row else None

    async def upsert_task(self, task_id: str, columns: list[str], values: list) -> None:
        sql, _ = _build_upsert_sql("tasks", columns)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *values)

    async def select_tasks_ordered(self, limit: int, offset: int) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tasks ORDER BY gmt_create DESC LIMIT $1 OFFSET $2",
                limit, offset,
            )
            return [_record_to_dict(r) for r in rows]

    async def select_tasks_by_state(self, task_state: str, limit: int, offset: int) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM tasks WHERE task_state = $1 ORDER BY gmt_create DESC LIMIT $2 OFFSET $3",
                task_state, limit, offset,
            )
            return [_record_to_dict(r) for r in rows]

    async def select_active_tasks(self, namespace: str = "", target_name: str = "") -> list[dict]:
        conditions = ["task_state IN ('injecting', 'injected')"]
        params: list = []
        idx = 0
        if namespace:
            idx += 1
            conditions.append(f"namespace = ${idx}")
            params.append(namespace)
        if target_name:
            idx += 1
            conditions.append(f"target_name = ${idx}")
            params.append(target_name)
        where = " AND ".join(conditions)
        sql = f"SELECT * FROM tasks WHERE {where} ORDER BY gmt_create DESC"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
            return [_record_to_dict(r) for r in rows]

    async def delete_task(self, task_id: str) -> bool:
        async with self._pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM tasks WHERE task_id = $1", task_id
            )
            # asyncpg returns "DELETE N"
            return result.endswith("1")

    async def count_tasks(self, task_state: str = None) -> int:
        async with self._pool.acquire() as conn:
            if task_state:
                row = await conn.fetchrow(
                    "SELECT COUNT(*) FROM tasks WHERE task_state = $1", task_state
                )
            else:
                row = await conn.fetchrow("SELECT COUNT(*) FROM tasks")
            return row[0]

    # -- task_details (wide, cold) -------------------------------------------

    async def select_details(self, task_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM task_details WHERE task_id = $1", task_id
            )
            return _record_to_dict(row) if row else None

    async def upsert_details(self, task_id: str, columns: list[str], values: list) -> None:
        sql, _ = _build_upsert_sql("task_details", columns)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *values)

    async def select_details_batch(self, task_ids: list[str]) -> list[dict]:
        if not task_ids:
            return []
        # asyncpg doesn't support IN with a list directly via $1;
        # use UNNEST or build positional params
        placeholders = ", ".join(f"${i}" for i in range(1, len(task_ids) + 1))
        sql = f"SELECT * FROM task_details WHERE task_id IN ({placeholders})"
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(sql, *task_ids)
            return [_record_to_dict(r) for r in rows]

    async def delete_details(self, task_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_details WHERE task_id = $1", task_id
            )

    # -- task_spans ----------------------------------------------------------

    async def delete_spans_by_task(self, task_id: str) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM task_spans WHERE task_id = $1", task_id
            )

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
        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_spans "
                "(task_id, node_name, start_time, end_time, duration_ms, "
                " token_input, token_output, tool_calls, error, gmt_create, gmt_modified) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                task_id, node_name, start_time, end_time, duration_ms,
                token_input, token_output, tool_calls_json, error,
                gmt_create, gmt_modified,
            )

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
        async with self._pool.acquire() as conn:
            await conn.execute(
                "UPDATE task_details SET "
                "  total_token_input = total_token_input + $1,"
                "  total_token_output = total_token_output + $2,"
                "  total_duration_ms = total_duration_ms + $3,"
                "  total_tool_calls = total_tool_calls + $4,"
                "  total_llm_calls = total_llm_calls + $5,"
                "  gmt_modified = $6 "
                "WHERE task_id = $7",
                token_input, token_output, duration_ms,
                tool_calls, llm_calls, gmt_modified, task_id,
            )

    async def select_spans(self, task_id: str) -> list[dict]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM task_spans WHERE task_id = $1 ORDER BY id",
                task_id,
            )
            return [_record_to_dict(r) for r in rows]

    # -- sessions ------------------------------------------------------------

    async def upsert_session(self, session_id: str, columns: list[str], values: list) -> None:
        sql, _ = _build_upsert_sql("sessions", columns, conflict_col="session_id")
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *values)

    async def select_session(self, session_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM sessions WHERE session_id = $1", session_id
            )
            return _record_to_dict(row) if row else None

    async def select_sessions_ordered(self, limit: int, offset: int, status: str = "") -> list[dict]:
        async with self._pool.acquire() as conn:
            if status:
                rows = await conn.fetch(
                    "SELECT * FROM sessions WHERE status = $1 ORDER BY gmt_create DESC LIMIT $2 OFFSET $3",
                    status, limit, offset,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM sessions ORDER BY gmt_create DESC LIMIT $1 OFFSET $2",
                    limit, offset,
                )
            return [_record_to_dict(r) for r in rows]

    # -- lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
