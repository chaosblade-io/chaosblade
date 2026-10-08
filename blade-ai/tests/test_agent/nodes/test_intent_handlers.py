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

    @staticmethod
    def _mk_rows(n):
        # gmt_create ascends with the index, so newest-first == reverse index.
        return [
            {
                "task_id": f"task-{i:03d}",
                "skill": "k8s-chaos-skills",
                "fault_type": "pod-cpu-fullload",
                "target": {"namespace": "default", "names": [f"pod-{i:03d}"]},
                "gmt_create": f"2026-06-20T09:00:{i:02d}+08:00",
                "plan_summary": "",
            }
            for i in range(n)
        ]

    async def _invoke(self, rows, **kwargs):
        mock_store = AsyncMock()
        mock_store.query_active = AsyncMock(return_value=rows)
        with patch("chaos_agent.persistence.task_store.get_task_store",
                   return_value=mock_store):
            from chaos_agent.agent.nodes.planning.intent_clarification import (
                query_active_experiments,
            )
            out = await query_active_experiments.ainvoke(dict(kwargs))
        return out, mock_store

    @pytest.mark.asyncio
    async def test_first_page_is_transparent_not_silent(self):
        out, _ = await self._invoke(self._mk_rows(25))
        # Default limit=20: the header states the TRUE total (25, not 20) plus
        # the exact withheld count and next offset. That is what separates
        # transparent paging from a silent [:N] cap the model cannot see past.
        assert "There are 25 recoverable" in out
        assert "showing rows 1-20" in out
        assert "5 older experiment(s) not shown" in out
        assert "offset=20" in out
        # Newest row is on page 1; the oldest is withheld to a later page.
        assert "task-024" in out
        assert "task-000" not in out

    @pytest.mark.asyncio
    async def test_second_page_numbering_is_continuous(self):
        out, _ = await self._invoke(self._mk_rows(25), offset=20)
        assert "showing rows 21-25" in out
        # Row numbers continue from 21 instead of restarting at 1.
        assert "\n      21." in out or " 21." in out
        assert "task-004" in out and "task-000" in out
        # Last page: no further-page hint remains.
        assert "older experiment(s) not shown" not in out

    @pytest.mark.asyncio
    async def test_offset_past_end_explains(self):
        out, _ = await self._invoke(self._mk_rows(5), offset=999)
        assert "past the end" in out
        assert "offset=0" in out

    @pytest.mark.asyncio
    async def test_paging_covers_every_row_no_loss(self):
        # The load-bearing contract: unlike a silent cap that permanently drops
        # rows, walking the pages surfaces EVERY candidate, so the real target
        # is only ever deferred to a later page, never lost.
        rows = self._mk_rows(45)
        seen, offset = set(), 0
        while True:
            out, _ = await self._invoke(rows, limit=20, offset=offset)
            for i in range(45):
                if f"task-{i:03d}" in out:
                    seen.add(f"task-{i:03d}")
            if f"offset={offset + 20}" not in out:
                break
            offset += 20
        assert seen == {f"task-{i:03d}" for i in range(45)}

    @pytest.mark.asyncio
    async def test_pagination_stays_in_tool_layer(self):
        # Paging must NOT sink into store.query_active: memory_nodes.load_memory
        # and cli/runner both depend on its full set. The store is still called
        # with no limit/offset — slicing happens strictly above it.
        _, mock_store = await self._invoke(self._mk_rows(25), limit=5)
        mock_store.query_active.assert_awaited_once()
        _, called_kwargs = mock_store.query_active.call_args
        assert "limit" not in called_kwargs and "offset" not in called_kwargs


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
            {"task_id": "task-001", "experiment_uid": "exp-abc"},
        ])
        mock_store.get = AsyncMock(return_value={
            "task_id": "task-001",
            "fault_type": "pod-cpu-fullload",
            "experiment_uid": "exp-abc",
            "target": {"namespace": "cms-demo"},
        })

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
        assert result["experiment_uid"] == "exp-abc"
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
            {"task_id": "task-001", "fault_type": "pod-cpu-fullload", "target": {"namespace": "cms-demo"}, "experiment_uid": "exp-1"},
            {"task_id": "task-002", "fault_type": "pod-mem-load", "target": {"namespace": "default"}, "experiment_uid": "exp-2"},
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
            {"task_id": "task-001", "experiment_uid": "exp-abc"},
        ])
        mock_store.get = AsyncMock(return_value=None)  # get fails → fallback to raw

        with patch("chaos_agent.agent.nodes.recover.recover_handler.get_task_store", return_value=mock_store):
            result = await recover_handler(sample_agent_state)

        assert result["operation"] == "recover"
        assert result["recover_task_id"] == "task-001"
