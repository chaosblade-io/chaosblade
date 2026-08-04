"""Tests for recover_handler bridge node.

The query_handler / explore_handler nodes were removed: their work is now
done inline by intent_clarification's LLM via kubectl / read_skill_resource.
"""

from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.agent.nodes.recover.recover_handler import recover_handler


class TestQueryActiveExperimentsTool:
    """query_active_experiments renders discriminating fields, newest first."""

    @pytest.mark.asyncio
    async def test_rich_render_and_ordering(self):
        rows = [
            {
                "task_id": "task-old",
                "skill": "k8s-chaos-skills",
                "fault_type": "pod-cpu-fullload",
                "target": {"namespace": "taokeeper", "names": ["tk-0"]},
                "gmt_create": "2026-06-20T09:00:00+08:00",
                "plan_summary": "",
            },
            {
                "task_id": "task-new",
                "skill": "k8s-chaos-skills",
                "fault_type": "pod-image-error",
                "target": {"namespace": "reg-center", "names": ["registry-sts"]},
                "gmt_create": "2026-06-23T15:02:00+08:00",
                "plan_summary": "将 StatefulSet registry-sts 镜像改为无效值",
            },
        ]
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=rows)

        with patch("chaos_agent.persistence.task_store.get_task_store",
                   return_value=mock_store):
            from chaos_agent.agent.nodes.planning.intent_clarification import (
                query_active_experiments,
            )
            out = await query_active_experiments.ainvoke({})

        # Real fault types shown, not the generic skill package name.
        assert "pod-image-error" in out
        assert "pod-cpu-fullload" in out
        assert "fault_type=k8s-chaos-skills" not in out
        # Target resource + description surfaced.
        assert "reg-center/registry-sts" in out
        assert "将 StatefulSet registry-sts 镜像改为无效值" in out
        # Newest first: task-new appears before task-old.
        assert out.index("task-new") < out.index("task-old")

    @pytest.mark.asyncio
    async def test_no_active(self):
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[])
        with patch("chaos_agent.persistence.task_store.get_task_store",
                   return_value=mock_store):
            from chaos_agent.agent.nodes.planning.intent_clarification import (
                query_active_experiments,
            )
            out = await query_active_experiments.ainvoke({})
        assert "no active fault-injection experiments" in out


class TestRecoverHandler:
    """Tests for recover_handler bridge node."""

    @pytest.mark.asyncio
    async def test_no_active_experiments(self, sample_agent_state):
        """No active experiments → inform user."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[])

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["result"]["status"] == "completed"
        assert "no active fault-injection experiments" in result["messages"][0].content

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

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
        assert result["blade_uid"] == "exp-abc"
        assert "Found 1 active experiment" in result["messages"][0].content
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

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["needs_task_selection"] is True
        assert "Found multiple active experiments" in result["messages"][0].content
        assert "pod-cpu-fullload" in result["messages"][0].content  # enriched

    @pytest.mark.asyncio
    async def test_query_active_failure(self, sample_agent_state):
        """Task store failure → error message, still set operation=recover."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(side_effect=Exception("DB error"))

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["result"]["status"] == "failed"
        assert "Failed to query active experiments" in result["messages"][0].content

    @pytest.mark.asyncio
    async def test_pass_through_when_recover_task_id_set(self, sample_agent_state):
        """recover_task_id already resolved by intent_clarification → skip store query."""
        sample_agent_state["recover_task_id"] = "task-already-known"

        result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-already-known"
        assert "messages" not in result

    @pytest.mark.asyncio
    async def test_enrichment_fallback_to_raw_data(self, sample_agent_state):
        """store.get returns None for a task → fall back to query_active raw data."""
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=[
            {"task_id": "task-001", "blade_uid": "exp-abc"},
        ])
        mock_store.get = AsyncMock(return_value=None)  # get fails → fallback to raw

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
