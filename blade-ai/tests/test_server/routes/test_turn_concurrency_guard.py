"""Per-session in-flight turn guard (concurrent /turn serialization).

Provenance: the fault-window hold review (2026-09-19) surfaced that the
/turn HTTP surface had NO per-session guard — one conversation thread
per session means two concurrently-running turns interleave graph
writes on the same thread. The client composers lock their inputs
while busy, but a direct API double-POST (curl, scripts) raced freely.

These tests pin the guard's full contract:

  - empty-slot registration (the atomic get→set claim);
  - 409 on a still-RUNNING previous turn (grace expires);
  - the supersede shape: the old turn tearing down wakes the queued
    newcomer, which takes the slot — this is the ConfirmMessage
    feedback path (client aborts old stream + posts the new turn in
    one synchronous sequence), a flat 409 would break it;
  - two queued newcomers serialize (no claim race after a release);
  - release match-guard + the set→del single synchronous stretch;
  - stale-slot reclaim (a generator that never reached release);
  - the finally wiring (release FIRST in event_generator's finally);
  - HTTP-level: the 409 through the mounted route, and the
    wait-then-stream path end-to-end with a stubbed generator.
"""

from __future__ import annotations

import asyncio
import ast
import inspect

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from chaos_agent.server.routes import turn as turn_mod
from chaos_agent.server.routes import turn_event_stream as stream_mod
from chaos_agent.server.routes.sessions import get_store, sessions_router


@pytest.fixture(autouse=True)
def _clean_slot_registry():
    stream_mod._ACTIVE_TURNS.clear()
    yield
    stream_mod._ACTIVE_TURNS.clear()


def _fill_slot(sid: str, turn_id: str, *, age_s: float = 0.0, set_done: bool = False):
    loop = asyncio.get_event_loop()
    ev = asyncio.Event()
    if set_done:
        ev.set()
    entry = (turn_id, ev, loop.time() - age_s)
    stream_mod._ACTIVE_TURNS[sid] = entry
    return entry


# ---------------------------------------------------------------------------
# Slot acquire / release units
# ---------------------------------------------------------------------------

async def test_acquire_registers_empty_slot():
    await stream_mod._acquire_turn_slot("sid-a", "turn-new")
    entry = stream_mod._ACTIVE_TURNS["sid-a"]
    assert entry[0] == "turn-new"
    assert not entry[1].is_set()


async def test_acquire_409_while_previous_running(monkeypatch):
    """A previous turn that is still RUNNING (not tearing down) holds
    the slot through the whole grace window → 409."""
    monkeypatch.setattr(stream_mod, "_TURN_SLOT_GRACE_S", 0.05)
    _fill_slot("sid-b", "turn-old")

    with pytest.raises(HTTPException) as exc:
        await stream_mod._acquire_turn_slot("sid-b", "turn-new")
    assert exc.value.status_code == 409
    assert "turn-old" in exc.value.detail
    # The old turn's slot is untouched by the rejection.
    assert stream_mod._ACTIVE_TURNS["sid-b"][0] == "turn-old"


async def test_acquire_waits_for_teardown_and_takes_slot():
    """The supersede shape: old turn's finally releases the slot while
    the newcomer is queued → the newcomer wakes and claims it."""
    _fill_slot("sid-c", "turn-old")

    async def _release_later():
        await asyncio.sleep(0.05)
        stream_mod._release_turn_slot("sid-c", "turn-old")

    releaser = asyncio.create_task(_release_later())
    await stream_mod._acquire_turn_slot("sid-c", "turn-new")
    await releaser

    assert stream_mod._ACTIVE_TURNS["sid-c"][0] == "turn-new"


async def test_release_is_match_guarded():
    _fill_slot("sid-d", "turn-mine")

    # Someone else's release must not delete (nor set) the slot.
    stream_mod._release_turn_slot("sid-d", "turn-not-mine")
    assert stream_mod._ACTIVE_TURNS["sid-d"][0] == "turn-mine"
    assert not stream_mod._ACTIVE_TURNS["sid-d"][1].is_set()

    # The owner's release: done set + slot gone in one synchronous
    # stretch — a waiter never observes the torn middle.
    stream_mod._release_turn_slot("sid-d", "turn-mine")
    assert "sid-d" not in stream_mod._ACTIVE_TURNS


async def test_two_waiters_serialize_no_claim_race():
    """Two newcomers queued on one release: the first scheduled wake
    claims the slot; the second re-reads, finds the FRESH entry, and
    keeps waiting on it — never a double claim."""
    _fill_slot("sid-e", "turn-old")

    w1 = asyncio.create_task(stream_mod._acquire_turn_slot("sid-e", "turn-w1"))
    w2 = asyncio.create_task(stream_mod._acquire_turn_slot("sid-e", "turn-w2"))
    await asyncio.sleep(0.02)  # both are now parked on the old done event

    stream_mod._release_turn_slot("sid-e", "turn-old")
    await asyncio.sleep(0.02)  # let exactly the first wake claim…

    first = stream_mod._ACTIVE_TURNS["sid-e"][0]
    assert first in ("turn-w1", "turn-w2")

    # …then that turn finishes, releasing to the second waiter.
    stream_mod._release_turn_slot("sid-e", first)
    await asyncio.gather(w1, w2)

    second = stream_mod._ACTIVE_TURNS["sid-e"][0]
    assert second in ("turn-w1", "turn-w2")
    assert {first, second} == {"turn-w1", "turn-w2"}


async def test_stale_slot_reclaimed(monkeypatch, caplog):
    """A slot whose generator never reached release (leak) is reclaimed
    after the stale threshold — with a loud warning, not a silent
    takeover."""
    monkeypatch.setattr(stream_mod, "_STALE_TURN_SLOT_S", 1.0)
    _fill_slot("sid-f", "turn-leaked", age_s=10.0)

    with caplog.at_level("WARNING", logger=stream_mod.__name__):
        await stream_mod._acquire_turn_slot("sid-f", "turn-new")

    assert stream_mod._ACTIVE_TURNS["sid-f"][0] == "turn-new"
    assert any("stale" in r.message for r in caplog.records)


async def test_long_lived_paused_turn_is_never_stale(monkeypatch):
    """The counter-case to the reclaim: a confirmation-gate turn can
    legally sit un-released for HOURS (6h wait ceiling). With the
    production 8h threshold, an hour-old slot must still 409 a newcomer
    (after a tiny grace), never be reclaimed."""
    monkeypatch.setattr(stream_mod, "_TURN_SLOT_GRACE_S", 0.05)
    # age 1h — far under the 8h production threshold
    _fill_slot("sid-g", "turn-paused", age_s=3600.0)

    with pytest.raises(HTTPException) as exc:
        await stream_mod._acquire_turn_slot("sid-g", "turn-new")
    assert exc.value.status_code == 409
    assert stream_mod._ACTIVE_TURNS["sid-g"][0] == "turn-paused"


# ---------------------------------------------------------------------------
# finally wiring (AST legislation — same family as the abort-cleanup
# invariant tests)
# ---------------------------------------------------------------------------

def test_release_wired_first_in_event_generator_finally():
    """The slot MUST be released FIRST in event_generator's finally —
    before end_task_span and the shielded terminal work, any of which
    can throw (or hit its ceiling) and skip the rest of the block. A
    later placement leaks the slot on exactly the exits where cleanup
    machinery is degraded."""
    src = inspect.getsource(stream_mod.event_generator)
    tree = ast.parse(src)
    finallys = [
        n for node in ast.walk(tree)
        if isinstance(node, ast.Try) for n in node.finalbody
    ]
    # event_generator has exactly one finally; its first statement is
    # the release call.
    assert finallys, "event_generator lost its finally block"
    first = finallys[0]
    assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), (
        "the finally block's first statement must be a call, found "
        f"{ast.dump(first)[:80]}"
    )
    func = first.value.func
    assert getattr(func, "id", None) == "_release_turn_slot", (
        "event_generator's finally must release the turn slot FIRST"
    )


# ---------------------------------------------------------------------------
# HTTP level (mounted route)
# ---------------------------------------------------------------------------

def _make_app():
    app = FastAPI()
    app.include_router(sessions_router)
    app.state.agents = {"intent": None, "pipeline": None}
    app.state.task_tracker = type("TT", (), {"is_shutting_down": False})()
    return app


def _inject_session(sid: str):
    store = get_store()
    store._items[sid] = {
        "conversation_thread_id": "thread-test-1",
        "first_turn_done": True,
    }


async def test_http_409_while_previous_turn_running(monkeypatch):
    monkeypatch.setattr(stream_mod, "_TURN_SLOT_GRACE_S", 0.05)
    sid = "sid-http-409"
    _inject_session(sid)
    _fill_slot(sid, "turn-old-running")

    app = _make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(f"/api/v1/sessions/{sid}/turn", json={"input": "hi"})
    assert r.status_code == 409
    assert "turn-old-running" in r.json()["detail"]


async def test_http_waits_out_teardown_then_streams(monkeypatch):
    """End-to-end supersede shape through the mounted route: the old
    turn releases while the new POST is queued in the handler's grace
    wait; the newcomer then streams (stubbed generator, which releases
    the slot from its finally exactly as the real one does)."""
    sid = "sid-http-wait"
    _inject_session(sid)
    _fill_slot(sid, "turn-old")

    async def _release_later():
        await asyncio.sleep(0.05)
        stream_mod._release_turn_slot(sid, "turn-old")

    async def _stub_generator(ctx):
        try:
            yield f'data: {{"type": "done", "task_id": "{ctx.turn_id}"}}\n\n'
        finally:
            stream_mod._release_turn_slot(ctx.sid, ctx.turn_id)

    monkeypatch.setattr(turn_mod, "event_generator", _stub_generator)
    releaser = asyncio.create_task(_release_later())

    app = _make_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        r = await client.post(f"/api/v1/sessions/{sid}/turn", json={"input": "hi"})
    await releaser

    assert r.status_code == 200
    assert '"type": "done"' in r.text
    # The stub generator's finally released the NEW turn's slot.
    assert sid not in stream_mod._ACTIVE_TURNS
