"""Cancellation cleanup for execution helper artifacts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.server.routes.turn_event_stream import (
    TurnContext,
    _cleanup_cancelled_execution_artifacts,
    _finalize_task_session,
)


@pytest.mark.asyncio
async def test_cancel_cleanup_uses_current_pipeline_checkpoint():
    artifact = {
        "artifact_id": "uid-1",
        "type": "debug_pod",
        "status": "active",
        "name": "debug-pod",
        "namespace": "ns",
        "uid": "uid-1",
        "target": {"scope": "node", "name": "node-a"},
    }
    cleaned_artifact = {**artifact, "status": "cleaned"}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values={
            "task_id": "task-1",
            "kubeconfig": "/tmp/kubeconfig",
            "execution_artifacts": [artifact],
        }))
    )
    ctx = TurnContext(
        sid="sid",
        turn_id="turn-1",
        thread_id="thread-1",
        input_text="inject",
        permission_mode="confirm",
        dry_run=False,
        req=SimpleNamespace(),
        store=SimpleNamespace(),
        agents={},
        task_tracker=SimpleNamespace(),
        intent_graph=graph,
        pipeline_graph=graph,
        graph_config={"configurable": {"thread_id": "task-1"}},
        initial_state={},
        tracker_key="tracker",
        tracker_queue=__import__("asyncio").Queue(),
    )
    ctx.result_graph = graph
    ctx.result_config = ctx.graph_config

    with patch(
        "chaos_agent.agent.execution_artifacts.cleanup_debug_pod_artifacts",
        new=AsyncMock(return_value=([cleaned_artifact], ["debug-pod"])),
    ) as cleanup, patch(
        "chaos_agent.agent.nodes.store._store_sync.sync_to_store",
        new=AsyncMock(),
    ) as sync:
        await _cleanup_cancelled_execution_artifacts(ctx)

    cleanup.assert_awaited_once_with(
        [artifact],
        kubeconfig="/tmp/kubeconfig",
        task_id="task-1",
    )
    sync.assert_awaited_once()
    assert sync.await_args.args[1] == {"execution_artifacts": [cleaned_artifact]}


@pytest.mark.asyncio
async def test_defensive_finalize_uses_canonical_inject_finalizer():
    values = {
        "task_id": "task-failed",
        "operation": "inject",
        "error": "execution_failed: dependency missing",
        "messages": [object()],
    }
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ) as finalize:
        await _finalize_task_session(
            graph,
            {"configurable": {"thread_id": "task-failed"}},
            "turn-1",
            lambda _task_id: None,
        )

    finalize.assert_awaited_once()
    assert finalize.await_args.kwargs["precomputed_values"] is values


@pytest.mark.asyncio
async def test_defensive_finalize_preserves_explicit_user_cancellation():
    values = {"task_id": "task-cancelled", "operation": "inject", "messages": []}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ) as finalize:
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None, cancelled=True,
        )

    assert finalize.await_args.kwargs["status_override"] == "cancelled"
