"""Round-52 fix: the inject stream's abort cleanup survives a scope cancel.

The third StreamingResponse module found carrying the round-48 defect
family (after the turn twin was fixed in r48-50 and the recover twin in
r52): the except handler's ``await auto_rollback(graph, config)`` — the
orphan-fault safety net, blade-family UIDs destroyed by kind — and the
finally block's ``await finalize_inject_session`` were both bare awaits
inside the Starlette task group. On a real client disconnect (the
scope-cancel path) the rollback died at its first suspension: a fault
committed by the crashed run stayed live in the cluster with no one left
to roll it back, and the session row stayed active forever.

Drives the REAL endpoint through a Starlette-replica task group with a
pre-set pipeline graph; every fake carries a real suspension point that
a bare await cannot survive.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from chaos_agent.server.routes.inject_stream import inject_stream
from chaos_agent.server.schemas import InjectRequest


def _make_fakes():
    calls = []

    async def _not_disconnected():
        return False

    async def fake_aget_state(config):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        return SimpleNamespace(values={}, next=(), config={"configurable": {}})

    graph = SimpleNamespace(
        astream_events=None,  # set per test
        aget_state=fake_aget_state,
    )

    session_store = SimpleNamespace(
        create_session=lambda *a, **k: calls.append(("create_session",)),
    )

    task_tracker = SimpleNamespace(
        register=lambda tid, task: calls.append(("register",)),
        unregister=lambda tid: calls.append(("unregister",)),
        is_shutting_down=False,
    )

    req = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                agents={"pipeline": graph, "session_store": session_store},
                task_tracker=task_tracker,
            ),
        ),
        state=SimpleNamespace(request_id=""),
        is_disconnected=_not_disconnected,
    )

    async def fake_auto_rollback(graph, config):
        # Suspension must OUTLAST the test's cancel delay (0.05s): the cancel
        # has to land mid-rollback for the test to discriminate a shielded
        # rollback from a bare one (a sleep(0) finishes before the cancel
        # arrives and both forms pass — the r51 "verified A, asserted B"
        # failure mode).
        await asyncio.sleep(0.2)  # real suspension: dies unless shielded
        calls.append(("auto_rollback",))
        return ""

    async def fake_finalize(*args, **kwargs):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("finalize", kwargs.get("status_override")))

    # The abort exits' TaskStore terminal write (cancel exits since
    # round-53; the error exit's "failed" word since round-54 G4). A fake
    # (not the real singleton): the pre-round-53 test reached for the REAL
    # store — the random task_id's missing row made it a silent no-op, so
    # the test never saw the write it now must assert. Signature carries
    # skip_if_terminal: the shared guarded helper (stream_abort.py) always
    # passes it — a fake without the kwarg raises TypeError inside the
    # helper's retry loop and the write silently never lands.
    async def fake_update_task_state(task_id, state, skip_if_terminal=False):
        await asyncio.sleep(0)  # real suspension: dies unless shielded
        calls.append(("taskstore_row", task_id, state, skip_if_terminal))

    fake_task_store = SimpleNamespace(
        update_task_state=fake_update_task_state,
    )

    async def fake_get_task_store():
        return fake_task_store

    return calls, req, graph, fake_auto_rollback, fake_finalize, fake_get_task_store


def _patchers(fake_auto_rollback, fake_finalize, fake_get_task_store):
    span_manager = SimpleNamespace(
        start_task_span=lambda *a, **k: None,
        end_task_span=lambda *a, **k: None,
    )
    fake_spec = SimpleNamespace(from_http_request=lambda request: SimpleNamespace())
    return [
        patch(
            "chaos_agent.server.routes.inject_stream.FaultSpec", new=fake_spec,
        ),
        patch(
            "chaos_agent.server.routes.inject_stream.build_inject_initial_state",
            new=lambda **kwargs: {},
        ),
        patch(
            "chaos_agent.cli.session_finalize.auto_rollback",
            new=fake_auto_rollback,
        ),
        patch(
            "chaos_agent.server.routes.inject_stream.finalize_inject_session",
            new=fake_finalize,
        ),
        patch(
            "chaos_agent.observability.otel_genai.get_task_span_manager",
            new=lambda: span_manager,
        ),
        patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=fake_get_task_store,
        ),
    ]


@pytest.mark.asyncio
async def test_error_plus_disconnect_rolls_back_through_shield():
    """Graph failure + a disconnect landing mid-rollback: the orphan-fault
    rollback still lands — the heaviest loss the old bare await caused."""
    import anyio
    from contextlib import ExitStack

    calls, req, graph, fake_auto_rollback, fake_finalize, fake_get_ts = _make_fakes()

    async def _boom(*args, **kwargs):
        raise RuntimeError("inject node blew up")
        yield  # pragma: no cover — makes this an async generator

    graph.astream_events = _boom

    request = InjectRequest(
        scope="pod", target="cpu", action="fullload",
        target_name="app=demo", namespace="cms-demo",
    )

    with ExitStack() as stack:
        for p in _patchers(fake_auto_rollback, fake_finalize, fake_get_ts):
            stack.enter_context(p)
        resp = await inject_stream(request, req)

        async def consume():
            async for chunk in resp.body_iterator:
                pass

        # Starlette's StreamingResponse structure: the generator's consumer
        # runs inside the task group; the "disconnect" cancels the scope —
        # landing while the error handler's rollback is in flight.
        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)
            tg.cancel_scope.cancel()

    assert ("auto_rollback",) in calls, calls
    assert ("finalize", None) in calls, calls
    assert ("unregister",) in calls, calls
    # The rollback is the point of the error path: it must precede the
    # terminal finalize (the last durable write on the abort path).
    assert calls.index(("auto_rollback",)) < calls.index(("finalize", None)), calls
    # Round-54 G4: the error exit is NOT a cancel — no override, the crash
    # path's session status comes from finalize's own inference — but its
    # TaskStore row STILL gets a terminal word: "failed", through the
    # shared guarded helper. Before G4 a crashed run's row stayed a zombie
    # at its last mid-graph upsert ("injecting" forever in /tasks; no
    # later writer comes on the abort path).
    _rows = [c for c in calls if c[0] == "taskstore_row"]
    assert len(_rows) == 1, calls
    assert _rows[0][2] == "failed" and _rows[0][3] is True, _rows[0]
    assert calls.index(_rows[0]) < calls.index(("finalize", None)), calls


@pytest.mark.asyncio
async def test_scope_cancel_mid_stream_finalizes_through_shield():
    """Clean cancel mid-stream (round-53): the terminal triad — TaskStore
    row write, finalize with status_override="cancelled", unregister —
    all land through the shield. Before round-53 the CancelledError fell
    straight to the finally with no flag: the TaskStore row stayed a
    zombie at its last mid-graph upsert and the session status came from
    finalize's inference ("completed" for an unreadable state — the lie
    the status_override now cuts off at this exit)."""
    import anyio
    from contextlib import ExitStack

    calls, req, graph, fake_auto_rollback, fake_finalize, fake_get_ts = _make_fakes()

    async def _hang(*args, **kwargs):
        await asyncio.sleep(30)
        yield  # pragma: no cover

    graph.astream_events = _hang

    request = InjectRequest(
        scope="pod", target="cpu", action="fullload",
        target_name="app=demo", namespace="cms-demo",
    )

    with ExitStack() as stack:
        for p in _patchers(fake_auto_rollback, fake_finalize, fake_get_ts):
            stack.enter_context(p)
        resp = await inject_stream(request, req)

        async def consume():
            async for chunk in resp.body_iterator:
                pass

        async with anyio.create_task_group() as tg:
            tg.start_soon(consume)
            await asyncio.sleep(0.05)  # let it reach the mid-stream hang
            tg.cancel_scope.cancel()

    # No error → no rollback; the terminal triad must all survive the
    # level-based cancellation, with the cancel exit's OWN semantics:
    # the TaskStore row write BEFORE the finalize (row = user-facing fact,
    # finalize = derived record), and the finalize carrying the explicit
    # cancelled override — not the inference's guess.
    assert ("auto_rollback",) not in calls, calls
    assert ("finalize", "cancelled") in calls, calls
    assert ("unregister",) in calls, calls
    _ts_writes = [c for c in calls if c[0] == "taskstore_row"]
    assert len(_ts_writes) == 1 and _ts_writes[0][2] == "cancelled", calls
    assert calls.index(_ts_writes[0]) < calls.index(("finalize", "cancelled")), calls


@pytest.mark.asyncio
async def test_poll_detected_disconnect_gets_cancel_terminal_triad():
    """Round-54 G1: the POLL-detected disconnect is an abort exit with the
    cancel exit's OWN terminal semantics.

    r48 established the scope cancel usually WINS the is_disconnected poll
    race on a real disconnect — but the poll wins sometimes (server-side
    cancels, fast disconnects, proxies), and before round-54 this stream's
    poll was a silent ``break`` into the NORMAL completion path: no
    cancelled flag, no TaskStore row write, no status_override — and the
    confirm-flow twin kept driving the unattended auto-approve resume
    with a dead client. One abort event, one semantics: the race must not
    pick the record.

    Discrimination: the poll drives the exit here (no task-group cancel),
    so the finally's triad only fires with the right words if the
    ClientDisconnected handler set the flag — the pre-G4 form finalized
    with override None (inference's guess) on this exact path.
    """
    from contextlib import ExitStack

    calls, req, graph, fake_auto_rollback, fake_finalize, fake_get_ts = _make_fakes()

    # The poll runs at the top of the loop body for each event: ONE event
    # arms it, the True return aborts before parse ever runs. The trailing
    # hang makes a regression (silent break) visibly hang instead of
    # completing quietly — the abort must fire on the FIRST poll.
    async def _one_then_hang(*args, **kwargs):
        yield {}
        await asyncio.sleep(30)
        yield {}  # pragma: no cover — the poll aborts before this

    graph.astream_events = _one_then_hang
    # The poll-loser path: is_disconnected returns True — the exit the
    # RACE decides, simulated directly.
    req.is_disconnected = _make_true_poll()

    request = InjectRequest(
        scope="pod", target="cpu", action="fullload",
        target_name="app=demo", namespace="cms-demo",
    )

    with ExitStack() as stack:
        for p in _patchers(fake_auto_rollback, fake_finalize, fake_get_ts):
            stack.enter_context(p)
        resp = await inject_stream(request, req)

        # No task-group cancel: the poll itself drives the abort exit.
        async for chunk in resp.body_iterator:
            pass

    # Interrupt, not crash: no rollback (the fault's own --timeout plus
    # the recover graph are the interrupt-side safety net — probe r53 D).
    assert ("auto_rollback",) not in calls, calls
    # The cancel exit's OWN triad words: row "cancelled" through the
    # guarded helper, then finalize carrying the explicit override (not
    # the inference's guess the pre-G1 break produced on this path).
    _rows = [c for c in calls if c[0] == "taskstore_row"]
    assert len(_rows) == 1, calls
    assert _rows[0][2] == "cancelled" and _rows[0][3] is True, _rows[0]
    assert ("finalize", "cancelled") in calls, calls
    assert calls.index(_rows[0]) < calls.index(("finalize", "cancelled")), calls
    assert ("unregister",) in calls, calls


def _make_true_poll():
    async def _disconnected():
        return True

    return _disconnected
