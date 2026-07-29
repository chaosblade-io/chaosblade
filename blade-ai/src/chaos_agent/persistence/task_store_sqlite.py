"""SQLite async backend for TaskStore, powered by *aiosqlite*.

Implements the ``StorageBackend`` protocol with a single persistent
``aiosqlite.Connection`` (lazy-initialised on first use) and the 3-table
DDL (``tasks``, ``task_details``, ``task_spans``) following MySQL design
conventions:

- Every table has ``id`` (INTEGER PRIMARY KEY AUTOINCREMENT),
  ``gmt_create``, ``gmt_modified``.
- Unique indexes: ``uk_{table}_{field}``; normal indexes: ``idx_{table}_{field}``.
- ``task_id`` is a UNIQUE INDEX (not PK); upserts use
  ``INSERT … ON CONFLICT(task_id) DO UPDATE SET …``.
"""

from pathlib import Path
from typing import Optional

import aiosqlite

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

_TASKS_DDL = """\
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    task_state      TEXT NOT NULL DEFAULT 'injecting',
    stage           TEXT NOT NULL DEFAULT 'injection',
    phase           TEXT NOT NULL DEFAULT 'planning',
    operation       TEXT NOT NULL DEFAULT 'inject',
    skill_name      TEXT,
    blade_uid       TEXT,
    namespace       TEXT,
    target_name     TEXT,
    tenant_id       TEXT DEFAULT '',
    error           TEXT,
    finished_at     TEXT,
    duration_ms     INTEGER DEFAULT 0,
    gmt_create      TEXT,
    gmt_modified    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_tasks_task_id ON tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_state ON tasks(task_state);
CREATE INDEX IF NOT EXISTS idx_tasks_namespace ON tasks(namespace);
CREATE INDEX IF NOT EXISTS idx_tasks_gmt_create ON tasks(gmt_create);
"""

_DETAILS_DDL = """\
CREATE TABLE IF NOT EXISTS task_details (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
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
    execution_artifacts TEXT,
    total_token_input   INTEGER NOT NULL DEFAULT 0,
    total_token_output  INTEGER NOT NULL DEFAULT 0,
    total_llm_calls     INTEGER NOT NULL DEFAULT 0,
    total_tool_calls    INTEGER NOT NULL DEFAULT 0,
    total_duration_ms   INTEGER NOT NULL DEFAULT 0,
    gmt_create          TEXT,
    gmt_modified        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_task_details_task_id ON task_details(task_id);
"""

_SPANS_DDL = """\
CREATE TABLE IF NOT EXISTS task_spans (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL,
    node_name       TEXT NOT NULL,
    start_time      REAL NOT NULL,
    end_time        REAL NOT NULL,
    duration_ms     REAL NOT NULL,
    token_input     INTEGER NOT NULL DEFAULT 0,
    token_output    INTEGER NOT NULL DEFAULT 0,
    tool_calls      TEXT DEFAULT '[]',
    error           TEXT,
    gmt_create      TEXT,
    gmt_modified    TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_spans_task_id ON task_spans(task_id);
CREATE INDEX IF NOT EXISTS idx_task_spans_gmt_create ON task_spans(gmt_create);
"""

_SESSIONS_DDL = """\
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    cluster_name    TEXT DEFAULT '',
    namespace       TEXT DEFAULT '',
    started_at      TEXT,
    finished_at     TEXT,
    gmt_create      TEXT,
    gmt_modified    TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_sessions_session_id ON sessions(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_sessions_gmt_create ON sessions(gmt_create);
"""

_SCHEMA_DDL = _TASKS_DDL + _DETAILS_DDL + _SPANS_DDL + _SESSIONS_DDL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_upsert_sql(table: str, columns: list[str], conflict_col: str = "task_id") -> str:
    """Build an ``INSERT … ON CONFLICT(<col>) DO UPDATE SET`` statement.

    The ``id`` column (auto-increment PK) is excluded from column lists at
    the call-site, so it never appears here.
    """
    col_names = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    update_clause = ", ".join(f"{c}=EXCLUDED.{c}" for c in columns if c != conflict_col)
    return (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_col}) DO UPDATE SET {update_clause}"
    )


def _row_to_dict(row: aiosqlite.Row) -> dict:  # type: ignore[name-defined]
    """Convert an aiosqlite.Row to a plain dict."""
    return dict(row)


# ---------------------------------------------------------------------------
# SQLiteBackend
# ---------------------------------------------------------------------------

class SQLiteBackend:
    """Async SQLite backend using a single persistent *aiosqlite* connection."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[aiosqlite.Connection] = None
        self._schema_initialized = False

    # -- factory -------------------------------------------------------------

    @classmethod
    async def create(cls, db_path: Path) -> "SQLiteBackend":
        """Factory: create a ``SQLiteBackend`` and initialise the schema."""
        backend = cls(db_path)
        try:
            await backend._get_conn()  # triggers schema init
        except Exception:
            # Connection was opened but schema init failed — close to avoid leak
            await backend.close()
            raise
        return backend

    # -- connection management -----------------------------------------------

    async def _get_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = await aiosqlite.connect(str(self.db_path))
            await self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.row_factory = aiosqlite.Row
            # First connection → ensure schema (bypasses _get_conn to avoid recursion)
            await self._ensure_schema_on_conn(self._conn)
            self._schema_initialized = True
        if not self._schema_initialized:
            await self._ensure_schema_on_conn(self._conn)
            self._schema_initialized = True
        return self._conn

    # -- schema --------------------------------------------------------------

    async def _ensure_schema_on_conn(self, conn: aiosqlite.Connection) -> None:
        """Execute DDL on a given connection (avoids recursion with _get_conn)."""
        await conn.executescript(_SCHEMA_DDL)
        # Migrations: add columns introduced after initial schema
        try:
            await conn.execute("ALTER TABLE task_details ADD COLUMN fault_spec TEXT")
        except Exception:
            pass
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
            # R18 — postmortem dict (JSON-serialised: path/markdown/summary).
            # Stored so future SQL queries can aggregate / filter by
            # postmortem content without having to walk
            # ~/.blade-ai/postmortems/ on disk.
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
            await conn.execute("ALTER TABLE task_details ADD COLUMN execution_artifacts TEXT")
        except Exception:
            pass
        try:
            await conn.execute("ALTER TABLE task_details ADD COLUMN kubectl_exec_pod_name TEXT")
        except Exception:
            pass
        try:
            # ``injection_start_time`` — the only field written exactly when an
            # injection command is *issued* (execute_loop / direct_execute set
            # it write-once and never clear it, unlike ``injection_method``).
            # ``select_active_tasks`` needs it to tell "confirmed but never
            # executed" apart from "really injected".
            await conn.execute("ALTER TABLE task_details ADD COLUMN injection_start_time TEXT")
            # One-shot backfill, deliberately INSIDE this try: it runs only on
            # the migration that adds the column (on later startups the ALTER
            # raises and we skip).
            #
            # ❗ 不要把它拆成独立的 try / 让它每次启动都跑：回填条件
            # （injection_start_time IS NULL 且有意图）恰好也匹配「方案已确认
            # 但命令从未发出」的**新行**，每次启动重跑会给它们盖上时间戳，
            # 永久废掉 select_active_tasks 的"已发出"判据。一次性回填失败最多
            # 让存量行暂时不可恢复（一次性窗口），而重复回填是永久性失效。
            #
            # 原子性：本方法所有语句共享末尾的单次 ``conn.commit()``，因此
            # ALTER 与 UPDATE 要么同时生效、要么都不生效 —— 不存在"列加上了
            # 但回填没跑"的中间态。
            #
            # Pre-existing rows have no recorded issue time, so assume they were
            # issued and stamp ``tasks.gmt_create`` — that keeps their current
            # recoverable status. Excluding them instead would hide real
            # in-flight injections, i.e. re-create the "注入了却恢复不了" bug
            # this column exists to avoid.
            await conn.execute(
                "UPDATE task_details SET injection_start_time = COALESCE("
                "  (SELECT t.gmt_create FROM tasks t WHERE t.task_id = task_details.task_id),"
                "  gmt_create)"
                " WHERE injection_start_time IS NULL"
                "   AND (target IS NOT NULL OR fault_spec IS NOT NULL)"
            )
        except Exception:
            pass
        # Migration: add tenant_id column to tasks table for multi-tenant isolation
        try:
            await conn.execute("ALTER TABLE tasks ADD COLUMN tenant_id TEXT DEFAULT ''")
        except Exception:
            pass
        await conn.commit()

    async def ensure_schema(self) -> None:
        """Public schema init — _get_conn handles schema on first use."""
        await self._get_conn()

    # -- tasks (narrow, hot) -------------------------------------------------

    async def select_task(self, task_id: str) -> Optional[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def upsert_task(self, task_id: str, columns: list[str], values: list) -> None:
        sql = _build_upsert_sql("tasks", columns)
        conn = await self._get_conn()
        await conn.execute(sql, values)
        await conn.commit()

    async def select_tasks_ordered(self, limit: int, offset: int) -> list[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM tasks ORDER BY gmt_create DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def select_tasks_by_state(self, task_state: str, limit: int, offset: int) -> list[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM tasks WHERE task_state = ? ORDER BY gmt_create DESC LIMIT ? OFFSET ?",
            (task_state, limit, offset),
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def select_active_tasks(self, namespace: str = "", target_name: str = "", tenant_id: str = "") -> list[dict]:
        # 与 PG 后端**逐字一致**的「可恢复实验」判据：落过注入意图 **且** 命令
        # 确已发出。两处必须同改 —— 历史上只改一处，结果在下一道关卡又报同样的错。
        #
        # fault_spec 是规范形态、target 是遗留形态。_store_sync 会从 fault_spec
        # 投影出 target，但全 SDK 有 4 处直接 upsert 绕过投影：
        # cli/runner.py:264/:568（只写 fault_spec，本判据正是为它们而改）、
        # tracer.py:229/:255（只建裸行、无意图字段，两种判据都排除）。
        #
        # injection_start_time 非空 = 注入命令确已发出。它是唯一写一次且**不会
        # 被清零**的"已发出"信号（详见 PG 后端注释）。少了这条，"方案已确认但
        # 卡在安全门、从未发出命令"的行会进可恢复列表，被恢复流程误选后报
        # 「找不到该任务的注入状态记录」。
        #
        # ❗ 不得换成 skill_name / target_name / blade_uid / injection_method /
        #    safety_status 任一判据 —— 每一条都曾（或经验证会）造成
        #    「注入了却恢复不了」，完整踩坑记录见
        #    task_store_postgresql.select_active_tasks。
        sql = (
            "SELECT t.* FROM tasks t"
            " LEFT JOIN task_details d ON d.task_id = t.task_id"
            " WHERE t.task_state IN ('injecting', 'injected')"
            " AND (d.target IS NOT NULL OR d.fault_spec IS NOT NULL)"
            " AND d.injection_start_time IS NOT NULL"
        )
        params: list = []
        if tenant_id:
            sql += " AND t.tenant_id = ?"
            params.append(tenant_id)
        if namespace:
            sql += " AND t.namespace = ?"
            params.append(namespace)
        if target_name:
            sql += " AND t.target_name = ?"
            params.append(target_name)
        sql += " ORDER BY t.gmt_create DESC"
        conn = await self._get_conn()
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def delete_task(self, task_id: str) -> bool:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "DELETE FROM tasks WHERE task_id = ?", (task_id,)
        )
        await conn.commit()
        return cursor.rowcount > 0

    async def count_tasks(self, task_state: str = None) -> int:
        conn = await self._get_conn()
        if task_state:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE task_state = ?", (task_state,)
            )
        else:
            cursor = await conn.execute("SELECT COUNT(*) FROM tasks")
        row = await cursor.fetchone()
        return row[0]

    # -- task_details (wide, cold) -------------------------------------------

    async def select_details(self, task_id: str) -> Optional[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM task_details WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def upsert_details(self, task_id: str, columns: list[str], values: list) -> None:
        sql = _build_upsert_sql("task_details", columns)
        conn = await self._get_conn()
        await conn.execute(sql, values)
        await conn.commit()

    async def select_details_batch(self, task_ids: list[str]) -> list[dict]:
        if not task_ids:
            return []
        placeholders = ", ".join("?" for _ in task_ids)
        sql = f"SELECT * FROM task_details WHERE task_id IN ({placeholders})"
        conn = await self._get_conn()
        cursor = await conn.execute(sql, task_ids)
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    async def delete_details(self, task_id: str) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "DELETE FROM task_details WHERE task_id = ?", (task_id,)
        )
        await conn.commit()

    # -- task_spans ----------------------------------------------------------

    async def delete_spans_by_task(self, task_id: str) -> None:
        conn = await self._get_conn()
        await conn.execute(
            "DELETE FROM task_spans WHERE task_id = ?", (task_id,)
        )
        await conn.commit()

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
        conn = await self._get_conn()
        await conn.execute(
            "INSERT INTO task_spans "
            "(task_id, node_name, start_time, end_time, duration_ms, "
            " token_input, token_output, tool_calls, error, gmt_create, gmt_modified) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                task_id, node_name, start_time, end_time, duration_ms,
                token_input, token_output, tool_calls_json, error,
                gmt_create, gmt_modified,
            ),
        )
        await conn.commit()

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
        conn = await self._get_conn()
        await conn.execute(
            "UPDATE task_details SET "
            "  total_token_input = total_token_input + ?,"
            "  total_token_output = total_token_output + ?,"
            "  total_duration_ms = total_duration_ms + ?,"
            "  total_tool_calls = total_tool_calls + ?,"
            "  total_llm_calls = total_llm_calls + ?,"
            "  gmt_modified = ? "
            "WHERE task_id = ?",
            (token_input, token_output, duration_ms, tool_calls, llm_calls, gmt_modified, task_id),
        )
        await conn.commit()

    async def select_spans(self, task_id: str) -> list[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM task_spans WHERE task_id = ? ORDER BY id",
            (task_id,),
        )
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- sessions ------------------------------------------------------------

    async def upsert_session(self, session_id: str, columns: list[str], values: list) -> None:
        sql = _build_upsert_sql("sessions", columns, conflict_col="session_id")
        conn = await self._get_conn()
        await conn.execute(sql, values)
        await conn.commit()

    async def select_session(self, session_id: str) -> Optional[dict]:
        conn = await self._get_conn()
        cursor = await conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        return _row_to_dict(row) if row else None

    async def select_sessions_ordered(self, limit: int, offset: int, status: str = "") -> list[dict]:
        conn = await self._get_conn()
        if status:
            cursor = await conn.execute(
                "SELECT * FROM sessions WHERE status = ? ORDER BY gmt_create DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cursor = await conn.execute(
                "SELECT * FROM sessions ORDER BY gmt_create DESC LIMIT ? OFFSET ?",
                (limit, offset),
            )
        rows = await cursor.fetchall()
        return [_row_to_dict(r) for r in rows]

    # -- lifecycle -----------------------------------------------------------

    async def close(self) -> None:
        if self._conn is not None:
            try:
                await self._conn.execute("PRAGMA optimize")
            except Exception:
                pass
            await self._conn.close()
            self._conn = None
