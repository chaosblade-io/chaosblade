"""Integration test: Memory flow (operational memory → context manager)."""

from chaos_agent.memory.context_manager import ContextManager
from chaos_agent.memory.operational_memory import OperationalMemory


class TestMemoryFlow:
    """Integration test for the memory system."""

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

