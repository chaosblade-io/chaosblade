"""Tests for experiment history store (JSONL)."""

from chaos_agent.persistence.experiment_store import ExperimentStore


class TestExperimentStoreAppend:
    """Test appending records."""

    def test_append_record(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({"task_id": "t1", "operation": "inject", "status": "success"})
        # Verify file exists and has content
        records = store.query_recent(limit=10)
        assert len(records) == 1
        assert records[0]["task_id"] == "t1"

    def test_append_adds_timestamp(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({"task_id": "t1"})
        records = store.query_recent(limit=1)
        assert "timestamp" in records[0]

    def test_append_multiple_records(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        for i in range(5):
            store.append({"task_id": f"t{i}", "operation": "inject", "status": "success"})
        records = store.query_recent(limit=10)
        assert len(records) == 5


class TestExperimentStoreQueryByTaskId:
    """Test querying by task_id."""

    def test_found(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({"task_id": "t1", "operation": "inject", "status": "success"})
        store.append({"task_id": "t2", "operation": "inject", "status": "success"})

        result = store.query_by_task_id("t1")
        assert result is not None
        assert result["task_id"] == "t1"

    def test_not_found(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({"task_id": "t1", "operation": "inject"})
        result = store.query_by_task_id("nonexistent")
        assert result is None

    def test_returns_most_recent(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({"task_id": "t1", "operation": "inject", "status": "success"})
        store.append({"task_id": "t1", "operation": "recover", "status": "success"})

        result = store.query_by_task_id("t1")
        assert result["operation"] == "recover"

    def test_nonexistent_file(self, tmp_path):
        store = ExperimentStore(tmp_path / "nonexistent" / "history.jsonl")
        result = store.query_by_task_id("any")
        assert result is None


class TestExperimentStoreQueryActive:
    """Test querying active experiments."""

    def test_active_inject_not_recovered(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({
            "task_id": "t1",
            "operation": "inject",
            "status": "success",
            "target": {"namespace": "default", "names": ["pod1"]},
        })
        result = store.query_active_experiments()
        assert len(result) == 1

    def test_recovered_not_listed(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({
            "task_id": "t1",
            "operation": "inject",
            "status": "success",
            "target": {"namespace": "default", "names": ["pod1"]},
        })
        store.append({
            "task_id": "t1",
            "operation": "recover",
            "status": "success",
        })
        result = store.query_active_experiments()
        assert len(result) == 0

    def test_namespace_filter(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({
            "task_id": "t1",
            "operation": "inject",
            "status": "success",
            "target": {"namespace": "default", "names": ["pod1"]},
        })
        store.append({
            "task_id": "t2",
            "operation": "inject",
            "status": "success",
            "target": {"namespace": "prod", "names": ["pod2"]},
        })
        result = store.query_active_experiments(namespace="default")
        assert len(result) == 1
        assert result[0]["task_id"] == "t1"

    def test_target_name_filter(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        store.append({
            "task_id": "t1",
            "operation": "inject",
            "status": "success",
            "target": {"namespace": "default", "names": ["pod1", "pod2"]},
        })
        result = store.query_active_experiments(target_name="pod1")
        assert len(result) == 1

    def test_empty_file(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        result = store.query_active_experiments()
        assert result == []


class TestExperimentStoreQueryRecent:
    """Test querying recent records."""

    def test_limit(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        for i in range(10):
            store.append({"task_id": f"t{i}"})
        records = store.query_recent(limit=3)
        assert len(records) == 3

    def test_returns_last_n(self, tmp_path):
        store = ExperimentStore(tmp_path / "history.jsonl")
        for i in range(5):
            store.append({"task_id": f"t{i}"})
        records = store.query_recent(limit=2)
        assert records[0]["task_id"] == "t3"
        assert records[1]["task_id"] == "t4"
