"""Tests for the async persistent TaskStore (SQLiteBackend)."""

import json
from pathlib import Path

import pytest
import pytest_asyncio

from chaos_agent.persistence.task_store import TaskStore, reset_task_store
from chaos_agent.persistence.task_store_backend import (
    _extract_index_fields,
    _set_timestamps,
)
from chaos_agent.persistence.task_store_sqlite import SQLiteBackend


@pytest_asyncio.fixture
async def backend(tmp_path):
    """Create a fresh SQLiteBackend with a temp DB."""
    b = await SQLiteBackend.create(db_path=tmp_path / "tasks.db")
    yield b
    await b.close()


@pytest_asyncio.fixture
async def store(backend):
    """Create a fresh TaskStore with a SQLiteBackend."""
    return TaskStore(backend=backend)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

class TestSchema:
    @pytest.mark.asyncio
    async def test_creates_tables_on_first_use(self, backend):
        """Tables and indexes should exist after schema init."""
        conn = backend._conn
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_details'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='task_spans'"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 1
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN "
            "('uk_tasks_task_id', 'idx_tasks_task_state', 'idx_tasks_namespace', "
            "'uk_task_details_task_id', 'idx_task_spans_task_id')"
        )
        rows = await cursor.fetchall()
        assert len(rows) == 5

    @pytest.mark.asyncio
    async def test_tasks_table_has_required_columns(self, backend):
        conn = backend._conn
        cursor = await conn.execute("PRAGMA table_info(tasks)")
        rows = await cursor.fetchall()
        col_names = {r[1] for r in rows}
        assert "id" in col_names
        assert "gmt_create" in col_names
        assert "gmt_modified" in col_names
        assert "namespace" in col_names
        assert "target_name" in col_names

    @pytest.mark.asyncio
    async def test_task_details_table_has_required_columns(self, backend):
        conn = backend._conn
        cursor = await conn.execute("PRAGMA table_info(task_details)")
        rows = await cursor.fetchall()
        col_names = {r[1] for r in rows}
        assert "id" in col_names
        assert "gmt_create" in col_names
        assert "gmt_modified" in col_names
        assert "fault_spec" in col_names
        # LLM model frozen at task finalize — the metric chain reads it
        # from task_details; without the column the drill's model is
        # invisible in every review surface.
        assert "model_name" in col_names
        # 六列已并入 DDL（fresh 库直建终态列集），同时启动迁移段的
        # ALTER 兑旧库双保险——两者不冲突：DDL 管新库，ALTER 管旧库。
        for migrated in ("baseline_data", "inject_context", "skill_use_case",
                         "injection_method", "kubectl_exec_pod_name",
                         "injection_start_time"):
            assert migrated in col_names, migrated
        # Round-32/32b — 负债账本列组：双翼 + combo 判别器。判别器
        # 闸门翼平衡的终审权（may_carry_live_fault A2），列缺失会让
        # 新行写不进 marker、旧库永远 NULL（保守但永不修复 C2）。
        for r32 in ("owned_experiment_uids", "retired_experiment_uids",
                    "combo_native_issued"):
            assert r32 in col_names, r32

    @pytest.mark.asyncio
    async def test_legacy_blade_uid_column_renamed_on_startup(self, tmp_path):
        """九期前旧库（tasks.blade_uid）：首次启动 RENAME 迁移生效一次——
        列名翻新、存量数据保留、随后 experiment_uid 的 upsert 不炸；
        第二次启动零动作（幂等，靠 ALTER 自身失败探列，无版本号）。"""
        import sqlite3

        db_path = tmp_path / "legacy.db"
        # 九期前真实形态：blade_uid 列名，无 tenant_id（由迁移段 ALTER 补）；
        # namespace/target_name 等列必须在——DDL 的 CREATE INDEX 依赖它们。
        raw = sqlite3.connect(db_path)
        raw.executescript(
            "CREATE TABLE tasks ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id TEXT NOT NULL,"
            "  task_state TEXT NOT NULL DEFAULT 'injecting',"
            "  stage TEXT NOT NULL DEFAULT 'injection',"
            "  phase TEXT NOT NULL DEFAULT 'planning',"
            "  operation TEXT NOT NULL DEFAULT 'inject',"
            "  skill_name TEXT,"
            "  blade_uid TEXT,"
            "  namespace TEXT,"
            "  target_name TEXT,"
            "  error TEXT,"
            "  finished_at TEXT,"
            "  duration_ms INTEGER DEFAULT 0,"
            "  gmt_create TEXT,"
            "  gmt_modified TEXT"
            ");"
        )
        raw.execute(
            "INSERT INTO tasks (task_id, task_state, blade_uid)"
            " VALUES ('task-legacy', 'injected', 'uid-old')"
        )
        raw.commit()
        raw.close()

        backend = await SQLiteBackend.create(db_path=db_path)
        try:
            cursor = await backend._conn.execute("PRAGMA table_info(tasks)")
            cols = {r[1] for r in await cursor.fetchall()}
            assert "experiment_uid" in cols
            assert "blade_uid" not in cols
            # 存量数据随列名翻新保留
            row = await backend.select_task("task-legacy")
            assert row["experiment_uid"] == "uid-old"
            # 迁移后新写入不再报 no such column
            await backend.upsert_task(
                "task-legacy",
                ["task_id", "experiment_uid"],
                ["task-legacy", "uid-new"],
            )
            row = await backend.select_task("task-legacy")
            assert row["experiment_uid"] == "uid-new"
        finally:
            await backend.close()

        # 第二次启动：列已改名，RENAME raise 被吞，无重复动作
        backend = await SQLiteBackend.create(db_path=db_path)
        try:
            row = await backend.select_task("task-legacy")
            assert row["experiment_uid"] == "uid-new"
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_injection_start_time_backfill_one_shot(self, tmp_path):
        """旧库缺 injection_start_time 列：首次启动补列 + 存量有意图行
        一次性回填；迁移后新插入的行永不被碰（重复回填会给「已确认
        但从未发出命令」的新行盖时间戳，永久废掉 select_active_tasks
        的「已发出」判据）。"""
        import sqlite3

        db_path = tmp_path / "backfill.db"
        # R18 时代形态：task_details 缺 injection_start_time；tasks 的
        # namespace/target_name 列必须在（DDL 索引依赖）。
        raw = sqlite3.connect(db_path)
        raw.executescript(
            "CREATE TABLE tasks ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id TEXT NOT NULL,"
            "  task_state TEXT NOT NULL DEFAULT 'injecting',"
            "  namespace TEXT,"
            "  target_name TEXT,"
            "  gmt_create TEXT"
            ");"
            "CREATE TABLE task_details ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id TEXT NOT NULL,"
            "  target TEXT,"
            "  gmt_create TEXT"
            ");"
        )
        raw.execute(
            "INSERT INTO tasks (task_id, task_state, gmt_create)"
            " VALUES ('task-legacy', 'injected', '2026-08-01T00:00:00')"
        )
        raw.execute(
            "INSERT INTO task_details (task_id, target, gmt_create)"
            " VALUES ('task-legacy', 'app=legacy', '2026-08-01T00:00:00')"
        )
        raw.commit()
        raw.close()

        backend = await SQLiteBackend.create(db_path=db_path)
        try:
            legacy = await backend.select_details("task-legacy")
            assert legacy["injection_start_time"] == "2026-08-01T00:00:00"

            # 迁移后新插入的行：有意图但从未发出命令 → 不被回填
            await backend.upsert_details(
                "task-new", ["task_id", "target"], ["task-new", "app=new"]
            )
        finally:
            await backend.close()

        # 第二次启动：ALTER raise → 回填短路，新行保持 NULL
        backend = await SQLiteBackend.create(db_path=db_path)
        try:
            new = await backend.select_details("task-new")
            assert new.get("injection_start_time") is None
            legacy = await backend.select_details("task-legacy")
            assert legacy["injection_start_time"] == "2026-08-01T00:00:00"
        finally:
            await backend.close()


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    @pytest.mark.asyncio
    async def test_insert_new_task(self, store):
        await store.upsert("task-t1", skill_name="pod-kill", operation="inject")
        data = await store.get("task-t1")
        assert data is not None
        assert data["skill_name"] == "pod-kill"
        assert data["operation"] == "inject"
        assert data["task_state"] == "injecting"

    @pytest.mark.asyncio
    async def test_update_existing_task(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.upsert("task-t1", experiment_uid="abc123")
        data = await store.get("task-t1")
        assert data["skill_name"] == "pod-kill"
        assert data["experiment_uid"] == "abc123"

    @pytest.mark.asyncio
    async def test_partial_update_preserves_other_fields(self, store):
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc")
        await store.upsert("task-t1", safety_status="safe")
        data = await store.get("task-t1")
        assert data["skill_name"] == "pod-kill"
        assert data["experiment_uid"] == "abc"
        assert data["safety_status"] == "safe"

    @pytest.mark.asyncio
    async def test_gmt_modified_is_set(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        data = await store.get("task-t1")
        assert data["gmt_modified"] is not None
        assert data["gmt_modified"] != ""

    @pytest.mark.asyncio
    async def test_gmt_create_preserved_on_update(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        data1 = await store.get("task-t1")
        gmt_create_1 = data1["gmt_create"]
        await store.upsert("task-t1", experiment_uid="abc")
        data2 = await store.get("task-t1")
        assert data2["gmt_create"] == gmt_create_1

    @pytest.mark.asyncio
    async def test_empty_task_id_is_noop(self, store):
        await store.upsert("", skill_name="pod-kill")
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_json_fields_serialized(self, store):
        target = {"namespace": "default", "names": ["pod1"], "resource_type": "pod"}
        await store.upsert("task-t1", target=target)
        data = await store.get("task-t1")
        assert data["target"] == target

    @pytest.mark.asyncio
    async def test_verification_json_roundtrip(self, store):
        verification = {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        }
        await store.upsert("task-t1", verification=verification, experiment_uid="abc")
        data = await store.get("task-t1")
        assert data["verification"] == verification
        assert data["task_state"] == "injected"

    @pytest.mark.asyncio
    async def test_namespace_target_name_extracted(self, store):
        target = {"namespace": "prod", "names": ["pod1"], "resource_type": "pod"}
        await store.upsert("task-t1", target=target)
        data = await store.get("task-t1")
        assert data["namespace"] == "prod"
        assert data["target_name"] == "pod1"

    @pytest.mark.asyncio
    async def test_update_task_state_updates_in_place_no_ghost_row(self, store):
        """Regression: task_id must be part of the upsert column list.

        Omitting it turned the upsert into a plain INSERT, creating a
        NULL-task_id ghost row instead of updating the task (and blowing
        up on PostgreSQL with a NOT NULL violation).
        """
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.update_task_state("task-t1", "recovering")
        data = await store.get("task-t1")
        assert data["task_state"] == "recovering"
        assert await store.count() == 1  # no ghost row

    @pytest.mark.asyncio
    async def test_update_task_state_skip_if_terminal_keeps_own_verdict(self, store):
        """Round-54 G6: a guarded abort write must never regress a row that
        already reached its own terminal word.

        The abort exits' row write (write_aborted_task_row) races the run's
        own tail writers: a cancel landing during result extraction, after
        the pipeline completed, used to rewrite "completed" into
        "cancelled". The run that finished keeps its own verdict; the
        abort word is only for runs whose graph never got to finish.
        """
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.update_task_state("task-t1", "completed")

        landed = await store.update_task_state(
            "task-t1", "cancelled", skip_if_terminal=True,
        )

        assert landed is False
        data = await store.get("task-t1")
        assert data["task_state"] == "completed", (
            "a completed run's verdict must survive a late abort write"
        )

    @pytest.mark.asyncio
    async def test_update_task_state_skip_if_terminal_writes_mid_flight_row(self, store):
        """The guard is scoped to terminal words only: a mid-flight row
        (the abort exits' actual target) still takes the abort word."""
        await store.upsert("task-t1", skill_name="pod-kill")
        # upsert's inference keeps an unevidenced row at "injecting" — the
        # mid-graph upsert a real abort interrupts.

        landed = await store.update_task_state(
            "task-t1", "cancelled", skip_if_terminal=True,
        )

        assert landed is True
        data = await store.get("task-t1")
        assert data["task_state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_update_task_state_default_keeps_legacy_overwrite_semantics(self, store):
        """The guard is opt-in: the non-abort write paths (the recover
        flow's own verdict upgrades — failed → recovered on the SAME row)
        must keep the plain overwrite semantics."""
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.update_task_state("task-t1", "failed")

        landed = await store.update_task_state("task-t1", "recovered")

        assert landed is True
        data = await store.get("task-t1")
        assert data["task_state"] == "recovered"


# ---------------------------------------------------------------------------
# Infer fields
# ---------------------------------------------------------------------------

class TestInferFields:
    @pytest.mark.asyncio
    async def test_injecting_state_inferred(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        data = await store.get("task-t1")
        assert data["task_state"] == "injecting"
        assert data["stage"] == "injection"
        assert data["phase"] == "planning"

    @pytest.mark.asyncio
    async def test_injected_state_inferred(self, store):
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc", verification=verification)
        data = await store.get("task-t1")
        assert data["task_state"] == "injected"
        assert data["phase"] == "verification_passed"

    @pytest.mark.asyncio
    async def test_rejected_state_inferred(self, store):
        await store.upsert("task-t1", safety_status="rejected", safety_reason="unsafe")
        data = await store.get("task-t1")
        assert data["task_state"] == "rejected"

    @pytest.mark.asyncio
    async def test_failed_state_inferred(self, store):
        await store.upsert("task-t1", error="something went wrong")
        data = await store.get("task-t1")
        assert data["task_state"] == "failed"

    @pytest.mark.asyncio
    async def test_recovered_state_inferred(self, store):
        recover_verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", operation="recover",
                           recover_verification=recover_verification,
                           result={"recovered": True})
        data = await store.get("task-t1")
        assert data["task_state"] == "recovered"
        assert data["stage"] == "recovery"


class TestInferenceRegressionGuard:
    """inject-9bf2dddd: a verified task was re-projected back to
    ``injecting`` by the final tracer flush. The flush carries no lifecycle
    fields, and inference used to merge only the ``tasks`` row — which has
    no ``verification`` column (it lives in ``task_details``) — so it saw
    "experiment_uid without verification" and returned the ``injecting``
    fallback over the stored ``injected`` verdict. Two defenses:
    (1) inference merges the FULL logical record (tasks + details);
    (2) a terminal verdict never regresses to the ``injecting`` fallback.
    """

    @pytest.mark.asyncio
    async def test_fieldless_upsert_keeps_injected(self, store):
        """tracer._persist_span style: upsert(task_id) with no fields."""
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc",
                           verification=verification)
        assert (await store.get("task-t1"))["task_state"] == "injected"
        await store.upsert("task-t1")  # field-less flush
        data = await store.get("task-t1")
        assert data["task_state"] == "injected"
        assert data["phase"] == "verification_passed"

    @pytest.mark.asyncio
    async def test_summary_only_upsert_keeps_injected(self, store):
        """tracer._persist_summary style: metrics fields, no lifecycle."""
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc",
                           verification=verification)
        await store.upsert("task-t1", total_token_input=100, total_llm_calls=3)
        data = await store.get("task-t1")
        assert data["task_state"] == "injected"
        assert data["total_token_input"] == 100

    @pytest.mark.asyncio
    async def test_terminal_state_never_regresses_to_injecting(self, store):
        """Monotonicity guard alone: verdict on record, no lifecycle fields
        anywhere in the merged record (legacy/corrupted shape)."""
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc")
        await store.update_task_state("task-t1", "injected")  # verdict, as-is
        await store.upsert("task-t1", total_llm_calls=1)  # re-projection
        assert (await store.get("task-t1"))["task_state"] == "injected"

    @pytest.mark.asyncio
    async def test_recovery_transition_not_blocked_by_guard(self, store):
        """The guard blocks regressions to the fallback only — a genuine
        injected -> recovered transition must still go through."""
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc",
                           verification=verification)
        recover_verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("task-t1", operation="recover",
                           recover_verification=recover_verification,
                           result={"recovered": True})
        assert (await store.get("task-t1"))["task_state"] == "recovered"


# ---------------------------------------------------------------------------
# Get / List / Count
# ---------------------------------------------------------------------------

class TestGetListCount:
    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store):
        assert await store.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_returns_ordered_by_gmt_create_desc(self, store):
        await store.upsert("task-t1", gmt_create="2026-01-01T00:00:00Z")
        await store.upsert("task-t2", gmt_create="2026-01-02T00:00:00Z")
        await store.upsert("task-t3", gmt_create="2026-01-03T00:00:00Z")
        result = await store.list_tasks()
        assert [d["task_id"] for d in result] == ["task-t3", "task-t2", "task-t1"]

    @pytest.mark.asyncio
    async def test_list_with_state_filter(self, store):
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="a",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.upsert("task-t2", skill_name="pod-kill")
        injected = await store.list_tasks(task_state="injected")
        assert len(injected) == 1
        assert injected[0]["task_id"] == "task-t1"

    @pytest.mark.asyncio
    async def test_list_with_limit_offset(self, store):
        for i in range(5):
            await store.upsert(f"task-t{i}", gmt_create=f"2026-01-0{i+1}T00:00:00Z")
        result = await store.list_tasks(limit=2, offset=1)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_count_all(self, store):
        await store.upsert("task-t1")
        await store.upsert("task-t2")
        assert await store.count() == 2

    @pytest.mark.asyncio
    async def test_count_by_state(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.upsert("task-t2", error="fail")
        assert await store.count(task_state="injecting") == 1
        assert await store.count(task_state="failed") == 1


# ---------------------------------------------------------------------------
# Query active
# ---------------------------------------------------------------------------

class TestQueryActive:
    """``query_active`` 只返回**可恢复**的实验 —— 判据两层，缺一不可：

    1. 落过注入意图：``target``（遗留形态）或 ``fault_spec``（规范形态）非空；
    2. 命令确已发出：``injection_start_time`` 非空。

    因此本类中每个"应可见"的行都必须同时给出这两者。下方
    ``_ISSUED`` 代表"注入命令已发出"这个事实，集中一处以免逐个用例遗漏。
    """

    # 「注入命令已发出」的时刻。真实流程由 execute_loop 在
    # 命令发出瞬间写入（写一次、永不清零）。
    _ISSUED = "2026-07-27T10:00:00+08:00"

    @pytest.mark.asyncio
    async def test_returns_injecting_and_injected(self, store):
        # 只写 skill_name 的空行按设计就该被排除 —— 它没有任何东西可回滚。
        await store.upsert("task-t1", skill_name="pod-kill",
                           target={"namespace": "default", "names": ["pod1"]},
                           injection_start_time=self._ISSUED)
        await store.upsert("task-t2", skill_name="pod-kill", experiment_uid="a",
                           target={"namespace": "default", "names": ["pod2"]},
                           injection_start_time=self._ISSUED,
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.upsert("task-t3", error="fail")
        active = await store.query_active()
        assert len(active) == 2
        task_ids = {r["task_id"] for r in active}
        assert "task-t1" in task_ids
        assert "task-t2" in task_ids

    @pytest.mark.asyncio
    async def test_excludes_rows_without_injection_intent(self, store):
        """没有注入意图的行不算可恢复实验（既无 target 也无 fault_spec）。

        这类行出现在「任务身份已分配但还没提交故障意图」的窗口里，
        没有任何副作用可回滚；若进入可恢复列表会被恢复流程误选，
        报「找不到该任务的注入状态记录」。
        """
        await store.upsert("task-empty", skill_name="pod-kill")
        assert await store.query_active() == []

    @pytest.mark.asyncio
    async def test_excludes_intent_without_issued_command(self, store):
        """有完整注入意图、但命令**从未发出**的行必须被排除。

        场景：用户确认了故障方案（于是分配 task_id、落了 target/fault_spec），
        但注入命令还没发出就中断了（安全门未过 / 超时 / 取消）。这类行没有
        任何副作用可回滚，进入可恢复列表就会报「找不到该任务的注入状态记录」。

        ``injection_start_time`` 是唯一可靠的"已发出"信号：
        ``blade_uid`` 对 native 类天然为空、``injection_method`` 会被
        execute_loop 的多步自检分支置回 None、``safety_status`` 的 schema
        默认值就是 'pending'（"真卡住"与"没写过"同形）。
        """
        await store.upsert(
            "task-not-issued", skill_name="pod-kill",
            target={"namespace": "default", "names": ["pod1"]},
            fault_spec={"namespace": "default", "scope": "pod", "names": ["pod1"],
                        "fault_target": "network", "fault_action": "loss"},
        )
        assert await store.query_active() == []

    @pytest.mark.asyncio
    async def test_native_injection_without_blade_uid_is_recoverable(self, store):
        """kubectl_native / host_native 天然无 blade_uid，但必须可恢复。

        它们的"发出即注入"由 injection_start_time 承载（execute_loop 在
        issue time 就写入）。判据若依赖 blade_uid 就会漏掉这类真实注入。
        """
        await store.upsert(
            "task-native", skill_name="k8s-chaos-skills",
            target={"namespace": "default", "names": ["pod1"]},
            injection_method="kubectl_native",
            injection_start_time=self._ISSUED,
        )
        active = await store.query_active()
        assert [r["task_id"] for r in active] == ["task-native"]

    @pytest.mark.asyncio
    async def test_canonical_fault_spec_alone_is_recoverable(self, store):
        """只写规范形态 fault_spec（未投影出遗留 target）也必须可恢复。

        cli/runner.py 的两处直接 upsert 绕过了 _store_sync 的
        fault_spec → target 投影，落库后只有 fault_spec。判据若只认
        ``target`` 就会把这类真实注入误藏成幽灵。
        """
        await store.upsert(
            "task-canonical",
            fault_spec={"namespace": "prod", "scope": "pod", "names": ["pod1"],
                        "fault_target": "network", "fault_action": "loss"},
            skill_name="pod-network-loss",
            injection_start_time=self._ISSUED,
        )
        active = await store.query_active()
        assert [r["task_id"] for r in active] == ["task-canonical"]

    @pytest.mark.asyncio
    async def test_filter_by_namespace(self, store):
        await store.upsert("task-t1", target={"namespace": "prod", "names": ["pod1"]},
                           skill_name="pod-kill", injection_start_time=self._ISSUED)
        await store.upsert("task-t2", target={"namespace": "staging", "names": ["pod2"]},
                           skill_name="pod-kill", injection_start_time=self._ISSUED)
        active = await store.query_active(namespace="prod")
        assert len(active) == 1
        assert active[0]["task_id"] == "task-t1"

    @pytest.mark.asyncio
    async def test_filter_by_target_name(self, store):
        """target_name column stores names[0] from the target JSON."""
        await store.upsert("task-t1", target={"namespace": "prod", "names": ["pod1"]},
                           skill_name="pod-kill", injection_start_time=self._ISSUED)
        await store.upsert("task-t2", target={"namespace": "prod", "names": ["pod2"]},
                           skill_name="pod-kill", injection_start_time=self._ISSUED)
        active = await store.query_active(target_name="pod1")
        assert len(active) == 1
        assert active[0]["task_id"] == "task-t1"

    @pytest.mark.asyncio
    async def test_filter_by_fault_spec_projection(self, store):
        fault_spec = {
            "namespace": "prod",
            "scope": "pod",
            "names": ["pod1"],
            "labels": {},
            "fault_target": "network",
            "fault_action": "loss",
            "params": {"percent": "100"},
        }
        await store.upsert("task-t1", fault_spec=fault_spec, skill_name="stale-active-skill",
                           injection_start_time=self._ISSUED)

        active = await store.query_active(namespace="prod", target_name="pod1")

        assert len(active) == 1
        assert active[0]["task_id"] == "task-t1"
        assert active[0]["fault_type"] == "pod-network-loss"
        assert active[0]["skill"] == "stale-active-skill"

    @pytest.mark.asyncio
    async def test_compatible_format(self, store):
        await store.upsert("task-t1", skill_name="pod-kill", target={"namespace": "default"},
                           experiment_uid="abc", injection_start_time=self._ISSUED)
        active = await store.query_active()
        record = active[0]
        assert "task_id" in record
        assert "operation" in record
        assert "skill" in record
        assert "fault_type" in record
        assert "target" in record
        assert "params" in record
        assert "experiment_uid" in record
        assert "status" in record

    @pytest.mark.asyncio
    async def test_active_includes_discriminators(self, store):
        """query_active surfaces the fields needed to tell experiments apart:
        gmt_create (time), target_name (resource), plan_summary (description)."""
        await store.upsert(
            "task-t1",
            skill_name="k8s-chaos-skills",
            target={"namespace": "reg-center", "names": ["registry-sts"]},
            plan_summary="将 StatefulSet registry-sts 镜像改为无效值",
            injection_start_time=self._ISSUED,
        )
        active = await store.query_active()
        record = active[0]
        assert "gmt_create" in record and record["gmt_create"]
        assert record["target_name"] == "registry-sts"
        assert record["plan_summary"] == "将 StatefulSet registry-sts 镜像改为无效值"

    @pytest.mark.asyncio
    async def test_unverified_inject_stays_recoverable(self, store):
        """End-to-end: an inject run that ends 'unverified' (verification
        ran, no conclusion) lands task_state='unverified' in the row — and
        MUST remain in the recoverable set. The command was issued, so the
        fault is likely still live on the cluster; dropping it from
        query_active would strand a live fault with no recovery entry
        (fail-closed: not-knowing is not evidence-of-absence)."""
        await store.upsert(
            "task-unv",
            target={"namespace": "reg-center", "names": ["registry-sts"]},
            injection_start_time=self._ISSUED,
            verification={
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        )
        data = await store.get("task-unv")
        assert data["task_state"] == "unverified"
        active = await store.query_active()
        assert "task-unv" in {t["task_id"] for t in active}

    @pytest.mark.asyncio
    async def test_unverified_is_terminal_no_regression_to_injecting(self, store):
        """Monotonicity guard: a later field-less flush (tracer-style
        upsert with no lifecycle evidence) must not regress a finished
        'unverified' run back to the 'injecting' fallback — that would show
        an ended run as in-flight."""
        await store.update_task_state("task-unv2", "unverified")
        # Field-less flush: no verification / result / intent evidence.
        await store.upsert("task-unv2", experiment_uid="uid-x")
        data = await store.get("task-unv2")
        assert data["task_state"] == "unverified"

    # ------------------------------------------------------------------
    # Workspace isolation（方案 A：workspace 列下沉 SDK 任务库）
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_workspace_filter_scopes_discovery_set(self, store):
        """workspace 轴过滤：李四的 query 只回自己空间的行 ——
        事故场景的直接断言（526255 的行不在李四的发现集里）。"""
        await store.upsert("task-a", skill_name="pod-kill",
                           target={"namespace": "default", "names": ["p1"]},
                           injection_start_time=self._ISSUED, workspace_id="ws-lisi")
        await store.upsert("task-b", skill_name="pod-kill",
                           target={"namespace": "default", "names": ["p2"]},
                           injection_start_time=self._ISSUED,
                           workspace_id="ws-526255")
        active = await store.query_active(workspace_id="ws-lisi")
        assert [r["task_id"] for r in active] == ["task-a"]

    @pytest.mark.asyncio
    async def test_empty_workspace_means_unfiltered(self, store):
        """空值 = 不过滤 —— 本地 CLI / 裸 SDK 入口的零回归契约：
        本地库所有行 workspace 为空串，查询行为与列存在前逐字节一致。"""
        await store.upsert("task-a", skill_name="pod-kill",
                           target={"namespace": "default", "names": ["p1"]},
                           injection_start_time=self._ISSUED)
        await store.upsert("task-b", skill_name="pod-kill",
                           target={"namespace": "default", "names": ["p2"]},
                           injection_start_time=self._ISSUED,
                           workspace_id="ws-x")
        # 不传 → 全量（含未标注空间的行）
        assert len(await store.query_active()) == 2
        # 显式空串 → 同样全量（falsy 不过滤，与 tenant_id 同构）
        assert len(await store.query_active(workspace_id="")) == 2

    @pytest.mark.asyncio
    async def test_tenant_and_workspace_compose(self, store):
        """双轴组合：同租户（同组织）不同空间的行互不可见 ——
        事故形态（realm 级 tenant 相同、workspace 不同）。"""
        for tid, ws in (("task-a", "ws-1"), ("task-b", "ws-2")):
            await store.upsert(tid, skill_name="pod-kill",
                               target={"namespace": "default", "names": ["p"]},
                               injection_start_time=self._ISSUED,
                               tenant_id="t-org", workspace_id=ws)
        assert [r["task_id"] for r in await store.query_active(
            tenant_id="t-org", workspace_id="ws-1")] == ["task-a"]
        assert [r["task_id"] for r in await store.query_active(
            tenant_id="t-org", workspace_id="ws-2")] == ["task-b"]


# ---------------------------------------------------------------------------
# Schema migration — workspace_id column (方案 A)
# ---------------------------------------------------------------------------

class TestWorkspaceMigration:
    @pytest.mark.asyncio
    async def test_legacy_db_gains_workspace_column_idempotently(self, tmp_path):
        """旧库（有 tenant_id、无 workspace_id）：启动迁移 ALTER 补列、
        存量行默认空串（= 不过滤，行为不变）、二次启动幂等存活。"""
        import sqlite3

        db_path = tmp_path / "legacy-ws.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(
            "CREATE TABLE tasks ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id TEXT NOT NULL,"
            "  task_state TEXT NOT NULL DEFAULT 'injecting',"
            "  stage TEXT NOT NULL DEFAULT 'injection',"
            "  phase TEXT NOT NULL DEFAULT 'planning',"
            "  operation TEXT NOT NULL DEFAULT 'inject',"
            "  skill_name TEXT,"
            "  experiment_uid TEXT,"
            "  namespace TEXT,"
            "  target_name TEXT,"
            "  tenant_id TEXT DEFAULT '',"
            "  liability_live INTEGER NOT NULL DEFAULT 0,"
            "  error TEXT,"
            "  finished_at TEXT,"
            "  duration_ms INTEGER DEFAULT 0,"
            "  gmt_create TEXT,"
            "  gmt_modified TEXT"
            ");"
        )
        raw.execute(
            "INSERT INTO tasks (task_id, task_state, tenant_id)"
            " VALUES ('task-old', 'injected', 't-org')"
        )
        raw.commit()
        raw.close()

        b = await SQLiteBackend.create(db_path=db_path)
        try:
            conn = b._conn
            cursor = await conn.execute("PRAGMA table_info(tasks)")
            col_names = {r[1] for r in await cursor.fetchall()}
            assert "workspace_id" in col_names
            # 存量行默认空串：不过滤契约让旧行保持全可见
            cursor = await conn.execute(
                "SELECT workspace_id FROM tasks WHERE task_id = 'task-old'")
            assert (await cursor.fetchone())[0] == ""
        finally:
            await b.close()
        # 二次启动：ALTER 因列已存在而 raise 被吞，迁移幂等
        b2 = await SQLiteBackend.create(db_path=db_path)
        await b2.close()


# ---------------------------------------------------------------------------
# Round-32 — the row-level liability ledger
# ---------------------------------------------------------------------------

class TestLiabilityLedger:
    """Round-32 根因修复的测试锚：可恢复性不再从 task_state **词**里猜，
    而是键在行级账本（owned/retired 双翼 + 物化列 liability_live，
    写侧单源谓词 may_carry_live_fault）。

    旧词谓词的两个推导错误（K1 recovering 孤儿行、K2
    failed-with-experiment）让确定活着的故障永久失明；本类钉三件事：
    谓词三分支、双翼单调合并、K1/K2 的端到端修复验证。"""

    _ISSUED = "2026-09-16T10:00:00+08:00"

    # -- single-source predicate: three branches --------------------------

    def test_branch_a_unbalanced_wings_mean_live(self):
        """A 支：账本翼不平衡（owned − retired 非空）→ 活。账本是权威：
        即使词读 'recovered'（判决面），未销毁的命名实验仍是活负债。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "recovered",
            "owned_experiment_uids": '["uid-1"]',
            "retired_experiment_uids": '[]',
        }) is True

    def test_branch_a_balanced_wings_fall_through_to_b(self):
        """A 支翼平衡 → 落 B；B 无 committed 证据 → 死。清算后的空负债
        不该因翼的存史而永久活。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "recovered",
            "owned_experiment_uids": '["uid-1"]',
            "retired_experiment_uids": '["uid-1"]',
        }) is False

    def test_branch_b_committed_fallback_without_ledger(self):
        """B 支：无账本（遗留行）但有 issued 证据 → 活；直到
        recover_verification/result 给出全恢复证明才清算。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        committed = {
            "task_state": "failed",
            "target": '{"names": ["pod1"]}',
            "injection_start_time": self._ISSUED,
        }
        assert may_carry_live_fault(committed) is True
        # fully-cleared recover proof settles the legacy row
        settled = dict(committed, recover_verification='{"level": "recovered"}')
        assert may_carry_live_fault(settled) is False
        # the result-dict shape of the same proof settles too
        settled2 = dict(committed, result='{"recovered": true, "recovery_level": "recovered"}')
        assert may_carry_live_fault(settled2) is False

    def test_branch_c_no_injection_intent_is_dead(self):
        """C 支：无账本、无 committed 证据 → 死（新生行/裸锚行）。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({"task_state": "injecting"}) is False
        assert may_carry_live_fault({}) is False

    # -- ledger wings roundtrip + monotonic merge -------------------------

    @pytest.mark.asyncio
    async def test_ledger_wings_roundtrip_through_store(self, store):
        """双翼以 JSON list 落库、以 list 读回 —— 账本的持久面。"""
        await store.upsert(
            "task-ledger",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1", "uid-2"],
            retired_experiment_uids=["uid-0"],
        )
        data = await store.get("task-ledger")
        assert data["owned_experiment_uids"] == ["uid-1", "uid-2"]
        assert data["retired_experiment_uids"] == ["uid-0"]

    @pytest.mark.asyncio
    async def test_wing_union_merge_is_monotonic(self, store):
        """单调守卫：同步路径写的是全量 AgentState 快照，水合缺口
        （state.owned=None → 空 list）不能抹掉 DB 里已存的翼值 ——
        upsert 对双翼取并集，双翼立法上是 append-only。"""
        await store.upsert(
            "task-mono",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1"],
            retired_experiment_uids=["uid-0"],
        )
        # hydration-gap flush: BOTH wings arrive empty lists
        await store.upsert(
            "task-mono",
            owned_experiment_uids=[],
            retired_experiment_uids=[],
        )
        data = await store.get("task-mono")
        assert data["owned_experiment_uids"] == ["uid-1"]  # survived
        assert data["retired_experiment_uids"] == ["uid-0"]  # survived
        # and genuine appends accumulate (no clobber between writes)
        await store.upsert("task-mono", retired_experiment_uids=["uid-1"])
        data = await store.get("task-mono")
        assert data["retired_experiment_uids"] == ["uid-0", "uid-1"]

    # -- K1 / K2 end-to-end repair verification ---------------------------

    @pytest.mark.asyncio
    async def test_k1_recovering_midword_keeps_liability(self, store):
        """K1 修复验证：崩溃孤儿行读 'recovering'（恢复在途、无判决）——
        update_task_state 的中途词写**不清除无判决证明的负债**，
        query_active 仍返回它（旧词谓词的 ACTIVE 集不含 recovering，
        这正是孤儿行失明的根因）。"""
        await store.upsert(
            "task-orphan",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1"],
        )
        await store.update_task_state("task-orphan", "recovering")
        active = await store.query_active()
        assert "task-orphan" in {t["task_id"] for t in active}

    @pytest.mark.asyncio
    async def test_k2_failed_word_with_experiment_keeps_liability(self, store):
        """K2 修复验证：判决词 'failed' 但实验在册（owned 未清算）→
        账本说活。旧词谓词把 failed 排除在 ACTIVE 集外，确定活着的
        故障永久失明；新谓词的 A 支直接看翼。"""
        await store.upsert(
            "task-fail-live",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1"],
        )
        await store.update_task_state("task-fail-live", "failed")
        active = await store.query_active()
        assert "task-fail-live" in {t["task_id"] for t in active}

    @pytest.mark.asyncio
    async def test_full_recovery_settles_row_out_of_active(self, store):
        """镜像面：全恢复判决（死亡翼补全后 retired 吞掉 owned）→
        翼平衡 + recover 证明 → 行离开可恢复集。恢复了的行不再
        haunt query_active（K1 的逆向，同一根因：DB 侧死亡证据不全）。"""
        await store.upsert(
            "task-settled",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1"],
        )
        # the finalize death-wing write: full verdict retires all owned
        await store.upsert(
            "task-settled",
            retired_experiment_uids=["uid-1"],
            recover_verification={"level": "recovered"},
        )
        await store.update_task_state("task-settled", "recovered")
        active = await store.query_active()
        assert "task-settled" not in {t["task_id"] for t in active}

    # -- round-33b: CLEARED word + clearance verdict are one atomic fact ----

    @pytest.mark.asyncio
    async def test_uidless_native_recovered_without_verdict_stays_live(self, store):
        """幽灵行复现（fail-closed 保留）：UID-less kubectl-native 注入
        ——无 experiment_uid、两翼空，账本（A 支）结构性失明；但
        injection_start_time + target 证明命令确已发出（B 支
        ``_injection_was_issued``）。只写 CLEARED 词 'recovered' 而本行
        无清算裁决时，``_recovery_fully_cleared`` 无从证明已清 →
        负债仍活（liability_live=1），行留在可恢复集。这正是 round-33b
        前 inject 行的形状：词落到了 inject 行、裁决却留在 recover 行。"""
        await store.upsert(
            "task-native-ghost",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            # no experiment_uid, no owned/retired wings — UID-less native
        )
        await store.update_task_state("task-native-ghost", "recovered")
        active = await store.query_active()
        assert "task-native-ghost" in {t["task_id"] for t in active}

    @pytest.mark.asyncio
    async def test_recovered_word_with_verdict_clears_same_row(self, store):
        """通用修复：CLEARED 词与其清算裁决是同一次写、同一行的原子事实。
        ``update_task_state`` 现在携带 ``recover_verification``——裁决先落
        到本行，再据合并行证据重算 liability_live，词与裁决一起清。同一
        条 UID-less native 行（上一测的幽灵），带上 level=recovered 的裁决
        即离开可恢复集，且裁决确落在本行（非另立 recover 行）。"""
        await store.upsert(
            "task-native-cleared",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
        )
        await store.update_task_state(
            "task-native-cleared",
            "recovered",
            recover_verification={"level": "recovered"},
        )
        active = await store.query_active()
        assert "task-native-cleared" not in {t["task_id"] for t in active}
        # the verdict landed on THIS row — word + proof travel together
        row = await store.get("task-native-cleared")
        assert row["recover_verification"]["level"] == "recovered"

    @pytest.mark.asyncio
    async def test_partial_verdict_does_not_clear(self, store):
        """反向闩锁：裁决 level=partial 不是全清证明（``_recovery_fully_cleared``
        刻意排除 partial——部分恢复意味着至少一个故障可能仍活）。即便词写
        'recovered' 且带了裁决，partial 裁决也不放行，行仍是负债。防止把
        '带裁决' 误当成 '带清算证明'。"""
        await store.upsert(
            "task-native-partial",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
        )
        await store.update_task_state(
            "task-native-partial",
            "recovered",
            recover_verification={"level": "partial"},
        )
        active = await store.query_active()
        assert "task-native-partial" in {t["task_id"] for t in active}


class TestComboDiscriminator:
    """Round-32b C2 修复的测试锚：翼平衡对「纯实验行」拥有终审权。

    r32v2 审计坐实的级联缺陷：A 支翼平衡后无条件落 B，而 B 的
    committed 谓词对每个跑完的 blade 任务都投影 fault_handle ——
    于是被框架 sweep 清算过、但从未跑 recover 的任务永久 haunt
    query_active（boot 待处理卡刷满、恢复入口自动选中幽灵行）。

    修复引入 combo 判别器（combo_native_issued 落库三态）：
    false = 出生 seam 断言「只有实验」，翼平衡即死；true / NULL
    （遗留行）/ native 族归因（upgrade 漏标的 criterion-2 镜像）
    保 B 回退。配套闩锁：None 冲刷不抹、True 粘滞。"""

    _ISSUED = "2026-09-16T10:00:00+08:00"

    # -- A2 gate: three-leg discrimination ----------------------------------

    def test_a2_experiments_only_balanced_wing_settles_dead(self):
        """C2 核心：marker=false + 实验族归因 + 翼平衡 → 死。
        行自己的死亡记录（retired 翼）终审，不再被 committed
        形状回退否决。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "completed",
            "experiment_uid": "u1",
            "injection_method": "host_blade",
            "injection_start_time": self._ISSUED,
            "target": '{"namespace": "default", "names": ["pod1"]}',
            "owned_experiment_uids": '["u1"]',
            "retired_experiment_uids": '["u1"]',
            "combo_native_issued": "false",
        }) is False

    def test_a2_combo_marker_keeps_committed_fallback(self):
        """combo（native 半边可能未清算）→ 翼平衡后仍走 B → 活。
        JSON 字串形态（DB 读路径）与 bool 直传形态同判。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        row = {
            "task_state": "completed",
            "experiment_uid": "u1",
            "injection_method": "host_blade",
            "injection_start_time": self._ISSUED,
            "target": '{"namespace": "default", "names": ["pod1"]}',
            "owned_experiment_uids": '["u1"]',
            "retired_experiment_uids": '["u1"]',
        }
        assert may_carry_live_fault(dict(row, combo_native_issued="true")) is True
        assert may_carry_live_fault(dict(row, combo_native_issued=True)) is True

    def test_a2_legacy_null_marker_is_conservative(self):
        """遗留行（marker 从未落库）→ 未知不是否证 → 保 B 回退。
        猜一个 false 会假清算 marker 从未落地的 combo 行。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "completed",
            "experiment_uid": "u1",
            "injection_method": "host_blade",
            "injection_start_time": self._ISSUED,
            "target": '{"namespace": "default", "names": ["pod1"]}',
            "owned_experiment_uids": '["u1"]',
            "retired_experiment_uids": '["u1"]',
        }) is True

    def test_a2_native_attribution_beats_false_marker(self):
        """criterion-2 镜像：native 族归因 + 实验在册 = marker 漏标的
        combo 证据 → 保 B 回退（upgrade seam 可能漏掉重归因，留下
        method=kubectl_native + 活实验的行）。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "completed",
            "experiment_uid": "u1",
            "injection_method": "kubectl_native",
            "injection_start_time": self._ISSUED,
            "target": '{"namespace": "default", "names": ["pod1"]}',
            "owned_experiment_uids": '["u1"]',
            "retired_experiment_uids": '["u1"]',
            "combo_native_issued": "false",
        }) is True

    def test_a2_imbalance_still_has_no_appeal(self):
        """不平衡翼的 A 支无上诉（不因 marker 改变）——回归守卫。"""
        from chaos_agent.persistence.task_store import may_carry_live_fault

        assert may_carry_live_fault({
            "task_state": "recovered",
            "injection_method": "host_blade",
            "owned_experiment_uids": '["u1"]',
            "retired_experiment_uids": '[]',
            "combo_native_issued": "false",
        }) is True

    # -- end-to-end: the C2 ghost leaves query_active ----------------------

    @pytest.mark.asyncio
    async def test_swept_completed_task_leaves_query_active(self, store):
        """C2 端到端：注入成功、框架 sweep 已销毁全部实验、从未跑
        recover 的完成任务 → 离开可恢复集（幽灵死亡）。
        真实形态 = 出生 seam 写 false + retired 翼由 sweep 回写。"""
        await store.upsert(
            "task-swept",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            injection_method="host_blade",
            experiment_uid="uid-1",
            owned_experiment_uids=["uid-1"],
            combo_native_issued=False,
        )
        # framework-side sweep retires the experiment (no recover flow)
        await store.upsert(
            "task-swept",
            retired_experiment_uids=["uid-1"],
        )
        active = await store.query_active()
        assert "task-swept" not in {t["task_id"] for t in active}

    @pytest.mark.asyncio
    async def test_swept_combo_task_stays_recoverable(self, store):
        """镜像面：同上形状但 combo（native 半边）→ 留在可恢复集。"""
        await store.upsert(
            "task-swept-combo",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            injection_method="host_blade",
            experiment_uid="uid-1",
            owned_experiment_uids=["uid-1"],
            combo_native_issued=True,
        )
        await store.upsert(
            "task-swept-combo",
            retired_experiment_uids=["uid-1"],
        )
        active = await store.query_active()
        assert "task-swept-combo" in {t["task_id"] for t in active}

    # -- marker latch --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_marker_latch_none_flush_never_erases(self, store):
        """闩锁：None 冲刷（replan 清零 / 水合缺口）不抹掉已落库定值。"""
        await store.upsert(
            "task-latch",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            combo_native_issued=True,
        )
        await store.upsert("task-latch", combo_native_issued=None)
        data = await store.get("task-latch")
        assert data["combo_native_issued"] is True

    @pytest.mark.asyncio
    async def test_marker_latch_true_sticks_over_false(self, store):
        """闩锁：True 粘滞 —— 后到的 False（新 epoch 出生断言）不能把
        已 committed 的 combo 降级回「纯实验」（假清算防线）。"""
        await store.upsert(
            "task-sticky",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            combo_native_issued=True,
        )
        await store.upsert("task-sticky", combo_native_issued=False)
        data = await store.get("task-sticky")
        assert data["combo_native_issued"] is True

    @pytest.mark.asyncio
    async def test_marker_latch_false_lands_on_fresh_row(self, store):
        """新行首写 False 正常落库（出生 seam 的主路径）。"""
        await store.upsert(
            "task-fresh",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            combo_native_issued=False,
        )
        data = await store.get("task-fresh")
        assert data["combo_native_issued"] is False

    @pytest.mark.asyncio
    async def test_marker_birth_order_false_then_true(self, store):
        """出生顺序模拟：先 False（出生 seam）后 True（combo 标记，
        任一顺序）→ 终值 True；再 None 冲刷 → 仍 True（闩锁双保险）。"""
        await store.upsert(
            "task-order",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["uid-1"],
            combo_native_issued=False,
        )
        await store.upsert("task-order", combo_native_issued=True)
        data = await store.get("task-order")
        assert data["combo_native_issued"] is True
        await store.upsert("task-order", combo_native_issued=None)
        data = await store.get("task-order")
        assert data["combo_native_issued"] is True


class TestLiabilityGroupSerialization:
    """Round-32b P3 — get_all_metrics 行的 ``liability_group`` 三组透传。

    分组立法在 state.py 的 ``liability_group_for``（同一词表单源：
    CLEARED / TERMINAL），服务端序列化面只在 liability-live 行上携带；
    dead 行 ship null。TUI 消费此字段分桶渲染 boot 卡，TS 侧零词表
    复制（PENDING_STATES 漂移家族保持退役）。"""

    _ISSUED = "2026-09-16T10:00:00+08:00"

    @pytest.mark.asyncio
    async def test_live_rows_carry_group_dead_rows_ship_null(self, store):
        """四形态：in_flight / needs_recovery / uncleared / dead(null)。"""
        # in_flight — 非终态词 + 翼不平衡 → live
        await store.upsert(
            "task-grp-inflight",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["u1"],
        )
        await store.update_task_state("task-grp-inflight", "injecting")

        # needs_recovery — 终态非清算词（fault 仍在账上）→ live
        await store.upsert(
            "task-grp-needs",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["u2"],
        )
        await store.update_task_state("task-grp-needs", "failed")

        # uncleared — CLEARED 词压活账本（round-32b C1 haunt 形状）→ live
        await store.upsert(
            "task-grp-uncleared",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            owned_experiment_uids=["u3"],
        )
        await store.update_task_state("task-grp-uncleared", "completed")

        # dead — 无注入意图（C 支）→ liability_live=False → group null
        await store.update_task_state("task-grp-dead", "rejected")

        result = await store.get_all_metrics()
        rows = {t["task_id"]: t for t in result["tasks"]}

        assert rows["task-grp-inflight"]["liability_live"] is True
        assert rows["task-grp-inflight"]["liability_group"] == "in_flight"
        assert rows["task-grp-needs"]["liability_live"] is True
        assert rows["task-grp-needs"]["liability_group"] == "needs_recovery"
        assert rows["task-grp-uncleared"]["liability_live"] is True
        assert rows["task-grp-uncleared"]["liability_group"] == "uncleared"
        assert rows["task-grp-dead"]["liability_live"] is False
        assert rows["task-grp-dead"]["liability_group"] is None

    @pytest.mark.asyncio
    async def test_c2_settled_row_has_no_group_either(self, store):
        """A2 门控与分组正交：marker=False 翼平衡即死的行同样无组
        （死行不管怎么死的都不进 boot 卡）。"""
        await store.upsert(
            "task-grp-c2",
            target={"namespace": "default", "names": ["pod1"]},
            injection_start_time=self._ISSUED,
            injection_method="host_blade",
            owned_experiment_uids=["u9"],
            combo_native_issued=False,
        )
        await store.upsert("task-grp-c2", retired_experiment_uids=["u9"])
        await store.update_task_state("task-grp-c2", "failed")

        rows = {t["task_id"]: t for t in (await store.get_all_metrics())["tasks"]}
        assert rows["task-grp-c2"]["liability_live"] is False
        assert rows["task-grp-c2"]["liability_group"] is None


# ---------------------------------------------------------------------------
# task_state closed-set guards (round-16 S4/S5)
# ---------------------------------------------------------------------------

class TestTaskStateClosedSetGuards:
    """Round-16 S4/S5: the task_state column was unprotected on BOTH
    sides — reads defaulted a missing column to "injecting" (dressing an
    unknown/terminal row up as in-flight), and update_task_state wrote
    any string as-is (a typo like "reocvered" persisted silently).
    Contrast: the verification vocabulary got a write clamp (round-14)
    AND a read gate (round-15 D5); these pins close the task_state gap."""

    @pytest.mark.asyncio
    async def test_bare_insert_row_defaults_to_injecting_via_ddl(
        self, backend, store
    ):
        """S4 事实修正后的正向立法：DDL
        ``task_state TEXT NOT NULL DEFAULT 'injecting'`` 是 schema 立法
        ——裸插（无 Python 推断）的行从 in-flight 起步，与 upsert 推断
        语义一致，不是漂移。（区分于读侧缺 key 防御：一个真的缺列
        legacy 库在 SQLiteBackend.create 的索引 DDL 处就 fail loudly
        ——实测 ``no such column: task_state``，根本到不了读侧。）"""
        await backend.upsert_task(
            "task-bare", ["task_id", "operation"], ["task-bare", "inject"]
        )
        row = await store.get("task-bare")
        assert row["task_state"] == "injecting"

    @pytest.mark.asyncio
    async def test_task_row_dict_missing_task_state_key_reports_unknown(
        self, store, monkeypatch
    ):
        """S4 防御位行为钉扎：行 dict 缺 ``task_state`` key（未来部分列
        查询 / 新 backend 构造路径）时，读侧拼 ``unknown``，不伪造
        in-flight。``unknown`` 刻意在 TaskState 闭集之外：它断言「行内
        无证据」，永远不是生命周期宣称。"""

        async def fake_select_task(task_id):
            return {"task_id": task_id, "operation": "inject"}  # no key

        async def fake_select_details(task_id):
            return None

        async def fake_select_spans(task_id):
            return []

        monkeypatch.setattr(store._backend, "select_task", fake_select_task)
        monkeypatch.setattr(store._backend, "select_details", fake_select_details)
        monkeypatch.setattr(store._backend, "select_spans", fake_select_spans)

        metric = await store.get_metric("task-x")
        assert metric["task_state"] == "unknown"

    def test_task_store_reads_have_no_hand_copied_injecting_default(self):
        """S4 源级钉扎：读路径不得再出现
        ``task.get("task_state", "injecting")`` 手抄默认形态（修复前
        的形态）——缺 key 防御必须走 ``or "unknown"`` sentinel。"""
        src = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "chaos_agent"
            / "persistence"
            / "task_store.py"
        )
        text = src.read_text(encoding="utf-8")
        assert 'task.get("task_state", "injecting")' not in text

    @pytest.mark.asyncio
    async def test_update_task_state_rejects_word_outside_closed_set(
        self, backend, store
    ):
        """S5: an out-of-set word is a PROGRAM BUG (typo, foreign domain
        word), not legacy data — reject loudly, never persist it. A
        silent clamp would mask the bug (contrast the read-side gate,
        which faces legacy rows and may only normalise)."""
        await backend.upsert_task(
            "task-typo", ["task_id", "operation"], ["task-typo", "inject"]
        )
        with pytest.raises(ValueError, match="value domain"):
            await store.update_task_state("task-typo", "reocvered")
        row = await store.get("task-typo")
        assert row["task_state"] != "reocvered"

    @pytest.mark.asyncio
    async def test_update_task_state_accepts_legislated_words(self, backend, store):
        """The guard rejects only out-of-set words; the legislated
        vocabulary (e.g. the recover flow's "recovered") still writes."""
        await backend.upsert_task(
            "task-ok", ["task_id", "operation"], ["task-ok", "inject"]
        )
        await store.update_task_state("task-ok", "recovered")
        row = await store.get("task-ok")
        assert row["task_state"] == "recovered"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_task(self, store):
        await store.upsert("task-t1")
        assert await store.delete("task-t1") is True
        assert await store.get("task-t1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, store):
        assert await store.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_delete_removes_associated_spans(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0, 1, 1000)
        assert len(await store.get_spans("task-t1")) == 1
        await store.delete("task-t1")
        assert len(await store.get_spans("task-t1")) == 0

    @pytest.mark.asyncio
    async def test_delete_removes_details(self, store):
        await store.upsert("task-t1", target={"namespace": "default"}, experiment_uid="abc")
        await store.delete("task-t1")
        assert await store.get("task-t1") is None


# ---------------------------------------------------------------------------
# Span methods
# ---------------------------------------------------------------------------

class TestSpans:
    @pytest.mark.asyncio
    async def test_append_span(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0.0, 1.5, 1500.0, token_input=100, token_output=50)
        spans = await store.get_spans("task-t1")
        assert len(spans) == 1
        assert spans[0]["node_name"] == "agent_loop"
        assert spans[0]["duration_ms"] == 1500.0

    @pytest.mark.asyncio
    async def test_append_span_updates_summary(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0.0, 1.0, 1000.0,
                                token_input=100, token_output=50,
                                tool_calls=["blade_create"])
        summary = await store.get_summary("task-t1")
        assert summary["total_token_input"] == 100
        assert summary["total_token_output"] == 50
        assert summary["total_tool_calls"] == 1
        assert summary["total_duration_ms"] == 1000

    @pytest.mark.asyncio
    async def test_multiple_spans_accumulate(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0.0, 1.0, 1000.0, token_input=100)
        await store.append_span("task-t1", "execute_loop", 1.0, 2.0, 1000.0, token_input=200)
        summary = await store.get_summary("task-t1")
        assert summary["total_token_input"] == 300
        assert summary["total_duration_ms"] == 2000

    @pytest.mark.asyncio
    async def test_span_tool_calls_roundtrip(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0.0, 1.0, 1000.0,
                                tool_calls=["blade_create", "kubectl"])
        spans = await store.get_spans("task-t1")
        assert spans[0]["tool_calls"] == ["blade_create", "kubectl"]

    @pytest.mark.asyncio
    async def test_span_error(self, store):
        await store.upsert("task-t1")
        await store.append_span("task-t1", "agent_loop", 0.0, 1.0, 1000.0, error="timeout")
        spans = await store.get_spans("task-t1")
        assert spans[0]["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_get_spans_empty(self, store):
        await store.upsert("task-t1")
        assert await store.get_spans("task-t1") == []


# ---------------------------------------------------------------------------
# total_token_cached — task-level prompt-cache persistence (design D4)
# ---------------------------------------------------------------------------

class TestTokenCachedPersistence:
    """task_details.total_token_cached — the task-level prompt-cache aggregate.

    Cache hits are a SUBSET of total_token_input (not additive), written
    ABSOLUTELY by ``tracer._persist_summary`` at finalize (never per-span
    rollup — cache is a task-level aggregate, unrelated to graph nodes).
    These tests pin the DB write/read path + the idempotent migration.
    """

    @pytest.mark.asyncio
    async def test_task_details_has_total_token_cached_column(self, backend):
        cursor = await backend._conn.execute("PRAGMA table_info(task_details)")
        col_names = {r[1] for r in await cursor.fetchall()}
        assert "total_token_cached" in col_names

    @pytest.mark.asyncio
    async def test_upsert_roundtrips_total_token_cached(self, store):
        await store.upsert("task-t1", total_token_input=2990,
                           total_token_cached=2176)
        summary = await store.get_summary("task-t1")
        assert summary["total_token_cached"] == 2176
        assert summary["total_token_input"] == 2990

    @pytest.mark.asyncio
    async def test_total_token_cached_defaults_to_zero(self, store):
        # A task written without cache (legacy producer / cold run) reads 0,
        # never absent — get_summary's column list always includes it.
        await store.upsert("task-t1", total_token_input=100)
        summary = await store.get_summary("task-t1")
        assert summary["total_token_cached"] == 0

    @pytest.mark.asyncio
    async def test_get_metric_summary_carries_total_token_cached(self, store):
        await store.upsert("task-t1", total_token_input=2990,
                           total_token_cached=2176)
        metric = await store.get_metric("task-t1")
        assert metric["summary"]["total_token_cached"] == 2176

    @pytest.mark.asyncio
    async def test_get_all_metrics_summary_carries_total_token_cached(self, store):
        # The list path (get_all_metrics) has its OWN summary column list —
        # it must carry cache too, or single-task vs list views diverge.
        await store.upsert("task-t1", total_token_input=2990,
                           total_token_cached=2176)
        result = await store.get_all_metrics()
        row = next(t for t in result["tasks"] if t["task_id"] == "task-t1")
        assert row["summary"]["total_token_cached"] == 2176

    @pytest.mark.asyncio
    async def test_legacy_db_gains_total_token_cached_on_startup(self, tmp_path):
        """A pre-cache DB (task_details WITHOUT total_token_cached) gains the
        column via the idempotent ALTER on reopen; a subsequent write carrying
        total_token_cached does not raise 'no such column'. Backend-level to
        isolate the migration from store.upsert's inference path."""
        import sqlite3

        db_path = tmp_path / "legacy_cache.db"
        raw = sqlite3.connect(db_path)
        raw.executescript(
            "CREATE TABLE task_details ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  task_id TEXT NOT NULL,"
            "  total_token_input INTEGER NOT NULL DEFAULT 0,"
            "  gmt_create TEXT,"
            "  gmt_modified TEXT"
            ");"
        )
        raw.commit()
        raw.close()

        backend = await SQLiteBackend.create(db_path=db_path)
        try:
            cursor = await backend._conn.execute("PRAGMA table_info(task_details)")
            cols = {r[1] for r in await cursor.fetchall()}
            assert "total_token_cached" in cols  # ALTER migration added it
            await backend.upsert_details(
                "task-legacy",
                ["task_id", "total_token_input", "total_token_cached"],
                ["task-legacy", 2990, 2176],
            )
            row = await backend.select_details("task-legacy")
            assert row["total_token_cached"] == 2176
        finally:
            await backend.close()


# ---------------------------------------------------------------------------
# Metric methods
# ---------------------------------------------------------------------------

class TestMetricMethods:
    @pytest.mark.asyncio
    async def test_get_metric_single_task(self, store):
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.append_span("task-t1", "agent_loop", 0.0, 1.0, 1000.0, token_input=100)
        metric = await store.get_metric("task-t1")
        assert metric is not None
        # Both raw lifecycle (``task_state``) and derived rollup
        # (``status``) are exposed: clients that need to gate on
        # "is this still in flight" (TS PendingTasksCard) read
        # ``task_state``; those that just want a coarse success /
        # failed verdict read ``status``. The previous shape exposed
        # only ``status`` and silently broke PendingTasksCard, which
        # filters on ``task_state in {"injecting","injected"}``.
        assert metric["task_state"] == "injected"
        # Default operation in the schema is "inject" (table column
        # default) — every record has a non-empty operation field.
        assert metric["operation"] == "inject"
        assert metric["skill_name"] == "pod-kill"
        assert metric["stage"] == "injection"
        assert metric["status"] == "success"
        assert "inject_status" not in metric
        assert "recover_status" not in metric
        assert "failure_reason" not in metric
        assert metric["error"] == ""
        assert len(metric["spans"]) == 1
        assert metric["summary"]["total_token_input"] == 100

    @pytest.mark.asyncio
    async def test_get_metric_nonexistent(self, store):
        assert await store.get_metric("nonexistent") is None

    @pytest.mark.asyncio
    async def test_get_metric_exposes_frozen_model_name(self, store):
        """model_name rides the metric envelope as an at-run snapshot."""
        await store.upsert("task-t1", skill_name="pod-kill",
                           model_name="qwen3.8-max")
        metric = await store.get_metric("task-t1")
        assert metric["model_name"] == "qwen3.8-max"

    @pytest.mark.asyncio
    async def test_get_metric_model_name_empty_for_legacy_tasks(self, store):
        """Tasks archived before the column existed yield "", never None
        (renderers gate on truthiness)."""
        await store.upsert("task-t1", skill_name="pod-kill")
        metric = await store.get_metric("task-t1")
        assert metric["model_name"] == ""

    @pytest.mark.asyncio
    async def test_get_metric_computes_fault_type(self, store):
        await store.upsert("task-t1", params={"scope": "pod", "target": "cpu", "action": "fullload"})
        metric = await store.get_metric("task-t1")
        assert metric["fault_type"] == "pod-cpu-fullload"

    @pytest.mark.asyncio
    async def test_get_metric_fault_type_prefers_fault_spec_projection(self, store):
        fault_spec = {
            "namespace": "default",
            "scope": "pod",
            "names": ["pod-a"],
            "labels": {},
            "fault_target": "network",
            "fault_action": "loss",
            "params": {"percent": "100"},
            "source": "test",
        }
        await store.upsert(
            "task-t1",
            skill_name="stale-active-skill",
            fault_spec=fault_spec,
        )

        metric = await store.get_metric("task-t1")
        data = await store.get("task-t1")

        assert metric["fault_type"] == "pod-network-loss"
        assert data["fault_spec"] == fault_spec

    @pytest.mark.asyncio
    async def test_get_metric_computes_duration_ms(self, store):
        await store.upsert("task-t1", gmt_create="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:05+00:00")
        metric = await store.get_metric("task-t1")
        assert metric["duration_ms"] == 5000

    @pytest.mark.asyncio
    async def test_get_all_metrics(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.upsert("task-t2", skill_name="pod-kill", experiment_uid="a",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        result = await store.get_all_metrics()
        assert result["total"] == 2
        assert len(result["tasks"]) == 2
        for task in result["tasks"]:
            assert "summary" in task
            assert "failure_reason" not in task
            assert "inject_status" not in task
            assert "recover_status" not in task
            assert "status" in task
            assert "fault_type" in task

    @pytest.mark.asyncio
    async def test_get_all_metrics_fault_type_prefers_fault_spec(self, store):
        fault_spec = {
            "namespace": "default",
            "scope": "pod",
            "names": ["pod-a"],
            "labels": {},
            "fault_target": "network",
            "fault_action": "loss",
            "params": {"percent": "100"},
        }
        await store.upsert(
            "task-t1",
            skill_name="stale-active-skill",
            fault_spec=fault_spec,
        )

        result = await store.get_all_metrics()

        assert result["tasks"][0]["fault_type"] == "pod-network-loss"
        assert result["tasks"][0]["skill_name"] == "stale-active-skill"

    @pytest.mark.asyncio
    async def test_get_all_metrics_with_state_filter(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        await store.upsert("task-t2", error="fail")
        result = await store.get_all_metrics(task_state="failed")
        assert result["total"] == 1
        # Raw ``task_state`` is now part of the wire shape (this used
        # to assert "task_state" was absent — that absence was the
        # bug that broke the TS TUI's PendingTasksCard).
        assert result["tasks"][0]["task_state"] == "failed"

    @pytest.mark.asyncio
    async def test_get_metric_with_failure_reason(self, store):
        await store.upsert("task-t1", error="timeout", failure_reason="execution_failed: timeout")
        metric = await store.get_metric("task-t1")
        assert "failure_reason" not in metric
        assert metric["error"] == "execution_failed: timeout"

    @pytest.mark.asyncio
    async def test_get_all_metrics_with_failure_reason(self, store):
        await store.upsert("task-t1", error="fail", failure_reason="execution_failed")
        await store.upsert("task-t2", skill_name="pod-kill")
        result = await store.get_all_metrics()
        failed_task = next(t for t in result["tasks"] if t["task_id"] == "task-t1")
        success_task = next(t for t in result["tasks"] if t["task_id"] == "task-t2")
        assert "failure_reason" not in failed_task
        assert failed_task["error"] == "execution_failed"
        assert success_task["error"] == ""

    @pytest.mark.asyncio
    async def test_inject_status_in_progress(self, store):
        await store.upsert("task-t1", skill_name="pod-kill")
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_inject_status_failed(self, store):
        await store.upsert("task-t1", error="something went wrong")
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "failed"

    @pytest.mark.asyncio
    async def test_recover_status_success(self, store):
        await store.upsert("task-t1", operation="recover",
                           recover_verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}},
                           result={"recovered": True})
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "success"

    @pytest.mark.asyncio
    async def test_recover_status_failed(self, store):
        await store.upsert("task-t1", operation="recover", error="recovery failed")
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "failed"

    @pytest.mark.asyncio
    async def test_stale_task_state_corrected_on_read(self, store):
        """When verification data arrives via a later upsert, the inferred
        task_state should update from "injecting" → "injected" even though
        the DB previously stored "injecting".

        This is the exact bug reported: phase="verification_passed" but
        status="in_progress" because task_state was stale.
        """
        # Step 1: initial inject → DB stores task_state="injecting"
        await store.upsert("task-t1", skill_name="pod-kill", experiment_uid="abc123",
                           operation="inject")
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "in_progress"  # still injecting

        # Step 2: verification arrives → should transition to "injected"
        await store.upsert("task-t1", verification={
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        })
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "success"
        assert metric["phase"] == "verification_passed"

    @pytest.mark.asyncio
    async def test_stale_recover_state_corrected_on_read(self, store):
        """Recovery task_state should update from "recovering" → "recovered"
        when recover_verification arrives, even though DB had "recovering".
        """
        # Step 1: start recovery
        await store.upsert("task-t1", operation="recover")
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "in_progress"

        # Step 2: recovery verification arrives
        await store.upsert("task-t1", recover_verification={
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        }, result={"recovered": True})
        metric = await store.get_metric("task-t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "success"
        assert metric["phase"] == "recovered"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

class TestHelperFunctions:
    def test_extract_index_fields_from_dict(self):
        fields = {"target": {"namespace": "prod", "names": ["pod1"]}}
        result = _extract_index_fields(fields)
        assert result["namespace"] == "prod"
        assert result["target_name"] == "pod1"

    def test_extract_index_fields_from_json_string(self):
        fields = {"target": json.dumps({"namespace": "staging", "names": ["pod2"]})}
        result = _extract_index_fields(fields)
        assert result["namespace"] == "staging"
        assert result["target_name"] == "pod2"

    def test_extract_index_fields_no_override(self):
        fields = {"target": {"namespace": "prod"}, "namespace": "custom"}
        result = _extract_index_fields(fields)
        assert result["namespace"] == "custom"

    def test_set_timestamps_new_row(self):
        fields = {}
        result = _set_timestamps(fields, None)
        assert "gmt_create" in result
        assert "gmt_modified" in result

    def test_set_timestamps_update_preserves_gmt_create(self):
        existing = {"gmt_create": "2026-01-01T00:00:00+00:00"}
        fields = {}
        result = _set_timestamps(fields, existing)
        assert result["gmt_create"] == "2026-01-01T00:00:00+00:00"
        assert result["gmt_modified"] is not None


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

class TestGetTaskStore:
    @pytest.mark.asyncio
    async def test_returns_same_instance(self, tmp_path, monkeypatch):
        import chaos_agent.persistence.task_store as mod
        monkeypatch.setattr(mod, "_store", None)
        monkeypatch.setattr(mod.settings, "tasks_db_path", tmp_path / "tasks.db")
        s1 = await mod.get_task_store()
        s2 = await mod.get_task_store()
        assert s1 is s2
        await reset_task_store()

    @pytest.mark.asyncio
    async def test_reset_task_store(self, tmp_path, monkeypatch):
        import chaos_agent.persistence.task_store as mod
        monkeypatch.setattr(mod, "_store", None)
        monkeypatch.setattr(mod.settings, "tasks_db_path", tmp_path / "tasks.db")
        await mod.get_task_store()
        await reset_task_store()
        assert mod._store is None


# ---------------------------------------------------------------------------
# Newborn / cancelled lifecycle (ghost-row fix)
# ---------------------------------------------------------------------------

class TestNewbornPendingLifecycle:
    """A row with zero lifecycle evidence is a newborn anchor (the bare
    ``upsert(task_id)`` the tracer does before persisting spans). It must
    surface as "pending" — reporting it as "injecting" made rejected /
    abandoned intents masquerade as unfinished work on the boot card.
    """

    @pytest.mark.asyncio
    async def test_bare_upsert_is_pending(self, store):
        """tracer._persist_span style: upsert(task_id) with no fields."""
        await store.upsert("inject-ghost1")
        data = await store.get("inject-ghost1")
        assert data["task_state"] == "pending"

    @pytest.mark.asyncio
    async def test_metrics_only_upsert_is_pending(self, store):
        """tracer._persist_summary style: usage counters are not evidence."""
        await store.upsert("inject-ghost1", total_token_input=100, total_llm_calls=3)
        data = await store.get("inject-ghost1")
        assert data["task_state"] == "pending"

    @pytest.mark.asyncio
    async def test_pending_upgrades_to_injecting_with_evidence(self, store):
        """The moment the pipeline takes ownership (fault_spec arrives),
        inference promotes the row — pending is a starting state, not a trap."""
        await store.upsert("inject-ghost1")
        assert (await store.get("inject-ghost1"))["task_state"] == "pending"
        await store.upsert("inject-ghost1", fault_spec={"target": "pod"}, needs_confirm=1)
        data = await store.get("inject-ghost1")
        assert data["task_state"] == "waiting_input"

    @pytest.mark.asyncio
    async def test_waiting_input_semantics_preserved(self, store):
        """interaction_mode is session context, NOT lifecycle evidence: the
        TUI crash-recovery detector keys on it, so such rows must keep
        reporting waiting_input (not be demoted to pending)."""
        await store.upsert("inject-ghost1", interaction_mode="tui")
        data = await store.get("inject-ghost1")
        assert data["task_state"] == "waiting_input"


class TestCancelledTerminalGuard:
    """cancelled is a terminal verdict: direct column write (intent
    rejection / turn abort), then the monotonicity guard keeps field-less
    re-projections from resurrecting the row."""

    @pytest.mark.asyncio
    async def test_cancelled_never_regresses_on_fieldless_flush(self, store):
        await store.upsert("inject-ghost1", fault_spec={"target": "pod"})
        await store.update_task_state("inject-ghost1", "cancelled")
        await store.upsert("inject-ghost1")  # tracer field-less flush
        await store.upsert("inject-ghost1", total_token_input=42)  # summary flush
        assert (await store.get("inject-ghost1"))["task_state"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancelled_row_still_accepts_verdict(self, store):
        """The guard only blocks the *fallback*, not genuine evidence: a
        verification verdict outranks a stale cancelled stamp."""
        await store.upsert("inject-ghost1", skill_name="pod-kill", experiment_uid="abc")
        await store.update_task_state("inject-ghost1", "cancelled")
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("inject-ghost1", verification=verification)
        assert (await store.get("inject-ghost1"))["task_state"] == "injected"


class TestMetricsCommittedFlag:
    """L1 read-path defence: ``committed`` is the evidence flag (same source
    of truth as select_active_tasks) so consumers can tell "running" from
    "died mid-flight" — task_state alone cannot."""

    @pytest.mark.asyncio
    async def test_committed_false_for_newborn_row(self, store):
        await store.upsert("inject-ghost1", fault_spec={"target": "pod"})
        metrics = await store.get_all_metrics()
        row = next(t for t in metrics["tasks"] if t["task_id"] == "inject-ghost1")
        assert row["committed"] is False

    @pytest.mark.asyncio
    async def test_committed_true_once_command_issued(self, store):
        await store.upsert(
            "inject-live1",
            fault_spec={"target": "pod"},
            injection_start_time="2026-08-19T12:00:00+00:00",
        )
        metrics = await store.get_all_metrics()
        row = next(t for t in metrics["tasks"] if t["task_id"] == "inject-live1")
        assert row["committed"] is True


class TestCancelledReuseLifecycle:
    """ID-reuse round trip: reject (cancel + flag clear) → re-converge
    (waiting_input must come back for crash recovery) → revive on approval.
    Pins the regressions found in the fix's own review:

    * a cancelled row carrying needs_confirm=1 used to be re-derived as
      waiting_input by any later flush — right after the user said no;
    * conversely, once cancelled pinned the row, the SECOND round's
      waiting_input was suppressed — blinding TUI crash recovery.
    """

    @pytest.mark.asyncio
    async def test_reject_clears_confirm_flag_and_stays_cancelled(self, store):
        """_cancel_task_row contract: cancel stamp + needs_confirm=0.
        Later field-less flushes must not resurrect waiting_input."""
        await store.upsert("inject-reuse1", fault_spec={"target": "pod"}, needs_confirm=1)
        assert (await store.get("inject-reuse1"))["task_state"] == "waiting_input"
        # The reject-path writes (intent_confirm._cancel_task_row):
        await store.update_task_state("inject-reuse1", "cancelled")
        await store.upsert("inject-reuse1", needs_confirm=0)
        # tracer field-less flush afterwards:
        await store.upsert("inject-reuse1")
        data = await store.get("inject-reuse1")
        assert data["task_state"] == "cancelled"
        assert data["needs_confirm"] == 0

    @pytest.mark.asyncio
    async def test_second_round_converge_reactivates_waiting_input(self, store):
        """After a rejection, the NEXT clarification round writes
        needs_confirm=1 again — the row must surface as waiting_input so
        crash recovery can find the fresh confirmation card."""
        await store.upsert("inject-reuse1", fault_spec={"target": "pod"}, needs_confirm=1)
        await store.update_task_state("inject-reuse1", "cancelled")
        await store.upsert("inject-reuse1", needs_confirm=0)
        # second round convergence sync:
        await store.upsert("inject-reuse1", needs_confirm=1, fault_spec={"target": "pod"})
        assert (await store.get("inject-reuse1"))["task_state"] == "waiting_input"

    @pytest.mark.asyncio
    async def test_result_alone_is_evidence(self, store):
        """Fields added late to the evidence set: result / artifacts /
        plan_summary / safety_reason alone must not leave a bare row at
        "pending" (the pipeline demonstrably owns it)."""
        await store.upsert("inject-ev1", result={"executed": True})
        assert (await store.get("inject-ev1"))["task_state"] != "pending"

    @pytest.mark.asyncio
    async def test_committed_is_verbatim_start_time_check(self, store):
        """committed mirrors select_active_tasks verbatim: no
        task_state OR-clause — a corrupted injected-without-evidence row
        must NOT be claimed as committed (display and recovery would
        split again)."""
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("inject-bad1", skill_name="pod-kill", verification=verification)
        # injected verdict but injection_start_time never landed (corruption)
        metrics = await store.get_all_metrics()
        row = next(t for t in metrics["tasks"] if t["task_id"] == "inject-bad1")
        assert row["task_state"] == "injected"
        assert row["committed"] is False


class TestExecutionGateRejection:
    """confirmation_gate (Layer-2 plan-execution card) rejection is
    covered by an EXISTING chain, not by the intent-card terminal write:
    the gate stamps ``safety_status='rejected'`` in state and immediately
    syncs it to the store, so inference derives the terminal "rejected"
    on its own. This test pins that chain — if someone reorders the
    safety/error branches in infer_task_state or drops the gate's
    sync_to_store call, gate rejections would regress into ghosts.
    """

    @pytest.mark.asyncio
    async def test_gate_rejection_infers_terminal_rejected(self, store):
        # Walked to the execution gate: spec planned, safety passed,
        # parked on the confirmation card.
        await store.upsert(
            "inject-gate1",
            fault_spec={"target": "pod"},
            plan_summary="fullload pods",
            safety_status="approved",
            needs_confirm=1,
        )
        assert (await store.get("inject-gate1"))["task_state"] == "waiting_input"

        # The gate's reject-path sync (confirmation_gate.py): safety
        # rejected + flag cleared + failure recorded.
        await store.upsert(
            "inject-gate1",
            safety_status="rejected",
            safety_reason="User rejected the execution",
            needs_confirm=0,
            error="USER_REJECTED: User rejected the execution at confirmation gate",
        )
        data = await store.get("inject-gate1")
        assert data["task_state"] == "rejected"

        # Terminal: field-less flushes must not resurrect it.
        await store.upsert("inject-gate1")
        assert (await store.get("inject-gate1"))["task_state"] == "rejected"
