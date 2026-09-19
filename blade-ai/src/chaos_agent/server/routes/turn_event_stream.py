"""SSE event generator and stream helpers for the /turn endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import anyio
from fastapi import HTTPException

from chaos_agent.agent.intent_handoff import (
    build_pipeline_handoff_from_intent_state,
    clear_dispatched_operation_payload_update,
    detect_dispatchable_operation,
)
from chaos_agent.agent.result.operation_summary import (
    build_batch_summary_text,
    build_operation_record,
    build_recover_summary_text,
)
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.agent.spec.fault_spec import FAULT_PROPOSAL_OPEN
from chaos_agent.agent.streaming import (
    ProtocolSuffixFilter,
    SSEBatcher,
    StreamEvent,
    parse_stream_events,
)
from chaos_agent.agent.result.task_snapshot import resolve_recover_initial_state
from chaos_agent.agent.spec.skill_identity import has_active_skill
from chaos_agent.config.settings import settings
from chaos_agent.memory.operation_summary_writer import write_operation_summary
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_RECOVER_PAYLOAD,
    finalize_recover_session,
)
from chaos_agent.persistence.task_identity import (
    is_real_task_id,
    new_inject_task_id,
    new_recover_task_id,
)
from chaos_agent.server.routes.stream_abort import (
    ABORT_SQLITE_CEILING_S,
    ClientDisconnected,
    abort_row_word,
    write_aborted_task_row,
)
from chaos_agent.server.routes.turn_interrupt import (
    ConfirmTimeout,
    _KEEPALIVE_FRAME,
    content_from_interrupt_payload,
    extract_pending_interrupt,
    format_auto_approve_info,
    normalise_answer,
    wait_for_confirmation,
)
from chaos_agent.server.routes.turn_result import (
    build_recover_result_payload,
    build_result_payload,
)
from chaos_agent.utils.time import BEIJING_TZ, now_iso, parse_iso_timestamp

logger = logging.getLogger(__name__)


@dataclass
class TurnContext:
    """All per-turn state needed by event_generator."""
    sid: str
    turn_id: str
    thread_id: str
    input_text: str
    permission_mode: str
    dry_run: bool
    req: Any
    store: Any
    agents: dict
    task_tracker: Any
    intent_graph: Any
    pipeline_graph: Any
    graph_config: dict
    initial_state: dict
    tracker_key: str
    tracker_queue: asyncio.Queue
    # Mutable — event_generator may reassign for result extraction
    result_graph: Any = field(default=None, init=False)
    result_config: dict = field(default_factory=dict, init=False)
    # Pipeline coordinates, recorded AT DISPATCH (not at the successful end) so
    # an interruption can still locate the pipeline thread and mirror its
    # progress ledger back to the intent graph.
    pipeline_task_id: str = field(default="", init=False)
    pipeline_config: dict = field(default_factory=dict, init=False)
    # Recover runs on its OWN graph and thread, so an interrupted recovery must be
    # read from here rather than from the pipeline coordinates above — otherwise
    # the record would carry the wrong task and miss the recover ledger entirely.
    recover_task_id: str = field(default="", init=False)
    recover_config: dict = field(default_factory=dict, init=False)
    # True once this turn has already written an operation record, so a failure
    # afterwards cannot append a contradicting interruption record on top.
    operation_record_written: bool = field(default=False, init=False)
    # True once a turn-dispatched recovery finalized its session on the
    # normal path (round-54 G5). The finally block's abort fallback keys on
    # it: a recovery killed mid-run leaves BOTH its session row active and
    # its TaskStore row a zombie otherwise — _run_recover's finalize only
    # runs on the normal path, and the finally's _finalize_task_session
    # reads the INTENT graph (recover turns never set ctx.result_graph).
    recover_finalized: bool = field(default=False, init=False)
    # The cause string the abort chain last ran with (round-55 F2). The
    # finally block's fallback arms run OUTSIDE _abort_turn_cleanup — the
    # except handlers cannot hand them arguments — so the recover fallback
    # reads this field to classify its terminal word with the SAME
    # cause taxonomy the decision table uses (see abort_row_word in
    # stream_abort — the shared module that owns the row writer; the
    # taxonomy lived here module-privately for one round before round-56
    # moved it beside the row writer after the sibling streams were
    # caught carrying private mappings of their own). Empty on the
    # normal path, where recover_finalized suppresses the fallback.
    abort_cause: str = field(default="", init=False)
    # Monotonic reading at the moment a pipeline (inject / batch) is dispatched
    # in this turn. The ResultCard duration measures ONLY the operation itself:
    # intent clarification preceding dispatch is conversation time, not
    # operation time, and must not inflate the reported duration. Zero when no
    # pipeline dispatched in this turn (chat / recover-only turns); consumers
    # fall back to the turn start defensively.
    pipeline_started_monotonic: float = field(default=0.0, init=False)
    # Fault-window hold (turn_hold_fault_window). Set by the
    # /early-recover endpoint (Ctrl+R in the TS TUI) to break the hold
    # loop out of its window wait and dispatch the recover graph in the
    # same turn. A bare asyncio.Event: waited on only inside
    # event_generator's loop, and set only from the endpoint handler on
    # that same loop, so there are no cross-loop concerns.
    hold_early_recover: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    # True once the hold path has emitted the inject ResultCard ahead of
    # the window. Step 3 of event_generator suppresses its own emission
    # on this flag: after the early emission the card would otherwise
    # either double-fire (hold skipped, pipeline state still "inject") or
    # be lost entirely (recover intent flipped, state no longer "inject").
    inject_result_emitted: bool = field(default=False, init=False)


# Fault-window hold tuning (turn_hold_fault_window). The tick is the SSE
# liveness channel during the hold: it re-synchronises the client's local
# countdown (clock-drift correction) and keeps proxies from closing an
# idle connection. 25s matches _CONFIRM_KEEPALIVE_INTERVAL_S
# (turn_interrupt) on purpose: the confirmation gate already ships 25s
# frame gaps on this very stream, so the hold inherits that proven
# tolerance envelope instead of inventing a coarser cadence. The primary
# consumers (TS TUI, benchmark harness) are localhost-direct — the
# proxy defence is for user-fronted deployments only.
_HOLD_TICK_INTERVAL_S = 25.0

# turn_id -> TurnContext for the turn currently inside its fault-window
# hold. Populated ONLY while the hold loop is awake (registered at enter,
# popped in the hold's finally), so the /early-recover endpoint's lookup
# misses naturally outside the window — no separate state machine to keep
# in sync. Event-loop confined (the endpoint handler and the generator
# share one loop); no locking.
_ACTIVE_HOLDS: dict[str, TurnContext] = {}


# Per-session in-flight turn guard (concurrent /turn serialization).
# One conversation thread per session means two concurrently-running
# turns interleave their graph writes on the SAME thread. The client
# sides already lock their composers while busy (TUI busy+
# faultWindowHold, web busy), but the HTTP surface itself had no guard:
# a direct API double-POST (curl, scripts) raced the shared thread.
# Registration happens in the /turn handler atomically (the get→set
# stretch carries no await, so the event loop cannot interleave another
# claim); release lives in event_generator's FINALLY — the single
# funnel every exit (normal, error, cancel, disconnect) already routes
# through, before any of the shielded terminal work that could throw.
#
# The grace window exists for the SUPERSEDE flow (useStream's
# ConfirmMessage feedback path): the client aborts the old stream and
# posts the new turn in one synchronous sequence, so the new POST can
# land BEFORE the old generator's finally released the slot. A flat 409
# would break that core UX path; the newcomer instead WAITS for the old
# turn's release — disconnect propagation plus the shielded terminal
# finalize typically complete in well under a second. A turn that is
# legitimately still RUNNING (not tearing down) keeps the newcomer
# waiting the full grace and then takes the 409 (the SQLite-class
# ABORT_SQLITE_CEILING_S of 30s bounds the pathological teardown; the
# grace deliberately sits under it — a teardown that overruns both is
# already a bigger incident than a superseded 409).
_TURN_SLOT_GRACE_S = 10.0
# Stale-slot defence: a slot whose generator never ran (a Starlette-
# level failure between handler registration and the response's first
# iteration — unreachable in normal uvicorn operation, defended anyway)
# would deadlock the session forever. 8h sits ABOVE the confirmation
# gate's 6h wait ceiling: a legally long-lived paused turn must NEVER be
# reclaimed, only a truly abandoned slot may.
_STALE_TURN_SLOT_S = 8 * 3600.0
# sid -> (turn_id, done_event, registered_at monotonic)
_ACTIVE_TURNS: dict[str, tuple[str, asyncio.Event, float]] = {}


async def _acquire_turn_slot(sid: str, turn_id: str) -> None:
    """Serialize /turn per session; 409 a still-running previous turn.

    Waiters loop: each wake re-reads the slot, so two newcomers that
    were BOTH released by the old turn's teardown do not race — the
    first scheduled wake claims the slot synchronously, the second
    finds the fresh entry and keeps waiting (or eventually 409s).
    """
    _loop = asyncio.get_event_loop()
    _deadline = _loop.time() + _TURN_SLOT_GRACE_S
    while True:
        _old = _ACTIVE_TURNS.get(sid)
        if _old is None:
            break
        _old_turn, _done, _reg = _old
        if _done.is_set():
            # Defensive: release() runs set+del in one synchronous
            # stretch, so this state is unobservable in practice. Treat
            # it as released and let the registration below overwrite
            # the leftover — never spin on it.
            break
        if _loop.time() - _reg > _STALE_TURN_SLOT_S:
            logger.warning(
                "turn slot for sid=%s is stale (turn=%s, age > %.0fh) — "
                "reclaiming (its generator never reached release)",
                sid, _old_turn, _STALE_TURN_SLOT_S / 3600.0,
            )
            del _ACTIVE_TURNS[sid]
            break
        _remaining = _deadline - _loop.time()
        if _remaining <= 0:
            raise HTTPException(
                409,
                f"Another turn is still running for this session "
                f"(turn={_old_turn}); retry after it completes",
            )
        try:
            # shield: a cancellation of THIS request (caller disconnect
            # while queued) must not disturb the old turn's done event.
            await asyncio.wait_for(
                asyncio.shield(_done.wait()), timeout=_remaining,
            )
        except asyncio.TimeoutError:
            raise HTTPException(
                409,
                f"Another turn is still running for this session "
                f"(turn={_old_turn}); retry after it completes",
            ) from None
        # Released while we waited — loop back: the slot is either gone
        # (claim it) or already claimed by a faster waiter (keep
        # waiting on the FRESH entry, grace still ticking).
    _ACTIVE_TURNS[sid] = (turn_id, asyncio.Event(), _loop.time())


def _release_turn_slot(sid: str, turn_id: str) -> None:
    """Release the session's turn slot from event_generator's finally.

    Match-guarded: a slot already overwritten (stale reclaim, or a
    supersede winner that claimed after our release fired once) is
    never deleted by the turn that no longer owns it — an unconditional
    delete would re-arm the exact double-turn race this guard closes.
    set + del run in one synchronous stretch: a waiter observes either
    "slot present, not done" or "slot gone", never the torn middle.
    """
    _old = _ACTIVE_TURNS.get(sid)
    if _old is not None and _old[0] == turn_id:
        _old[1].set()
        del _ACTIVE_TURNS[sid]


def get_active_hold(turn_id: str) -> TurnContext | None:
    """Accessor for the early-recover endpoint; keeps the registry private."""
    return _ACTIVE_HOLDS.get(turn_id)


# ---------------------------------------------------------------------------
# Status event converters
# ---------------------------------------------------------------------------

def _convert_compaction_status(status_evt, turn_id: str) -> StreamEvent | None:
    if getattr(status_evt, "source", "") != "memory_compression":
        return None
    detail = getattr(status_evt, "detail", None) or {}
    return StreamEvent(
        type="memory_compaction",
        content=getattr(status_evt, "message", ""),
        task_id=turn_id,
        compaction_phase=getattr(status_evt, "phase", ""),
        tokens_before=int(detail.get("total_tokens_before") or detail.get("tokens_before") or 0),
        tokens_after=int(detail.get("tokens_after") or 0),
        messages_compacted=int(detail.get("messages_to_compact") or detail.get("messages_compacted") or 0),
        duration_ms=float(getattr(status_evt, "duration_ms", 0.0) or 0.0),
        layer="llm_summary",
    )


def _convert_context_size_status(status_evt, turn_id: str) -> StreamEvent | None:
    if getattr(status_evt, "source", "") != "context_size":
        return None
    detail = getattr(status_evt, "detail", None) or {}
    return StreamEvent(
        type="context_size",
        task_id=turn_id,
        context_current_tokens=int(detail.get("current_tokens") or 0),
        context_trigger_tokens=int(detail.get("trigger_tokens") or 0),
        context_max_tokens=int(detail.get("max_tokens") or 0),
        context_messages_count=int(detail.get("messages_count") or 0),
    )


def _convert_postmortem_status(status_evt, turn_id: str) -> StreamEvent | None:
    if getattr(status_evt, "source", "") != "postmortem":
        return None
    phase = getattr(status_evt, "phase", "")
    msg = getattr(status_evt, "message", "") or "Generating postmortem"
    if phase in ("completed", "failed"):
        return StreamEvent(type="node_end", task_id=turn_id, node="postmortem", content=msg, phase="save")
    return StreamEvent(type="node_start", task_id=turn_id, node="postmortem", content=msg, phase="save")


# ---------------------------------------------------------------------------
# Merged graph + status stream
# ---------------------------------------------------------------------------

# Heartbeat interval for the main SSE stream (seconds). During long-running
# operations (LLM thinking, tool execution), if no graph/status events arrive
# within this window, a heartbeat sentinel is pushed to the unified queue.
# The downstream ``_drain_merged`` converts it to a ``: keepalive\n\n`` SSE
# comment that:
#   - Keeps the TCP connection alive (prevents OS/proxy idle timeout)
#   - Prevents Starlette ``req.is_disconnected()`` false positives
#   - Tells the client the server is still working
_STREAM_HEARTBEAT_INTERVAL_S = 15

# Ceiling for the shielded cancelled-turn cleanup chain (round-49). While the
# shield scope is active it blocks ALL cancellation — including uvicorn's
# graceful shutdown (the project runs uvicorn without timeout_graceful_shutdown,
# i.e. it waits forever for in-flight connections). The chain's only long await
# is the kubectl-delete inside ``_cleanup_cancelled_execution_artifacts``
# (timeout=30 per call, fan-out concurrency 10), so 90s covers the realistic
# worst case; hitting the ceiling means the environment is pathological and the
# remaining steps are abandoned (bounded loss) rather than holding the task
# group — and the server — hostage indefinitely.
_CANCEL_CLEANUP_CEILING_S = 90.0


async def _merged_stream(graph_iter, tracker_queue: asyncio.Queue):
    """Yield ``("graph", event)`` / ``("status", event)`` tuples from two
    concurrent sources (LangGraph astream_events + status tracker queue).

    Also yields ``("heartbeat", None)`` every ``_STREAM_HEARTBEAT_INTERVAL_S``
    seconds when no real events arrive, keeping the SSE connection alive.
    """
    unified: asyncio.Queue = asyncio.Queue()
    graph_done = object()

    async def _graph_pump():
        try:
            async for raw in graph_iter:
                await unified.put(("graph", raw))
        finally:
            # abort-safe: unbounded-queue put never suspends — no
            # cancellation delivery point (allowlist, invariants test).
            await unified.put(("graph_done", graph_done))

    async def _status_pump():
        try:
            while True:
                evt = await tracker_queue.get()
                await unified.put(("status", evt))
        except asyncio.CancelledError:
            pass

    async def _heartbeat_pump():
        """Periodically push heartbeat sentinels when no real events flow."""
        try:
            while True:
                await asyncio.sleep(_STREAM_HEARTBEAT_INTERVAL_S)
                await unified.put(("heartbeat", None))
        except asyncio.CancelledError:
            pass

    g_task = asyncio.create_task(_graph_pump())
    s_task = asyncio.create_task(_status_pump())
    h_task = asyncio.create_task(_heartbeat_pump())
    try:
        while True:
            kind, payload = await unified.get()
            if kind == "heartbeat":
                yield kind, payload
                continue
            if kind == "graph_done":
                s_task.cancel()
                h_task.cancel()
                try:
                    await s_task
                except asyncio.CancelledError:
                    pass
                try:
                    await h_task
                except asyncio.CancelledError:
                    pass
                while True:
                    try:
                        nk, np = unified.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nk in ("graph_done", "heartbeat"):
                        continue
                    yield nk, np
                while True:
                    try:
                        evt = tracker_queue.get_nowait()
                        yield "status", evt
                    except asyncio.QueueEmpty:
                        break
                if g_task.done():
                    exc = g_task.exception()
                    if exc is not None:
                        raise exc
                return
            yield kind, payload
    finally:
        if not s_task.done():
            s_task.cancel()
            try:
                await s_task  # abort-safe: see invariants allowlist
            except asyncio.CancelledError:
                pass
        if not g_task.done():
            g_task.cancel()
            try:
                await g_task  # abort-safe: see invariants allowlist
            except asyncio.CancelledError:
                pass
        if not h_task.done():
            h_task.cancel()
            try:
                await h_task  # abort-safe: see invariants allowlist
            except asyncio.CancelledError:
                pass


# ---------------------------------------------------------------------------
# Reusable stream consumption + interrupt drain helpers
# ---------------------------------------------------------------------------

def _make_sidewrite(sid: str):
    """Return a sidewrite callback that logs events to the TUI session store."""
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store

    def _sidewrite(evt: StreamEvent, source: str = "pipeline") -> None:
        try:
            _ts = _get_tui_store()
            if _ts is not None and sid:
                _ts.append_event(sid, {
                    "ts": evt.timestamp,
                    "source": source,
                    "task_id": evt.task_id or "",
                    "event_type": evt.type,
                    "data": evt.to_dict(),
                })
        except Exception:
            pass

    return _sidewrite


def _make_converters(turn_id: str):
    """Build the list of status-event converter functions for a turn."""
    return [
        lambda s, tid=turn_id: _convert_compaction_status(s, tid),
        lambda s, tid=turn_id: _convert_context_size_status(s, tid),
        lambda s, tid=turn_id: _convert_postmortem_status(s, tid),
    ]


async def _drain_merged(merged_iter, turn_id, batcher, sidewrite, converters, req, *, source="pipeline"):
    """Consume a merged stream, yielding SSE frames.

    Handles three kinds from ``_merged_stream``:
      - ``"graph"`` — LangGraph streaming events → parsed, batched, yielded
      - ``"status"`` — status tracker events → converted, yielded
      - ``"heartbeat"`` — keepalive sentinel → yields SSE comment to keep
        the TCP connection alive and prevent ``is_disconnected()`` false
        positives during long-running operations (LLM thinking, tool exec).

    Raises ``ClientDisconnected`` if the client drops the connection.
    """
    intent_protocol_filter = ProtocolSuffixFilter(FAULT_PROPOSAL_OPEN)
    async for kind, payload in merged_iter:
        if kind == "heartbeat":
            # Yield SSE comment (invisible to the JSON parser on the client
            # but keeps the TCP socket active).
            yield _KEEPALIVE_FRAME
            continue
        if await req.is_disconnected():
            raise ClientDisconnected()
        if kind == "graph":
            for evt in parse_stream_events(payload):
                if evt.type == "token" and evt.node == "intent_clarification":
                    public_content = intent_protocol_filter.feed(evt.content)
                    if not public_content:
                        continue
                    evt.content = public_content
                evt.task_id = turn_id
                sidewrite(evt, source=source)
                for sse in batcher.feed(evt):
                    yield sse
        elif kind == "status":
            for sse in batcher.flush():
                yield sse
            for convert in converters:
                converted = convert(payload)
                if converted is not None:
                    sidewrite(converted, source=source)
                    yield converted.to_sse()
    remaining_public_content = intent_protocol_filter.flush()
    if remaining_public_content:
        remaining_evt = StreamEvent(
            type="token",
            content=remaining_public_content,
            node="intent_clarification",
            task_id=turn_id,
        )
        sidewrite(remaining_evt, source=source)
        for sse in batcher.feed(remaining_evt):
            yield sse
    for sse in batcher.flush():
        yield sse


async def _drain_interrupts(graph, config, ctx, batcher, sidewrite, converters):
    """Drain all pending interrupts from a graph, yielding SSE frames.

    For each interrupt: emit confirm/auto-approve → wait → resume → stream.
    Raises ``ConfirmTimeout`` on timeout.
    """
    from langgraph.types import Command
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store

    while True:
        current_state = await graph.aget_state(config)
        pending = extract_pending_interrupt(current_state)
        if pending is None:
            break

        node, payload = pending
        is_auto = ctx.permission_mode != "confirm"

        if is_auto and node in ("confirmation_gate", "plan_change_confirm", "tool_screener"):
            # Emit a structured ``auto_approved`` event carrying the full
            # interrupt payload so the TS TUI renders the SAME read-only card
            # as a manual confirm (ConfirmContextRenderer) — just without the
            # interactive prompt. ``content`` keeps the plain-text summary as a
            # graceful fallback for clients that don't render the card.
            info_evt = StreamEvent(
                type="auto_approved",
                content=format_auto_approve_info(node, payload),
                node=node,
                task_id=ctx.turn_id,
                payload=payload,
            )
            sidewrite(info_evt)
            yield info_evt.to_sse()
            normalised = "approved"
        else:
            confirm_evt = StreamEvent(
                type="confirm",
                content=content_from_interrupt_payload(payload),
                node=node, task_id=ctx.turn_id, payload=payload,
            )
            sidewrite(confirm_evt)
            yield confirm_evt.to_sse()

            answer = None
            async for frame in wait_for_confirmation(
                ctx.store, ctx.turn_id, settings.confirm_wait_timeout,
            ):
                if frame == _KEEPALIVE_FRAME:
                    yield frame  # real-time keepalive to client
                else:
                    answer = frame  # user's answer

            normalised = answer if node == "plan_builder" else normalise_answer(answer)

        try:
            _ts = _get_tui_store()
            if _ts is not None and ctx.sid:
                _ts.append_event(ctx.sid, {
                    "ts": now_iso(), "source": "user",
                    "task_id": ctx.turn_id, "event_type": "confirm_answer",
                    "data": {"content": normalised},
                })
        except Exception:
            pass

        async for sse in _drain_merged(
            _merged_stream(
                graph.astream_events(Command(resume=normalised), config, version="v2"),
                ctx.tracker_queue,
            ),
            ctx.turn_id, batcher, sidewrite, converters, ctx.req,
        ):
            yield sse


# ---------------------------------------------------------------------------
# Defensive finalize
# ---------------------------------------------------------------------------

async def _finalize_task_session(
    graph, config, turn_id, store_cancel_fn, *, cancelled: bool = False,
    abort_cause: str = "",
):
    """Flush messages and finalize the task session on the clean-exit path.

    ``abort_cause`` (round-60 F1''') carries the finally's cause memo —
    the same ``ctx.abort_cause`` the G5 recover fallback reads — so the
    terminal word classifies through the shared taxonomy for EVERY abort
    cause, not just the two that set the binary ``cancelled`` flag
    (confirm_timeout sets no flag; internal_error needs none).
    """
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        _store = get_global_session_store()
        if _store is None:
            return
        try:
            _final = await graph.aget_state(config)
        except Exception:
            _final = None
        _op_tid = ""
        _state_msgs: list = []
        _state_values: dict = {}
        if _final and getattr(_final, "values", None):
            _state_values = _final.values
            _candidate = _state_values.get("task_id", "")
            if isinstance(_candidate, str) and is_real_task_id(_candidate):
                _op_tid = _candidate
            _state_msgs = list(_state_values.get("messages") or [])
        paused_at_interrupt = bool(_final and getattr(_final, "next", None))
        is_inject_task = bool(
            is_real_task_id(_op_tid)
            and _state_values.get("operation") != "recover"
        )
        if _op_tid and _store.has_active(_op_tid):
            # Round-60 F5''': a user-requested recover turn's intent state
            # carries the RECOVER session id (allocated by the
            # clarification recover branch) with NO ``operation`` field,
            # so the inject-arm classification below misreads it and
            # finalizes the recover session from INTENT values — wrong
            # graph, fail-OPEN word (infer_task_state maps the "recover"
            # bridge state to "completed") and no parent_task_id link.
            # The finally block's G5 fallback owns recover-session closure
            # (finalize_recover_session + parent link + the classified
            # word); skipping here also stops the append below from
            # routing intent dialogue into the recover task file.
            if _state_values.get("confirmed_intent") == "recover":
                logger.info(
                    "Recover-shaped final state for task=%s; session left "
                    "to the recover fallback",
                    _op_tid,
                )
                return
            if _state_msgs and (paused_at_interrupt or not is_inject_task):
                try:
                    _store.append_messages(_op_tid, _state_msgs)
                    logger.info("Flushed %d state messages to task=%s", len(_state_msgs), _op_tid)
                except Exception:
                    logger.warning("Pre-finalize flush failed for task=%s", _op_tid, exc_info=True)
            if not paused_at_interrupt:
                _vals = _state_values
                if is_inject_task:
                    from chaos_agent.memory.session_finalizer import (
                        finalize_inject_session,
                    )
                    from chaos_agent.agent.state import (
                        infer_task_state as _infer_state,
                    )

                    # Round-61 R61-5 (G6 on the session surface): the
                    # inject session is NEVER finalized on the normal
                    # path — this defensive arm is its only closer — so
                    # on an auto-recover turn aborted DURING the recover
                    # segment, the inject pipeline's own thread still
                    # reads active here with its verdict already on
                    # record. The cause gate must not fire for a run
                    # that reached its own verdict: infer first, and
                    # let the abort word only classify runs still
                    # mid-flight ("injecting"). The row write below
                    # carries the same gate (the store layer's
                    # skip_if_terminal is the second line of defense).
                    _run_finished = _infer_state(_vals) != "injecting"
                    _abort_terminal = (
                        (cancelled or abort_cause) and not _run_finished
                    )

                    if _abort_terminal:
                        # Terminal write for the TaskStore row, BEFORE the
                        # session finalize: the row is the user-facing fact
                        # (boot card / /tasks), the session record is
                        # derived — a failure in the latter must not swallow
                        # the former. No later writer comes on the abort
                        # path, and inference cannot derive "cancelled" on
                        # its own. Shared guarded helper since round-54: the
                        # terminal-regression guard (G6) keeps a row that
                        # already reached its OWN terminal word (the
                        # pipeline completed before the abort raced in).
                        # Round-60 F1''': the word classifies from the
                        # cause memo (the G5 fallback's own read), so the
                        # no-flag causes (confirm_timeout, internal_error)
                        # carry their own word too — same event, one word
                        # on all surfaces, the r55-r57 taxonomy contract.
                        await write_aborted_task_row(
                            _op_tid,
                            abort_row_word(abort_cause or "user_cancel"),
                        )
                    await finalize_inject_session(
                        _store,
                        graph,
                        config,
                        _op_tid,
                        precomputed_values=_vals,
                        # Round-57 F2' / round-60 F1''': word value from the
                        # shared taxonomy, cause-routed — the binary flag
                        # covered only the user_cancel/disconnected exits;
                        # confirm_timeout (no flag) landed on the inference
                        # path and the session said "failed" while the G4
                        # arm's row said "cancelled". None on the clean
                        # exit is inference, the deliberate session-surface
                        # semantic — no regression guard here to justify an
                        # unconditional classified write.
                        status_override=(
                            abort_row_word(abort_cause or "user_cancel")
                            if _abort_terminal
                            else None
                        ),
                    )
                    logger.info(
                        "Defensive inject finalize for task=%s from graph state",
                        _op_tid,
                    )
                else:
                    from chaos_agent.agent.state import TaskState, infer_task_state

                    _task_state = infer_task_state(_vals)
                    # "unverified" is a terminal knowledge claim (the run
                    # ENDED with an honest "no conclusion") — session-level
                    # completion, with the verdict carried in
                    # result_summary, not a session failure. Word set derives
                    # from the TaskState legislation (round-15).
                    _final_status = (
                        "completed"
                        if _task_state in (
                            TaskState.RECOVERED.value,
                            TaskState.PARTIAL_RECOVERED.value,
                            TaskState.COMPLETED.value,
                            TaskState.UNVERIFIED.value,
                        )
                        else "failed"
                    )
                    _store.finalize_session(
                        _op_tid,
                        remaining_messages=[],
                        status=_final_status,
                    )
                    logger.info(
                        "Defensive finalize for task=%s status=%s",
                        _op_tid, _final_status,
                    )
            else:
                logger.info(
                    "Paused at interrupt for task=%s (next=%s); kept session active",
                    _op_tid, list(getattr(_final, "next", []) or []),
                )
    except Exception:
        logger.warning("Defensive task-session finalize failed for turn=%s", turn_id, exc_info=True)


# ---------------------------------------------------------------------------
# Pipeline sub-generators
# ---------------------------------------------------------------------------

async def _run_inject_pipeline(ctx, iv, batcher, sidewrite, converters):
    """Launch and stream the single-inject Pipeline Graph."""
    import uuid

    from langchain_core.messages import SystemMessage as _SM
    from chaos_agent.agent.nodes.planning.intent_clarification import bootstrap_task_session

    # Duration origin for the ResultCard: the pipeline dispatch moment, not
    # the turn start — the intent clarification that led here is conversation
    # time and must not inflate the reported operation duration.
    ctx.pipeline_started_monotonic = time.monotonic()

    _handoff_data = build_pipeline_handoff_from_intent_state(
        iv,
        operation="inject",
        task_id=iv.get("task_id", "") or new_inject_task_id(),
        default_tui_session_id=ctx.sid,
    )
    _p_task_id = _handoff_data.task_id
    _handoff = _handoff_data.handoff_summary
    _tui_sid = _handoff_data.tui_session_id

    # P0-7-6 (ported 2026-09-01 from the retired local converse_stream twin):
    # ONE message object with an explicit id, reused by both write paths —
    # the session-store jsonl first entry (bootstrap_task_session) and the
    # pipeline graph input below. Two bare constructions produce two ids,
    # which defeats read_session's id-first dedup and the handoff then
    # appears twice in the task file (observed in task-866648cc on the CLI
    # twin; add_messages preserves an existing id instead of replacing it,
    # so one shared object keeps both dedup keys identical).
    _handoff_msg = _SM(content=_handoff, id=str(uuid.uuid4())) if _handoff else None

    if _p_task_id:
        bootstrap_task_session(
            _p_task_id, operation="inject",
            tui_session_id=_tui_sid,
            handoff_message=_handoff_msg,
        )

    _p_config = {
        "configurable": {"thread_id": _p_task_id},
        "recursion_limit": settings.recursion_limit,
    }
    # Record at dispatch so an interruption anywhere below can still read this
    # pipeline's ledger (the success path's ctx.result_* assignment is too late).
    ctx.pipeline_task_id = _p_task_id
    ctx.pipeline_config = _p_config
    _p_input = build_inject_initial_state(
        task_id=_p_task_id,
        tui_session_id=_tui_sid,
        confirmed_intent="inject",
        fault_spec=_handoff_data.fault_spec,
        needs_confirmation=True,
        interaction_mode="tui",
        # Isolation axes from the ContextVar settings (platform injects both
        # via blade_ai_context; empty locally = unfiltered): without this,
        # dialogue-dispatched tasks persisted with tenant_id="" — visible
        # to every tenant's query_active — the latent gap this column closes.
        tenant_id=getattr(settings, "tenant_id", "") or "",
        workspace_id=getattr(settings, "workspace_id", "") or "",
        kubeconfig=settings.kubeconfig_path,
        kube_context=settings.kube_context,
        kubewiz_cluster_uuid=settings.kubewiz_cluster_uuid,
        kubewiz_profile=settings.kubewiz_profile,
        kube_connection_mode=settings.kube_connection_mode,
        host_name=getattr(settings, "host_name", ""),
        ssh_host=getattr(settings, "ssh_host", ""),
        ssh_user=getattr(settings, "ssh_user", ""),
        ssh_key_path=getattr(settings, "ssh_key_path", ""),
        ssh_port=getattr(settings, "ssh_port", None),
        messages=[_handoff_msg] if _handoff_msg else [],
        dry_run=ctx.dry_run,
        planning_mode=iv.get("planning_mode"),
        # Cross-graph bridge (tier1-speedup): intent-time evidence (ledger +
        # probe snapshot) rides the handoff into the pipeline graph.
        progress_ledger=_handoff_data.progress_ledger,
        probe_snapshot=_handoff_data.probe_snapshot,
    )

    try:
        if not ctx.dry_run:
            await _clear_dispatched_inject_intent_state(ctx, reason="inject dispatch")

        async for sse in _drain_merged(
            _merged_stream(ctx.pipeline_graph.astream_events(_p_input, _p_config, version="v2"), ctx.tracker_queue),
            ctx.turn_id, batcher, sidewrite, converters, ctx.req,
        ):
            yield sse

        async for sse in _drain_interrupts(ctx.pipeline_graph, _p_config, ctx, batcher, sidewrite, converters):
            yield sse

        if ctx.dry_run:
            return

        ctx.result_graph = ctx.pipeline_graph
        ctx.result_config = _p_config
        ctx.store.add_task(ctx.sid, _p_task_id)

        # Write task summary back to Intent Graph
        try:
            _pfinal = await ctx.pipeline_graph.aget_state(_p_config)
            _psv = _pfinal.values if _pfinal else {}
            # ONE combined record: task summary headline + the ledger's process
            # detail (established facts + milestone log), so the context-isolated
            # intent graph sees how the operation went in a single coherent
            # message — not a separate, possibly-contradicting ledger blob.
            _summary_text = build_operation_record(_psv, _p_task_id)
            await write_operation_summary(
                _summary_text,
                intent_graph=ctx.intent_graph,
                thread_id=ctx.thread_id,
                state_update={"pipeline_task_id": _p_task_id},
                tui_session_id=ctx.sid,
                recursion_limit=settings.recursion_limit,
            )
            # This turn now has its record; a later failure must not append a
            # contradicting interruption note on top of a completed operation.
            ctx.operation_record_written = True
        except Exception:
            logger.debug("Failed to write task summary to Intent Graph", exc_info=True)
    finally:
        # abort-safe: normal-path backstop (runs uncancelled on the plain
        # exception unwinds); the abort path's remover is the gated clear
        # inside _abort_turn_cleanup (invariants I5) — idempotent pair.
        if not ctx.dry_run:
            await _clear_dispatched_inject_intent_state(ctx, reason="inject pipeline finalization")


async def _clear_dispatched_inject_intent_state(ctx, *, reason: str) -> None:
    """Clear one-shot inject intent fields after pipeline dispatch.

    Injection execution runs in a separate Pipeline Graph thread. Once the
    pipeline has been dispatched, the Intent Graph must stop carrying
    executable intent fields; stale ones re-open an old intent_confirm
    card on the next user turn.

    Call sites and their coverage (round-52 audit): the dispatch-time call
    — awaited BEFORE streaming starts — is the main-path remover (a
    mid-stream abort finds the fields already gone); the pipeline
    finalizers' finally call is the normal-path backstop; the gated arm in
    ``_abort_turn_cleanup`` covers the residual windows neither of those
    survives (a cancel landing on the dispatch-time clear's own await, its
    fail-soft failure under SQLite lock contention, and the finally's
    inline death under scope cancel). Idempotent all-fields-None update,
    so the overlapping calls are harmless.
    """
    try:
        await ctx.intent_graph.aupdate_state(
            {
                "configurable": {"thread_id": ctx.thread_id},
                "recursion_limit": settings.recursion_limit,
            },
            clear_dispatched_operation_payload_update(),
            as_node="save_dialogue",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Failed to clear dispatched inject intent state after %s",
            reason,
            exc_info=True,
        )


async def _run_batch_pipeline(ctx, iv, batcher, sidewrite, converters):
    """Launch and stream the batch-inject Pipeline Graph."""
    import uuid

    from langchain_core.messages import SystemMessage as _SM
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store
    from pathlib import Path

    # Duration origin for the ResultCard: batch dispatch, same rationale as
    # the single-inject path above (intent clarification is conversation time).
    ctx.pipeline_started_monotonic = time.monotonic()

    _handoff_data = build_pipeline_handoff_from_intent_state(
        iv,
        operation="batch_inject",
        task_id=new_inject_task_id(),
        default_tui_session_id=ctx.sid,
    )
    _p_task_id = _handoff_data.task_id
    _tui_sid = _handoff_data.tui_session_id
    _handoff = _handoff_data.handoff_summary

    _p_config = {
        "configurable": {"thread_id": _p_task_id},
        "recursion_limit": settings.recursion_limit,
    }
    # Record at dispatch (same reason as the single-inject path above).
    ctx.pipeline_task_id = _p_task_id
    ctx.pipeline_config = _p_config
    _p_input = build_inject_initial_state(
        task_id=_p_task_id,
        tui_session_id=_tui_sid,
        fault_spec=_handoff_data.fault_spec,
        needs_confirmation=True,
        interaction_mode="tui",
        # Same isolation wiring as the single-inject dispatch above (batch
        # twin): both axes from settings, or rows persist unscoped.
        tenant_id=getattr(settings, "tenant_id", "") or "",
        workspace_id=getattr(settings, "workspace_id", "") or "",
        kubeconfig=settings.kubeconfig_path,
        kube_context=settings.kube_context,
        kubewiz_cluster_uuid=settings.kubewiz_cluster_uuid,
        kubewiz_profile=settings.kubewiz_profile,
        kube_connection_mode=settings.kube_connection_mode,
        host_name=getattr(settings, "host_name", ""),
        ssh_host=getattr(settings, "ssh_host", ""),
        ssh_user=getattr(settings, "ssh_user", ""),
        ssh_key_path=getattr(settings, "ssh_key_path", ""),
        ssh_port=getattr(settings, "ssh_port", None),
        batch_submit_args=_handoff_data.batch_submit_args,
        # P0-7-6 form (see _run_inject_pipeline above): explicit id up front.
        # The batch path has a single write path today (no bootstrap_task_session
        # call), but an id keeps the dedup contract intact the moment a second
        # write path ever appears — same discipline as the single-inject twin.
        messages=[
            _SM(content=_handoff, id=str(uuid.uuid4()))
        ] if _handoff else [],
        dry_run=False,
        # Cross-graph bridge deliberately NOT wired here (tier1-speedup, code
        # review round 6): ``route_pipeline_start`` sends batch dispatches
        # straight to ``batch_setup``, whose per-fault reset (which EVERY
        # fault including the first passes through) clears both fields —
        # passing them would be dead code. Batch clarification evidence is
        # multi-target anyway; a per-fault snapshot would need per-spec
        # harvesting that the single-spec harvester cannot express. The
        # single-inject path above is where the bridge actually pays off.
    )

    _bp_timed_out = False
    try:
        await _clear_dispatched_inject_intent_state(ctx, reason="batch dispatch")

        async for sse in _drain_merged(
            _merged_stream(ctx.pipeline_graph.astream_events(_p_input, _p_config, version="v2"), ctx.tracker_queue),
            ctx.turn_id, batcher, sidewrite, converters, ctx.req,
        ):
            yield sse

        try:
            async for sse in _drain_interrupts(ctx.pipeline_graph, _p_config, ctx, batcher, sidewrite, converters):
                yield sse
        except ConfirmTimeout:
            _bp_timed_out = True

        # Batch post-processing (runs even on timeout to preserve partial results)
        _bp_final = await ctx.pipeline_graph.aget_state(_p_config)
        _bpv = _bp_final.values if _bp_final else {}
        _batch_results = _bpv.get("batch_results") or []

        for _br in _batch_results:
            _br_tid = _br.get("task_id", "")
            if _br_tid:
                ctx.store.add_task(ctx.sid, _br_tid)
                _br_tui = _get_tui_store()
                if _br_tui is not None:
                    try:
                        _br_tui.add_task(ctx.sid, _br_tid)
                    except Exception:
                        pass

        if _batch_results:
            # Aggregate per-fault postmortems into one batch report
            _batch_pm_path_str = ""
            try:
                _pm_dir = Path(settings.resolved_memory_dir).parent / "postmortems"
                _pm_sections = [
                    "# Batch Fault Injection Analysis Report\n",
                    f"{len(_batch_results)} fault(s) in total\n",
                ]
                for _bi, _br in enumerate(_batch_results):
                    _br_tid = _br.get("task_id", "")
                    _br_ft = _br.get("fault_type", "unknown")
                    _br_ts = _br.get("task_state", "unknown")
                    _pm = _br.get("postmortem")
                    _pm_sections.append(f"---\n\n## Fault {_bi+1}: {_br_ft} → {_br_ts}\n")
                    _pm_sections.append(f"task_id: `{_br_tid}`\n")
                    if _pm and isinstance(_pm, dict) and _pm.get("markdown"):
                        _pm_sections.append(_pm["markdown"])
                    else:
                        _pm_path = _pm_dir / f"{_br_tid}.md" if _br_tid else None
                        if _pm_path and _pm_path.exists():
                            _pm_sections.append(_pm_path.read_text(encoding="utf-8"))
                        else:
                            _pm_sections.append("*No post-mortem analysis was generated*\n")

                _batch_pm_file = _pm_dir / f"batch-{ctx.turn_id}.md"
                _pm_dir.mkdir(parents=True, exist_ok=True)
                _batch_pm_file.write_text("\n".join(_pm_sections), encoding="utf-8")
                _batch_pm_path_str = str(_batch_pm_file)

                _pm_evt = StreamEvent(
                    type="node_message",
                    content=f"\n📝 Batch analysis report: {_batch_pm_path_str}\n",
                    node="batch_postmortem",
                    task_id=ctx.turn_id,
                )
                sidewrite(_pm_evt)
                yield _pm_evt.to_sse()
            except Exception:
                logger.warning("Failed to write batch postmortem report", exc_info=True)

            # Write summary to Intent Graph
            try:
                _batch_summary_text = build_batch_summary_text(
                    _batch_results,
                    _batch_pm_path_str,
                )
                await write_operation_summary(
                    _batch_summary_text,
                    intent_graph=ctx.intent_graph,
                    thread_id=ctx.thread_id,
                    state_update=clear_dispatched_operation_payload_update(),
                    tui_session_id=ctx.sid,
                    recursion_limit=settings.recursion_limit,
                )
                ctx.operation_record_written = True
            except Exception:
                logger.warning("Failed to write batch summary to Intent Graph", exc_info=True)

        if _bp_timed_out:
            raise ConfirmTimeout("Batch confirmation timed out")
    finally:
        # abort-safe: normal-path backstop (runs uncancelled on the plain
        # exception unwinds); the abort path's remover is the gated clear
        # inside _abort_turn_cleanup (invariants I5) — idempotent pair.
        await _clear_dispatched_inject_intent_state(ctx, reason="batch pipeline finalization")


async def _run_recover(ctx, graph, config, batcher, sidewrite, converters):
    """Launch and stream the recover graph if intent was classified as recover."""
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store

    _recover_final = await graph.aget_state(config)
    _rv = _recover_final.values if _recover_final else {}
    _recover_inject_tid = _rv.get("recover_task_id", "")
    # Round-64 R64-1: the dry_run leg is the preview-safety backstop. The
    # intent branch now refuses to confirm recover under /plan (the branch
    # early-returns before bootstrap), so the confirmed-intent leg cannot
    # be true on a dry-run turn today — but the dispatch is the
    # side-effecting act itself (it runs the REAL recovery), so the
    # preview-safety guarantee lives HERE too, at the single source that
    # would execute it, not only at the branch that feeds it.
    if not (
        _rv.get("confirmed_intent") == "recover"
        and _recover_inject_tid
        and not _recover_final.next
        and not ctx.dry_run
    ):
        return

    recover_graph = ctx.agents.get("recover")
    if recover_graph is None:
        return

    # Round-61 R61-2: the intent clarification's recover branch ALREADY
    # bootstrapped the recover session (its own bootstrap_task_session
    # call); the abort-fallback in the finally (G5) keys on these
    # coordinates. They used to be recorded only AFTER the resolve's
    # awaits below — an abort landing during the resolve reached the
    # finally with an ACTIVE recover session, the P2 recover guard
    # yielded it to G5, and the G5 gate was still falsy: nobody closed
    # the session. Record before the resolve: the resolve's awaits were
    # the widest suspension span inside _run_recover. The span from the
    # branch's in-graph bootstrap to this point still suspends (the
    # graph's own remaining super-steps, the aget_state above) — an abort
    # landing there is closed by the finally's G5 coordinate self-rescue
    # (round-63 N2), which reads the same intent-state fields recorded
    # here.
    _rec_task_id = _rv.get("task_id", "") or new_recover_task_id()
    recover_config = {
        "configurable": {"thread_id": _rec_task_id},
        "recursion_limit": settings.recursion_limit,
    }
    ctx.recover_task_id = _rec_task_id
    ctx.recover_config = recover_config

    # Duration origin for the recover ResultCard: the recover graph's own
    # start. The dialogue that identified WHICH task to recover is
    # conversation time, not recovery time — same rationale as the inject
    # pipeline's dispatch-moment origin.
    _rec_started_monotonic = time.monotonic()

    # Resolve inject state as optional live context; TaskSnapshot remains the
    # primary recover source inside resolve_recover_initial_state().
    checkpoint_values = {}
    _inj_config = {
        "configurable": {"thread_id": _recover_inject_tid},
        "recursion_limit": settings.recursion_limit,
    }
    try:
        _inj_state = await ctx.agents["pipeline"].aget_state(_inj_config)
    except Exception:
        _inj_state = None
    from chaos_agent.agent.state import has_active_fault, has_live_fault
    # Fault lane gates on the LIVE predicate (round-27 R5): the graft is
    # "optional live context" by its own comment — a destroyed experiment's
    # corpse values must not seed the recover flow (the emergency-gate
    # family, round-25). The skill / spec lanes are NOT fault-lifecycle
    # (a skill-bearing task keeps its graft regardless of experiment
    # death), and the elif's committed predicate on the RECOVER REQUEST's
    # own values is identity semantics — the user's recover targeting
    # legitimately names a dead experiment, and the handle must survive
    # the death for the recover graph to resolve and report it.
    if _inj_state and _inj_state.values and (
        has_live_fault(_inj_state.values)
        or has_active_skill(_inj_state.values)
        or _inj_state.values.get("fault_spec")
    ):
        checkpoint_values = _inj_state.values
    elif has_active_fault(_rv) or has_active_skill(_rv):
        checkpoint_values = _rv

    resolution = await resolve_recover_initial_state(
        _recover_inject_tid,
        record_task_id=_rec_task_id,
        agents=ctx.agents,
        checkpoint_values=checkpoint_values,
        tui_session_id=ctx.sid,
    )
    recover_initial = resolution.initial_state if resolution is not None else None
    sv = resolution.source_values if resolution is not None else {}

    if recover_initial is None:
        logger.warning("Auto-recover: no inject state found for %s", _recover_inject_tid)
        _no_state_evt = StreamEvent(
            type="error",
            content=f"Could not find the injection state for experiment {_recover_inject_tid}; recovery was skipped.",
            task_id=ctx.turn_id,
        )
        sidewrite(_no_state_evt)
        yield _no_state_evt.to_sse()
        return

    # Bootstrap SessionStore so recover messages persist to memory/tasks/
    from chaos_agent.agent.nodes.planning.intent_clarification import bootstrap_task_session
    _rec_tui_sid = recover_initial.get("tui_session_id", "") or ctx.sid
    bootstrap_task_session(_rec_task_id, operation="recover", tui_session_id=_rec_tui_sid, handoff_message=None)

    async for sse in _drain_merged(
        _merged_stream(
            recover_graph.astream_events(recover_initial, recover_config, version="v2"),
            ctx.tracker_queue,
        ),
        ctx.turn_id, batcher, sidewrite, converters, ctx.req,
        source="recover",
    ):
        yield sse

    _rec_result = await build_recover_result_payload(
        recover_graph, recover_config,
        _rec_task_id, _recover_inject_tid,
        sv, _rec_started_monotonic,
    )
    if _rec_result is not None:
        ctx.store.add_task(ctx.sid, _rec_task_id)
        _tui_store = _get_tui_store()
        if _tui_store is not None:
            try:
                _tui_store.add_task(ctx.sid, _rec_task_id)
            except Exception:
                logger.warning("recover task_id disk persist failed sid=%s task=%s", ctx.sid, _rec_task_id)
        try:
            _recover_summary_text = build_recover_summary_text(
                _rec_result,
                _recover_inject_tid,
                sv,
            )
            # ONE combined record: append the recover ledger's process detail.
            from chaos_agent.agent.result.operation_summary import (
                append_ledger_process_detail as _append_ledger,
            )
            _recover_summary_text = _append_ledger(_recover_summary_text, sv)
            await write_operation_summary(
                _recover_summary_text,
                intent_graph=ctx.intent_graph,
                thread_id=ctx.thread_id,
                state_update={
                    "confirmed_intent": None,
                    "recover_task_id": None,
                    "pipeline_task_id": _rec_task_id,
                },
                tui_session_id=ctx.sid,
                recursion_limit=settings.recursion_limit,
            )
            ctx.operation_record_written = True
        except Exception:
            logger.warning("Failed to write recover summary to Intent Graph", exc_info=True)
        _rec_evt = StreamEvent(
            type="result",
            content=json.dumps(_rec_result, ensure_ascii=False),
            task_id=ctx.turn_id,
        )
        sidewrite(_rec_evt, source="recover")
        yield _rec_evt.to_sse()

    # Finalize recover session — flush remaining messages + mark complete/failed
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        _rec_store = get_global_session_store()
        if _rec_store and _rec_store.has_active(_rec_task_id):
            await finalize_recover_session(
                _rec_store,
                recover_graph,
                recover_config,
                _rec_task_id,
                _recover_inject_tid,
                sv,
                result_payload=_rec_result if isinstance(_rec_result, dict) else None,
                result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
            )
            # The finally block's abort fallback (round-54 G5) keys on
            # this flag: a recovery killed mid-run leaves BOTH its
            # session row active and its TaskStore row a zombie —
            # this is the normal-path marker that suppresses the
            # fallback for recoveries that closed themselves.
            ctx.recover_finalized = True
    except Exception:
        logger.warning("Failed to finalize recover session %s", _rec_task_id, exc_info=True)


# ---------------------------------------------------------------------------
# Checkpoint rollback on cancellation
# ---------------------------------------------------------------------------

async def _write_interrupted_record(ctx, *, cause: str, error_detail: str = "") -> None:
    """Mirror an interruption record to the intent graph.

    The intent graph is context-isolated from execution: without this it cannot
    tell the next dialogue turn that anything happened at all, and after an
    interruption mid-execution it would not know a fault may still be live. The
    progress ledger read here is whatever the executor had recorded when it
    stopped, so this works from ANY interruption point.

    Best-effort and idempotent per turn: it runs while an abort unwinds and must
    never mask the original exception, and it is skipped once this turn already
    wrote a record (so a completed operation is never contradicted).
    """
    if ctx.operation_record_written:
        return
    try:
        from chaos_agent.agent.result.operation_summary import build_interrupted_record

        if ctx.recover_task_id and ctx.recover_config:
            # Recovery is its own graph/thread and takes precedence: if a recovery
            # was in flight, THAT is the operation the user interrupted.
            _rec_graph = ctx.agents.get("recover") if isinstance(ctx.agents, dict) else None
            snapshot = await _rec_graph.aget_state(ctx.recover_config) if _rec_graph else None
            record_task_id = ctx.recover_task_id
        elif ctx.pipeline_task_id and ctx.pipeline_config:
            snapshot = await ctx.pipeline_graph.aget_state(ctx.pipeline_config)
            record_task_id = ctx.pipeline_task_id
        else:
            # Interrupted before any pipeline was dispatched (still clarifying):
            # the intent thread itself is the only place with context.
            snapshot = await ctx.intent_graph.aget_state(ctx.graph_config)
            record_task_id = ctx.turn_id
        values = snapshot.values if snapshot and snapshot.values else {}

        await write_operation_summary(
            build_interrupted_record(
                values, record_task_id, cause=cause, error_detail=error_detail,
            ),
            intent_graph=ctx.intent_graph,
            thread_id=ctx.thread_id,
            tui_session_id=ctx.sid,
            recursion_limit=settings.recursion_limit,
            raise_graph_error=False,
        )
        ctx.operation_record_written = True
    except asyncio.CancelledError:
        raise
    except Exception:
        # Warning, not debug (r51 — same family as the round-50 capture
        # fix): this record is the thread's ONLY memory that a fault may
        # still be live; at debug level its loss was invisible in
        # production.
        logger.warning(
            "Failed to write interruption record for turn %s (cause=%s)",
            ctx.turn_id, cause, exc_info=True,
        )


async def _rollback_intent_checkpoint(
    intent_graph,
    thread_id: str,
    pre_turn_checkpoint_id: str | None,
) -> None:
    """Roll back intent graph checkpoint after a cancelled turn.

    Creates a new checkpoint forked from the pre-turn state, which becomes
    the latest checkpoint for the thread. This prevents incomplete/empty
    AIMessages from polluting subsequent turns.

    If no pre-turn checkpoint exists (brand-new thread), removes all messages
    from the dirty checkpoint to restore a clean slate.
    """
    try:
        if pre_turn_checkpoint_id:
            # Fork from pre-turn checkpoint — the new checkpoint (with a newer
            # UUID6 id) becomes "latest", effectively discarding dirty state.
            rollback_config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": pre_turn_checkpoint_id,
                    # The explicit ns is load-bearing: a checkpoint tuple
                    # fetched BY id carries no checkpoint_ns (a by-thread
                    # fetch does), and aput_writes indexes the key
                    # unconditionally — without this entry the fork raises
                    # KeyError and the fail-soft handler below silently
                    # degrades the rollback into a no-op (round-47).
                    "checkpoint_ns": "",
                },
                "recursion_limit": settings.recursion_limit,
            }
            await intent_graph.aupdate_state(
                rollback_config, {"messages": []}, as_node="save_dialogue",
            )
            logger.info(
                "Rolled back intent checkpoint to pre-turn state "
                "(thread=%s, checkpoint=%s)",
                thread_id, pre_turn_checkpoint_id,
            )
        else:
            # Brand-new thread with no prior checkpoint — remove all messages
            # from the dirty state to prevent incomplete AIMessage leakage.
            from langchain_core.messages import RemoveMessage

            config = {
                "configurable": {"thread_id": thread_id},
                "recursion_limit": settings.recursion_limit,
            }
            dirty_state = await intent_graph.aget_state(config)
            if dirty_state and dirty_state.values:
                msgs = dirty_state.values.get("messages") or []
                removals = [
                    RemoveMessage(id=m.id)
                    for m in msgs
                    if getattr(m, "id", None)
                ]
                if removals:
                    await intent_graph.aupdate_state(
                        config, {"messages": removals}, as_node="save_dialogue",
                    )
                    logger.info(
                        "Cleared %d dirty messages from new thread (thread=%s)",
                        len(removals), thread_id,
                    )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Failed to rollback intent checkpoint (thread=%s)",
            thread_id, exc_info=True,
        )


async def _rollback_intent_checkpoint_for_turn(
    ctx: TurnContext,
    pre_turn_checkpoint_id: str | None,
) -> None:
    """Roll the intent thread back for a turn exit, unless it already
    recorded its operation.

    The operation record is the dialogue's only "a fault may still be
    live" memory, and the rollback discards the turn's intent-thread
    writes WHOLESALE — record included — while `_write_interrupted_record`
    skips on the very same flag, so nothing would re-write it: rolling a
    record-bearing thread back would amputate live-fault awareness. A turn
    that wrote its record is a legitimate completed turn (the pipeline
    finished and the intent graph finished before dispatch), so the dirty
    half-message hazard the rollback exists for is not in play.
    """
    if ctx.operation_record_written:
        return
    await _rollback_intent_checkpoint(
        ctx.intent_graph, ctx.thread_id, pre_turn_checkpoint_id,
    )


# Causes that roll the intent thread back to its pre-turn checkpoint. The
# crash-like exits fork the dirty half-dialogue away; ``confirm_timeout`` is
# a DESIGNED pause (the confirmation gate holds a Command(resume=) path), so
# its thread must not be forked.
_ABORT_ROLLBACK_CAUSES = frozenset({"user_cancel", "disconnected", "internal_error"})

# The terminal-WORD taxonomy (interrupt causes → "cancelled") lives in
# stream_abort.py since round-56 — ABORT_INTERRUPT_CAUSES / abort_row_word,
# beside the shared row writer this module already routes through. It was
# declared here module-privately in round-55, which left the sibling
# streams free to grow private mappings (and they did: recover_stream's
# inline conditional carried the OPPOSITE unknown-cause polarity — unknown
# → "cancelled", fail-open).


async def _abort_turn_cleanup(
    ctx: TurnContext,
    *,
    cause: str,
    pre_turn_checkpoint_id: str | None = None,
    capture_failed: bool = False,
    error_detail: str = "",
) -> None:
    """Single-source cleanup for EVERY turn-abort exit (round-50).

    Starlette serves the turn generator inside an anyio task group: a client
    disconnect cancels the task-group SCOPE — level-based cancellation that
    re-delivers CancelledError at every await suspension until the scope
    exits — and an exception raised inside one except-handler escapes the
    whole try block (sibling handlers cannot catch it). Every abort exit
    therefore routes its cleanup through THIS one shielded function instead
    of hand-writing await chains per exit; an AST legislation test pins the
    invariant (no bare awaits in the abort handlers) so the pattern cannot
    silently regress exit-by-exit the way round-48's four-site sweep missed
    the fifth.

    Decision table (single source of truth — differences between exits are
    visible HERE, not scattered across handlers):
      shield          — always: the whole body. The only form that carries an
                        await chain through level-based cancellation (r48).
      vehicle cleanup — ``user_cancel`` only: the TUI-interrupt window where
                        the graph's own cleanup nodes (verifier_finalize /
                        planning_cleanup / execute_loop) never ran. Other
                        exits' leak is open question C1 (round-50). Round-53
                        audited C1 against the cross-module matrix: this
                        stream's internal_error does NOT dispatch the blade
                        rollback inject_stream's error exit does — a
                        deliberate stance split, not an oversight: the turn
                        is INTERACTIVE (the interrupted record already
                        tells the intent thread "fault may still be live"
                        and the user's next message is guided to the
                        recover graph), while inject_stream is UNATTENDED
                        (no next turn — the rollback must fire in-process).
                        C1 remains open only for its own leak surface
                        (helper artifacts on non-user_cancel exits).
      ceiling (r49→54) — ``user_cancel``: the vehicle-class 90s bound (the
                        one cause whose chain carries the kubectl-delete hang
                        surface); every other cause: the shared SQLite-class
                        bound — the old ``fail_after(None)`` no-op was
                        checkpointer-shaped (AsyncSqliteSaver, verified r53)
                        and a future remote checkpointer re-opens the r49
                        hang surface. A hit is bounded abandon.
      rollback        — crash-like causes, and ONLY when the pre-turn capture
                        succeeded (``capture_failed`` skips it: a None handed
                        to the rollback would hit the brand-new-thread sweep
                        branch and wipe the thread's WHOLE history — r50
                        proved that on a real checkpointer — so on an unknown
                        rollback target the only safe action is no action;
                        the dirty partial turn stays as PERMANENT context
                        noise, because add_messages is append-only and
                        nothing removes it — r51 proved the old "self-heals
                        on the next one" claim wrong — and one noisy turn
                        beats a wiped history). The wrapper itself skips
                        record-bearing turns.
      dispatched clear
                     — every cause, once this turn handed off a pipeline
                        (``pipeline_task_id`` set, non-dry-run): the
                        residual-window remover. The main path's clear is the
                        dispatch-time call, awaited BEFORE streaming starts
                        (round-52 re-audit corrected round-51's original
                        "mid-stream abort leaves the fields" headline — a
                        mid-stream abort finds them already gone); what this
                        shielded arm covers is what that call cannot
                        guarantee: a cancellation landing ON its own await,
                        its own fail-soft failure (the SQLite
                        lock-contention family), and the pipeline
                        subgenerators' finally-clear, whose aupdate_state
                        dies inline at its first suspension under scope
                        cancel (r51 two-path experiment) — whatever leaks
                        re-opens an old intent_confirm card next turn.
                        Ordered after the rollback: a successful fork
                        already discards the dispatched fields, so this arm
                        matters exactly when the rollback is skipped or
                        fails. Idempotent all-fields-None update, so the
                        normal-path double-clear is harmless.
      record          — every cause: the intent thread must learn that the
                        turn ended abnormally (a fault may still be live).
                        Ordered AFTER the rollback, which discards this
                        turn's intent messages — a record written before it
                        would be discarded together with them.
      operation row   — every cause, once a pipeline was dispatched
                        (round-54 G4): the TaskStore row's terminal word via
                        the shared guarded helper — abort_row_word (the
                        shared taxonomy in stream_abort since round-56):
                        "failed" on internal_error, "cancelled" on the
                        interrupt causes (user_cancel / disconnected /
                        confirm_timeout — round-55 F1: the inline tuple
                        dropped the last one onto "failed" while this module
                        called it a designed pause three lines away). The
                        r53 triad was wired for the cancel exits only, and a
                        crashed dispatch left its row a zombie at the last
                        mid-graph upsert. The helper's guard keeps a row
                        that already reached its OWN terminal word.
      cause memo      — the cause is recorded on ctx (abort_cause) so the
                        FINALLY block's recover fallback — which runs outside
                        this function and receives no arguments — classifies
                        its terminal word with the same taxonomy instead of
                        a private one (round-55 F2).
    """
    # Before anything else: the finally block's fallback arms key on this
    # (see the cause-memo row of the decision table above).
    ctx.abort_cause = cause
    with anyio.CancelScope(shield=True):
        try:
            # Ceiling since round-54 (G7/F5): BOTH arms are bounded now.
            # user_cancel keeps the vehicle-class 90s ceiling (r49); the
            # other causes' chain is SQLite-class (checkpointer
            # aget_state/aupdate_state, TaskStore upserts) and carries the
            # shared SQLite-class bound — the old ``fail_after(None)``
            # no-op was a checkpointer-shaped ruling (AsyncSqliteSaver,
            # verified r53) that a future remote checkpointer re-opens
            # into the r49 hang surface. A hit is bounded abandon.
            with anyio.fail_after(
                _CANCEL_CLEANUP_CEILING_S
                if cause == "user_cancel"
                else ABORT_SQLITE_CEILING_S
            ):
                if cause == "user_cancel":
                    await _cleanup_cancelled_execution_artifacts(ctx)
                if cause in _ABORT_ROLLBACK_CAUSES and not capture_failed:
                    await _rollback_intent_checkpoint_for_turn(
                        ctx, pre_turn_checkpoint_id,
                    )
                if ctx.pipeline_task_id and not ctx.dry_run:
                    # The residual-window remover (decision table above):
                    # the dispatch-time clear covers the main path, but a
                    # cancel landing on its own await, its fail-soft failure,
                    # or the pipeline finallys' inline death under scope
                    # cancel (r51 two-path experiment) all leak past it.
                    await _clear_dispatched_inject_intent_state(
                        ctx, reason="turn abort cleanup",
                    )
                await _write_interrupted_record(
                    ctx, cause=cause, error_detail=error_detail,
                )
                # Round-54 G4: the operation row's terminal write — the
                # r53 triad was wired for the cancel exits only, so a
                # crashed dispatch left its row a zombie at the last
                # mid-graph upsert ("injecting" forever in /tasks; no
                # later writer comes on the abort path). The shared
                # guarded helper keeps a row that already reached its OWN
                # terminal word (G6). Recover dispatches need no row write
                # here: the finally block's G5 fallback covers them (this
                # arm would target the intent thread's task_id, not the
                # recover row's).
                if ctx.pipeline_task_id and not ctx.dry_run:
                    await write_aborted_task_row(
                        ctx.pipeline_task_id, abort_row_word(cause),
                    )
        except TimeoutError:
            # fail_after's own scope is not shielded, so it fires inside the
            # outer shield (round-49 probe, D1). Bounded abandon: the skipped
            # steps are logged, not retried — after a pathological 90s hang
            # a retry could hang just as long.
            logger.warning(
                f"Turn {ctx.turn_id} cancelled-cleanup exceeded "
                f"{_CANCEL_CLEANUP_CEILING_S}s ceiling; remaining cleanup "
                "steps (rollback/interrupted-record) skipped"
            )


async def _cleanup_cancelled_execution_artifacts(ctx: TurnContext) -> None:
    """Clean helper resources from a pipeline interrupted by the TS TUI.

    Cancellation can occur between ``kubectl debug`` and verifier finalization,
    so the graph's normal cleanup node may never run. Only registered helper
    artifacts are removed here; real fault recovery remains the recover graph's
    responsibility.
    """
    graph = ctx.result_graph or ctx.pipeline_graph
    config = ctx.result_config or ctx.graph_config
    try:
        snapshot = await graph.aget_state(config)
        values = snapshot.values if snapshot and snapshot.values else {}
        from chaos_agent.agent.execution_artifacts import (
            cleanup_debug_pod_artifacts,
            collect_execution_artifacts,
        )
        from chaos_agent.agent.spec.fault_spec import read_fault_spec

        spec = read_fault_spec(values)
        artifacts = collect_execution_artifacts(
            list(values.get("messages") or []),
            list(values.get("execution_artifacts") or []),
            task_id=str(values.get("task_id") or ctx.turn_id),
            operation_family=(spec.fault_target if spec else ""),
        )
        if not artifacts:
            return
        from chaos_agent.agent.nodes.store._store_sync import sync_to_store

        cleaned, names = await cleanup_debug_pod_artifacts(
            artifacts,
            kubeconfig=str(values.get("kubeconfig") or ""),
            task_id=str(values.get("task_id") or ctx.turn_id),
        )
        if cleaned != artifacts:
            await sync_to_store(values, {"execution_artifacts": cleaned})
        if names:
            logger.info(
                "Cancelled turn %s cleaned debug artifacts: %s",
                ctx.turn_id, names,
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "Failed to clean execution artifacts for cancelled turn %s",
            ctx.turn_id,
            exc_info=True,
        )


# ---------------------------------------------------------------------------
# Fault-window hold (evaluation protocol opt-in)
# ---------------------------------------------------------------------------

async def _emit_result_card(ctx, result_payload: dict, sidewrite):
    """Emit the inject ResultCard plus its task bookkeeping.

    Step 3 of event_generator and the fault-window hold path need exactly
    this sequence — bookkeeping first, then the SSE frame — so the hold
    path's early emission (protocol: inject evidence lands before the
    window starts) cannot drift from the late emission it replaces.
    """
    op_task_id = ""
    data_obj = result_payload.get("data")
    if isinstance(data_obj, dict):
        candidate = data_obj.get("task_id", "")
        if isinstance(candidate, str) and is_real_task_id(candidate):
            op_task_id = candidate
    if op_task_id:
        ctx.store.add_task(ctx.sid, op_task_id)
        from chaos_agent.memory.tui_session_store import get_global_tui_session_store
        tui_store = get_global_tui_session_store()
        if tui_store is not None:
            try:
                tui_store.add_task(ctx.sid, op_task_id)
            except Exception as e:
                logger.warning(f"task_id disk persist failed sid={ctx.sid} task={op_task_id}: {e}")
    _result_evt = StreamEvent(
        type="result",
        content=json.dumps(result_payload, ensure_ascii=False),
        task_id=ctx.turn_id,
    )
    sidewrite(_result_evt)
    yield _result_evt.to_sse()


async def _hold_fault_window(ctx, graph, config, sidewrite):
    """Hold the turn open through the injection contract window.

    The evaluation protocol requires the fault to stay present for the
    WHOLE approved window and the recovery to be reported from THIS
    turn's event stream — a turn that ends at verify leaves the server
    with no execution body to recover with. When the gate above us is on
    and the pipeline stamped a live window, this generator:

    1. emits ``fault_window`` enter/tick/exit events (the tick doubles as
       the SSE liveness channel and the client's clock-drift correction);
    2. waits out the REMAINING window — measured from
       ``injection_window_start_time`` (stamped at the VERIFIER entry,
       i.e. the execute-loop end), never re-armed from verify
       completion or from the blade_create moment: verification time
       legitimately counts against the window, while execute-loop work
       after the create (UID reconcile, follow-up probes) must not erode
       it. A window already SPENT at verify end (verify outlasted the
       duration) skips the hold entirely but still dispatches recovery
       — the protocol wants the agent's recover report either way;
    3. wakes early on ``ctx.hold_early_recover`` (the /early-recover
       endpoint, Ctrl+R in the TUI) — the same dual-wait shape as
       ``wait_for_confirmation``;
    4. on either exit, flips the intent graph into a recover intent so
       the step-2.6 ``_run_recover`` drains the recovery — destroy,
       verify, result card, session finalize — on the SAME stream.

    Abort safety: a client disconnect during the hold cancels the wait,
    which lands on the existing user_cancel path verbatim — the fault's
    own recovery stays with the blade ``--timeout`` / carrier timers, no
    new leak surface. The exit event for that path is sidewrite-only
    (jsonl evidence); the SSE side is terminated by the outer error+done
    handlers, and yielding from a finally mid-cancellation is off the
    table.
    """
    try:
        _final = await graph.aget_state(config)
    except Exception:
        logger.warning(
            "fault-window hold: failed to read pipeline state for %s",
            ctx.turn_id, exc_info=True,
        )
        return
    _v = _final.values if _final else {}
    # Window origin: the verifier-entry stamp (execute-loop end). Falls
    # back to nothing — an un-stamped state (old pipeline / never-injected
    # turn) simply has no window to hold, identical to the switch off.
    _start_iso = str(_v.get("injection_window_start_time") or "")
    _spec = _v.get("fault_spec")
    _duration = int((_spec.get("duration_seconds") if isinstance(_spec, dict) else 0) or 0)
    _inject_tid = str(_v.get("task_id") or ctx.pipeline_task_id or "")
    if not _start_iso or _duration <= 0 or not is_real_task_id(_inject_tid):
        # No stamped live window (failed inject / chat turn / a start time
        # that never landed): nothing to hold and no recover to dispatch —
        # the turn ends exactly as it would with the switch off.
        return
    try:
        _start_dt = parse_iso_timestamp(_start_iso)
    except (ValueError, TypeError):
        logger.warning(
            "fault-window hold: unparseable injection_window_start_time %r", _start_iso,
        )
        return
    _until_dt = _start_dt + timedelta(seconds=_duration)
    _remaining = (_until_dt - datetime.now(BEIJING_TZ)).total_seconds()

    def _fw_evt(payload: dict) -> StreamEvent:
        return StreamEvent(
            type="fault_window",
            content=json.dumps(payload, ensure_ascii=False),
            task_id=ctx.turn_id,
        )

    _reason = ""
    if _remaining > 0:
        _deadline = time.monotonic() + _remaining
        _next_tick = time.monotonic() + _HOLD_TICK_INTERVAL_S
        _ACTIVE_HOLDS[ctx.turn_id] = ctx
        try:
            _enter = _fw_evt({
                "phase": "enter",
                "inject_task_id": _inject_tid,
                "duration_sec": _duration,
                "remaining_sec": round(_remaining, 1),
                "until_ts": _until_dt.isoformat(),
            })
            sidewrite(_enter, source="hold")
            yield _enter.to_sse()

            while True:
                _now = time.monotonic()
                _wait = min(_deadline - _now, _next_tick - _now)
                if _wait > 0:
                    try:
                        # No shield, unlike wait_for_confirmation's future: an
                        # Event.wait() is cancellation-clean, and shielding
                        # would orphan the inner waiter on every tick timeout.
                        await asyncio.wait_for(
                            ctx.hold_early_recover.wait(), timeout=_wait,
                        )
                        _reason = "early"
                        break
                    except asyncio.TimeoutError:
                        pass
                if time.monotonic() >= _deadline:
                    _reason = "elapsed"
                    break
                _tick = _fw_evt({
                    "phase": "tick",
                    "remaining_sec": round(max(0.0, _deadline - time.monotonic()), 1),
                })
                sidewrite(_tick, source="hold")
                yield _tick.to_sse()
                _next_tick = time.monotonic() + _HOLD_TICK_INTERVAL_S

            _exit = _fw_evt({
                "phase": "exit",
                "reason": _reason,
                "remaining_sec": round(max(0.0, _deadline - time.monotonic()), 1),
            })
            sidewrite(_exit, source="hold")
            yield _exit.to_sse()
        finally:
            _ACTIVE_HOLDS.pop(ctx.turn_id, None)
            if not _reason:
                # Abnormal teardown (client cancel / internal error before the
                # window's exit event fired): jsonl evidence only. Never yield
                # from a finally that may be unwinding a cancellation — the
                # outer handlers already terminate the stream with error+done.
                sidewrite(_fw_evt({
                    "phase": "exit",
                    "reason": "aborted",
                    "remaining_sec": round(max(0.0, _deadline - time.monotonic()), 1),
                }), source="hold")
    else:
        # Window already spent at verify end: the fault is at or past its
        # self-recovery deadline — no SSE hold (nothing left to count down)
        # and no registry entry (nothing for /early-recover to wake), but
        # the protocol still requires the AGENT's recovery report, so the
        # flip below runs unconditionally.
        logger.info(
            "fault-window hold: window already spent for %s (verify outlasted "
            "duration=%ds) — dispatching recover without holding",
            ctx.turn_id, _duration,
        )

    # Flip the intent graph into a recover intent so step 2.6's
    # _run_recover dispatches on this same stream: the field shape the
    # intent clarification's recover branch leaves behind (task_id =
    # fresh recover id, recover_task_id = the inject thread to resolve
    # from), so the resolve → drain → result → finalize lifecycle and
    # the G5 abort fallback behave exactly as on a dedicated recover
    # turn — including the summary write that clears these fields
    # again at the end of _run_recover.
    #
    # One deliberate asymmetry vs that branch: this flip is a PURE state
    # write — no recover session bootstrap (that happens inside
    # _run_recover, after it records ctx.recover_task_id). That is what
    # keeps the finally block's G5 self-rescue arm correctly silent on
    # this path: an abort before the recording has no session to close
    # (the self-rescue reads the pipeline graph here and finds "inject"),
    # an abort after it is carried by the primary arm. A refactor that
    # adds a bootstrap to this flip must re-visit the G5 self-rescue —
    # as wired it would miss the session.
    _rec_tid = new_recover_task_id()
    await ctx.intent_graph.aupdate_state(
        {
            "configurable": {"thread_id": ctx.thread_id},
            "recursion_limit": settings.recursion_limit,
        },
        {
            "confirmed_intent": "recover",
            "recover_task_id": _inject_tid,
            "task_id": _rec_tid,
        },
        as_node="save_dialogue",
    )
    # _run_recover reads ctx.result_graph or ctx.intent_graph; after an
    # inject dispatch that override points at the PIPELINE thread,
    # whose confirmed_intent is still "inject". Drop it so the recover
    # intent just written is the one the dispatch gate sees. The
    # finally block is unaffected: it prefers the pipeline coordinates
    # recorded at dispatch time.
    ctx.result_graph = None
    ctx.result_config = None


# ---------------------------------------------------------------------------
# Main event generator
# ---------------------------------------------------------------------------

async def event_generator(ctx: TurnContext):
    """Main SSE event generator for a /turn request."""
    from chaos_agent.observability.otel_genai import get_task_span_manager
    from chaos_agent.observability import status_tracker as _st_mod
    from chaos_agent.observability.status_tracker import unsubscribe as _status_unsubscribe
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store

    _tsm = get_task_span_manager()
    _otel_cb = getattr(_st_mod, "_otel_callback", None)
    stream_task = asyncio.current_task()
    ctx.task_tracker.register(ctx.turn_id, stream_task)
    turn_started_monotonic = time.monotonic()
    batcher = SSEBatcher(
        flush_interval_ms=settings.sse_batch_interval_ms,
        flush_chars=settings.sse_batch_chars,
    )
    sidewrite = _make_sidewrite(ctx.sid)
    converters = _make_converters(ctx.turn_id)

    # Track which graph/config to use for final result extraction
    ctx.result_graph = ctx.intent_graph
    ctx.result_config = ctx.graph_config

    # Capture pre-turn checkpoint for rollback on cancellation.
    # If the turn is cancelled mid-stream, dirty (incomplete) AIMessages may
    # have been checkpointed. We fork from the pre-turn checkpoint to restore
    # a clean state for the next turn.
    _pre_turn_checkpoint_id: str | None = None
    # Tri-state, not a None-sentinel: on an OLD thread a capture failure used
    # to hand the rollback a None that its sweep branch reads as "brand-new
    # thread" — wiping the thread's whole legitimate history (round-50,
    # real-checkpointer proof). The marker routes the abort cleanup to skip
    # the rollback instead.
    _pre_turn_capture_failed = False
    _turn_cancelled = False
    try:
        _pre_turn_snap = await ctx.intent_graph.aget_state(ctx.graph_config)
        if _pre_turn_snap and _pre_turn_snap.created_at:
            _pre_turn_checkpoint_id = (
                _pre_turn_snap.config.get("configurable", {}).get("checkpoint_id")
            )
    except Exception:
        _pre_turn_capture_failed = True
        # Warning, not debug: this single event changes abort-cleanup
        # behavior for the turn, and at debug it was invisible in
        # production — the history-wipe it armed looked like a silent,
        # no-symptom data loss (round-50).
        logger.warning(
            "Failed to capture pre-turn checkpoint (thread=%s); abort "
            "rollback will be skipped and the turn's partial messages "
            "will remain in the thread as permanent context noise",
            ctx.thread_id, exc_info=True,
        )

    try:
        _tsm.start_task_span(ctx.turn_id)
        if _otel_cb is not None:
            _otel_cb.set_task_id(ctx.turn_id)
        try:
            _ts = _get_tui_store()
            if _ts is not None and ctx.sid:
                _ts.append_event(ctx.sid, {
                    "ts": now_iso(), "source": "user",
                    "task_id": ctx.turn_id, "event_type": "user_input",
                    "data": {"content": ctx.input_text},
                })
        except Exception:
            pass

        # 1. Stream intent graph
        async for sse in _drain_merged(
            _merged_stream(ctx.intent_graph.astream_events(ctx.initial_state, ctx.graph_config, version="v2"), ctx.tracker_queue),
            ctx.turn_id, batcher, sidewrite, converters, ctx.req,
        ):
            if sse is True:
                return
            yield sse

        # 2. Handle intent graph interrupts
        async for sse in _drain_interrupts(ctx.intent_graph, ctx.graph_config, ctx, batcher, sidewrite, converters):
            yield sse

        # 2.5 Check confirmed intent → launch pipeline
        _intent_final = await ctx.intent_graph.aget_state(ctx.graph_config)
        _iv = _intent_final.values if _intent_final else {}
        _dispatch_operation = detect_dispatchable_operation(
            _iv,
            has_pending_interrupt=bool(_intent_final and _intent_final.next),
        )

        if _dispatch_operation == "batch_inject":
            async for sse in _run_batch_pipeline(ctx, _iv, batcher, sidewrite, converters):
                yield sse
        elif _dispatch_operation == "inject":
            async for sse in _run_inject_pipeline(ctx, _iv, batcher, sidewrite, converters):
                yield sse

        # 2.55 Fault-window hold (evaluation protocol opt-in): emit the
        # inject ResultCard ahead of the window, hold the turn open until
        # the contract window expires (or Ctrl+R), then flip into a
        # recover intent that 2.6 below dispatches on this same stream.
        # Default off: with the switch unset the stream is byte-identical
        # to the pre-hold behaviour. Batch dispatches are excluded — a
        # batch carries one window per fault, there is no single contract
        # window to hold.
        if (
            _dispatch_operation == "inject"
            and settings.turn_hold_fault_window
            and not ctx.dry_run
        ):
            _result_graph = ctx.result_graph or ctx.intent_graph
            _result_config = ctx.result_config or ctx.graph_config
            _duration_origin = ctx.pipeline_started_monotonic or turn_started_monotonic
            _hold_payload = await build_result_payload(
                _result_graph, _result_config, ctx.turn_id, _duration_origin,
            )
            if _hold_payload is not None:
                async for sse in _emit_result_card(ctx, _hold_payload, sidewrite):
                    yield sse
                ctx.inject_result_emitted = True
            async for sse in _hold_fault_window(
                ctx, _result_graph, _result_config, sidewrite,
            ):
                yield sse
        # 2.6 Auto-recover
        _result_graph = ctx.result_graph or ctx.intent_graph
        _result_config = ctx.result_config or ctx.graph_config
        async for sse in _run_recover(ctx, _result_graph, _result_config, batcher, sidewrite, converters):
            yield sse

        # 3. Result
        _result_graph = ctx.result_graph or ctx.intent_graph
        _result_config = ctx.result_config or ctx.graph_config
        # Duration origin: pipeline dispatch, not the turn start. The intent
        # clarification (potentially many dialogue rounds) precedes dispatch
        # and must not inflate the reported operation duration. Fallback to
        # the turn start is defensive only — an inject-confirmed final state
        # implies a pipeline ran and stamped the origin.
        _duration_origin = ctx.pipeline_started_monotonic or turn_started_monotonic
        # inject_result_emitted: the hold path already emitted the card
        # ahead of the window — re-emitting would double-fire on a skipped
        # hold (pipeline state still "inject") and the flip leaves nothing
        # to emit on a held one.
        result_payload = None
        if not ctx.dry_run and not ctx.inject_result_emitted:
            result_payload = await build_result_payload(
                _result_graph, _result_config, ctx.turn_id, _duration_origin,
            )
        if result_payload is not None:
            async for sse in _emit_result_card(ctx, result_payload, sidewrite):
                yield sse

        # 4. Done
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()

    except ClientDisconnected:
        logger.info(f"Client disconnected during turn {ctx.turn_id}")
        # Round-54 G3: the poll-detected disconnect sets the SAME flag the
        # scope-cancel exit sets — the finally's terminal triad (TaskStore
        # row + finalize override) keys on it, and before this the
        # poll-loser path finalized by INFERENCE while the scope-cancel
        # path wrote "cancelled" (the r53 triad existed one exit over, in
        # the fixed module itself). Which semantics a disconnect gets must
        # not be decided by the poll-vs-cancel race.
        _turn_cancelled = True
        # Single-source abort cleanup (round-50): its shield carries the
        # rollback + record through the anyio level-based scope cancellation
        # (round-48 live probe, L1/L2).
        await _abort_turn_cleanup(
            ctx,
            cause="disconnected",
            pre_turn_checkpoint_id=_pre_turn_checkpoint_id,
            capture_failed=_pre_turn_capture_failed,
        )
        return
    except ConfirmTimeout as cte:
        # Written BEFORE the terminating events: once the consumer sees ``done``
        # it stops iterating, the generator is closed, and anything after the
        # final ``yield`` would never run. Routed through the single-source
        # helper since round-50: this handler's record write was the fifth
        # shield site the round-48 sweep missed — a CE landing mid-write
        # would escape the whole try block (sibling handlers cannot catch it)
        # and bypass the cancel exit's shielded chain wholesale. No rollback
        # for this cause: a designed pause with a Command(resume=) path.
        await _abort_turn_cleanup(ctx, cause="confirm_timeout")
        _timeout_evt = StreamEvent(type="error", content=str(cte), task_id=ctx.turn_id)
        sidewrite(_timeout_evt)
        yield _timeout_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
    except asyncio.CancelledError:
        _turn_cancelled = True
        logger.info(f"Turn {ctx.turn_id} cancelled by client")
        # Starlette runs this generator inside an anyio task group: a client
        # disconnect cancels the task-group SCOPE (level-based — every await
        # suspension is re-cancelled until the scope exits), so a bare await
        # in this handler dies mid-flight and an asyncio.shield outer await
        # dies too (only its background job survives). Single-source abort
        # cleanup since round-50 — the shielded decision table (vehicle
        # cleanup + r49 ceiling + rollback + record) lives in ONE place,
        # pinned by an AST legislation test (round-48 live probe, L3).
        await _abort_turn_cleanup(
            ctx,
            cause="user_cancel",
            pre_turn_checkpoint_id=_pre_turn_checkpoint_id,
            capture_failed=_pre_turn_capture_failed,
        )
        _cancel_evt = StreamEvent(type="error", content="Turn cancelled", task_id=ctx.turn_id)
        sidewrite(_cancel_evt)
        yield _cancel_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
        raise
    except Exception as e:
        logger.exception(f"Turn failed for {ctx.turn_id}")
        # Single-source abort cleanup since round-50: rollback (unless the
        # pre-turn capture failed) then the interrupted record — the record
        # is written INTO the intent thread's state, so written first it
        # would be discarded together with the dirt by the fork — and both
        # shielded against an anyio scope cancellation arriving while this
        # handler runs (client dropped mid-error: round-48). The operation
        # row's terminal word (G4) rides the decision table's own arm.
        await _abort_turn_cleanup(
            ctx,
            cause="internal_error",
            pre_turn_checkpoint_id=_pre_turn_checkpoint_id,
            capture_failed=_pre_turn_capture_failed,
            error_detail=f"{type(e).__name__}: {e}",
        )
        _exc_evt = StreamEvent(type="error", content=f"{type(e).__name__}: {e}", task_id=ctx.turn_id)
        sidewrite(_exc_evt)
        yield _exc_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
    finally:
        # Per-session turn slot: released FIRST, before any of the
        # shielded terminal work below (any of which can throw or hit
        # its ceiling and skip the rest) — a leaked slot would 409 the
        # session's every subsequent turn until the stale reclaim.
        # Pure sync dict work; cancellation cannot interrupt it.
        _release_turn_slot(ctx.sid, ctx.turn_id)
        _tsm.end_task_span(ctx.turn_id)
        ctx.store.cancel_interrupt(ctx.turn_id)
        ctx.task_tracker.unregister(ctx.turn_id)
        try:
            _status_unsubscribe(ctx.tracker_key, ctx.tracker_queue)
        except Exception:
            pass

        # Round-60 F2'''/F3''': ``result_graph`` points at the pipeline
        # only on the SUCCESS path (assigned after both drains complete,
        # the dry-run early return precedes it), so every mid-pipeline exit
        # — the abort handlers AND the dry-run preview — inherited the
        # INTENT graph here and finalized the session from intent values:
        # no verification, fault_spec already cleared at dispatch — the
        # persisted summary lost the durable recover context and the
        # inferred word said "failed" for a successful /plan preview.
        # When this turn dispatched a pipeline, that pipeline's OWN thread
        # holds the run's real final state — read that instead.
        if ctx.pipeline_task_id and ctx.pipeline_config:
            _result_graph = ctx.pipeline_graph
            _result_config = ctx.pipeline_config
        else:
            _result_graph = ctx.result_graph or ctx.intent_graph
            _result_config = ctx.result_config or ctx.graph_config
        # Shielded for the same reason as the exit handlers: under a real
        # client disconnect the task-group scope cancellation is level-based
        # and a bare await here dies at its first suspension — skipping the
        # terminal TaskStore write (the "cancelled" row is the ONLY writer
        # on the abort path; inference cannot derive it on its own) and the
        # done marker below (round-48). Ceiling since round-54 (G7): the
        # old no-ceiling ruling was checkpointer-shaped (AsyncSqliteSaver,
        # verified r53) and this chain carries aget_state — a checkpointer
        # READ — so a future remote checkpointer re-opens the r49 hang
        # surface; the SQLite-class bound is generous and a hit is bounded
        # abandon.
        with anyio.CancelScope(shield=True):
            try:
                with anyio.fail_after(ABORT_SQLITE_CEILING_S):
                    await _finalize_task_session(
                        _result_graph,
                        _result_config,
                        ctx.turn_id,
                        ctx.store.cancel_interrupt,
                        cancelled=_turn_cancelled,
                        # Round-60 F1''': the cause memo — the same
                        # ctx.abort_cause the G5 fallback below classifies
                        # from — so the session word matches the row word
                        # for EVERY abort cause, not just the flag-causes.
                        abort_cause=ctx.abort_cause,
                    )
                    # Round-54 G5: a turn-dispatched recovery killed mid-run
                    # (auto-recover from dialogue) left BOTH its session row
                    # active forever AND its recover TaskStore row a zombie —
                    # _run_recover's finalize only runs on the normal path,
                    # and the _finalize_task_session call above reads the
                    # INTENT graph (recover turns never set ctx.result_graph).
                    # This fallback keys on the dispatch coordinates recorded
                    # AT dispatch, and on the normal-path marker that
                    # suppresses it for recoveries that closed themselves.
                    # Round-63 N2: the coordinates themselves can be missing
                    # on a legitimate abort — the intent clarification's
                    # recover branch bootstraps the recover session MID-GRAPH,
                    # and an abort between that bootstrap and _run_recover's
                    # own recording unwinds through this finally with the
                    # session already active but the gate falsy (the
                    # defensive arm above yields this exact shape to G5 —
                    # F5'''). Self-rescue the coordinates from the intent
                    # state: the same source _run_recover reads ("task_id"
                    # is the recover session id at that point). Paused intent
                    # states are excluded — a paused turn stays resumable,
                    # its session stays active (the inject twin keeps it
                    # too).
                    # The not-ctx.dry_run gate is legislated (round-64
                    # R64-1): a dry-run turn bootstraps NO recover session
                    # (the intent branch early-returns before bootstrap),
                    # so the fallback has nothing to close on a preview —
                    # and it must never write terminal words on one.
                    if not ctx.recover_finalized and not ctx.dry_run:
                        _rec_id = ctx.recover_task_id
                        _rec_cfg = ctx.recover_config
                        if not _rec_id:
                            _snap = None
                            try:
                                _snap = await _result_graph.aget_state(
                                    _result_config,
                                )
                            except Exception:
                                # Round-64 R64-2 (the r50 capture-warning
                                # family): a silent self-rescue failure
                                # re-arms the very leak the rescue exists to
                                # close — nobody closes the bootstrapped
                                # session and nothing says why.
                                logger.warning(
                                    "G5 recover self-rescue aborted: failed "
                                    "to read the intent state; the "
                                    "bootstrapped recover session (if any) "
                                    "stays active until the shutdown sweep",
                                    exc_info=True,
                                )
                                _snap = None
                            _iv = _snap.values if _snap and _snap.values else {}
                            if (
                                _iv.get("confirmed_intent") == "recover"
                                and _snap is not None
                                and not getattr(_snap, "next", None)
                            ):
                                _cand = _iv.get("task_id", "")
                                if isinstance(_cand, str) and is_real_task_id(_cand):
                                    _rec_id = _cand
                                    _rec_cfg = {
                                        "configurable": {"thread_id": _cand},
                                        "recursion_limit": settings.recursion_limit,
                                    }
                        if _rec_id and _rec_cfg:
                            from chaos_agent.memory.session_store import (
                                get_global_session_store as _get_rec_store,
                            )

                            _rec_store = _get_rec_store()
                            # Round-55 F2: the fallback runs for EVERY abort
                            # cause (the suppression key only spares runs that
                            # closed themselves), so the word must be
                            # classified, not hard-coded — a user-cancelled
                            # recovery is "cancelled", not "failed". Same
                            # taxonomy as the G4 arm via the cause memo
                            # (round-56: abort_row_word now lives in the shared
                            # stream_abort module).
                            _rec_word = abort_row_word(
                                ctx.abort_cause or "internal_error",
                            )
                            if _rec_store and _rec_store.has_active(_rec_id):
                                _rec_graph = (
                                    ctx.agents.get("recover")
                                    if isinstance(ctx.agents, dict) else None
                                )
                                await finalize_recover_session(
                                    _rec_store,
                                    _rec_graph,
                                    _rec_cfg,
                                    _rec_id,
                                    "",
                                    None,
                                    result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
                                    default_status=_rec_word,
                                    error_log_level="debug",
                                )
                            if is_real_task_id(_rec_id):
                                await write_aborted_task_row(
                                    _rec_id, _rec_word,
                                )
            except TimeoutError:
                logger.warning(
                    "Turn %s terminal finalize exceeded %.0fs ceiling; "
                    "remaining terminal writes abandoned",
                    ctx.turn_id, ABORT_SQLITE_CEILING_S,
                )
        # Terminal "done" marker for the events jsonl. The SSE side has
        # four separate done-yield sites (normal + three error paths);
        # the finally block is the ONE place all of them flow through,
        # so a single sidewrite here records the turn boundary without
        # touching each yield. ClientDisconnected returns early (no
        # done on the wire either) and CancelledError re-raises AFTER
        # this — both still get the marker, which is what a later
        # ``/resume`` visual rebuild needs to know the turn ended.
        # Excluded: the sidewrite itself must never throw from finally.
        try:
            _done_evt = StreamEvent(type="done", task_id=ctx.turn_id)
            sidewrite(_done_evt)
        except Exception:
            pass

