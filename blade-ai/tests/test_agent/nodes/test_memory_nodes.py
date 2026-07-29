"""Tests for memory_nodes: load_memory and save_memory."""

import pytest

from chaos_agent.agent.nodes.store.memory_nodes import load_memory, pipeline_init, save_memory
from chaos_agent.config.settings import settings


class TestLoadMemory:
    """Tests for the load_memory node function."""

    @pytest.mark.asyncio
    async def test_loads_operational_notes(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = sample_agent_state
        result = await load_memory(state)
        assert "operational_notes" in result
        assert "Operational Memory" in result["operational_notes"]

    @pytest.mark.asyncio
    async def test_loads_experiment_history(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = sample_agent_state
        result = await load_memory(state)
        assert "experiment_history" in result
        assert isinstance(result["experiment_history"], list)

    @pytest.mark.asyncio
    async def test_experiment_history_with_records(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        import json
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        records = [
            {"task_id": "task-1", "operation": "inject", "status": "success", "target": {"namespace": "default"}},
            {"task_id": "task-2", "operation": "inject", "status": "success", "target": {"namespace": "production"}},
        ]
        with open(history_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        state = sample_agent_state
        result = await load_memory(state)
        assert "experiment_history" in result

    @pytest.mark.asyncio
    async def test_handles_missing_memory_dir(self, sample_agent_state, tmp_path, monkeypatch):
        """When memory directory doesn't exist, OperationalMemory creates default content."""
        monkeypatch.setattr(settings, "memory_dir", tmp_path / "nonexistent" / "memory")

        state = sample_agent_state
        result = await load_memory(state)
        # OperationalMemory.read() creates default content if file missing
        assert "operational_notes" in result
        assert "experiment_history" in result

    @pytest.mark.asyncio
    async def test_namespace_filter(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        import json
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        records = [
            {"task_id": "t1", "operation": "inject", "status": "success", "target": {"namespace": "default"}},
            {"task_id": "t2", "operation": "inject", "status": "success", "target": {"namespace": "production"}},
        ]
        with open(history_path, "w") as f:
            for r in records:
                f.write(json.dumps(r) + "\n")

        state = sample_agent_state
        state["target"] = {"namespace": "default"}
        result = await load_memory(state)
        assert "experiment_history" in result

    @pytest.mark.asyncio
    async def test_seeded_human_message_has_explicit_id(
        self, sample_agent_state, tmp_memory_dir, monkeypatch,
    ):
        """Seeded HumanMessages must carry an explicit id.

        The node appends the message to the session store BEFORE the
        LangGraph ``add_messages`` reducer runs. If the message had no id
        at that point, the store's dedup key fell back to type+content,
        while the post-reducer copy (recorded later by the memory hook)
        carried a reducer-assigned UUID — the key mismatch defeated dedup
        and the same human message was written twice to the task JSONL.
        """
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = dict(sample_agent_state)
        state["input"] = "注入 CPU 故障"
        for node in (load_memory, pipeline_init):
            result = await node(dict(state))
            msgs = result.get("messages") or []
            assert msgs, f"{node.__name__} should seed a HumanMessage"
            assert msgs[0].id, f"{node.__name__} message must have explicit id"

    @pytest.mark.asyncio
    async def test_direct_mode_message_has_explicit_id(
        self, sample_agent_state, tmp_memory_dir, monkeypatch,
    ):
        """Direct-mode synthesised HumanMessages also need an explicit id."""
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = dict(sample_agent_state)
        state["input"] = ""
        for node in (load_memory, pipeline_init):
            result = await node(dict(state))
            msgs = result.get("messages") or []
            if msgs:  # only asserted when the spec is complete enough to seed
                assert msgs[0].id, f"{node.__name__} message must have explicit id"


class TestSaveMemory:
    """Tests for the save_memory node function."""

    @pytest.mark.asyncio
    async def test_saves_experiment_record(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = sample_agent_state
        state["task_id"] = "task-save-001"
        state["skill_name"] = "pod-delete"
        state["blade_uid"] = "uid-123"
        state["operation"] = "inject"
        state["error"] = None

        result = await save_memory(state)
        assert "finished_at" in result

    @pytest.mark.asyncio
    async def test_saves_failed_experiment(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        import chaos_agent.persistence.task_store as store_mod
        monkeypatch.setattr(store_mod, "_store", None)
        monkeypatch.setattr(store_mod.settings, "tasks_db_path", tmp_memory_dir / "tasks.db")
        try:
            monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

            state = sample_agent_state
            state["task_id"] = "task-save-002"
            state["skill_name"] = "pod-delete"
            state["blade_uid"] = ""
            state["operation"] = "inject"
            state["error"] = "Execution failed"

            result = await save_memory(state)
            assert "finished_at" in result

            # Verify in TaskStore
            store = await store_mod.get_task_store()
            task = await store.get("task-save-002")
            assert task is not None
            assert task["task_id"] == "task-save-002"
        finally:
            await store_mod.reset_task_store()

    @pytest.mark.asyncio
    async def test_handles_missing_directory(self, sample_agent_state, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_path / "nonexistent" / "memory")

        state = sample_agent_state
        state["task_id"] = "task-save-003"
        state["error"] = None

        result = await save_memory(state)
        assert "finished_at" in result

    @pytest.mark.asyncio
    async def test_returns_finished_at(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

        state = sample_agent_state
        result = await save_memory(state)
        assert "finished_at" in result
        assert result["finished_at"]  # non-empty ISO timestamp

    @pytest.mark.asyncio
    async def test_record_structure(self, sample_agent_state, tmp_memory_dir, monkeypatch):
        import chaos_agent.persistence.task_store as store_mod
        monkeypatch.setattr(store_mod, "_store", None)
        monkeypatch.setattr(store_mod.settings, "tasks_db_path", tmp_memory_dir / "tasks.db")
        try:
            monkeypatch.setattr(settings, "memory_dir", tmp_memory_dir)

            state = sample_agent_state
            state["task_id"] = "task-struct"
            state["skill_name"] = "network-delay"
            state["target"] = {"namespace": "default", "names": ["my-pod"]}
            state["params"] = {"duration": 60}
            state["blade_uid"] = "uid-struct"
            state["operation"] = "inject"
            state["error"] = None

            await save_memory(state)

            # Verify in TaskStore
            store = await store_mod.get_task_store()
            task = await store.get("task-struct")
            assert task is not None
            assert task["task_id"] == "task-struct"
            assert task["operation"] == "inject"
            assert task["skill_name"] == "network-delay"
        finally:
            await store_mod.reset_task_store()
