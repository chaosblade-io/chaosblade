"""Integration test: Memory flow (experiment store → operational memory → context manager)."""

from chaos_agent.memory.context_manager import ContextManager
from chaos_agent.memory.operational_memory import OperationalMemory
from chaos_agent.persistence.experiment_store import ExperimentStore


class TestMemoryFlow:
    """Integration test for the three-layer memory system."""

    def test_experiment_store_crud(self, tmp_memory_dir):
        """Test full CRUD lifecycle for experiment store."""
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        store = ExperimentStore(history_path)

        # Append
        record = {
            "task_id": "task-001",
            "operation": "inject",
            "target": {"namespace": "default"},
            "status": "success",
        }
        store.append(record)

        # Query by task_id (returns a single dict or None)
        result = store.query_by_task_id("task-001")
        assert result is not None
        assert result["task_id"] == "task-001"

        # Query active
        active = store.query_active_experiments()
        assert len(active) >= 1

    def test_operational_memory_read_write(self, tmp_memory_dir):
        """Test reading and writing operational memory."""
        memory_path = tmp_memory_dir / "MEMORY.md"
        op_memory = OperationalMemory(memory_path)

        # Read initial content
        content = op_memory.read()
        assert "Operational Memory" in content

        # Write new content
        new_content = "# Updated Memory\n\n## Notes\nTest update"
        op_memory.write(new_content)

        # Read back
        content = op_memory.read()
        assert "Updated Memory" in content

    def test_operational_memory_append(self, tmp_memory_dir):
        """Test appending sections to operational memory."""
        memory_path = tmp_memory_dir / "MEMORY.md"
        op_memory = OperationalMemory(memory_path)

        op_memory.append_section("## Experiment Log", "- task-001 completed")

        content = op_memory.read()
        assert "Experiment Log" in content
        assert "task-001" in content

    def test_context_manager_with_messages(self, tmp_memory_dir):
        """Test context manager checking messages."""
        from langchain_core.messages import HumanMessage, AIMessage

        manager = ContextManager(max_tokens=1000)

        messages = [
            HumanMessage(content="Hello"),
            AIMessage(content="Hi there"),
            HumanMessage(content="Inject a fault"),
            AIMessage(content="Sure, let me check"),
        ]

        to_compact, to_keep, is_valid = manager.check_context(messages)
        assert is_valid is True
        assert len(to_keep) > 0

    def test_experiment_store_multiple_records(self, tmp_memory_dir):
        """Test querying with multiple experiment records."""
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        store = ExperimentStore(history_path)

        # Add multiple records (active = inject with status "success" and not recovered)
        for i in range(5):
            store.append({
                "task_id": f"task-{i:03d}",
                "operation": "inject",
                "target": {"namespace": "default" if i % 2 == 0 else "production"},
                "status": "success",
            })

        # Query active (all inject+success with no matching recover)
        active = store.query_active_experiments()
        assert len(active) >= 3

        # Query by namespace (filters on target.namespace)
        default_active = store.query_active_experiments(namespace="default")
        for exp in default_active:
            assert exp.get("target", {}).get("namespace") == "default"

    def test_experiment_store_recent(self, tmp_memory_dir):
        """Test querying recent experiments."""
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        store = ExperimentStore(history_path)

        for i in range(10):
            store.append({
                "task_id": f"task-{i:03d}",
                "operation": "inject",
                "status": "success",
            })

        recent = store.query_recent(limit=3)
        assert len(recent) <= 3

    def test_end_to_end_memory_flow(self, tmp_memory_dir):
        """Test the complete memory flow: write experiment → query → load into context."""
        # 1. Write experiment records
        history_path = tmp_memory_dir / "experiments" / "history.jsonl"
        store = ExperimentStore(history_path)

        store.append({
            "task_id": "task-integration",
            "operation": "inject",
            "skill": "pod-delete",
            "target": {"namespace": "default", "names": ["my-pod"]},
            "status": "success",
        })

        # 2. Write operational notes
        memory_path = tmp_memory_dir / "MEMORY.md"
        op_memory = OperationalMemory(memory_path)
        op_memory.append_section("## Recent Experiments", "- pod-delete on my-pod: success")

        # 3. Query back
        active = store.query_active_experiments(namespace="default")
        notes = op_memory.read()
        assert "Recent Experiments" in notes

        # 4. Context manager can process messages
        manager = ContextManager(max_tokens=2000)
        from langchain_core.messages import HumanMessage
        messages = [HumanMessage(content=notes)]
        to_compact, to_keep, is_valid = manager.check_context(messages)
        assert is_valid is True
        assert len(to_keep) > 0
