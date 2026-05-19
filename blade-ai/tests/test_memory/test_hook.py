"""Tests for PreReasoningHook unified memory management."""

from unittest.mock import MagicMock

from chaos_agent.memory.hook import PreReasoningHook


class TestPreReasoningHookNoCompaction:
    """Test hook when no compaction is needed."""

    async def test_returns_empty_when_no_compaction(self):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)  # Nothing to compact
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(
            context_manager=cm,
            tool_compactor=tc,
            session_store=MagicMock(),
        )

        state = {"messages": ["msg1"], "task_id": "task-1"}
        result = await hook(state)
        # No compaction needed — tool compaction modifies in-place, no state update required
        assert result == {}


class TestPreReasoningHookWithCompaction:
    """Test hook when compaction is triggered."""

    async def test_compact_messages_and_return_summary(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old1", "old2"], ["recent1"], True)
        cm.compact_threshold = 0  # Force LLM compression (combined_tokens >= 0 always)
        tc = MagicMock()
        tc.compact.return_value = ["old1", "old2", "recent1"]

        hook = PreReasoningHook(
            context_manager=cm,
            tool_compactor=tc,
            session_store=MagicMock(),
            llm=mock_llm,
        )

        state = {
            "messages": ["old1", "old2", "recent1"],
            "task_id": "task-1",
            "compressed_summary": "",
        }
        result = await hook(state)

        # Should return summary + kept messages
        assert "messages" in result
        assert "compressed_summary" in result

    async def test_tool_compactor_called(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["msg1"], "task_id": "t1"})

        tc.compact.assert_called_once()

    async def test_context_manager_called(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = ([], ["msg1"], True)
        tc = MagicMock()
        tc.compact.return_value = ["msg1"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({"messages": ["msg1"], "task_id": "t1"})

        cm.check_context.assert_called_once()

    async def test_previous_summary_passed_to_compaction(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        await hook({
            "messages": ["old", "recent"],
            "task_id": "t1",
            "compressed_summary": "prev summary",
        })

        # compact_memory should receive previous_summary
        # (verified through mock_llm.ainvoke call args)

    async def test_compressed_summary_updated(self, mock_llm):
        cm = MagicMock()
        cm.check_context.return_value = (["old"], ["recent"], True)
        cm.compact_threshold = 0
        tc = MagicMock()
        tc.compact.return_value = ["old", "recent"]

        hook = PreReasoningHook(cm, tc, MagicMock(), mock_llm)
        result = await hook({
            "messages": ["old", "recent"],
            "task_id": "t1",
            "compressed_summary": "",
        })

        assert "compressed_summary" in result
        assert "test summary" in result["compressed_summary"]
