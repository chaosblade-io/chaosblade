"""Cancellation cleanup for execution helper artifacts.

Round-64 R64-1 addition: the recover dispatch's preview-safety backstop
(``_run_recover``'s dry_run leg) is pinned here as a behavior test — the
single-variable design keeps every other gate leg TRUE so the early
return is attributable to dry_run alone.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.server.routes.turn_event_stream import (
    TurnContext,
    _cleanup_cancelled_execution_artifacts,
    _finalize_task_session,
    _run_recover,
)


@pytest.mark.asyncio
async def test_run_recover_dry_run_refuses_to_dispatch():
    """Round-64 R64-1: a dry-run turn (/plan preview) must not dispatch
    the REAL recovery. The intent branch now refuses to confirm recover
    under /plan, so the confirmed-intent leg cannot be true on a preview
    today — but the dispatch is the side-effecting act itself, so the
    preview-safety guarantee lives HERE too, at the single source that
    would execute it, not only at the branch that feeds it.

    Single-variable design: every other gate leg is TRUE (confirmed
    intent set, recover task id present, graph not mid-flight), so the
    early return is attributable to ``not ctx.dry_run`` alone — a
    regression that drops the leg walks PAST the gate and fails on the
    unmocked pipeline-graph access below.
    """
    values = {
        "confirmed_intent": "recover",
        "recover_task_id": "task-inj-x",
        "task_id": "recover-abc",
        "messages": [],
    }
    graph = SimpleNamespace(
        aget_state=AsyncMock(
            return_value=SimpleNamespace(values=values, next=())),
    )
    recover_graph = SimpleNamespace(
        astream_events=AsyncMock(side_effect=AssertionError(
            "a dry-run preview must not start the recover graph")),
    )
    ctx = TurnContext(
        sid="sid-1",
        turn_id="turn-1",
        thread_id="thread-1",
        input_text="recover task-inj-x",
        permission_mode="confirm",
        dry_run=True,
        req=SimpleNamespace(is_disconnected=AsyncMock(return_value=False)),
        store=SimpleNamespace(cancel_interrupt=lambda *a, **k: None),
        # NO "pipeline" key on purpose: if the dry_run leg regresses, the
        # gate opens and the resolve's live-context read raises KeyError —
        # the test fails for the RIGHT reason (the dispatch began).
        agents={"recover": recover_graph},
        task_tracker=SimpleNamespace(
            register=lambda *a, **k: None,
            unregister=lambda *a, **k: None,
        ),
        intent_graph=graph,
        pipeline_graph=graph,
        graph_config={"configurable": {"thread_id": "thread-1"}},
        initial_state={},
        tracker_key="tracker-1",
        tracker_queue=asyncio.Queue(),
    )
    ctx.result_graph = graph
    ctx.result_config = ctx.graph_config

    batcher = SimpleNamespace()
    sidewrite = lambda evt: None  # noqa: E731 — never reached on the gate
    converters = SimpleNamespace()

    sse_frames = [
        sse async for sse in _run_recover(
            ctx, graph, ctx.graph_config, batcher, sidewrite, converters,
        )
    ]

    assert sse_frames == [], (
        "a dry-run turn must not stream any recover events"
    )
    recover_graph.astream_events.assert_not_called()
    # No recover coordinates recorded on the turn either — nothing for
    # the finally's G5 fallback to close.
    assert ctx.recover_task_id == ""
    assert ctx.recover_config == {}


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


@pytest.mark.asyncio
async def test_cancelled_finalize_writes_task_row_cancelled():
    """Turn abort must stamp the TaskStore row cancelled: no later writer
    comes, and inference cannot derive "cancelled" on its own."""
    values = {"task_id": "task-cancelled", "operation": "inject", "messages": []}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None, cancelled=True,
        )

    # Round-54: the row write rides the shared guarded helper
    # (stream_abort.write_aborted_task_row) — skip_if_terminal=True is
    # the terminal-regression guard (G6), part of the write's contract.
    task_store.update_task_state.assert_awaited_once_with(
        "task-cancelled", "cancelled", skip_if_terminal=True,
    )


@pytest.mark.asyncio
async def test_clean_finalize_leaves_task_row_to_inference():
    """cancelled=False (clean exit): the row's state stays with normal
    inference — the abort stamp is reserved for actual aborts."""
    values = {"task_id": "task-done", "operation": "inject", "messages": []}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None,
        )

    task_store.update_task_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause", "expected_word"),
    [
        # Interrupt causes: the abort word is "cancelled" — the run was
        # killed from outside, its graph never got to finish.
        ("user_cancel", "cancelled"),
        ("disconnected", "cancelled"),
        # Round-55 F1: confirm_timeout is an INTERRUPT cause too — the
        # pipeline was parked at its confirmation gate waiting for the
        # user and nothing was injected. The r54 inline tuple mapped it to
        # "failed" while the same module called it "a DESIGNED pause" in
        # three other places (the rollback table, the handler comment, the
        # decision-table docstring): a /tasks row saying FAILED for a run
        # that never started is the declared-vs-implemented domain split.
        ("confirm_timeout", "cancelled"),
        # Crash cause: the run's own machinery failed — "failed".
        ("internal_error", "failed"),
    ],
)
async def test_abort_cleanup_writes_pipeline_row_terminal_word(cause, expected_word):
    """Round-54 G4: the single-source cleanup writes the dispatched
    pipeline's TaskStore row terminal word for EVERY abort cause.

    The r53 triad was wired for the cancel exits only: a crashed dispatch
    (internal_error) left its row a zombie at the last mid-graph upsert —
    "injecting" forever in /tasks, no later writer coming. The decision
    table's own arm carries the write (not a hand-written handler await),
    through the shared guarded helper.
    """
    from unittest.mock import AsyncMock

    from chaos_agent.server.routes.turn_event_stream import _abort_turn_cleanup

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
        intent_graph=SimpleNamespace(),
        pipeline_graph=SimpleNamespace(),
        graph_config={"configurable": {"thread_id": "thread-1"}},
        initial_state={},
        tracker_key="tracker",
        tracker_queue=__import__("asyncio").Queue(),
    )
    # The dispatch marker the real _run_inject_pipeline writes at handoff
    # time — the arm keys on it (and on non-dry-run).
    ctx.pipeline_task_id = "inject-42"

    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._rollback_intent_checkpoint_for_turn",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._clear_dispatched_inject_intent_state",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._write_interrupted_record",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        # capture_failed=True skips the rollback branch — not this test's
        # subject (pinned by test_internal_error_rolls_back_intent_checkpoint_before_record).
        await _abort_turn_cleanup(ctx, cause=cause, capture_failed=True)

    task_store.update_task_state.assert_awaited_once_with(
        "inject-42", expected_word, skip_if_terminal=True,
    )


@pytest.mark.asyncio
async def test_abort_cleanup_skips_row_when_no_pipeline_dispatched():
    """The G4 arm keys on a real dispatch: a chat turn (no pipeline
    handed off) must not stamp any row — there is no operation row to
    finalize, and the turn's own session finalize handles the rest."""
    from unittest.mock import AsyncMock

    from chaos_agent.server.routes.turn_event_stream import _abort_turn_cleanup

    ctx = TurnContext(
        sid="sid",
        turn_id="turn-1",
        thread_id="thread-1",
        input_text="hello",
        permission_mode="confirm",
        dry_run=False,
        req=SimpleNamespace(),
        store=SimpleNamespace(),
        agents={},
        task_tracker=SimpleNamespace(),
        intent_graph=SimpleNamespace(),
        pipeline_graph=SimpleNamespace(),
        graph_config={"configurable": {"thread_id": "thread-1"}},
        initial_state={},
        tracker_key="tracker",
        tracker_queue=__import__("asyncio").Queue(),
    )  # pipeline_task_id left empty — a chat turn

    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._rollback_intent_checkpoint_for_turn",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._clear_dispatched_inject_intent_state",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._write_interrupted_record",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _abort_turn_cleanup(ctx, cause="user_cancel", capture_failed=True)

    task_store.update_task_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_but_paused_at_interrupt_keeps_row_alive():
    """paused_at_interrupt means the graph is parked at a confirmation
    card waiting for resume — an abort flag must NOT stamp the row
    cancelled (the task is resumable, that is the whole point of the
    interrupt checkpoint)."""
    values = {"task_id": "task-paused", "operation": "inject", "messages": []}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=("intent_confirm",))),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None, cancelled=True,
        )

    task_store.update_task_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cause", "expected_word"),
    [
        ("confirm_timeout", "cancelled"),
        ("internal_error", "failed"),
    ],
)
async def test_no_flag_causes_classify_session_word_from_cause_memo(
    cause, expected_word,
):
    """Round-60 F1''': the two abort causes that set NO ``_turn_cancelled``
    flag (confirm_timeout — the ConfirmTimeout handler never flags;
    internal_error — the generic crash exit) must reach the session
    surface with their OWN classified word. Before round-60 they landed
    on the inference path (``status_override=None``) and the intent-graph
    values — no verdict — mapped them to "failed": a confirm_timeout
    abort said "failed" on the session while the G4 arm's row said
    "cancelled" for the SAME event.
    """
    values = {"task_id": "task-ct", "operation": "inject", "messages": []}
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ) as finalize, patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None,
            cancelled=False, abort_cause=cause,
        )

    assert finalize.await_args.kwargs["status_override"] == expected_word
    task_store.update_task_state.assert_awaited_once_with(
        "task-ct", expected_word, skip_if_terminal=True,
    )


@pytest.mark.asyncio
async def test_recover_shaped_state_leaves_session_to_recover_fallback():
    """Round-60 F5''': a user-requested recover turn's intent state
    carries the RECOVER session id with NO ``operation`` field (the
    clarification recover branch never sets it), so the inject-arm
    classification would misread it — wrong graph, fail-OPEN word
    (infer_task_state maps the "recover" bridge state to "completed"),
    and no parent_task_id link. The finally's G5 fallback owns
    recover-session closure: finalize, append AND the row write must all
    stay silent here.
    """
    values = {
        "task_id": "recover-abc",
        "confirmed_intent": "recover",
        "recover_task_id": "inject-x",
        "messages": [],
        # deliberately NO "operation" key — the recover branch's shape
    }
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(
        has_active=lambda _task_id: True,
        append_messages=AsyncMock(),
        finalize_session=AsyncMock(),
    )
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ) as finalize, patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None,
            cancelled=True, abort_cause="user_cancel",
        )

    finalize.assert_not_awaited()
    store.append_messages.assert_not_called()
    store.finalize_session.assert_not_called()
    task_store.update_task_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_reached_verdict_keeps_session_word_over_cause_gate():
    """Round-61 R61-5/P5: the cause gate classifies runs still MID-FLIGHT
    only. An auto-recover turn aborted DURING the recover segment reads
    the inject pipeline's own thread here with its verdict already on
    record — the bare cause gate (round-60 shape) force-overrode the
    COMPLETED session to the abort word; the gate must yield to the
    verdict (`infer_task_state != "injecting"`), on the row write too.
    The row surface's skip_if_terminal guard was already this rule —
    this pins it on the session surface of the turn stream."""
    values = {
        "task_id": "task-done",
        "operation": "inject",
        "messages": [],
        "verification": {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }
    graph = SimpleNamespace(
        aget_state=AsyncMock(return_value=SimpleNamespace(values=values, next=())),
    )
    store = SimpleNamespace(has_active=lambda _task_id: True)
    task_store = SimpleNamespace(update_task_state=AsyncMock())

    with patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        return_value=store,
    ), patch(
        "chaos_agent.memory.session_finalizer.finalize_inject_session",
        new=AsyncMock(),
    ) as finalize, patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=task_store),
    ):
        await _finalize_task_session(
            graph, {}, "turn-1", lambda _task_id: None,
            cancelled=False, abort_cause="internal_error",
        )

    assert finalize.await_args.kwargs["status_override"] is None
    task_store.update_task_state.assert_not_awaited()
