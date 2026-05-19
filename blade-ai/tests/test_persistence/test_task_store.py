"""Tests for the async persistent TaskStore (SQLiteBackend)."""

import json

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


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

class TestUpsert:
    @pytest.mark.asyncio
    async def test_insert_new_task(self, store):
        await store.upsert("t1", skill_name="pod-kill", operation="inject")
        data = await store.get("t1")
        assert data is not None
        assert data["skill_name"] == "pod-kill"
        assert data["operation"] == "inject"
        assert data["task_state"] == "injecting"

    @pytest.mark.asyncio
    async def test_update_existing_task(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        await store.upsert("t1", blade_uid="abc123")
        data = await store.get("t1")
        assert data["skill_name"] == "pod-kill"
        assert data["blade_uid"] == "abc123"

    @pytest.mark.asyncio
    async def test_partial_update_preserves_other_fields(self, store):
        await store.upsert("t1", skill_name="pod-kill", blade_uid="abc")
        await store.upsert("t1", safety_status="safe")
        data = await store.get("t1")
        assert data["skill_name"] == "pod-kill"
        assert data["blade_uid"] == "abc"
        assert data["safety_status"] == "safe"

    @pytest.mark.asyncio
    async def test_gmt_modified_is_set(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        data = await store.get("t1")
        assert data["gmt_modified"] is not None
        assert data["gmt_modified"] != ""

    @pytest.mark.asyncio
    async def test_gmt_create_preserved_on_update(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        data1 = await store.get("t1")
        gmt_create_1 = data1["gmt_create"]
        await store.upsert("t1", blade_uid="abc")
        data2 = await store.get("t1")
        assert data2["gmt_create"] == gmt_create_1

    @pytest.mark.asyncio
    async def test_empty_task_id_is_noop(self, store):
        await store.upsert("", skill_name="pod-kill")
        assert await store.count() == 0

    @pytest.mark.asyncio
    async def test_json_fields_serialized(self, store):
        target = {"namespace": "default", "names": ["pod1"], "resource_type": "pod"}
        await store.upsert("t1", target=target)
        data = await store.get("t1")
        assert data["target"] == target

    @pytest.mark.asyncio
    async def test_verification_json_roundtrip(self, store):
        verification = {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        }
        await store.upsert("t1", verification=verification, blade_uid="abc")
        data = await store.get("t1")
        assert data["verification"] == verification
        assert data["task_state"] == "injected"

    @pytest.mark.asyncio
    async def test_namespace_target_name_extracted(self, store):
        target = {"namespace": "prod", "names": ["pod1"], "resource_type": "pod"}
        await store.upsert("t1", target=target)
        data = await store.get("t1")
        assert data["namespace"] == "prod"
        assert data["target_name"] == "pod1"


# ---------------------------------------------------------------------------
# Infer fields
# ---------------------------------------------------------------------------

class TestInferFields:
    @pytest.mark.asyncio
    async def test_injecting_state_inferred(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        data = await store.get("t1")
        assert data["task_state"] == "injecting"
        assert data["stage"] == "injection"
        assert data["phase"] == "planning"

    @pytest.mark.asyncio
    async def test_injected_state_inferred(self, store):
        verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("t1", skill_name="pod-kill", blade_uid="abc", verification=verification)
        data = await store.get("t1")
        assert data["task_state"] == "injected"
        assert data["phase"] == "verification_passed"

    @pytest.mark.asyncio
    async def test_rejected_state_inferred(self, store):
        await store.upsert("t1", safety_status="rejected", safety_reason="unsafe")
        data = await store.get("t1")
        assert data["task_state"] == "rejected"

    @pytest.mark.asyncio
    async def test_failed_state_inferred(self, store):
        await store.upsert("t1", error="something went wrong")
        data = await store.get("t1")
        assert data["task_state"] == "failed"

    @pytest.mark.asyncio
    async def test_recovered_state_inferred(self, store):
        recover_verification = {"layer1": {"status": "passed"}, "layer2": {"status": "passed"}}
        await store.upsert("t1", operation="recover",
                           recover_verification=recover_verification,
                           result={"recovered": True})
        data = await store.get("t1")
        assert data["task_state"] == "recovered"
        assert data["stage"] == "recovery"


# ---------------------------------------------------------------------------
# Get / List / Count
# ---------------------------------------------------------------------------

class TestGetListCount:
    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, store):
        assert await store.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_returns_ordered_by_gmt_create_desc(self, store):
        await store.upsert("t1", gmt_create="2026-01-01T00:00:00Z")
        await store.upsert("t2", gmt_create="2026-01-02T00:00:00Z")
        await store.upsert("t3", gmt_create="2026-01-03T00:00:00Z")
        result = await store.list()
        assert [d["task_id"] for d in result] == ["t3", "t2", "t1"]

    @pytest.mark.asyncio
    async def test_list_with_state_filter(self, store):
        await store.upsert("t1", skill_name="pod-kill", blade_uid="a",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.upsert("t2", skill_name="pod-kill")
        injected = await store.list(task_state="injected")
        assert len(injected) == 1
        assert injected[0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_list_with_limit_offset(self, store):
        for i in range(5):
            await store.upsert(f"t{i}", gmt_create=f"2026-01-0{i+1}T00:00:00Z")
        result = await store.list(limit=2, offset=1)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_count_all(self, store):
        await store.upsert("t1")
        await store.upsert("t2")
        assert await store.count() == 2

    @pytest.mark.asyncio
    async def test_count_by_state(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        await store.upsert("t2", error="fail")
        assert await store.count(task_state="injecting") == 1
        assert await store.count(task_state="failed") == 1


# ---------------------------------------------------------------------------
# Query active
# ---------------------------------------------------------------------------

class TestQueryActive:
    @pytest.mark.asyncio
    async def test_returns_injecting_and_injected(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        await store.upsert("t2", skill_name="pod-kill", blade_uid="a",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.upsert("t3", error="fail")
        active = await store.query_active()
        assert len(active) == 2
        task_ids = {r["task_id"] for r in active}
        assert "t1" in task_ids
        assert "t2" in task_ids

    @pytest.mark.asyncio
    async def test_filter_by_namespace(self, store):
        await store.upsert("t1", target={"namespace": "prod", "names": ["pod1"]}, skill_name="pod-kill")
        await store.upsert("t2", target={"namespace": "staging", "names": ["pod2"]}, skill_name="pod-kill")
        active = await store.query_active(namespace="prod")
        assert len(active) == 1
        assert active[0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_filter_by_target_name(self, store):
        """target_name column stores names[0] from the target JSON."""
        await store.upsert("t1", target={"namespace": "prod", "names": ["pod1"]}, skill_name="pod-kill")
        await store.upsert("t2", target={"namespace": "prod", "names": ["pod2"]}, skill_name="pod-kill")
        active = await store.query_active(target_name="pod1")
        assert len(active) == 1
        assert active[0]["task_id"] == "t1"

    @pytest.mark.asyncio
    async def test_compatible_format(self, store):
        await store.upsert("t1", skill_name="pod-kill", target={"namespace": "default"}, blade_uid="abc")
        active = await store.query_active()
        record = active[0]
        assert "task_id" in record
        assert "operation" in record
        assert "skill" in record
        assert "target" in record
        assert "params" in record
        assert "blade_uid" in record
        assert "status" in record


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

class TestDelete:
    @pytest.mark.asyncio
    async def test_delete_removes_task(self, store):
        await store.upsert("t1")
        assert await store.delete("t1") is True
        assert await store.get("t1") is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_false(self, store):
        assert await store.delete("nonexistent") is False

    @pytest.mark.asyncio
    async def test_delete_removes_associated_spans(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0, 1, 1000)
        assert len(await store.get_spans("t1")) == 1
        await store.delete("t1")
        assert len(await store.get_spans("t1")) == 0

    @pytest.mark.asyncio
    async def test_delete_removes_details(self, store):
        await store.upsert("t1", target={"namespace": "default"}, blade_uid="abc")
        await store.delete("t1")
        assert await store.get("t1") is None


# ---------------------------------------------------------------------------
# Span methods
# ---------------------------------------------------------------------------

class TestSpans:
    @pytest.mark.asyncio
    async def test_append_span(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0.0, 1.5, 1500.0, token_input=100, token_output=50)
        spans = await store.get_spans("t1")
        assert len(spans) == 1
        assert spans[0]["node_name"] == "agent_loop"
        assert spans[0]["duration_ms"] == 1500.0

    @pytest.mark.asyncio
    async def test_append_span_updates_summary(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0.0, 1.0, 1000.0,
                                token_input=100, token_output=50,
                                tool_calls=["blade_create"])
        summary = await store.get_summary("t1")
        assert summary["total_token_input"] == 100
        assert summary["total_token_output"] == 50
        assert summary["total_tool_calls"] == 1
        assert summary["total_duration_ms"] == 1000

    @pytest.mark.asyncio
    async def test_multiple_spans_accumulate(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0.0, 1.0, 1000.0, token_input=100)
        await store.append_span("t1", "execute_loop", 1.0, 2.0, 1000.0, token_input=200)
        summary = await store.get_summary("t1")
        assert summary["total_token_input"] == 300
        assert summary["total_duration_ms"] == 2000

    @pytest.mark.asyncio
    async def test_span_tool_calls_roundtrip(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0.0, 1.0, 1000.0,
                                tool_calls=["blade_create", "kubectl"])
        spans = await store.get_spans("t1")
        assert spans[0]["tool_calls"] == ["blade_create", "kubectl"]

    @pytest.mark.asyncio
    async def test_span_error(self, store):
        await store.upsert("t1")
        await store.append_span("t1", "agent_loop", 0.0, 1.0, 1000.0, error="timeout")
        spans = await store.get_spans("t1")
        assert spans[0]["error"] == "timeout"

    @pytest.mark.asyncio
    async def test_get_spans_empty(self, store):
        await store.upsert("t1")
        assert await store.get_spans("t1") == []


# ---------------------------------------------------------------------------
# Metric methods
# ---------------------------------------------------------------------------

class TestMetricMethods:
    @pytest.mark.asyncio
    async def test_get_metric_single_task(self, store):
        await store.upsert("t1", skill_name="pod-kill", blade_uid="abc",
                           verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}})
        await store.append_span("t1", "agent_loop", 0.0, 1.0, 1000.0, token_input=100)
        metric = await store.get_metric("t1")
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
    async def test_get_metric_computes_fault_type(self, store):
        await store.upsert("t1", params={"scope": "pod", "target": "cpu", "action": "fullload"})
        metric = await store.get_metric("t1")
        assert metric["fault_type"] == "pod-cpu-fullload"

    @pytest.mark.asyncio
    async def test_get_metric_computes_duration_ms(self, store):
        await store.upsert("t1", gmt_create="2026-01-01T00:00:00+00:00", finished_at="2026-01-01T00:00:05+00:00")
        metric = await store.get_metric("t1")
        assert metric["duration_ms"] == 5000

    @pytest.mark.asyncio
    async def test_get_all_metrics(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        await store.upsert("t2", skill_name="pod-kill", blade_uid="a",
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

    @pytest.mark.asyncio
    async def test_get_all_metrics_with_state_filter(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        await store.upsert("t2", error="fail")
        result = await store.get_all_metrics(task_state="failed")
        assert result["total"] == 1
        # Raw ``task_state`` is now part of the wire shape (this used
        # to assert "task_state" was absent — that absence was the
        # bug that broke the TS TUI's PendingTasksCard).
        assert result["tasks"][0]["task_state"] == "failed"

    @pytest.mark.asyncio
    async def test_get_metric_with_failure_reason(self, store):
        await store.upsert("t1", error="timeout", failure_reason="execution_failed: timeout")
        metric = await store.get_metric("t1")
        assert "failure_reason" not in metric
        assert metric["error"] == "execution_failed: timeout"

    @pytest.mark.asyncio
    async def test_get_all_metrics_with_failure_reason(self, store):
        await store.upsert("t1", error="fail", failure_reason="execution_failed")
        await store.upsert("t2", skill_name="pod-kill")
        result = await store.get_all_metrics()
        failed_task = next(t for t in result["tasks"] if t["task_id"] == "t1")
        success_task = next(t for t in result["tasks"] if t["task_id"] == "t2")
        assert "failure_reason" not in failed_task
        assert failed_task["error"] == "execution_failed"
        assert success_task["error"] == ""

    @pytest.mark.asyncio
    async def test_inject_status_in_progress(self, store):
        await store.upsert("t1", skill_name="pod-kill")
        metric = await store.get_metric("t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "in_progress"

    @pytest.mark.asyncio
    async def test_inject_status_failed(self, store):
        await store.upsert("t1", error="something went wrong")
        metric = await store.get_metric("t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "failed"

    @pytest.mark.asyncio
    async def test_recover_status_success(self, store):
        await store.upsert("t1", operation="recover",
                           recover_verification={"layer1": {"status": "passed"}, "layer2": {"status": "passed"}},
                           result={"recovered": True})
        metric = await store.get_metric("t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "success"

    @pytest.mark.asyncio
    async def test_recover_status_failed(self, store):
        await store.upsert("t1", operation="recover", error="recovery failed")
        metric = await store.get_metric("t1")
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
        await store.upsert("t1", skill_name="pod-kill", blade_uid="abc123",
                           operation="inject")
        metric = await store.get_metric("t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "in_progress"  # still injecting

        # Step 2: verification arrives → should transition to "injected"
        await store.upsert("t1", verification={
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        })
        metric = await store.get_metric("t1")
        assert metric["stage"] == "injection"
        assert metric["status"] == "success"
        assert metric["phase"] == "verification_passed"

    @pytest.mark.asyncio
    async def test_stale_recover_state_corrected_on_read(self, store):
        """Recovery task_state should update from "recovering" → "recovered"
        when recover_verification arrives, even though DB had "recovering".
        """
        # Step 1: start recovery
        await store.upsert("t1", operation="recover")
        metric = await store.get_metric("t1")
        assert metric["stage"] == "recovery"
        assert metric["status"] == "in_progress"

        # Step 2: recovery verification arrives
        await store.upsert("t1", recover_verification={
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        }, result={"recovered": True})
        metric = await store.get_metric("t1")
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
