"""Fault-window hold (``turn_hold_fault_window``): lifecycle + recover flip.

The hold is the evaluation protocol's answer to "the turn must stay
alive through the approved window and the recovery must be reported
from THIS stream". These tests pin the behavioural contract:

  - window math from ``injection_window_start_time`` (the verifier-entry
    stamp = execute-loop end; verify time counts against the window,
    execute-loop work after blade_create must not);
  - enter/tick/exit ``fault_window`` events on the SSE stream + the
    jsonl sidewrite with ``source="hold"``;
  - the registry lifecycle the /early-recover endpoint keys on —
    pinned at the HTTP level (route path, cross-session 404, 422
    sanitisation) because the TUI client's 404→false→cancelTurn
    fallback would degrade a path drift SILENTLY;
  - the intent-graph recover flip that step 2.6's _run_recover
    dispatches on;
  - the no-window / dead-window / unparseable guards;
  - aborted teardown (client disconnect) — sidewrite-only exit, no
    flip, registry cleared.
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from chaos_agent.server.routes import turn_event_stream as stream_mod
from chaos_agent.utils.time import BEIJING_TZ, now_iso


class RecordingIntentGraph:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def aupdate_state(self, config, values, as_node=None):
        self.updates.append(
            {"config": config, "values": values, "as_node": as_node}
        )


class SnapshotPipelineGraph:
    """Pipeline stand-in: aget_state returns a fixed values dict."""

    def __init__(self, values: dict) -> None:
        self._values = values

    async def aget_state(self, _config):
        return SimpleNamespace(values=self._values, next=())


class SidewriteRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict, str]] = []

    def __call__(self, evt, source="pipeline"):
        try:
            payload = json.loads(evt.content)
        except Exception:
            payload = {"raw": evt.content}
        self.rows.append((evt.type, payload, source))


def _ctx(**overrides) -> stream_mod.TurnContext:
    base = dict(
        sid="sid-1",
        turn_id="turn-hold-1",
        thread_id="thread-1",
        input_text="inject cpu",
        permission_mode="confirm",
        dry_run=False,
        req=SimpleNamespace(),
        store=SimpleNamespace(),
        agents={},
        task_tracker=SimpleNamespace(),
        intent_graph=RecordingIntentGraph(),
        pipeline_graph=None,
        graph_config={
            "configurable": {"thread_id": "thread-1"},
            "recursion_limit": 10,
        },
        initial_state={},
        tracker_key="k",
        tracker_queue=asyncio.Queue(),
    )
    ctx = stream_mod.TurnContext(**base)
    for k, v in overrides.items():
        setattr(ctx, k, v)
    return ctx


def _window_values(*, duration: float, age: float, task_id: str = "inject-1") -> dict:
    from datetime import datetime

    start = datetime.now(BEIJING_TZ) - timedelta(seconds=age)
    return {
        "task_id": task_id,
        "injection_window_start_time": start.isoformat(),
        "fault_spec": {"duration_seconds": int(duration)},
    }


async def _collect(agen):
    out = []
    async for sse in agen:
        out.append(sse)
    return out


# ---------------------------------------------------------------------------
# Elapsed window: enter → (short wait) → exit(elapsed) → recover flip
# ---------------------------------------------------------------------------

async def test_hold_elapsed_flips_recover_intent():
    """Window nearly spent at verify end: hold the remainder, then flip
    the intent graph into a recover intent for step 2.6 to dispatch."""
    # 10s window, 9.0s already consumed → ~1s remainder: below the
    # 25s tick interval, so the stream is enter → exit(elapsed).
    values = _window_values(duration=10, age=9.0)
    pipeline = SnapshotPipelineGraph(values)
    intent = RecordingIntentGraph()
    ctx = _ctx()
    ctx.intent_graph = intent

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )

    assert len(events) == 2  # enter + exit
    enter = json.loads(events[0].replace("data: ", "").strip())
    assert enter["type"] == "fault_window"
    payload = json.loads(enter["content"])
    assert payload["phase"] == "enter"
    assert payload["inject_task_id"] == "inject-1"
    assert payload["duration_sec"] == 10
    assert 0 < payload["remaining_sec"] <= 1.2
    exit_payload = json.loads(json.loads(events[1].replace("data: ", "").strip())["content"])
    assert exit_payload["phase"] == "exit"
    assert exit_payload["reason"] == "elapsed"

    # The recover flip: field shape the intent clarification's recover
    # branch leaves behind.
    assert len(intent.updates) == 1
    update = intent.updates[0]["values"]
    assert update["confirmed_intent"] == "recover"
    assert update["recover_task_id"] == "inject-1"
    assert str(update["task_id"]).startswith("recover-")
    assert intent.updates[0]["as_node"] == "save_dialogue"

    # result_graph override dropped so _run_recover reads the INTENT
    # state the flip just wrote.
    assert ctx.result_graph is None
    assert ctx.result_config is None
    # Registry must be empty after a completed hold.
    assert stream_mod.get_active_hold("turn-hold-1") is None


async def test_hold_registry_lifecycle_during_window():
    """While the hold loop is awake, the turn is discoverable by the
    /early-recover endpoint; on exit it is gone."""
    # 60s window, fresh start: loop alive well past the first tick.
    values = _window_values(duration=60, age=0.1)
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    agen = stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    first = await agen.__anext__()
    assert "fault_window" in first
    assert stream_mod.get_active_hold("turn-hold-1") is ctx

    # Break out via the early-recover event, then drain the generator.
    ctx.hold_early_recover.set()
    rest = await _collect(agen)
    assert stream_mod.get_active_hold("turn-hold-1") is None
    assert rest  # exit event + flip happened


# ---------------------------------------------------------------------------
# Early recover: Ctrl+R wakes the loop, reason="early"
# ---------------------------------------------------------------------------

async def test_hold_early_recover_reason():
    values = _window_values(duration=120, age=1)
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    async def _poke() -> None:
        await asyncio.sleep(0.05)
        ctx.hold_early_recover.set()

    poker = asyncio.create_task(_poke())
    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )
    await poker

    payloads = [
        json.loads(json.loads(e.replace("data: ", "").strip())["content"])
        for e in events
    ]
    phases = [p["phase"] for p in payloads]
    assert phases[0] == "enter"
    assert phases[-1] == "exit"
    assert payloads[-1]["reason"] == "early"
    # The hold ended long before the window: the remaining reported at
    # exit is the unspent balance, not zero.
    assert payloads[-1]["remaining_sec"] > 60
    # Recover flip still happened — early recover is a recovery, not a
    # cancellation.
    assert ctx.intent_graph.updates


# ---------------------------------------------------------------------------
# Sidewrite evidence channel
# ---------------------------------------------------------------------------

async def test_hold_sidewrites_hold_source_rows():
    # 10s window, 9.0s consumed → ~1s remainder, the SAME margin as the
    # elapsed test: the spent-window branch makes an overrun a LOUD
    # failure (rows == [] violates the two-row assertion below), so keep
    # ≥1s of head-room for slow CI instead of the 0.3s this test once
    # had (age=9.7) — that margin could silently flip into the spent
    # branch under load.
    values = _window_values(duration=10, age=9.0)
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()
    recorder = SidewriteRecorder()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, recorder)
    )

    # Every SSE frame the client saw also landed in the jsonl channel
    # with source="hold" (the /resume rebuild path's evidence).
    assert len(recorder.rows) == len(events)
    assert all(source == "hold" for _t, _p, source in recorder.rows)
    assert [r[0] for r in recorder.rows] == ["fault_window", "fault_window"]
    assert recorder.rows[0][1]["phase"] == "enter"
    assert recorder.rows[1][1]["phase"] == "exit"


# ---------------------------------------------------------------------------
# Guards: nothing to hold
# ---------------------------------------------------------------------------

async def test_hold_skipped_without_start_time():
    """No injection_window_start_time (failed inject / chat turn / a
    pre-feature pipeline): the hold is a no-op — no events, no flip,
    byte-identical to switch-off."""
    values = _window_values(duration=10, age=0)
    values.pop("injection_window_start_time")
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )
    assert events == []
    assert ctx.intent_graph.updates == []
    assert stream_mod.get_active_hold("turn-hold-1") is None


async def test_hold_spent_window_still_flips_recover():
    """Verify outlasted the contract window (verify time > duration): no
    SSE hold — nothing left to count down — but the protocol still
    requires the agent's recovery report, so the recover flip runs."""
    values = _window_values(duration=5, age=30)  # 25s past the deadline
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()
    recorder = SidewriteRecorder()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, recorder)
    )
    # No SSE events and no jsonl rows: the spent branch holds nothing.
    assert events == []
    assert recorder.rows == []
    # No registry entry — /early-recover has nothing to wake.
    assert stream_mod.get_active_hold("turn-hold-1") is None
    # The flip still happened: recovery is dispatched on the same stream.
    assert len(ctx.intent_graph.updates) == 1
    assert ctx.intent_graph.updates[0]["values"]["confirmed_intent"] == "recover"


async def test_hold_skipped_without_real_task_id():
    """A state without a persistable task identity has no experiment to
    recover — the flip would hand _run_recover garbage."""
    values = _window_values(duration=10, age=0, task_id="turn-abc")
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )
    assert events == []
    assert ctx.intent_graph.updates == []


async def test_hold_skipped_on_unparseable_start_time():
    values = _window_values(duration=10, age=0)
    values["injection_window_start_time"] = "not-a-timestamp"
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )
    assert events == []
    assert ctx.intent_graph.updates == []


# ---------------------------------------------------------------------------
# Aborted teardown: client disconnect during the hold
# ---------------------------------------------------------------------------

async def test_hold_abort_sidewrites_exit_and_skips_flip():
    """A cancellation arriving mid-hold (client ESC → stream abort):
    jsonl-only ``exit(aborted)`` evidence, registry cleared, and NO
    recover flip — the outer user_cancel path owns the cleanup."""
    values = _window_values(duration=120, age=1)
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()
    recorder = SidewriteRecorder()

    agen = stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, recorder)
    await agen.__anext__()  # enter consumed; loop now waiting

    # aclose() raises GeneratorExit at the suspended await — the same
    # teardown shape a client disconnect drives from the outside.
    await agen.aclose()

    # Registry cleared by the hold's finally.
    assert stream_mod.get_active_hold("turn-hold-1") is None
    # The aborted exit landed in the EVIDENCE channel only.
    aborted = [r for r in recorder.rows if r[1].get("phase") == "exit"]
    assert len(aborted) == 1
    assert aborted[0][1]["reason"] == "aborted"
    # No flip: the cancellation path must not race a recover dispatch.
    assert ctx.intent_graph.updates == []


# ---------------------------------------------------------------------------
# Tick cadence + drift correction
# ---------------------------------------------------------------------------

async def test_hold_tick_rebases_remaining(monkeypatch):
    """The tick interval is patchable; with a tiny interval the stream
    carries enter → tick… → exit and every tick reports the REAL
    remaining (server clock), not an interpolation."""
    monkeypatch.setattr(stream_mod, "_HOLD_TICK_INTERVAL_S", 0.05)
    values = _window_values(duration=1, age=0.2)  # ~0.8s to live
    pipeline = SnapshotPipelineGraph(values)
    ctx = _ctx()
    ctx.intent_graph = RecordingIntentGraph()

    events = await _collect(
        stream_mod._hold_fault_window(ctx, pipeline, {"configurable": {}}, SidewriteRecorder())
    )
    payloads = [
        json.loads(json.loads(e.replace("data: ", "").strip())["content"])
        for e in events
    ]
    ticks = [p for p in payloads if p["phase"] == "tick"]
    assert len(ticks) >= 3  # 0.8s at 50ms cadence
    # Monotonically decreasing remaining across the tick series.
    remaining = [p["remaining_sec"] for p in ticks]
    assert remaining == sorted(remaining, reverse=True)


# ---------------------------------------------------------------------------
# _emit_result_card: the hold path's early inject ResultCard
# ---------------------------------------------------------------------------

async def test_emit_result_card_bookkeeping_and_sse():
    ctx = _ctx()
    recorded: list[tuple[str, str]] = []
    ctx.store = SimpleNamespace(add_task=lambda sid, tid: recorded.append((sid, tid)))
    recorder = SidewriteRecorder()

    payload = {"status": "success", "data": {"task_id": "inject-card-1"}}
    events = await _collect(stream_mod._emit_result_card(ctx, payload, recorder))

    assert len(events) == 1
    frame = json.loads(events[0].removeprefix("data: ").strip())
    assert frame["type"] == "result"
    assert json.loads(frame["content"])["data"]["task_id"] == "inject-card-1"
    assert recorded == [("sid-1", "inject-card-1")]
    assert recorder.rows and recorder.rows[0][0] == "result"


async def test_emit_result_card_skips_bookkeeping_for_non_task_ids():
    ctx = _ctx()
    recorded: list[tuple[str, str]] = []
    ctx.store = SimpleNamespace(add_task=lambda sid, tid: recorded.append((sid, tid)))

    events = await _collect(
        stream_mod._emit_result_card(
            ctx, {"status": "success", "data": {"task_id": "turn-x"}}, SidewriteRecorder(),
        )
    )
    assert len(events) == 1
    assert recorded == []


# ---------------------------------------------------------------------------
# /early-recover endpoint: HTTP-level contract
# ---------------------------------------------------------------------------

async def test_early_recover_endpoint_http_contract():
    """The endpoint as MOUNTED (sessions_router at its real prefix), not
    just the registry function it calls.

    Why HTTP-level at all: the TS client's earlyRecover maps 404 → false
    → cancelTurn — a route path drift would NOT crash, it would
    silently degrade Ctrl+R into a stream cancel and lose the in-band
    recovery evidence the evaluation protocol exists to produce. This
    test pins the exact URL the client builds (see
    core/src/api/client.ts earlyRecover) against the mounted route.
    ASGITransport keeps the endpoint on THIS test's event loop, so the
    asyncio.Event handshake is same-loop (what production uvicorn
    guarantees with its single loop). Minimal-app mounting follows the
    test_sessions_list convention: importing turn.py registers the
    early-recover decorator on sessions_router, and the router carries
    the same /api/v1/sessions prefix the real app mounts it under.
    """
    import httpx
    from fastapi import FastAPI

    from chaos_agent.server.routes import turn as _turn_module  # noqa: F401
    from chaos_agent.server.routes.sessions import sessions_router

    app = FastAPI()
    app.include_router(sessions_router)

    sid = "sid-http-1"
    turn_id = "turn-http-1"
    ev = asyncio.Event()
    stub = SimpleNamespace(turn_id=turn_id, sid=sid, hold_early_recover=ev)
    stream_mod._ACTIVE_HOLDS[turn_id] = stub
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = f"/api/v1/sessions/{sid}/turns/{turn_id}/early-recover"

            # 1. Happy path: the hold wakes, 200 with the ack body.
            r = await client.post(url)
            assert r.status_code == 200
            assert r.json() == {"ok": True, "turn_id": turn_id, "early_recover": True}
            assert ev.is_set()

            # 2. Cross-session request: another session must not wake
            # this session's hold (the ctx.sid check).
            ev2 = asyncio.Event()
            stub.hold_early_recover = ev2
            r = await client.post(
                f"/api/v1/sessions/sid-other/turns/{turn_id}/early-recover"
            )
            assert r.status_code == 404
            assert not ev2.is_set()

            # 3. Sanitisation: _SAFE_ID_PATTERN rejects the dot → 422
            # before any registry lookup.
            r = await client.post(
                f"/api/v1/sessions/bad.sid/turns/{turn_id}/early-recover"
            )
            assert r.status_code == 422

        # 4. Outside a hold the registry lookup misses: 404, nothing to
        # wake (a stale client retry after the window expired).
        stream_mod._ACTIVE_HOLDS.pop(turn_id, None)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            r = await client.post(
                f"/api/v1/sessions/{sid}/turns/{turn_id}/early-recover"
            )
            assert r.status_code == 404
    finally:
        stream_mod._ACTIVE_HOLDS.pop(turn_id, None)
