"""Round-52 fix: the recover stream's abort cleanup survives a scope cancel.

The recover twin of test_turn_internal_error_rollback.py's round-48 test.
recover_stream.py's event_generator carried the entire round-48 defect
family, never swept when the turn twin was fixed: the user-cancel handler
used an asyncio.shield OUTER await (the form round-48 proved ineffective —
the shield only keeps its background job alive, the outer await dies), the
internal-error handler used a bare await for the interrupted-record write,
and the finally block's terminal finalize was a bare await too. On a real
client disconnect — the scope-cancel path — all three died at their first
suspension: the fault-still-live memory was lost and the recover session
row stayed active forever.

This test drives the REAL endpoint through a Starlette-replica task group,
cancels the scope mid-stream (astream_events hangs), and asserts the whole
cleanup chain executes — record (user_cancel) → terminal finalize
(round-57: the session word classifies from the cause memo — "cancelled"
for the interrupt exits) → unregister — with every fake carrying a real
suspension point that a bare await or an asyncio.shield outer await cannot
survive.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chaos_agent.server.routes.recover_stream import recover_stream
from chaos_agent.server.schemas import RecoverRequest


def _make_fakes():
    calls = []

    async def _not_disconnected():
        return False

    async def _hang(*args, **kwargs):
        # Mid-stream hang: the scope cancellation interrupts THIS await —
        # the realistic cancel scenario.
        await asyncio.sleep(30)
        yield  # pragma: no cover

    async def fake_aget_state(config):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        return SimpleNamespace(values={}, config={"configurable": {}})

    recover_graph = SimpleNamespace(
        astream_events=_hang,
        aget_state=fake_aget_state,
    )
    intent_graph = SimpleNamespace(
        aget_state=fake_aget_state,
        aupdate_state=SimpleNamespace(),  # unused: writer is patched
    )

    session_store = SimpleNamespace(
        create_session=lambda *a, **k: calls.append(("create_session",)),
        has_active=lambda task_id: True,
    )

    task_tracker = SimpleNamespace(
        register=lambda tid, task: calls.append(("register",)),
        unregister=lambda tid: calls.append(("unregister",)),
        is_shutting_down=False,
    )

    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(agents=None, task_tracker=task_tracker),
        ),
        state=SimpleNamespace(request_id=""),
        is_disconnected=_not_disconnected,
    )
    req.app.state.agents = {
        "intent": intent_graph,
        "recover": recover_graph,
        "session_store": session_store,
    }

    async def fake_write_summary(record, **kwargs):
        # Suspension must OUTLAST the test's cancel delay (0.05s): the cancel
        # has to land mid-record for the error-path test to discriminate a
        # shielded write from a bare one (a sleep(0) finishes before the
        # cancel arrives and both forms pass — the r51 "verified A, asserted
        # B" failure mode).
        await asyncio.sleep(0.2)  # real suspension: dies unless shielded
        calls.append(("record", kwargs.get("thread_id", "<none>")))

    async def fake_finalize(*args, **kwargs):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", kwargs.get("default_status")))

    # The abort exits' TaskStore terminal write (round-54 G4): "cancelled"
    # on the interrupt exits, "failed" on internal error, always through
    # the shared guarded helper (skip_if_terminal — see the inject twin's
    # note on the fake signature).
    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("taskstore_row", task_id, state, skip_if_terminal))

    fake_task_store = SimpleNamespace(
        update_task_state=fake_update_task_state,
    )

    async def fake_get_task_store():
        return fake_task_store

    return calls, req, fake_write_summary, fake_finalize, fake_get_task_store


@pytest.mark.asyncio
async def test_scope_cancel_runs_recover_cleanup_chain():
    """Cancel mid-stream: record + failed-finalize + unregister all run."""
    import anyio

    calls, req, fake_write_summary, fake_finalize, fake_get_ts = _make_fakes()

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.recover_stream.build_recover_initial_state",
        new=_fake_build_initial_state,
    ), patch(
        "chaos_agent.memory.operation_summary_writer.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.server.routes.recover_stream.finalize_recover_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.server.routes.sessions.get_store",
        new=lambda: SimpleNamespace(get=lambda sid: None),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=fake_get_ts,
    ):
        resp = await recover_stream(RecoverRequest(task_id="inject-1"), req)

        async def consume():
            async for chunk in resp.body_iterator:
                pass

        # Starlette's StreamingResponse structure: the generator's consumer
        # runs inside the task group; the "disconnect" cancels the scope.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    # The whole abort chain executed, in order, through the level-based
    # cancellation — the record (the fault-is-live memory), the recover
    # row's terminal word (round-54 G4: cancelled on the interrupt exits,
    # through the guarded helper), the terminal finalize — round-57 F1':
    # its default_status now classifies from the abort-cause memo, so a
    # user_cancelled recovery records "cancelled" on the session record
    # TOO (same word as the row; the old hard-coded "failed" stamped a
    # user cancel as a crash on the second user-visible surface), and
    # the tracker unregister. Every fake carries a real suspension point
    # that the old asyncio.shield outer await / bare awaits died on.
    assert [c[0] for c in calls] == [
        "register",
        "create_session",
        "record",
        "taskstore_row",
        "finalize",
        "unregister",
    ], calls
    _row = next(c for c in calls if c[0] == "taskstore_row")
    assert _row[1].startswith("recover-"), _row  # the recover row, not the inject id
    assert _row[2] == "cancelled" and _row[3] is True, _row
    # Round-57 F1': session word == row word for the SAME abort event.
    _fin = next(c for c in calls if c[0] == "finalize")
    assert _fin[1] == "cancelled", _fin


async def _fake_build_initial_state(agents, inject_task_id, record_task_id, req_id):
    """Bypass the inject-checkpoint read; minimal viable initial state."""
    return {"tui_session_id": ""}, {"messages": []}


@pytest.mark.asyncio
async def test_internal_error_writes_record_through_shield():
    """Error mid-stream + running inside a cancelled scope: the record
    still lands (the error-plus-disconnect defense, turn-twin parity)."""
    import anyio

    calls, req, fake_write_summary, fake_finalize, fake_get_ts = _make_fakes()

    # Make the recover graph blow up on the FIRST pull — the exception
    # surfaces through astream_events exactly like a graph node failure.
    async def _boom(*args, **kwargs):
        raise RuntimeError("recover node blew up")
        yield  # pragma: no cover — makes this an async generator

    req.app.state.agents["recover"] = SimpleNamespace(
        astream_events=_boom,
        aget_state=req.app.state.agents["recover"].aget_state,
    )

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    with patch(
        "chaos_agent.server.routes.recover_stream.build_recover_initial_state",
        new=_fake_build_initial_state,
    ), patch(
        "chaos_agent.memory.operation_summary_writer.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.server.routes.recover_stream.finalize_recover_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.server.routes.sessions.get_store",
        new=lambda: SimpleNamespace(get=lambda sid: None),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=fake_get_ts,
    ):
        resp = await recover_stream(RecoverRequest(task_id="inject-1"), req)

        async def consume():
            async for chunk in resp.body_iterator:
                pass

        # The exception path runs the handler INSIDE a scope-cancelled
        # task group — a disconnect landing mid-error-handler is exactly
        # the sibling-bypass window round-50 closed on the turn twin.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the failing pull
            tg.cancel_scope.cancel()

    assert ("record", "") in calls, calls
    assert ("finalize", "failed") in calls, calls
    assert ("unregister",) in calls, calls
    # Round-54 G4: the internal-error exit writes the recover row's
    # terminal word — "failed" on this cause — through the guarded
    # helper, ordered with the record (the live-fault memory) first.
    _rows = [c for c in calls if c[0] == "taskstore_row"]
    assert len(_rows) == 1, calls
    assert _rows[0][2] == "failed" and _rows[0][3] is True, _rows[0]
    # The record must precede the finalize: the finalize's terminal write
    # is the LAST durable state change on the abort path.
    assert calls.index(("record", "")) < calls.index(("finalize", "failed")), calls
    # Round-57 F1': internal_error classifies "failed" via the memo —
    # the SAME word the row carries (crash cause, fail-closed).
    assert any(
        c[0] == "finalize" and c[1] == "failed" for c in calls
    ), calls


@pytest.mark.asyncio
async def test_poll_detected_disconnect_aborts_before_result_extraction():
    """Round-54 G2: the POLL-detected disconnect is an abort exit, not a
    silent ``break`` into result extraction.

    r48 established the scope cancel usually WINS the is_disconnected poll
    race on a real disconnect — but the poll wins sometimes (server-side
    cancels, fast disconnects, proxies), and before round-54 this stream's
    poll was a silent ``break``: a half-run recovery fell into the NORMAL
    completion path, where result extraction could write a
    COMPLETED-LOOKING summary record (record_written=True) on the very
    stream whose interrupted record is the ONLY "fault may still be live"
    memory. One abort event, one semantics: the race must not pick the
    record.

    Discrimination: the poll drives the exit (no task-group cancel), so
    the interrupted record + the cancelled row + the failed finalize only
    fire if the ClientDisconnected exit routes through the single-source
    cleanup — and build_recover_result_payload must NOT run (the
    completed-looking path the old break fell into).
    """
    from unittest.mock import AsyncMock

    calls, req, fake_write_summary, fake_finalize, fake_get_ts = _make_fakes()

    # One event arms the poll; the True return aborts before parse. The
    # trailing hang makes a regression (silent break) visibly hang
    # instead of completing quietly.
    async def _one_then_hang(*args, **kwargs):
        yield {}
        await asyncio.sleep(30)
        yield {}  # pragma: no cover — the poll aborts before this

    req.app.state.agents["recover"] = SimpleNamespace(
        astream_events=_one_then_hang,
        aget_state=req.app.state.agents["recover"].aget_state,
    )
    # The poll-loser path: is_disconnected returns True.
    async def _disconnected():
        return True

    req.is_disconnected = _disconnected

    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )

    result_payload_mock = AsyncMock()
    with patch(
        "chaos_agent.server.routes.recover_stream.build_recover_initial_state",
        new=_fake_build_initial_state,
    ), patch(
        "chaos_agent.memory.operation_summary_writer.write_operation_summary",
        new=fake_write_summary,
    ), patch(
        "chaos_agent.agent.result.operation_summary.build_interrupted_record",
        new=lambda *a, **k: {},
    ), patch(
        "chaos_agent.server.routes.recover_stream.finalize_recover_session",
        new=fake_finalize,
    ), patch(
        "chaos_agent.observability.otel_genai.get_task_span_manager",
        new=lambda: span_manager,
    ), patch(
        "chaos_agent.server.routes.sessions.get_store",
        new=lambda: SimpleNamespace(get=lambda sid: None),
    ), patch(
        "chaos_agent.persistence.task_store.get_task_store",
        new=fake_get_ts,
    ), patch(
        "chaos_agent.server.routes.recover_stream.build_recover_result_payload",
        new=result_payload_mock,
    ):
        resp = await recover_stream(RecoverRequest(task_id="inject-1"), req)

        # No task-group cancel: the poll itself drives the abort exit.
        async for chunk in resp.body_iterator:
            pass

    # The abort exit's full semantics: interrupted record (cause lands in
    # the record writer), cancelled row through the guarded helper, then
    # the finalize — round-57 F1': the memo classifies "disconnected" to
    # "cancelled" on the session surface too — and the unregister.
    assert ("record", "") in calls, calls
    _rows = [c for c in calls if c[0] == "taskstore_row"]
    assert len(_rows) == 1, calls
    assert _rows[0][1].startswith("recover-"), _rows[0]
    assert _rows[0][2] == "cancelled" and _rows[0][3] is True, _rows[0]
    assert ("finalize", "cancelled") in calls, calls
    assert ("unregister",) in calls, calls
    assert calls.index(("record", "")) < calls.index(_rows[0]) < calls.index(("finalize", "cancelled")), calls
    # The completed-looking path NEVER ran: no result payload was built
    # for the half-run recovery (the G2 defect's user-facing half).
    result_payload_mock.assert_not_awaited()
