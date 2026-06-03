"""Tests for recover_handler bridge node.

The query_handler / explore_handler nodes were removed: their work is now
done inline by intent_clarification's LLM via kubectl / read_skill_resource.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.recover_handler import recover_handler


class TestRecoverHandler:
    """Tests for recover_handler bridge node."""

    @pytest.mark.asyncio
    async def test_no_active_experiments(self, sample_agent_state):
        """No active experiments → inform user."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[])

        with patch("chaos_agent.agent.nodes.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["result"]["status"] == "completed"
        assert "没有活跃" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_single_active_experiment_auto_select(self, sample_agent_state):
        """Exactly 1 active experiment → auto-select with enriched detail."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[
            {"task_id": "task-001", "blade_uid": "exp-abc"},
        ])
        mock_store.get = AsyncMock(return_value={
            "task_id": "task-001",
            "fault_type": "pod-cpu-fullload",
            "blade_uid": "exp-abc",
            "target": {"namespace": "cms-demo"},
        })

        with patch("chaos_agent.agent.nodes.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
        assert result["blade_uid"] == "exp-abc"
        assert "1 个活跃" in result["messages"][0].content
        assert "pod-cpu-fullload" in result["messages"][0].content  # enriched fault_type

    @pytest.mark.asyncio
    async def test_multiple_active_experiments_needs_selection(self, sample_agent_state):
        """Multiple active experiments → list for user selection."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[
            {"task_id": "task-001"},
            {"task_id": "task-002"},
        ])
        mock_store.get = AsyncMock(side_effect=[
            {"task_id": "task-001", "fault_type": "pod-cpu-fullload", "target": {"namespace": "cms-demo"}, "blade_uid": "exp-1"},
            {"task_id": "task-002", "fault_type": "pod-mem-load", "target": {"namespace": "default"}, "blade_uid": "exp-2"},
        ])

        with patch("chaos_agent.agent.nodes.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["needs_task_selection"] is True
        assert "多个" in result["messages"][0].content
        assert "pod-cpu-fullload" in result["messages"][0].content  # enriched

    @pytest.mark.asyncio
    async def test_query_active_failure(self, sample_agent_state):
        """Task store failure → error message, still set operation=recover."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(side_effect=Exception("DB error"))

        with patch("chaos_agent.agent.nodes.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["result"]["status"] == "failed"
        assert "失败" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_enrichment_fallback_to_raw_data(self, sample_agent_state):
        """store.get returns None for a task → fall back to query_active raw data."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[
            {"task_id": "task-001", "blade_uid": "exp-abc"},
        ])
        mock_store.get = AsyncMock(return_value=None)  # get fails → fallback to raw

        with patch("chaos_agent.agent.nodes.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
