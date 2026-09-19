"""Round-46 fix: the internal_error exit rolls the intent thread back.

The turn generator has three crash-like exits. User-cancel and
client-disconnect fork the intent thread back to the pre-turn checkpoint
so the next turn doesn't run on top of the crashed turn's half-dialogue.
Until round-46 the internal_error exit skipped the rollback and wrote its
interrupted record onto the dirty mid-graph state instead — the next turn
then ran ON TOP of the crashed turn's messages.

This test drives the REAL event_generator through a node failure (the
astream_events iterator raises on first pull, exactly how a LangGraph
node exception surfaces) and asserts the fix's two behavioral
properties: the rollback fires (fork to the pre-turn checkpoint) AND it
fires BEFORE the interrupted record is written (the record lands on the
clean state — the cancel path's own ordering rationale, which the fix
comment cites).
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.server.routes.turn_event_stream import TurnContext, event_generator


def _make_req():
    async def _not_disconnected():
        return False

    return SimpleNamespace(is_disconnected=_not_disconnected)


def _make_ctx(intent_graph):
    ctx = TurnContext(
        sid="sid-1",
        turn_id="turn-1",
        thread_id="thread-1",
        input_text="inject cpu fault",
        permission_mode="confirm",
        dry_run=False,
        req=_make_req(),
        store=SimpleNamespace(cancel_interrupt=lambda *a, **k: None),
        agents={},
        task_tracker=SimpleNamespace(
            register=lambda *a, **k: None,
            unregister=lambda *a, **k: None,
        ),
        intent_graph=intent_graph,
        pipeline_graph=intent_graph,
        graph_config={"configurable": {"thread_id": "thread-1"}},
        initial_state={"messages": []},
        tracker_key="tracker-1",
        tracker_queue=asyncio.Queue(),
    )
    ctx.result_graph = intent_graph
    ctx.result_config = ctx.graph_config
    return ctx


def _make_abort_chain(ctx):
    """Fake _abort_turn_cleanup that records the REAL cause memo.

    Patching the abort chain with a bare AsyncMock leaves ctx.abort_cause
    unset, so the finally's fallback classifies via the empty-string
    defensive default — tests would assert the right word for the wrong
    reason. This fake does the one externally visible thing the real
    chain does before anything else (round-55 F2): records the cause.
    """
    from unittest.mock import AsyncMock

    inner = AsyncMock()

    async def chain(*args, **kwargs):
        ctx.abort_cause = kwargs.get("cause", "")
        await inner(*args, **kwargs)

    return chain


@pytest.mark.asyncio
async def test_internal_error_rolls_back_intent_checkpoint_before_record():
    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        # Call 1: the generator's pre-turn capture (must see the
        # checkpoint_id it will later fork from). Later calls: the
        # interrupted-record snapshot read and any finalize reads.
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        calls.append((
            "rollback",
            config["configurable"].get("checkpoint_id"),
            config["configurable"].get("checkpoint_ns", "<MISSING>"),
            values,
        ))

    async def _boom(*args, **kwargs):
        # A node exception surfacing through astream_events: the first
        # pull of the iterator raises (no event ever reaches the
        # converters, so the failure mode is unambiguous).
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        calls.append(("record",))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    frames = []
    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ):
        async for sse in event_generator(ctx):
            frames.append(sse)

    # 1. The rollback fired, forking to the pre-turn checkpoint (the
    #    established-thread branch of _rollback_intent_checkpoint). The
    #    checkpoint_ns entry is load-bearing (round-47): without it the
    #    real checkpointer raises KeyError and the fail-soft handler
    #    silently turns the rollback into a no-op.
    rollbacks = [c for c in calls if c[0] == "rollback"]
    assert rollbacks == [
        ("rollback", "cp-pre", "", {"messages": []}),
    ], f"expected exactly one pre-turn fork rollback, got {calls}"

    # 2. The record was written, and AFTER the rollback (written first it
    #    would be discarded together with the dirt by the fork).
    assert calls[-1][0] == "record", (
        f"the interrupted record must be the LAST intent-thread write, "
        f"got {calls}"
    )

    # 3. The turn still terminates honestly over SSE: an error frame
    #    carrying the exception, then the done marker.
    assert any("error" in f and "node blew up" in f for f in frames), frames
    assert any('"type": "done"' in f or '"type":"done"' in f for f in frames), frames


@pytest.mark.asyncio
async def test_internal_error_new_thread_sweeps_messages(monkeypatch):
    """Brand-new thread (no pre-turn checkpoint): rollback falls back to
    the RemoveMessage sweep instead of the fork — the dirty half-dialogue
    is removed, not kept."""
    calls = []

    aget_calls = {"n": 0}

    class _Msg:
        def __init__(self, mid):
            self.id = mid

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        if aget_calls["n"] == 1:
            # Pre-turn capture on a brand-new thread: no checkpoint yet.
            return SimpleNamespace(
                created_at=None,
                config={"configurable": {}},
                values={},
                next=(),
            )
        # The rollback's dirty-state read: the crashed turn left two
        # messages behind (user input + a half AIMessage).
        return SimpleNamespace(
            created_at="2026-09-17T00:00:01Z",
            config={"configurable": {"checkpoint_id": "cp-dirty"}},
            values={"messages": [_Msg("h1"), _Msg("ai-1")]},
            next=("intent_confirm",),
        )

    async def fake_aupdate_state(config, values, as_node=None):
        removed = values.get("messages")
        is_sweep = removed and all(
            getattr(m, "type", "") == "remove" for m in removed
        )
        calls.append(("update", is_sweep, len(removed) if removed else 0))

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        calls.append(("record",))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ):
        async for sse in event_generator(ctx):
            pass

    # The sweep branch: exactly one update whose payload is RemoveMessage
    # entries covering BOTH dirty messages — then the record.
    updates = [c for c in calls if c[0] == "update"]
    assert updates == [("update", True, 2)], calls
    assert calls[-1][0] == "record", calls


@pytest.mark.asyncio
async def test_internal_error_skips_rollback_when_record_already_written():
    """Round-47 layer-2: once the turn wrote its operation record, the
    rollback must NOT fire — the fork discards the record (the dialogue's
    only live-fault memory) together with the dirt, and the record writer
    skips on the same flag, so nothing would re-write it: rolling back a
    record-bearing thread would amputate live-fault awareness."""
    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        calls.append(("rollback", config))

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)
    # The turn already recorded its operation (pipeline completed before
    # the crash — e.g. the failure hit during result assembly).
    ctx.operation_record_written = True

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    frames = []
    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ):
        async for sse in event_generator(ctx):
            frames.append(sse)

    # No rollback, no record write — the completed operation keeps both
    # its dialogue and its live-fault awareness.
    assert calls == [], (
        f"a record-bearing turn must not be rolled back, got {calls}"
    )
    assert any("error" in f and "node blew up" in f for f in frames), frames
    assert any('"type": "done"' in f or '"type":"done"' in f for f in frames), frames


@pytest.mark.asyncio
async def test_poll_detected_disconnect_sets_cancel_triad_flag():
    """Round-54 G3: the POLL-detected disconnect sets _turn_cancelled.

    The finally block's terminal triad keys on that flag — the r53 triad
    existed but was wired for the scope-cancel exit only, so the
    poll-loser path ran the abort chain yet finalized by INFERENCE
    (cancelled=False: no row word, no status override) while the
    scope-cancel twin wrote "cancelled". Which semantics a disconnect
    gets must not be decided by the poll-vs-cancel race (r48: the cancel
    usually wins, but the poll wins server-side cancels and fast/proxied
    disconnects).

    Discrimination: the poll drives the exit here (no task-group
    cancel) — the finally only sees cancelled=True if the
    ClientDisconnected handler set the flag.
    """
    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)
        calls.append(("rollback",))

    # One event arms the merged-stream poll (it runs per merged event,
    # before parse); the True return aborts. The trailing hang makes a
    # regression visibly hang instead of completing quietly.
    async def _one_then_hang(*args, **kwargs):
        yield {}
        await asyncio.sleep(30)
        yield {}  # pragma: no cover — the poll aborts before this

    intent_graph = SimpleNamespace(
        astream_events=_one_then_hang,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)
    # The poll-loser path: is_disconnected returns True — the exit the
    # RACE decides, simulated directly.
    async def _disconnected():
        return True

    ctx.req = SimpleNamespace(is_disconnected=_disconnected)

    async def fake_write_summary(record, **kwargs):
        await asyncio.sleep(0)
        calls.append(("record",))

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)
        calls.append(("finalize", cancelled))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ):
        # No task-group cancel: the poll itself drives the abort exit.
        async for sse in event_generator(ctx):
            pass

    # The abort chain ran through the single source (rollback + record —
    # "disconnected" is a crash-like cause), and the finally's triad saw
    # cancelled=TRUE — the G3 flag. The pre-fix form finalized with
    # cancelled=False on this exact path.
    assert ("rollback",) in calls, calls
    assert ("record",) in calls, calls
    assert ("finalize", True) in calls, (
        f"the poll-detected disconnect must get the cancel exit's terminal "
        f"semantics (G3), got {calls}"
    )


@pytest.mark.asyncio
async def test_turn_dispatched_recover_abort_gets_finalized_in_finally():
    """Round-54 G5: a turn-dispatched recovery killed mid-run is closed
    by the finally block's fallback.

    _run_recover's session finalize only runs on the NORMAL path, and the
    finally's _finalize_task_session reads the INTENT graph (recover
    turns never set ctx.result_graph) — so before G5 a recovery killed
    mid-run left BOTH its session row active forever AND its recover
    TaskStore row a zombie. The fallback keys on the dispatch coordinates
    recorded AT dispatch plus the normal-path marker (recover_finalized)
    that suppresses it for recoveries that closed themselves.
    """
    from unittest.mock import AsyncMock

    calls = []

    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )

    async def fake_aget_state(config):
        return empty_snap

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    ctx = _make_ctx(intent_graph)
    # The recover dispatch coordinates _run_recover records AT dispatch —
    # a recovery was handed off and never closed itself.
    ctx.recover_task_id = "recover-1"
    ctx.recover_config = {"configurable": {"thread_id": "recover-1"}}

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)
        calls.append(("finalize", cancelled))

    async def fake_rec_finalize(*args, **kwargs):
        await asyncio.sleep(0)
        calls.append((
            "rec_finalize", args[3] if len(args) > 3 else "<id>",
            kwargs.get("default_status"),
        ))

    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        await asyncio.sleep(0)
        calls.append(("taskstore_row", task_id, state, skip_if_terminal))

    fake_task_store = SimpleNamespace(update_task_state=fake_update_task_state)

    session_store = SimpleNamespace(has_active=lambda tid: True)
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream._abort_turn_cleanup",
        new=_make_abort_chain(ctx),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream.finalize_recover_session",
        new=fake_rec_finalize,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: session_store,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=fake_task_store),
    ):
        async for sse in event_generator(ctx):
            pass

    # The fallback closed the orphaned recovery: session finalize with
    # default_status=failed (an aborted recovery never completed), then
    # the recover row's terminal word through the guarded helper. The
    # abort-chain fake records the real cause memo (round-55 F2) so the
    # "failed" here is the CLASSIFIED word for internal_error, not the
    # empty-cause defensive default — a bare AsyncMock patch would leave
    # ctx.abort_cause unset and the assertion would pass for the wrong
    # reason.
    assert ("rec_finalize", "recover-1", "failed") in calls, calls
    assert ("taskstore_row", "recover-1", "failed", True) in calls, calls
    assert (
        calls.index(("rec_finalize", "recover-1", "failed"))
        < calls.index(("taskstore_row", "recover-1", "failed", True))
    ), calls


@pytest.mark.asyncio
async def test_turn_recover_abort_user_cancel_gets_cancelled_word():
    """Round-55 F2: the recover fallback classifies its terminal word
    by the abort CAUSE, not a hard-coded "failed".

    The G5 fallback runs in the finally block for EVERY abort cause — the
    suppression key only spares recoveries that closed themselves — so a
    user-CANCELLED recovery must read "cancelled" in both its session
    finalize (default_status) and its TaskStore row. The r54 first cut
    hard-coded "failed" on both sites: a user cancel showed as a FAILED
    recovery in /tasks. This test runs the REAL abort chain (only its
    decision-table helpers are patched) so the cause memo
    (ctx.abort_cause) is written by the production path the finally
    actually reads.
    """
    from unittest.mock import AsyncMock

    calls = []

    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )

    async def fake_aget_state(config):
        return empty_snap

    async def _cancelled(*args, **kwargs):
        raise asyncio.CancelledError()
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_cancelled,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    ctx = _make_ctx(intent_graph)
    ctx.recover_task_id = "recover-1"
    ctx.recover_config = {"configurable": {"thread_id": "recover-1"}}

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)
        calls.append(("finalize", cancelled))

    async def fake_rec_finalize(*args, **kwargs):
        await asyncio.sleep(0)
        calls.append((
            "rec_finalize", args[3] if len(args) > 3 else "<id>",
            kwargs.get("default_status"),
        ))

    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        await asyncio.sleep(0)
        calls.append(("taskstore_row", task_id, state, skip_if_terminal))

    fake_task_store = SimpleNamespace(update_task_state=fake_update_task_state)

    session_store = SimpleNamespace(has_active=lambda tid: True)
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        # NOT _abort_turn_cleanup itself — the real chain must run so it
        # writes the cause memo the finally reads. Only the decision
        # table's own arms are stubbed (same set the G4 parametrised test
        # patches).
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
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream.finalize_recover_session",
        new=fake_rec_finalize,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: session_store,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=fake_task_store),
    ):
        with pytest.raises(asyncio.CancelledError):
            async for sse in event_generator(ctx):
                pass

    # user_cancel is an interrupt cause: BOTH the session finalize word
    # and the row word classify as "cancelled" — a hard-coded "failed"
    # (the r54 shape) would fail both assertions.
    assert ("rec_finalize", "recover-1", "cancelled") in calls, calls
    assert ("taskstore_row", "recover-1", "cancelled", True) in calls, calls
    assert (
        calls.index(("rec_finalize", "recover-1", "cancelled"))
        < calls.index(("taskstore_row", "recover-1", "cancelled", True))
    ), calls


@pytest.mark.asyncio
async def test_recover_finalized_on_normal_path_suppresses_fallback():
    """The G5 fallback's suppression key: a recovery that finalized
    itself on the normal path (recover_finalized=True) must NOT be
    finalized again — the fallback is for recoveries that never closed."""
    from unittest.mock import AsyncMock

    calls = []

    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )

    async def fake_aget_state(config):
        return empty_snap

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    ctx = _make_ctx(intent_graph)
    ctx.recover_task_id = "recover-1"
    ctx.recover_config = {"configurable": {"thread_id": "recover-1"}}
    # The normal-path marker: _run_recover's finalize already ran.
    ctx.recover_finalized = True

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)
        calls.append(("finalize", cancelled))

    async def fake_rec_finalize(*args, **kwargs):
        calls.append(("rec_finalize",))  # pragma: no cover — must not run

    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        calls.append(("taskstore_row", task_id, state))  # pragma: no cover

    fake_task_store = SimpleNamespace(update_task_state=fake_update_task_state)
    session_store = SimpleNamespace(has_active=lambda tid: True)
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream._abort_turn_cleanup",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream.finalize_recover_session",
        new=fake_rec_finalize,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: session_store,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=fake_task_store),
    ):
        async for sse in event_generator(ctx):
            pass

    assert not any(c[0] == "rec_finalize" for c in calls), calls
    assert not any(c[0] == "taskstore_row" for c in calls), calls


async def _run_abort_with_intent_values(
    calls: list,
    *,
    intent_values: dict,
    intent_next: tuple = (),
) -> None:
    """Shared harness for the G5 coordinate self-rescue cells (round-63
    N2): drive the REAL event_generator through a node blowup with NO
    recover coordinates on ctx (the abort landed between the intent
    branch's in-graph bootstrap and _run_recover's own recording), and
    an intent state that carries the recover-shaped fields the rescue
    reads. The aget_state stub sequences: call 1 is the pre-turn
    checkpoint capture, call 2 is the self-rescue read."""
    from unittest.mock import AsyncMock

    state_calls = []

    async def fake_aget_state(config):
        state_calls.append(config)
        if len(state_calls) == 1:
            return SimpleNamespace(
                created_at=None,
                config={"configurable": {}},
                values={},
                next=(),
            )
        return SimpleNamespace(
            created_at=None,
            config={"configurable": {}},
            values=intent_values,
            next=intent_next,
        )

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    ctx = _make_ctx(intent_graph)
    # NO recover_task_id / recover_config: the coordinates were never
    # recorded — the window the self-rescue exists to close.

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)
        calls.append(("finalize", cancelled))

    async def fake_rec_finalize(*args, **kwargs):
        await asyncio.sleep(0)
        calls.append((
            "rec_finalize", args[3] if len(args) > 3 else "<id>",
            kwargs.get("default_status"),
        ))

    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        await asyncio.sleep(0)
        calls.append(("taskstore_row", task_id, state, skip_if_terminal))

    fake_task_store = SimpleNamespace(update_task_state=fake_update_task_state)
    session_store = SimpleNamespace(has_active=lambda tid: True)
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream._abort_turn_cleanup",
        new=_make_abort_chain(ctx),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream.finalize_recover_session",
        new=fake_rec_finalize,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: session_store,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=AsyncMock(return_value=fake_task_store),
    ):
        async for sse in event_generator(ctx):
            pass


@pytest.mark.asyncio
async def test_recover_coordinates_self_rescued_from_intent_state_on_abort():
    """Round-63 N2: the R61-2 residual window. The intent clarification's
    recover branch bootstraps the recover session MID-GRAPH, and an abort
    between that bootstrap and _run_recover's own recording unwinds
    through the finally with the session already active but the G5 gate
    falsy — the defensive arm yields this exact shape to G5 (F5'''), so
    nobody closed the session and the row stayed a zombie. The fallback
    now self-rescues the coordinates from the intent state (the same
    source _run_recover reads) and closes the session with the classified
    word."""
    calls: list = []

    await _run_abort_with_intent_values(
        calls,
        intent_values={
            "confirmed_intent": "recover",
            "task_id": "recover-9",
        },
    )

    assert ("rec_finalize", "recover-9", "failed") in calls, calls
    assert ("taskstore_row", "recover-9", "failed", True) in calls, calls
    assert (
        calls.index(("rec_finalize", "recover-9", "failed"))
        < calls.index(("taskstore_row", "recover-9", "failed", True))
    ), calls


@pytest.mark.asyncio
async def test_recover_self_rescue_skips_paused_intent_state():
    """The self-rescue must not close a PAUSED intent state's session: a
    paused turn stays resumable, its session stays active — the same
    ruling the defensive arm's inject twin applies (paused_at_interrupt
    keeps the session)."""
    calls: list = []

    await _run_abort_with_intent_values(
        calls,
        intent_values={
            "confirmed_intent": "recover",
            "task_id": "recover-8",
        },
        intent_next=("save_memory",),
    )

    assert not any(c[0] == "rec_finalize" for c in calls), calls
    assert not any(c[0] == "taskstore_row" for c in calls), calls


@pytest.mark.asyncio
async def test_recover_self_rescue_ignores_non_recover_intent_state():
    """Control: an abort on a turn whose intent state is NOT recover-
    shaped must not rescue anything — the fallback stays keyed on real
    recover coordinates or a real recover-shaped intent state."""
    calls: list = []

    await _run_abort_with_intent_values(
        calls,
        intent_values={
            "confirmed_intent": "inject",
            "task_id": "task-abc",
        },
    )

    assert not any(c[0] == "rec_finalize" for c in calls), calls
    assert not any(c[0] == "taskstore_row" for c in calls), calls


@pytest.mark.asyncio
async def test_rollback_fork_executes_against_real_checkpointer():
    """Round-47 lesson: the round-46 mock-based tests could not see that
    the product's rollback config shape raised KeyError inside the real
    saver — every established-thread rollback had been a silent no-op.
    This test runs the REAL helper against a REAL AsyncSqliteSaver so the
    config shape is pinned by execution, not by mock assertions."""
    import tempfile
    from pathlib import Path
    from typing import Annotated, TypedDict

    import aiosqlite
    from langchain_core.messages import HumanMessage
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
    from langgraph.graph import END, START, StateGraph
    from langgraph.graph.message import add_messages

    from chaos_agent.server.routes.turn_event_stream import (
        _rollback_intent_checkpoint,
    )

    class _S(TypedDict, total=False):
        messages: Annotated[list, add_messages]

    async def echo(state):
        return {}

    async def save_dialogue(state):
        return {}

    g = StateGraph(_S)
    g.add_node("echo", echo)
    g.add_node("save_dialogue", save_dialogue)
    g.add_edge(START, "echo")
    g.add_edge("echo", "save_dialogue")
    g.add_edge("save_dialogue", END)

    db = Path(tempfile.mkdtemp()) / "rollback-checkpoints.db"
    conn = await aiosqlite.connect(str(db))
    try:
        saver = AsyncSqliteSaver(conn=conn, serde=JsonPlusSerializer())
        await saver.setup()
        graph = g.compile(checkpointer=saver)
        cfg = {"configurable": {"thread_id": "t-real", "checkpoint_ns": ""}}

        await graph.ainvoke(
            {"messages": [HumanMessage(content="u0", id="h0")]}, cfg,
        )
        pre_cp = (await graph.aget_state(cfg)).config["configurable"]["checkpoint_id"]

        # The dirty turn: a message written after the pre-turn checkpoint.
        await graph.aupdate_state(
            cfg,
            {"messages": [HumanMessage(content="u1-dirty", id="h1")]},
            as_node="save_dialogue",
        )
        assert len((await graph.aget_state(cfg)).values["messages"]) == 2

        # The REAL helper call — no mock anywhere on this path.
        await _rollback_intent_checkpoint(graph, "t-real", pre_cp)

        msgs = (await graph.aget_state(cfg)).values["messages"]
        assert [m.content for m in msgs] == ["u0"], (
            "the fork must roll the thread back to the pre-turn state"
        )
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_cancel_exit_cleanup_chain_survives_scope_cancellation():
    """Round-48 fix: under a REAL client disconnect the cleanup chain runs.

    Starlette serves this generator inside an anyio task group; a client
    disconnect cancels the task-group SCOPE — level-based cancellation
    that re-delivers CancelledError at every await suspension until the
    scope exits. Before round-48 the cancel exit died at its FIRST await
    (the shielded cleanup's outer await), the CancelledError escaped the
    handler, and rollback + interrupted record + the finally-block
    terminal finalize were ALL skipped on every real disconnect.

    This test replicates Starlette's task-group structure around the REAL
    event_generator (a mid-stream scope cancellation — exactly the live
    probe's scenario) and asserts the whole cleanup chain executes:
    cleanup -> rollback fork -> interrupted record -> terminal finalize.
    """
    import anyio

    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append((
            "rollback",
            config["configurable"].get("checkpoint_id"),
            config["configurable"].get("checkpoint_ns", "<MISSING>"),
        ))

    async def _hang(*args, **kwargs):
        # Mid-stream hang: the scope cancellation interrupts THIS await,
        # the realistic cancel scenario (not a first-pull raise).
        await asyncio.sleep(30)
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_hang,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("record",))

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", cancelled))

    async def fake_cleanup(c):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("cleanup",))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=fake_cleanup,
    ):
        async def consume():
            async for sse in event_generator(ctx):
                pass

        # Starlette's StreamingResponse structure: the generator's consumer
        # runs inside the task group; the "disconnect" cancels the scope.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    # The whole cleanup chain executed, in order, through the
    # level-based cancellation — with real suspension points that a
    # bare await or an asyncio.shield outer await could not survive.
    assert calls == [
        ("cleanup",),
        ("rollback", "cp-pre", ""),
        ("record",),
        ("finalize", True),
    ], calls


@pytest.mark.asyncio
async def test_cancel_cleanup_hitting_ceiling_abandons_rest_bounded(caplog):
    """Round-49 fix: the shielded cleanup chain has a timeout ceiling.

    Round-48's shield scope carries the cleanup chain through a real
    disconnect, but while it is active it blocks ALL cancellation —
    including uvicorn's graceful shutdown, which this project runs with
    no timeout (waits forever for in-flight connections). The chain's
    boundedness relied entirely on every await having its own timeout;
    round-49 wraps the chain in anyio.fail_after so a pathological hang
    (e.g. a future unbounded await added to the chain) abandons the
    remaining steps at the ceiling instead of holding the task group —
    and the server — hostage forever.

    Same Starlette task-group structure as the round-48 test, with the
    ceiling patched to 0.1s and a cleanup that hangs 10s: the chain must
    abandon at the ceiling (rollback/record skipped, warning logged),
    the terminal finalize must still run (shielded, outside the chain),
    and the group must exit bounded (~0.15s, not 10s).
    """
    import logging
    import time

    import anyio

    calls = []
    t0 = time.monotonic()

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)
        calls.append(("rollback",))  # pragma: no cover — must be skipped

    async def _hang(*args, **kwargs):
        # Mid-stream hang: the scope cancellation interrupts THIS await,
        # the realistic cancel scenario (not a first-pull raise).
        await asyncio.sleep(30)
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_hang,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        await asyncio.sleep(0)
        calls.append(("record",))  # pragma: no cover — must be skipped

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", cancelled))

    async def hanging_cleanup(c):
        # Pathological hang INSIDE the shield: 10s against the 0.1s
        # ceiling. Without the ceiling this would hold the task group
        # (and a shutdown-waiting uvicorn) for the full 10s.
        await asyncio.sleep(10)
        calls.append(("cleanup",))  # pragma: no cover

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    caplog.set_level(logging.WARNING, logger="chaos_agent.server.routes.turn_event_stream")

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=hanging_cleanup,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._CANCEL_CLEANUP_CEILING_S",
        new=0.1,
    ):
        async def consume():
            async for sse in event_generator(ctx):
                pass

        # Starlette's StreamingResponse structure: the generator's consumer
        # runs inside the task group; the "disconnect" cancels the scope.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    elapsed = time.monotonic() - t0

    # Bounded abandon: the hung cleanup never completed, rollback and the
    # interrupted record were skipped past the ceiling — bounded loss, not
    # an unbounded hold. The terminal finalize (outside the chain, still
    # shielded) ran regardless.
    assert calls == [("finalize", True)], calls
    assert elapsed < 5.0, elapsed  # the 10s hang did NOT hold the group
    assert any("ceiling" in r.message for r in caplog.records), [
        r.message for r in caplog.records
    ]


@pytest.mark.asyncio
async def test_capture_failure_skips_rollback_keeps_record(caplog):
    """Round-50 fix: a pre-turn capture failure no longer arms the sweep.

    The pre-turn capture's None used to be indistinguishable from a
    brand-new thread's None, so a TRANSIENT aget_state failure on an OLD
    thread routed the next cancel into the rollback's sweep branch —
    which wipes the thread's WHOLE history (real-checkpointer proof,
    round-50 probe A2). The capture now sets a failure marker: the abort
    cleanup skips the rollback entirely (one dirty turn, self-healing —
    the r47-era lesser evil) while the record and the terminal finalize
    still run, and the arming event is logged at WARNING (it was debug —
    the wipe looked like a silent data loss).

    Same Starlette task-group structure as the round-48 test; the fake
    aget_state raises on its FIRST call (the pre-turn capture) and serves
    empty snapshots afterwards (the record path).
    """
    import logging
    import time

    import anyio

    calls = []
    t0 = time.monotonic()

    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        if aget_calls["n"] == 1:
            # The pre-turn capture fails — the transient-lock profile.
            raise RuntimeError("sqlite: database is locked")
        return empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)
        calls.append(("rollback",))  # pragma: no cover — must be skipped

    async def _hang(*args, **kwargs):
        # Mid-stream hang: the scope cancellation interrupts THIS await.
        await asyncio.sleep(30)
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_hang,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("record",))

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", cancelled))

    async def fake_cleanup(c):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("cleanup",))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    caplog.set_level(logging.WARNING, logger="chaos_agent.server.routes.turn_event_stream")

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=fake_cleanup,
    ):
        async def consume():
            async for sse in event_generator(ctx):
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    elapsed = time.monotonic() - t0

    # The rollback was SKIPPED (the sweep that would wipe an old thread's
    # history never ran), while the vehicle cleanup, the interrupted
    # record, and the terminal finalize all survived the scope cancel.
    assert calls == [
        ("cleanup",),
        ("record",),
        ("finalize", True),
    ], calls
    assert elapsed < 5.0, elapsed
    # The arming event is now visible at WARNING, not buried at debug.
    assert any(
        "abort rollback will be skipped" in r.message for r in caplog.records
    ), [r.message for r in caplog.records]


@pytest.mark.asyncio
async def test_cancel_with_dispatched_pipeline_clears_state_via_single_source():
    """Round-51 fix: abort clears dispatched one-shot intent state.

    The dispatch-time clear (awaited BEFORE streaming starts) already
    removes the dispatched fields on the main path — a mid-stream abort
    finds them gone (round-52 re-audit corrected round-51's original
    headline claim). What this test pins is the residual-window remover:
    a cancel landing ON the dispatch-clear's own await, its fail-soft
    failure, or the pipeline finallys' clear — which runs INLINE inside
    the cancelled task on the scope-cancel path (the path real
    disconnects take, round-48 live) and whose aupdate_state dies at the
    first suspension (round-51 two-path experiment). Whatever window
    leaks, the stale fault_spec/batch_submit_args re-opens an old
    intent_confirm card next turn.

    ``pipeline_task_id`` is pre-set — the field the real dispatch writes
    at handoff time — the stream hangs mid-flight, the scope cancels. The
    clear must arrive through _abort_turn_cleanup's shielded chain, AFTER
    the rollback fork (a successful fork already discards the dispatched
    fields; this arm matters when the rollback is skipped or fails),
    despite the level-based cancellation.
    """
    import anyio

    from chaos_agent.agent.intent_handoff import DISPATCHED_OPERATION_CLEAR_UPDATE

    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        if values == DISPATCHED_OPERATION_CLEAR_UPDATE:
            calls.append(("clear",))
        else:
            calls.append((
                "rollback",
                config["configurable"].get("checkpoint_id"),
                config["configurable"].get("checkpoint_ns", "<MISSING>"),
            ))

    async def _hang(*args, **kwargs):
        await asyncio.sleep(30)
        yield  # pragma: no cover

    intent_graph = SimpleNamespace(
        astream_events=_hang,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)
    # The dispatch marker the real _run_inject_pipeline/_run_batch_pipeline
    # write at handoff time — set for the whole pipeline streaming window.
    ctx.pipeline_task_id = "pipeline-42"

    async def fake_write_summary(record, **kwargs):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("record",))

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", cancelled))

    async def fake_cleanup(c):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("cleanup",))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._cleanup_cancelled_execution_artifacts",
        new=fake_cleanup,
    ):
        async def consume():
            async for sse in event_generator(ctx):
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    # The dispatched-state clear arrived through the shielded single-source
    # chain — with a real suspension point that the pipeline finallys' own
    # inline clear cannot survive — ordered after the rollback fork.
    assert calls == [
        ("cleanup",),
        ("rollback", "cp-pre", ""),
        ("clear",),
        ("record",),
        ("finalize", True),
    ], calls


@pytest.mark.asyncio
async def test_internal_error_cleanup_survives_disconnect_mid_handler():
    """Round-52: a disconnect landing MID internal-error handler.

    The turn twin's four scope-cancel tests all cancel a MID-STREAM HANG —
    the cleanup chain then starts AFTER the cancel, inside an already
    cancelled scope, where a bare await dies at its first suspension. This
    test covers the other window (the one the recover/inject twins grew in
    round-52): the graph FAILS first, the internal_error handler's
    rollback+record chain is IN FLIGHT, and the client disconnects while
    the record write is suspended. Before round-50's single-source shield
    the handler's awaits died mid-handler — the sibling-bypass window.

    Discriminating power: the record fake suspends for 0.2s while the
    cancel lands at 0.05s, so the cancel provably lands INSIDE the record
    write (a sleep(0) fake would finish before the cancel arrives and both
    the shielded and bare forms would pass — the r51 "verified A, asserted
    B" failure mode caught live on the inject/recover twins in round-52).
    """
    import anyio

    calls = []

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )
    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )
    aget_calls = {"n": 0}

    async def fake_aget_state(config):
        aget_calls["n"] += 1
        return pre_turn_snap if aget_calls["n"] == 1 else empty_snap

    async def fake_aupdate_state(config, values, as_node=None):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append((
            "rollback",
            config["configurable"].get("checkpoint_id"),
            config["configurable"].get("checkpoint_ns", "<MISSING>"),
        ))

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=fake_aupdate_state,
    )
    ctx = _make_ctx(intent_graph)

    async def fake_write_summary(record, **kwargs):
        # Suspension must OUTLAST the cancel delay (0.05s): the cancel has
        # to land mid-record to discriminate the shielded chain from a
        # bare one.
        await asyncio.sleep(0.2)  # real suspension: dies unless shielded
        calls.append(("record",))

    async def fake_finalize(
    graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause="",
):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", cancelled))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.turn_event_stream.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ):
        async def consume():
            async for sse in event_generator(ctx):
                pass

        # The error handler is in flight (record suspended at 0.2s) when
        # the task-group scope is cancelled at 0.05s — the disconnect lands
        # MID-handler, the exact sibling-bypass window round-50 closed.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)
            tg.cancel_scope.cancel()

    # The rollback fork AND the interrupted record both landed through the
    # level-based cancellation — in the decision-table order (rollback
    # first: the record would be discarded together with the dirt by the
    # fork) — and the terminal finalize (cancelled=False: internal_error
    # is not a user cancel) closed the row.
    assert calls == [
        ("rollback", "cp-pre", ""),
        ("record",),
        ("finalize", False),
    ], calls


@pytest.mark.asyncio
async def test_abort_after_pipeline_dispatch_finalizes_from_pipeline_thread():
    """Round-60 F2'''/F3'''/F1''': the finally's session finalize must read
    the PIPELINE's own thread and the cause memo.

    ``ctx.result_graph`` points at the pipeline only on the SUCCESS path,
    so before round-60 every mid-pipeline abort (and the dry-run early
    return) inherited the INTENT graph in the finally: the session word
    and summary were inferred from intent values (no verification,
    fault_spec cleared at dispatch) — a confirm_timeout abort said
    "failed" on the session while the G4 arm's row said "cancelled", and
    the persisted result_summary lost the durable recover context. This
    drives the real generator through a node failure AFTER a pipeline was
    dispatched (pipeline_task_id/config recorded at dispatch) and pins
    the three wiring facts: pipeline coordinates, the cause memo, and
    the flag.
    """
    calls = []

    empty_snap = SimpleNamespace(
        created_at=None,
        config={"configurable": {}},
        values={},
        next=(),
    )

    async def fake_aget_state(config):
        return empty_snap

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    # A DISTINCT pipeline object: the assertion is about WHICH graph the
    # finalize receives, and a shared object would make it vacuous.
    pipeline_graph = SimpleNamespace(aget_state=fake_aget_state)
    ctx = _make_ctx(intent_graph)
    ctx.pipeline_graph = pipeline_graph
    # The dispatch coordinates _run_inject_pipeline records BEFORE the
    # stream starts — an abort mid-pipeline finds these already set while
    # ctx.result_graph still names the intent graph (L1505's pre-try
    # assignment; the pipeline assignment is success-path only).
    ctx.pipeline_task_id = "inject-dispatched"
    ctx.pipeline_config = {"configurable": {"thread_id": "inject-dispatched"}}

    async def fake_finalize(graph, config, turn_id, store_cancel_fn, *, cancelled=False, abort_cause=""):
        calls.append((
            "finalize",
            graph is pipeline_graph,
            config.get("configurable", {}).get("thread_id"),
            cancelled,
            abort_cause,
        ))

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )
    session_store = SimpleNamespace(has_active=lambda tid: False)

    with patch(
        "chaos_agent.server.routes.turn_event_stream._abort_turn_cleanup",
        new=_make_abort_chain(ctx),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: session_store,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ):
        async for sse in event_generator(ctx):
            pass

    # The finalize received the PIPELINE's own thread (not the intent
    # graph the result_graph fallback names) and the cause memo the
    # abort chain recorded — internal_error sets no cancel flag.
    assert calls == [
        ("finalize", True, "inject-dispatched", False, "internal_error"),
    ], calls


@pytest.mark.asyncio
async def test_g5_self_rescue_failure_warns_loudly(caplog):
    """Round-64 R64-2 (the r50 capture-warning family): when the finally's
    G5 fallback must self-rescue the recover coordinates from the intent
    state and that read itself fails, the failure is a silent no-op — the
    very leak the rescue exists to close re-arms, and until round-64
    nothing said why. The bare ``except Exception: _snap = None`` now
    warns LOUDLY (warning level, exc_info attached), matching the
    pre-turn-capture warning's own rationale: this single event changes
    abort-cleanup behavior for the turn, and at debug it was invisible in
    production.

    Drive: an internal_error unwind with EMPTY recover coordinates (the
    self-rescue precondition), the self-rescue's aget_state raising, and
    the finalize task-session surface patched away so the G5 read is the
    only aget_state after the pre-turn capture.
    """
    import logging

    aget_calls = {"n": 0}

    pre_turn_snap = SimpleNamespace(
        created_at="2026-09-17T00:00:00Z",
        config={"configurable": {"checkpoint_id": "cp-pre"}},
        values={"messages": []},
        next=(),
    )

    async def fake_aget_state(config):
        # Call 1: the pre-turn capture (succeeds). Call 2: the G5
        # self-rescue — the read whose failure this test pins.
        aget_calls["n"] += 1
        if aget_calls["n"] == 1:
            return pre_turn_snap
        raise RuntimeError("checkpointer unreachable")

    async def _boom(*args, **kwargs):
        raise RuntimeError("node blew up")
        yield  # pragma: no cover — makes this an async generator

    intent_graph = SimpleNamespace(
        astream_events=_boom,
        aget_state=fake_aget_state,
        aupdate_state=AsyncMock(),
    )
    ctx = _make_ctx(intent_graph)
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )
    row_writes = AsyncMock()

    caplog.set_level(
        logging.WARNING, logger="chaos_agent.server.routes.turn_event_stream",
    )
    with patch(
        "chaos_agent.server.routes.turn_event_stream._abort_turn_cleanup",
        new=_make_abort_chain(ctx),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream._finalize_task_session",
        new=AsyncMock(),
    ), patch(
        "chaos_agent.server.routes.turn_event_stream.write_aborted_task_row",
        new=row_writes,
    ), patch(
        "chaos_agent.memory.tui_session_store.get_global_tui_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.memory.session_store.get_global_session_store",
        new=lambda: None,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.observability.status_tracker.unsubscribe",
        new=lambda *a, **k: None,
    ):
        async for _sse in event_generator(ctx):
            pass

    # The loud warning fired, at WARNING level with the traceback.
    rescue_warnings = [
        r for r in caplog.records
        if "G5 recover self-rescue aborted" in r.message
    ]
    assert rescue_warnings, (
        "a failed G5 self-rescue must warn loudly (it silently re-arms "
        "the session leak the rescue exists to close)"
    )
    assert rescue_warnings[0].levelno == logging.WARNING
    assert rescue_warnings[0].exc_info is not None

    # And the failed rescue stays a no-op on the write surfaces: no
    # coordinates were recovered, so no recover row is written.
    row_writes.assert_not_awaited()
