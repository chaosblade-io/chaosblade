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
    experiment_uid  TEXT,
    namespace       TEXT,
    target_name     TEXT,
    tenant_id       TEXT DEFAULT '',
    workspace_id    TEXT DEFAULT '',
    -- Round-32: materialised liability verdict (see task_store_backend.py
    -- _TASK_COLUMNS). 0 by default: a newborn row owes no destroy yet, and
    -- legacy rows backfilled by the one-shot migration below inherit the
    -- clearing-word fallback until their next upsert re-derives the ledger.
    liability_live  INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    finished_at     TEXT,
    duration_ms     INTEGER DEFAULT 0,
    gmt_create      TIMESTAMPTZ,
    gmt_modified    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uk_tasks_task_id ON tasks(task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_task_state ON tasks(task_state);
CREATE INDEX IF NOT EXISTS idx_tasks_namespace ON tasks(namespace);
CREATE INDEX IF NOT EXISTS idx_tasks_tenant ON tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id);
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
    -- Round-32: liability ledger wings (JSON arrays, see _JSON_COLUMNS).
    -- Warning: keep semicolons OUT of -- comments in these DDL strings —
    -- ensure_schema splits statements on the semicolon character and a
    -- commented one shreds the fragment stream (asyncpg has no
    -- executescript), leaking a syntax-error statement.
    owned_experiment_uids TEXT,
    retired_experiment_uids TEXT,
    -- Round-32b: combo discriminator (JSON true/false/NULL, see
    -- _JSON_COLUMNS) — NULL = never asserted (legacy), false = the
    -- execute side asserted experiments-only at birth, true = a native
    -- mutation rode alongside a live experiment. Gates the
    -- balanced-wing verdict in may_carry_live_fault branch A2.
    combo_native_issued TEXT,
    postmortem          TEXT,
    target_health_report TEXT,
    feasibility_report  TEXT,
    baseline_data       TEXT,
    inject_context      TEXT,
    skill_use_case      TEXT,
    injection_method    TEXT,
    kubectl_exec_pod_name TEXT,
    injection_start_time TEXT,
    execution_artifacts TEXT,
    model_name          TEXT,
    total_token_input   INTEGER NOT NULL DEFAULT 0,
    total_token_output  INTEGER NOT NULL DEFAULT 0,
    total_token_cached  INTEGER NOT NULL DEFAULT 0,
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

    When *columns* contains only the conflict key there is nothing to
    update — ``DO UPDATE SET`` with an empty assignment list is a syntax
    error in PostgreSQL (SQLite tolerates it). Fall back to ``DO NOTHING``:
    the row already exists and carries every column being "upserted", so
    the semantics are identical. This path is hit by bare existence
    anchors such as ``tracer._persist_span`` calling ``store.upsert(task_id)``.
    """
    col_names = ", ".join(columns)
    placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
    update_cols = [c for c in columns if c != conflict_col]
    if update_cols:
        conflict_action = "DO UPDATE SET " + ", ".join(
            f"{c}=EXCLUDED.{c}" for c in update_cols
        )
    else:
        conflict_action = "DO NOTHING"
    sql = (
        f"INSERT INTO {table} ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT({conflict_col}) {conflict_action}"
    )
    return sql, len(columns)


def _record_to_dict(record) -> dict:
    """Convert an asyncpg Record to a plain dict.

    TIMESTAMPTZ columns are returned as datetime objects by asyncpg;
    convert them back to ISO strings for consistency with the SQLite
    backend and upstream code that expects string timestamps.
    """
    from datetime import datetime as _dt

    d = dict(record)
    for col in _TIMESTAMP_COLUMNS:
        val = d.get(col)
        if isinstance(val, _dt):
            d[col] = val.isoformat()
    return d


# Columns declared as TIMESTAMPTZ in the DDL — asyncpg requires datetime objects.
_TIMESTAMP_COLUMNS: frozenset[str] = frozenset(
    {"gmt_create", "gmt_modified", "started_at", "finished_at"}
)


def _coerce_timestamps(columns: list[str], values: list) -> list:
    """Convert ISO-string timestamps to datetime objects for asyncpg TIMESTAMPTZ."""
    from datetime import datetime as _dt

    result = list(values)
    for i, col in enumerate(columns):
        if col in _TIMESTAMP_COLUMNS and isinstance(result[i], str) and result[i]:
            try:
                result[i] = _dt.fromisoformat(result[i])
            except (ValueError, TypeError):
                pass
    return result


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

        pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=5,
            command_timeout=30,
            server_settings={
                "statement_timeout": "30000",
            },
        )
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
        """DDL + startup migrations (phase-14 G6's fresh-database ruling
        reverted).

        The kubectl-native injection path dominates real usage and
        produces no experiment uid, so databases carrying the pre-phase-9
        ``blade_uid`` column (and/or pre-phase-14 missing detail columns)
        are common, not exceptional. Each migration step doubles as its
        own probe: on a current database every ALTER raises and is
        swallowed; only a legacy database performs actual work, exactly
        once.
        """
        async with self._pool.acquire() as conn:
            # Pre-migration: add tenant_id column BEFORE running DDL, because
            # DDL includes CREATE INDEX idx_tasks_tenant which requires the
            # column to exist. Without this, ensure_schema() crashes on the
            # index creation and the ALTER TABLE below never runs (chicken-and-egg).
            try:
                await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id TEXT DEFAULT ''")
            except Exception:
                pass
            # Same chicken-and-egg for workspace_id (idx_tasks_workspace in DDL).
            try:
                await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS workspace_id TEXT DEFAULT ''")
            except Exception:
                pass
            # asyncpg does not support executescript; run statements individually
            # Split by semicolons, filter empty lines. ❗ "--" comments are
            # legal INSIDE a statement fragment (PG parses them), but a
            # semicolon inside such a comment would split mid-comment and
            # leak a syntax-error fragment into the stream — see the warning
            # in _DETAILS_DDL (round-32 found this via the fake pool).
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
                await conn.execute("ALTER TABLE task_details ADD COLUMN execution_artifacts TEXT")
            except Exception:
                pass
            try:
                await conn.execute("ALTER TABLE task_details ADD COLUMN kubectl_exec_pod_name TEXT")
            except Exception:
                pass
            try:
                # LLM model frozen at task finalize time — synced from the
                # session record by ``_finalize_session_store`` so the
                # metric envelope can report which model ran the drill.
                await conn.execute("ALTER TABLE task_details ADD COLUMN model_name TEXT")
            except Exception:
                pass
            try:
                # Prompt-cache hits aggregated over the whole task — a SUBSET
                # of total_token_input (not additive). Written absolutely by
                # tracer._persist_summary at finalize; read back by
                # get_summary / _load_trace_from_store so the per-task hit
                # rate survives a restart and is SQL-queryable.
                await conn.execute(
                    "ALTER TABLE task_details ADD COLUMN total_token_cached"
                    " INTEGER NOT NULL DEFAULT 0"
                )
            except Exception:
                pass
            try:
                # ``injection_start_time`` — the only field written exactly when
                # an injection command is *issued* (execute_loop sets it
                # write-once and never clears it, unlike
                # ``injection_method``). ``select_active_tasks`` needs it to tell
                # "confirmed but never executed" apart from "really injected".
                #
                # ALTER + backfill are wrapped in ONE explicit transaction:
                # asyncpg auto-commits each ``execute`` outside a transaction,
                # so without this the process could die between them and leave
                # the column added but never backfilled. Because the backfill is
                # one-shot (see below), that middle state would be permanent.
                async with conn.transaction():
                    await conn.execute(
                        "ALTER TABLE task_details ADD COLUMN injection_start_time TEXT"
                    )
                    # One-shot backfill, deliberately INSIDE this try: it runs
                    # only on the migration that adds the column (on later
                    # startups the ALTER raises and we skip).
                    #
                    # ❗ 不要把它拆成独立的 try / 让它每次启动都跑：回填条件
                    # （injection_start_time IS NULL 且有意图）恰好也匹配
                    # 「方案已确认但命令从未发出」的**新行**，每次启动重跑会给
                    # 它们盖上时间戳，永久废掉 select_active_tasks 的"已发出"
                    # 判据。一次性回填失败最多让存量行暂时不可恢复（一次性
                    # 窗口），而重复回填是永久性失效。上面的显式事务已消除
                    # "列加上了但回填没跑"这个中间态。
                    #
                    # Pre-existing rows have no recorded issue time, so assume
                    # they were issued and stamp ``tasks.gmt_create`` — that
                    # keeps their current recoverable status. Excluding them
                    # instead would hide real in-flight injections, i.e.
                    # re-create the "注入了却恢复不了" bug this column exists
                    # to avoid.
                    await conn.execute(
                        "UPDATE task_details d SET injection_start_time = COALESCE("
                        "  (SELECT t.gmt_create FROM tasks t WHERE t.task_id = d.task_id),"
                        "  d.gmt_create)"
                        " WHERE d.injection_start_time IS NULL"
                        "   AND (d.target IS NOT NULL OR d.fault_spec IS NOT NULL)"
                    )
            except Exception:
                pass
            # Phase-9 contract rename: tasks.blade_uid → tasks.experiment_uid.
            # One-time hard cut — no dual-write, no legacy fallback. Existing
            # online databases migrate via RENAME COLUMN on this startup
            # (equivalent manual SQL for a reviewed change window:
            #   ALTER TABLE tasks RENAME COLUMN blade_uid TO experiment_uid; )
            # Fresh databases already create the new name, so the ALTER then
            # fails with undefined_column and is swallowed, like every
            # migration above.
            try:
                await conn.execute(
                    "ALTER TABLE tasks RENAME COLUMN blade_uid TO experiment_uid"
                )
            except Exception:
                pass
            try:
                await conn.execute(
                    "ALTER TABLE task_details ADD COLUMN owned_experiment_uids TEXT"
                )
            except Exception:
                pass
            try:
                await conn.execute(
                    "ALTER TABLE task_details ADD COLUMN retired_experiment_uids TEXT"
                )
            except Exception:
                pass
            try:
                # Round-32b — combo discriminator. Plain ADD COLUMN, NO
                # backfill: NULL is the tri-state's unknown leg and is
                # deliberately preserved — legacy rows (and any window row
                # written before this migration) keep the committed-carrier
                # fallback in may_carry_live_fault instead of guessing
                # "experiments-only" from absence of evidence (a guessed
                # false would false-clear combo rows whose marker never
                # landed). New rows get their assertion from the execute
                # birth seam.
                await conn.execute(
                    "ALTER TABLE task_details ADD COLUMN combo_native_issued TEXT"
                )
            except Exception:
                pass
            try:
                # Round-32 — materialised liability verdict on the tasks table.
                # ALTER + backfill wrapped in ONE explicit transaction (same
                # rationale as injection_start_time above: asyncpg
                # auto-commits outside a transaction, so the process dying
                # between them would leave the column added but never
                # backfilled — permanent, because the backfill is one-shot).
                # The backfill uses the CLEARED-word fallback
                # (may_carry_live_fault's legacy branch), NOT the old
                # ACTIVE-word predicate — the old words guessed wrong for
                # 'recovering' / 'failed'-with-experiment /
                # 'partial_recovered' rows (round-32 K1/K2), and this one-time
                # pass is exactly where those already-blinded legacy rows get
                # their recovery entrance back. The full ledger re-derivation
                # happens on each row's next upsert; this backfill is the
                # stopgap that makes those rows visible UNTIL then.
                #
                # 单源渲染：cleared 词集来自 TASK_STATE_CLEARED_VALUES 立法
                # （agent/state.py），不在此手抄具体词。
                from chaos_agent.agent.state import TASK_STATE_CLEARED_VALUES

                cleared = ", ".join(
                    f"'{v}'" for v in sorted(TASK_STATE_CLEARED_VALUES)
                )
                async with conn.transaction():
                    await conn.execute(
                        "ALTER TABLE tasks ADD COLUMN liability_live INTEGER NOT NULL DEFAULT 0"
                    )
                    await conn.execute(
                        "UPDATE tasks SET liability_live = 1"
                        " WHERE liability_live = 0"
                        "   AND task_state NOT IN (" + cleared + ")"
                        "   AND EXISTS ("
                        "     SELECT 1 FROM task_details d"
                        "     WHERE d.task_id = tasks.task_id"
                        "       AND (d.target IS NOT NULL OR d.fault_spec IS NOT NULL)"
                        "       AND d.injection_start_time IS NOT NULL"
                        "   )"
                    )
            except Exception:
                pass
            # Index AFTER the column exists, in its own try: on a fresh DB
            # the ALTER above raises inside the transaction-wrapped block
            # and skips to here (the index still needs creating); on a
            # legacy DB an index inside the DDL statement list would run
            # BEFORE the ALTER and crash ensure_schema (no per-statement
            # try on the PG path — the chicken-and-egg the tenant_id
            # pre-migration solves by ALTERing ahead of the DDL).
            try:
                await conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tasks_liability_live"
                    " ON tasks(liability_live)"
                )
            except Exception:
                pass
            # tenant_id migration already done above (pre-DDL)

    # -- tasks (narrow, hot) -------------------------------------------------

    async def select_task(self, task_id: str) -> Optional[dict]:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM tasks WHERE task_id = $1", task_id
            )
            return _record_to_dict(row) if row else None

    async def upsert_task(self, task_id: str, columns: list[str], values: list) -> None:
        sql, _ = _build_upsert_sql("tasks", columns)
        coerced = _coerce_timestamps(columns, values)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *coerced)

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

    async def select_active_tasks(self, namespace: str = "", target_name: str = "", tenant_id: str = "", workspace_id: str = "") -> list[dict]:
        # Round-32 根因修复：可恢复集不再从 task_state **词**猜测，改键在物化列
        # tasks.liability_live（写侧 may_carry_live_fault 单源渲染，见
        # task_store.py）。旧谓词（task_state IN ACTIVE_SET + target/fault_spec
        # + injection_start_time 三判据）回答的是“哪些生命周期**词**听起来没
        # 完”——每个新词（recovering、failed-with-experiment、
        # partial_recovered）都要求人工重新推导一遍“这个词下故障还活着吗”，
        # 而推导错的两个词（round-32 K1/K2）让确定活着的故障永久失明。
        # 物化列把三判据全部吸收为写时一次计算。
        #
        # 与 SQLite 后端**逐字一致**的判据契约：两处必须同改 —— 历史上只改
        # 一处，结果在下一道关卡又报同样的错。
        #
        # ── 词汇陷阱档案（旧词谓词时代的历史踩坑，每一条都曾造成
        # 「注入了却恢复不了」；旧判据的“已发出”证据 injection_start_time /
        # target / fault_spec 仍由 may_carry_live_fault 的遗留回退分支消费）──
        #
        # ``fault_spec`` 是规范形态，``target`` 是遗留形态（见
        # task_store_backend._extract_index_fields 的 docstring 与其两级回退
        # 逻辑）。生产主路径两者都写 —— _store_sync 在写库前用 setdefault 从
        # fault_spec 投影出 target/params/namespace/target_name。但全 SDK 共有
        # 4 处**直接** store.upsert()、绕过该投影：
        #   • cli/runner.py:264 / :568 —— pipeline 启动前写初始状态，只带
        #     fault_spec（规范形态），无 target；
        #   • observability/tracer.py:229 / :255 —— 仅为让 span/summary 有行
        #     可挂而建裸行，不带任何意图字段，落库后 target 与 fault_spec
        #     双 NULL，**新旧判据都排除**，不影响正确性。
        #
        # ❗ 替代判据都曾（或经验证会）误藏真实注入：
        #   • skill_name IS NOT NULL  —— 误藏不激活 skill 的真实注入；
        #   • target_name <> ''       —— 误藏 host 类与按 labels 选目标的注入；
        #   • 仅 target IS NOT NULL   —— 误藏只写规范形态的注入；
        #   • experiment_uid IS NOT NULL  —— 误藏 kubectl_native / host_native
        #     （它们天然无 uid，"attempt IS the injection"）；
        #   • injection_method IS NOT NULL —— execute_loop 的多步自检分支会把
        #     已发出命令的 method 置回 None，无法区分"已执行但 method 为空"；
        #   • safety_status <> 'pending' —— schema 默认值就是 'pending'
        #     (NOT NULL DEFAULT)，"真的卡在安全门"与"从未写过"在库里同形。
        #
        # ⚠️ 新接缝的立法见 may_carry_live_fault（task_store.py）与
        # TASK_STATE_CLEARED_VALUES（agent/state.py）。
        conditions = [
            "t.liability_live = 1",
        ]
        params: list = []
        idx = 0
        if tenant_id:
            idx += 1
            conditions.append(f"t.tenant_id = ${idx}")
            params.append(tenant_id)
        if workspace_id:
            idx += 1
            conditions.append(f"t.workspace_id = ${idx}")
            params.append(workspace_id)
        if namespace:
            idx += 1
            conditions.append(f"t.namespace = ${idx}")
            params.append(namespace)
        if target_name:
            idx += 1
            conditions.append(f"t.target_name = ${idx}")
            params.append(target_name)
        where = " AND ".join(conditions)
        sql = (
            "SELECT t.* FROM tasks t "
            f"WHERE {where} ORDER BY t.gmt_create DESC"
        )
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
        coerced = _coerce_timestamps(columns, values)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *coerced)

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
        from datetime import datetime as _dt

        # Convert timestamp strings to datetime for TIMESTAMPTZ columns
        try:
            gmt_create_dt = _dt.fromisoformat(gmt_create) if isinstance(gmt_create, str) else gmt_create
        except (ValueError, TypeError):
            gmt_create_dt = gmt_create
        try:
            gmt_modified_dt = _dt.fromisoformat(gmt_modified) if isinstance(gmt_modified, str) else gmt_modified
        except (ValueError, TypeError):
            gmt_modified_dt = gmt_modified

        async with self._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO task_spans "
                "(task_id, node_name, start_time, end_time, duration_ms, "
                " token_input, token_output, tool_calls, error, gmt_create, gmt_modified) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)",
                task_id, node_name, start_time, end_time, duration_ms,
                token_input, token_output, tool_calls_json, error,
                gmt_create_dt, gmt_modified_dt,
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
        from datetime import datetime as _dt

        try:
            gmt_modified_dt = _dt.fromisoformat(gmt_modified) if isinstance(gmt_modified, str) else gmt_modified
        except (ValueError, TypeError):
            gmt_modified_dt = gmt_modified

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
                tool_calls, llm_calls, gmt_modified_dt, task_id,
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
        coerced = _coerce_timestamps(columns, values)
        async with self._pool.acquire() as conn:
            await conn.execute(sql, *coerced)

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
