"""Tests for the PostgreSQL TaskStore backend (offline, fake asyncpg pool).

asyncpg / a real PostgreSQL server are not available in CI, so these tests
run ``PostgreSQLBackend`` against ``FakePool`` — an in-memory pool that
implements the SQL semantics the backend depends on (ON CONFLICT merges,
incremental summary updates, TIMESTAMPTZ type checks). This verifies SQL
construction, parameter ordering, timestamp coercion and result mapping
rather than merely asserting that calls were made.
"""

import sys
import types
from datetime import datetime

import pytest
import pytest_asyncio

from chaos_agent.persistence.task_store_postgresql import (
    PostgreSQLBackend,
    _build_upsert_sql,
    _coerce_timestamps,
    _record_to_dict,
)

from .fake_asyncpg_pool import FakePgError, FakePool, FakeRecord


@pytest_asyncio.fixture
async def backend():
    """PostgreSQLBackend wired to a fresh in-memory FakePool."""
    return PostgreSQLBackend(FakePool())


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestBuildUpsertSql:
    def test_placeholders_are_positional_and_numbered(self):
        sql, n = _build_upsert_sql("tasks", ["task_id", "skill_name", "namespace"])
        assert n == 3
        assert "INSERT INTO tasks (task_id, skill_name, namespace)" in sql
        assert "VALUES ($1, $2, $3)" in sql
        assert "ON CONFLICT(task_id) DO UPDATE SET" in sql

    def test_conflict_column_excluded_from_update_clause(self):
        sql, _ = _build_upsert_sql("tasks", ["task_id", "skill_name"])
        update_clause = sql.split("DO UPDATE SET", 1)[1]
        assert "skill_name=EXCLUDED.skill_name" in update_clause
        assert "task_id=EXCLUDED" not in update_clause

    def test_custom_conflict_column_for_sessions(self):
        sql, _ = _build_upsert_sql(
            "sessions", ["session_id", "status"], conflict_col="session_id"
        )
        assert "ON CONFLICT(session_id) DO UPDATE SET status=EXCLUDED.status" in sql

    def test_single_conflict_column_falls_back_to_do_nothing(self):
        """Regression: a conflict-key-only upsert must not emit an empty
        'DO UPDATE SET' clause (a syntax error in real PostgreSQL, hit by
        bare existence anchors like tracer's store.upsert(task_id))."""
        sql, n = _build_upsert_sql("task_details", ["task_id"])
        assert n == 1
        assert sql.endswith("ON CONFLICT(task_id) DO NOTHING")
        assert "DO UPDATE SET" not in sql


class TestCoerceTimestamps:
    def test_iso_string_becomes_datetime(self):
        out = _coerce_timestamps(["gmt_create"], ["2026-08-13T10:00:00+00:00"])
        assert isinstance(out[0], datetime)
        assert out[0].year == 2026

    def test_non_timestamp_columns_and_empty_values_untouched(self):
        out = _coerce_timestamps(
            ["task_id", "gmt_create"], ["task-1", ""]
        )
        assert out[0] == "task-1"
        assert out[1] == ""

    def test_invalid_iso_string_passes_through(self):
        out = _coerce_timestamps(["gmt_create"], ["not-a-timestamp"])
        assert out[0] == "not-a-timestamp"


class TestRecordToDict:
    def test_datetime_timestamps_converted_to_iso_strings(self):
        dt = datetime(2026, 8, 13, 10, 0, 0)
        d = _record_to_dict(FakeRecord({"task_id": "t", "gmt_create": dt}))
        assert d["gmt_create"] == dt.isoformat()
        assert d["task_id"] == "t"

    def test_none_timestamp_stays_none(self):
        d = _record_to_dict(FakeRecord({"gmt_modified": None}))
        assert d["gmt_modified"] is None


# ---------------------------------------------------------------------------
# Schema / migrations
# ---------------------------------------------------------------------------

class TestSchema:
    async def test_ensure_schema_runs_full_ddl_and_migrations(self, backend):
        """[已翻转回 G6 前] 迁移段已恢复：四表 DDL + 全部一次性迁移
        ALTER 都会发出（旧库原地升级；fresh 库上 ALTER 全部失败被吞）。"""
        await backend.ensure_schema()
        executed = "\n".join(backend._pool.db.executed)
        for table in ("tasks", "task_details", "task_spans", "sessions"):
            assert f"CREATE TABLE IF NOT EXISTS {table}" in executed
        # one-shot migration columns all attempted on first run
        for col in ("fault_spec", "failure_reason", "baseline_data",
                    "postmortem", "injection_start_time"):
            assert f"ADD COLUMN {col}" in executed
        assert "ADD COLUMN IF NOT EXISTS tenant_id" in executed
        # phase-9 rename also attempted (fresh FakePool: raises, swallowed)
        assert "RENAME COLUMN blade_uid TO experiment_uid" in executed
        # prompt-cache aggregate column: idempotent migration ALTER is
        # attempted (legacy DBs gain it; fresh FakePool raises, swallowed)
        assert "ADD COLUMN total_token_cached" in executed

    async def test_ensure_schema_carries_workspace_column_and_index(self, backend):
        """方案 A：workspace_id 与 tenant_id 同构——前置 ALTER（鸡蛋
        顺序：idx_tasks_workspace 在 DDL 里，列必须先存在）+ DDL 自带
        列与索引。双后端逐字一致契约的 PG 侧锁。"""
        await backend.ensure_schema()
        executed = "\n".join(backend._pool.db.executed)
        assert "ADD COLUMN IF NOT EXISTS workspace_id" in executed
        from chaos_agent.persistence.task_store_postgresql import _SCHEMA_DDL
        assert "workspace_id    TEXT DEFAULT ''" in _SCHEMA_DDL
        assert "idx_tasks_workspace ON tasks(workspace_id)" in _SCHEMA_DDL

    async def test_select_active_tasks_workspace_filter(self, backend):
        """select_active_tasks(workspace_id=...) 在 fake pool 上行为级验证：
        双轴过滤生效、空值不过滤（fetch 不进 executed 日志，只能
        走行为断言——比 SQL 子串匹配更强）。"""
        for tid, ws in (("task-a", "ws-lisi"), ("task-b", "ws-526255")):
            await backend.upsert_task(
                tid,
                ["task_id", "task_state", "liability_live",
                 "tenant_id", "workspace_id"],
                [tid, "injected", 1, "t-org", ws],
            )
        rows = await backend.select_active_tasks(tenant_id="t-org", workspace_id="ws-lisi")
        assert [r["task_id"] for r in rows] == ["task-a"]
        rows = await backend.select_active_tasks(tenant_id="t-org", workspace_id="ws-526255")
        assert [r["task_id"] for r in rows] == ["task-b"]
        # 空值 → 不过滤：全量（本地 CLI / 裸 SDK 契约）
        rows = await backend.select_active_tasks(tenant_id="t-org")
        assert {r["task_id"] for r in rows} == {"task-a", "task-b"}
        assert await backend.select_active_tasks() == await backend.select_active_tasks(
            tenant_id="", workspace_id="")

    async def test_ensure_schema_is_idempotent(self, backend):
        """二次运行必须存活：ALTER ADD COLUMN 全部 raise（DuplicateColumn）
        被吞，DDL 幂等（IF NOT EXISTS）。"""
        await backend.ensure_schema()
        await backend.ensure_schema()  # ALTER ADD COLUMN raises → swallowed

    async def test_fresh_ddl_carries_all_migration_columns(self, backend):
        """六列已并入 _DETAILS_DDL——fresh 库由 DDL 直接建成终态列集；
        启动迁移段的 ALTER 只负责兑旧库（双保险，两者不冲突）。"""
        from chaos_agent.persistence.task_store_postgresql import _DETAILS_DDL
        for col in ("baseline_data", "inject_context", "skill_use_case",
                    "injection_method", "kubectl_exec_pod_name",
                    "injection_start_time"):
            assert f"{col}" in _DETAILS_DDL, col
        # round-32: ledger wings + the verdict column ship in the DDL too
        for col in ("owned_experiment_uids", "retired_experiment_uids"):
            assert col in _DETAILS_DDL, col
        from chaos_agent.persistence.task_store_postgresql import _TASKS_DDL
        assert "liability_live" in _TASKS_DDL
        # prompt-cache aggregate ships in the fresh DDL too (a SUBSET of
        # total_token_input, not additive) — PG parity with SQLite
        assert "total_token_cached" in _DETAILS_DDL

    async def test_injection_start_time_backfill_is_one_shot(self, backend):
        """[已翻转回 G6 前] 存量行回填一次；迁移后新插入的行永不被碰。"""
        # pre-migration legacy row: has intent, no injection_start_time
        await backend.upsert_task(
            "task-legacy",
            ["task_id", "task_state", "gmt_create"],
            ["task-legacy", "injected", "2026-08-01T00:00:00+00:00"],
        )
        await backend.upsert_details(
            "task-legacy",
            ["task_id", "target", "gmt_create"],
            ["task-legacy", "app=legacy", "2026-08-01T00:00:00+00:00"],
        )
        await backend.ensure_schema()
        legacy = await backend.select_details("task-legacy")
        assert legacy["injection_start_time"] is not None  # backfilled

        # new row inserted AFTER the migration ran once
        await backend.upsert_details(
            "task-new", ["task_id", "target"], ["task-new", "app=new"]
        )
        await backend.ensure_schema()  # ALTER raises → backfill skipped
        new = await backend.select_details("task-new")
        # real PG keeps the column with NULL; the fake omits the key — both
        # mean "never backfilled"
        assert new.get("injection_start_time") is None

    async def test_round32_liability_migrations_attempted_on_first_run(self, backend):
        """Round-32 一次性迁移全发出（fresh FakePool：ALTER 全 raise 被
        各自的 try 吞）：双翼 ALTER + 负债列 ALTER + 索引（独立 try，
        两种库形态都建）。"""
        await backend.ensure_schema()
        executed = "\n".join(backend._pool.db.executed)
        for col in ("owned_experiment_uids", "retired_experiment_uids"):
            assert f"ADD COLUMN {col}" in executed, col
        assert "ADD COLUMN liability_live" in executed
        # index in its own try — fresh DB lands here after the ALTER
        # raised inside the transaction-wrapped block
        assert "CREATE INDEX IF NOT EXISTS idx_tasks_liability_live" in executed

    async def test_liability_backfill_is_one_shot(self, backend):
        """Round-32 回填：存量「已发出但词未清算」的行一次性拿回
        liability_live=1（K1/K2 已致盲的存量行正是在这里拿回恢复入口）；
        清算词行不被碰；迁移后写入的行永不被碰。"""
        # pre-migration legacy rows — direct backend writes, pre-column
        await backend.upsert_task(
            "task-legacy", ["task_id", "task_state", "gmt_create"],
            ["task-legacy", "failed", "2026-08-01T00:00:00+00:00"],
        )
        await backend.upsert_task(
            "task-cleared", ["task_id", "task_state", "gmt_create"],
            ["task-cleared", "recovered", "2026-08-01T00:00:00+00:00"],
        )
        for tid in ("task-legacy", "task-cleared"):
            await backend.upsert_details(
                tid,
                ["task_id", "target", "injection_start_time"],
                [tid, "app=x", "2026-08-01T00:00:00+00:00"],
            )
        await backend.ensure_schema()
        # 'failed' with an issued injection → re-admitted (the K2 shape);
        # 'recovered' → stays cleared even with identical evidence
        assert (await backend.select_task("task-legacy"))["liability_live"] == 1
        assert (await backend.select_task("task-cleared"))["liability_live"] == 0

        # row written AFTER the migration ran — an explicit verdict-0
        # (what TaskStore.upsert materialises for a cleared row) must
        # stay 0 across restarts: the backfill is one-shot
        await backend.upsert_task(
            "task-new", ["task_id", "task_state", "liability_live"],
            ["task-new", "injected", 0],
        )
        await backend.ensure_schema()  # ALTER raises → backfill skipped
        assert (await backend.select_task("task-new"))["liability_live"] == 0


# ---------------------------------------------------------------------------
# tasks table
# ---------------------------------------------------------------------------

class TestTasks:
    async def test_upsert_insert_then_partial_update_merges(self, backend):
        await backend.upsert_task(
            "task-1",
            ["task_id", "skill_name", "operation", "gmt_create", "gmt_modified"],
            ["task-1", "pod-kill", "inject",
             "2026-08-13T10:00:00+00:00", "2026-08-13T10:00:00+00:00"],
        )
        await backend.upsert_task("task-1", ["task_id", "experiment_uid"],
                                  ["task-1", "uid-abc"])
        row = await backend.select_task("task-1")
        assert row["skill_name"] == "pod-kill"   # preserved by merge
        assert row["experiment_uid"] == "uid-abc"
        assert row["gmt_create"] == "2026-08-13T10:00:00+00:00"  # dt → ISO str

    async def test_upsert_rejects_uncoerced_string_timestamp(self, backend):
        """_coerce_timestamps must run before execute; raw str → asyncpg error."""
        with pytest.raises(FakePgError, match="TIMESTAMPTZ"):
            # bypass coercion by calling the pool directly with a raw string
            async with backend._pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO tasks (task_id, gmt_create) VALUES ($1, $2)",
                    "task-x", "2026-08-13T10:00:00+00:00",
                )

    async def test_select_task_returns_none_for_missing(self, backend):
        assert await backend.select_task("ghost") is None

    async def test_bare_task_id_upsert_is_valid_anchor(self, backend):
        """The tracer's existence anchor must work on first and repeat call."""
        await backend.upsert_task("task-anchor", ["task_id"], ["task-anchor"])
        await backend.upsert_task("task-anchor", ["task_id"], ["task-anchor"])
        row = await backend.select_task("task-anchor")
        assert row["task_id"] == "task-anchor"

    async def test_select_tasks_ordered_desc_with_paging(self, backend):
        for i, ts in enumerate(["2026-08-11", "2026-08-13", "2026-08-12"]):
            await backend.upsert_task(
                f"task-{i}",
                ["task_id", "gmt_create"],
                [f"task-{i}", f"{ts}T00:00:00+00:00"],
            )
        rows = await backend.select_tasks_ordered(limit=2, offset=0)
        assert [r["task_id"] for r in rows] == ["task-1", "task-2"]
        rows = await backend.select_tasks_ordered(limit=2, offset=2)
        assert [r["task_id"] for r in rows] == ["task-0"]

    async def test_select_tasks_by_state_filters(self, backend):
        await backend.upsert_task("task-a", ["task_id", "task_state"],
                                  ["task-a", "injecting"])
        await backend.upsert_task("task-b", ["task_id", "task_state"],
                                  ["task-b", "recovered"])
        rows = await backend.select_tasks_by_state("injecting", limit=10, offset=0)
        assert [r["task_id"] for r in rows] == ["task-a"]

    async def test_delete_task_returns_whether_row_existed(self, backend):
        await backend.upsert_task("task-d", ["task_id"], ["task-d"])
        assert await backend.delete_task("task-d") is True
        assert await backend.delete_task("task-d") is False

    async def test_count_tasks_with_and_without_state(self, backend):
        await backend.upsert_task("task-a", ["task_id", "task_state"],
                                  ["task-a", "injecting"])
        await backend.upsert_task("task-b", ["task_id", "task_state"],
                                  ["task-b", "recovered"])
        assert await backend.count_tasks() == 2
        assert await backend.count_tasks("injecting") == 1


# ---------------------------------------------------------------------------
# select_active_tasks — the "recoverable experiment" criteria
# ---------------------------------------------------------------------------

class TestSelectActiveTasks:
    """Round-32：后端谓词键在 tasks.liability_live（TaskStore.upsert /
    update_task_state 经 may_carry_live_fault 写入的物化判决）。

    词与负债的语义判断（never-issued 幽灵、无意图裸行、unverified
    fail-closed、K1/K2 致盲词）上移一层，锚在
    test_task_store.py::TestQueryActive；本类钉 SQL 契约：账本列说了
    算，task_state 词说什么都不影响。"""

    async def _seed(self, backend, task_id, *, liability_live=1,
                    state="injected", **task_fields):
        cols = ["task_id", "task_state", "liability_live"]
        vals = [task_id, state, liability_live]
        for k, v in task_fields.items():
            cols.append(k)
            vals.append(v)
        await backend.upsert_task(task_id, cols, vals)

    async def test_liability_live_row_is_returned(self, backend):
        """账本标活的行可恢复——谓词的唯一正向路径。"""
        await self._seed(backend, "task-ok", namespace="ns1", target_name="app")
        rows = await backend.select_active_tasks()
        assert [r["task_id"] for r in rows] == ["task-ok"]

    async def test_liability_cleared_row_is_excluded(self, backend):
        """清算行离开可恢复集，即使词还读作 'injected'——判决列而非词
        说了算（K1/K2 盲区的镜像面：陈旧的词永不能重新放行或重新
        隐藏一行）。"""
        await self._seed(backend, "task-done", liability_live=0, state="injected")
        assert await backend.select_active_tasks() == []

    async def test_state_word_does_not_override_ledger_verdict(self, backend):
        """K1/K2 的 SQL 侧锚：'failed'-with-experiment、'recovering' 孤儿
        行、'partial_recovered'、'unverified'——甚至清算词 'completed'
        ——只要账本说活就返回。旧 IN 子句对恰恰这些词猜错过。"""
        for word in ("failed", "recovering", "partial_recovered",
                     "unverified", "completed"):
            await self._seed(backend, f"task-{word}", state=word)
        rows = await backend.select_active_tasks()
        assert sorted(r["task_id"] for r in rows) == [
            "task-completed", "task-failed", "task-partial_recovered",
            "task-recovering", "task-unverified",
        ]

    async def test_filters_by_namespace_target_tenant(self, backend):
        await self._seed(backend, "task-1",
                         namespace="ns-a", target_name="app-a", tenant_id="t-1")
        await self._seed(backend, "task-2",
                         namespace="ns-b", target_name="app-b", tenant_id="t-2")
        rows = await backend.select_active_tasks(namespace="ns-a")
        assert [r["task_id"] for r in rows] == ["task-1"]
        rows = await backend.select_active_tasks(target_name="app-b")
        assert [r["task_id"] for r in rows] == ["task-2"]
        rows = await backend.select_active_tasks(
            tenant_id="t-1", namespace="ns-a", target_name="app-a"
        )
        assert [r["task_id"] for r in rows] == ["task-1"]
        assert await backend.select_active_tasks(namespace="ns-x") == []


# ---------------------------------------------------------------------------
# task_details
# ---------------------------------------------------------------------------

class TestDetails:
    async def test_upsert_and_select_roundtrip(self, backend):
        await backend.upsert_details(
            "task-1", ["task_id", "safety_status", "plan_summary"],
            ["task-1", "safe", "kill one pod"],
        )
        row = await backend.select_details("task-1")
        assert row["safety_status"] == "safe"
        assert row["plan_summary"] == "kill one pod"

    async def test_upsert_details_roundtrips_total_token_cached(self, backend):
        """PG parity with SQLite: total_token_cached (a subset of input)
        survives the upsert→select roundtrip through the PG backend."""
        await backend.upsert_details(
            "task-1",
            ["task_id", "total_token_input", "total_token_cached"],
            ["task-1", 2990, 2176],
        )
        row = await backend.select_details("task-1")
        assert row["total_token_input"] == 2990
        assert row["total_token_cached"] == 2176

    async def test_select_details_none_when_missing(self, backend):
        assert await backend.select_details("ghost") is None

    async def test_select_details_batch(self, backend):
        # ❗ seed with a non-conflict column: a conflict-key-only upsert
        # produces an empty DO UPDATE SET clause, which real PG rejects.
        for tid in ("task-1", "task-2"):
            await backend.upsert_details(
                tid, ["task_id", "plan_summary"], [tid, "seeded"]
            )
        rows = await backend.select_details_batch(["task-1", "task-2", "ghost"])
        assert {r["task_id"] for r in rows} == {"task-1", "task-2"}

    async def test_select_details_batch_empty_short_circuits(self, backend):
        assert await backend.select_details_batch([]) == []

    async def test_delete_details(self, backend):
        await backend.upsert_details(
            "task-1", ["task_id", "plan_summary"], ["task-1", "seeded"]
        )
        await backend.delete_details("task-1")
        assert await backend.select_details("task-1") is None


# ---------------------------------------------------------------------------
# task_spans + summary rollup
# ---------------------------------------------------------------------------

class TestSpansAndSummary:
    async def test_insert_span_coerces_timestamps_and_select_orders_by_id(self, backend):
        await backend.insert_span(
            task_id="task-1", node_name="agent_loop",
            start_time=1.0, end_time=2.5, duration_ms=1500.0,
            token_input=100, token_output=20,
            tool_calls_json='["kubectl"]', error=None,
            gmt_create="2026-08-13T10:00:00+00:00",
            gmt_modified="2026-08-13T10:00:01+00:00",
        )
        await backend.insert_span(
            task_id="task-1", node_name="execute_loop",
            start_time=3.0, end_time=4.0, duration_ms=1000.0,
            token_input=50, token_output=10,
            tool_calls_json="[]", error="boom",
            gmt_create="2026-08-13T10:00:02+00:00",
            gmt_modified="2026-08-13T10:00:03+00:00",
        )
        spans = await backend.select_spans("task-1")
        assert [s["node_name"] for s in spans] == ["agent_loop", "execute_loop"]
        assert spans[0]["tool_calls"] == '["kubectl"]'
        assert spans[0]["gmt_create"] == "2026-08-13T10:00:00+00:00"
        assert spans[1]["error"] == "boom"
        # spans of other tasks are not returned
        assert await backend.select_spans("task-other") == []

    async def test_delete_spans_by_task_only_touches_that_task(self, backend):
        for tid in ("task-1", "task-2"):
            await backend.insert_span(
                task_id=tid, node_name="n", start_time=0.0, end_time=1.0,
                duration_ms=1000.0, token_input=0, token_output=0,
                tool_calls_json="[]", error=None,
                gmt_create="2026-08-13T10:00:00+00:00",
                gmt_modified="2026-08-13T10:00:00+00:00",
            )
        await backend.delete_spans_by_task("task-1")
        assert await backend.select_spans("task-1") == []
        assert len(await backend.select_spans("task-2")) == 1

    async def test_update_task_summary_accumulates_not_overwrites(self, backend):
        """The flush_trace regression: rollups must ADD, never reset."""
        await backend.upsert_details(
            "task-1", ["task_id", "plan_summary"], ["task-1", "seeded"]
        )
        for _ in range(2):
            await backend.update_task_summary(
                task_id="task-1", token_input=100, token_output=10,
                duration_ms=500, tool_calls=3, llm_calls=1,
                gmt_modified="2026-08-13T10:00:00+00:00",
            )
        row = await backend.select_details("task-1")
        assert row["total_token_input"] == 200
        assert row["total_token_output"] == 20
        assert row["total_duration_ms"] == 1000
        assert row["total_tool_calls"] == 6
        assert row["total_llm_calls"] == 2
        # datetime is coerced on write and converted back to ISO on read
        assert row["gmt_modified"] == "2026-08-13T10:00:00+00:00"

    async def test_update_task_summary_missing_row_is_noop(self, backend):
        await backend.update_task_summary(
            task_id="ghost", token_input=1, token_output=1,
            duration_ms=1, tool_calls=1, llm_calls=1,
            gmt_modified="2026-08-13T10:00:00+00:00",
        )


# ---------------------------------------------------------------------------
# sessions
# ---------------------------------------------------------------------------

class TestSessions:
    async def test_upsert_and_select_session(self, backend):
        await backend.upsert_session(
            "sess-1",
            ["session_id", "status", "cluster_name", "started_at"],
            ["sess-1", "active", "prod", "2026-08-13T10:00:00+00:00"],
        )
        row = await backend.select_session("sess-1")
        assert row["status"] == "active"
        assert row["started_at"] == "2026-08-13T10:00:00+00:00"
        # conflict key is session_id, not task_id
        await backend.upsert_session("sess-1", ["session_id", "status"],
                                     ["sess-1", "closed"])
        row = await backend.select_session("sess-1")
        assert row["status"] == "closed"
        assert row["cluster_name"] == "prod"

    async def test_select_sessions_ordered_with_status_filter(self, backend):
        for i, status in enumerate(["active", "closed", "active"]):
            await backend.upsert_session(
                f"sess-{i}",
                ["session_id", "status", "gmt_create"],
                [f"sess-{i}", status, f"2026-08-1{i + 1}T00:00:00+00:00"],
            )
        rows = await backend.select_sessions_ordered(limit=10, offset=0)
        assert [r["session_id"] for r in rows] == ["sess-2", "sess-1", "sess-0"]
        rows = await backend.select_sessions_ordered(limit=10, offset=0,
                                                     status="active")
        assert [r["session_id"] for r in rows] == ["sess-2", "sess-0"]


# ---------------------------------------------------------------------------
# Factory + lifecycle
# ---------------------------------------------------------------------------

class TestFactoryAndLifecycle:
    async def test_create_builds_pool_with_hardening_settings(self, monkeypatch):
        pool = FakePool()
        captured = {}

        fake_asyncpg = types.ModuleType("asyncpg")

        async def create_pool(dsn, **kwargs):
            captured["dsn"] = dsn
            captured.update(kwargs)
            return pool

        fake_asyncpg.create_pool = create_pool
        monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

        backend = await PostgreSQLBackend.create("postgres://h/db")
        assert captured["dsn"] == "postgres://h/db"
        assert captured["min_size"] == 1
        assert captured["max_size"] == 5
        assert captured["command_timeout"] == 30
        assert captured["server_settings"] == {"statement_timeout": "30000"}
        assert backend._pool is pool
        await backend.close()

    async def test_create_closes_pool_when_schema_init_fails(self, monkeypatch):
        pool = FakePool()

        fake_asyncpg = types.ModuleType("asyncpg")

        async def create_pool(dsn, **kwargs):
            return pool

        fake_asyncpg.create_pool = create_pool
        monkeypatch.setitem(sys.modules, "asyncpg", fake_asyncpg)

        async def boom(self):
            raise RuntimeError("ddl failed")

        monkeypatch.setattr(PostgreSQLBackend, "ensure_schema", boom)
        with pytest.raises(RuntimeError, match="ddl failed"):
            await PostgreSQLBackend.create("postgres://h/db")
        assert pool.closed is True  # no leaked pool

    async def test_close_is_idempotent(self, backend):
        await backend.close()
        await backend.close()
        assert backend._pool is None


# ---------------------------------------------------------------------------
# get_task_store routing (postgresql branch)
# ---------------------------------------------------------------------------

class TestGetTaskStoreRouting:
    async def test_postgresql_branch_uses_pg_backend(self, monkeypatch):
        import chaos_agent.persistence.task_store as mod

        fake_backend = PostgreSQLBackend(FakePool())

        async def fake_create(dsn):
            fake_create.dsn = dsn
            return fake_backend

        monkeypatch.setattr(PostgreSQLBackend, "create",
                            classmethod(lambda cls, dsn: fake_create(dsn)))
        monkeypatch.setattr(mod, "_store", None)
        monkeypatch.setattr(mod.settings, "tasks_db_backend", "postgresql")
        monkeypatch.setattr(mod.settings, "tasks_pg_dsn", "postgres://h/db")
        try:
            store = await mod.get_task_store()
            assert store._backend is fake_backend
            assert fake_create.dsn == "postgres://h/db"
        finally:
            await mod.reset_task_store()

    async def test_postgresql_branch_requires_dsn(self, monkeypatch):
        import chaos_agent.persistence.task_store as mod

        monkeypatch.setattr(mod, "_store", None)
        monkeypatch.setattr(mod.settings, "tasks_db_backend", "postgresql")
        monkeypatch.setattr(mod.settings, "tasks_pg_dsn", "")
        with pytest.raises(ValueError, match="tasks_pg_dsn"):
            await mod.get_task_store()
        assert mod._store is None
