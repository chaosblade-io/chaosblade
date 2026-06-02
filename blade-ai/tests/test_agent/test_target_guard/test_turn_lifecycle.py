"""Tests for turn-boundary lifecycle of ``approved_target`` /
``screener_route``.

These fields are PER-TURN state, but LangGraph checkpoints persist
everything across turns by default. Without an explicit clear at
turn-start, a chat-only follow-up turn would inherit the previous
inject turn's frozen approval and the screener would compare
unrelated tool_calls against a stale snapshot.

Cleared in ``load_memory`` (the inject graph entry point) because
that node runs once per fresh graph invocation. Resume-from-interrupt
re-enters at the interrupted node, not at load_memory, so an
in-flight turn keeps its approval intact across the user's approve
gesture.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.nodes.memory_nodes import load_memory


@pytest.mark.asyncio
async def test_load_memory_clears_stale_approved_target():
    # State carrying a leftover approved_target from a previous turn
    # (typical when the checkpoint thread is reused across turns).
    state = {
        "task_id": "t1",
        "approved_target": {
            "scope": "pod", "namespace": "old-ns",
            "names": ["old-pod"], "labels": {},
            "is_namespace_wide": False, "blade_target": "cpu",
            "blade_action": "fullload", "lock_fault_type": True,
        },
        "screener_route": "pass",  # stale from previous turn
        "target": {},
    }
    with patch("chaos_agent.agent.nodes.memory_nodes.OperationalMemory") as MockMem, \
         patch("chaos_agent.persistence.task_store.get_task_store",
               new=AsyncMock(return_value=MagicMock(
                   query_active=AsyncMock(return_value=[]),
               ))), \
         patch("chaos_agent.agent.nodes.memory_nodes.sync_to_store",
               new=AsyncMock()):
        MockMem.return_value.read.return_value = ""
        updates = await load_memory(state)

    assert updates["approved_target"] is None
    assert updates["screener_route"] is None


@pytest.mark.asyncio
async def test_load_memory_clear_preserves_other_state():
    # The clear must not wipe unrelated fields the caller set.
    state = {
        "task_id": "t2",
        "approved_target": {"scope": "pod"},
        "experiment_history": ["should-not-leak-here"],  # caller-supplied
        "target": {},
    }
    with patch("chaos_agent.agent.nodes.memory_nodes.OperationalMemory") as MockMem, \
         patch("chaos_agent.persistence.task_store.get_task_store",
               new=AsyncMock(return_value=MagicMock(
                   query_active=AsyncMock(return_value=[]),
               ))), \
         patch("chaos_agent.agent.nodes.memory_nodes.sync_to_store",
               new=AsyncMock()):
        MockMem.return_value.read.return_value = "some notes"
        updates = await load_memory(state)

    assert updates["approved_target"] is None
    # operational_notes and experiment_history come from fresh loads,
    # NOT from input state — the clear shouldn't bleed across.
    assert "operational_notes" in updates
    assert updates["experiment_history"] == []
