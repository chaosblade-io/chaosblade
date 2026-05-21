"""Unified SSE turn endpoint for the TS TUI front-end.

Frame format reuses the existing ``StreamEvent.to_sse()``: each frame is a
single ``data: {...json...}\\n\\n`` line whose JSON carries a ``type``
field (no ``event:`` row). This matches the legacy ``/inject-stream``
shape so a TS client can decode either endpoint with the same parser.

Event types yielded:
  token / thinking / tool_start / tool_end / node_start / node_end
  confirm / result / error / done

The ``done`` event is the explicit terminator so the client knows the
turn is complete (vs. just the connection closing).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from chaos_agent.agent.state import (
    extract_ui_diagnostics,
    infer_task_state,
    strip_side_effects,
)
from chaos_agent.agent.streaming import StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.server.routes.sessions import SessionStore, get_store, sessions_router
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# How often to emit an SSE comment frame (``: keepalive\n\n``) while a
# turn is parked at a confirm interrupt. The wait itself can be long —
# see ``settings.confirm_wait_timeout`` — and during that window the
# server emits no frames otherwise. Most reverse proxies / load
# balancers / corporate firewalls drop idle TCP connections after
# 30–60s; 25s leaves a safe margin under the lowest common bound. The
# TS client's ``parseFrame`` ignores ``:`` comments, so this is
# completely invisible to application-level event handling.
_CONFIRM_KEEPALIVE_INTERVAL_S = 25


class TurnRequest(BaseModel):
    input: str
    permission_mode: str = "confirm"  # "confirm" | "auto"
    display_mode: str | None = "calm"
    # Phase 3c.2 — Dry-Run multi-turn planning. When True the agent
    # graph runs intent_clarification → agent_loop → safety_check →
    # confirmation_gate normally, but ``confirmation_gate`` emits a
    # plan-preview AIMessage instead of pausing on ``interrupt()`` and
    # the post-gate router skips straight to END. Lets ``/plan <NL>``
    # produce a "what would happen" summary the user can iterate on
    # before commiting via ``/run``. Whole flow is read-only — no
    # blade_create runs, no checkpoint mutation that survives past
    # turn-end. Default False so legacy callers and the existing
    # ``/run`` path keep their current behaviour unchanged.
    dry_run: bool = False


def _extract_pending_interrupt(graph_state) -> tuple[str, dict] | None:
    """Pull the first unresolved interrupt from a paused graph state.

    LangGraph stores ``interrupt(value)`` payloads on
    ``state.tasks[i].interrupts[j].value`` and the node where the
    interrupt fired in ``state.tasks[i].name``. We walk the tasks in
    declaration order and surface the first one with at least one
    interrupt — which mirrors what ``cli/runner.py:539-552`` does for
    the in-process Python TUI path, so both front-ends get identical
    semantics across multi-layer interrupts (intent_confirm + confirmation_gate).

    Returning ``None`` means either: (a) graph has finished and there's
    nothing pending, or (b) graph paused at a non-interrupt task (rare;
    usually a sign of corrupted checkpoint state). Caller treats both
    as "no confirm needed, fall through to result".

    Non-dict payloads are wrapped in ``{"value": payload}`` so callers
    can rely on a uniform shape downstream — every interrupt() call site
    in this codebase passes a dict, but tightening the contract here
    would surprise an external caller that doesn't.
    """
    if not graph_state or not graph_state.tasks:
        return None
    for task in graph_state.tasks:
        interrupts = getattr(task, "interrupts", None) or ()
        for it in interrupts:
            value = getattr(it, "value", None)
            if value is None:
                continue
            node = getattr(task, "name", "") or ""
            if isinstance(value, dict):
                return (node, value)
            return (node, {"value": value})
    return None


def _content_from_interrupt_payload(payload: dict) -> str:
    """Pick a human-readable string for the ``content`` field of a confirm event.

    The TS TUI v2+ reads the structured ``payload`` field directly, but
    older clients only know about ``content``. We pick a pre-formatted
    summary the node has already prepared:

      * ``intent_confirm`` puts a multi-line markdown summary at
        ``payload["summary"]``.
      * ``confirmation_gate`` puts a plan summary at
        ``payload["plan_summary"]``.

    Falling back to a JSON dump is a last-resort defense — every current
    interrupt() call site supplies one of the two keys above.
    """
    return (
        payload.get("summary")
        or payload.get("plan_summary")
        or json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _normalise_answer(answer: str) -> str:
    """Normalise a free-text confirmation answer to LangGraph's expected resume token.

    Both intent_confirm and confirmation_gate gate on ``decision == "approved"``,
    so anything that doesn't read as approval is treated as rejection. Keeps
    the Y/yes/y/ok aliases for parity with the CLI confirm prompt.
    """
    return (
        "approved"
        if answer.strip().lower() in ("approved", "yes", "y", "ok")
        else "rejected"
    )


SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@sessions_router.post("/{sid}/turn")
async def turn(sid: str, body: TurnRequest, req: Request):
    """Run one conversation turn and stream events as SSE."""
    store: SessionStore = get_store()
    sess = store.get(sid)
    if sess is None:
        raise HTTPException(404, "Session not found")

    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker

    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([
                StreamEvent(
                    type="error",
                    content="Server is shutting down",
                ).to_sse()
            ]),
            media_type="text/event-stream",
            headers=SSE_HEADERS,
        )

    # Per-turn correlation ID. The ``turn-`` prefix is intentional:
    # during intent clarification / chat / capability Q&A there is
    # no "task" yet — the user is still talking, not running an
    # operation. We use a non-task prefix so server logs and SSE
    # event task_id fields don't masquerade as operational task
    # records. Real operations (inject / recover) get a separate
    # ``task-<hex>`` allocated below when the pipeline completes
    # — that's what lands in the on-disk session ``task_ids`` audit
    # list. Clarification / chat turns never produce a ``task-`` ID
    # anywhere user-visible.
    turn_id = f"turn-{uuid4().hex[:12]}"
    # NOTE: deliberately NOT calling ``store.add_task(sid, turn_id)``.
    # The in-memory ``sess["task_ids"]`` is read by the TS TUI's
    # ``/status`` command (see ``tui/src/state/commands.ts``), which
    # displays it as "Tasks: N" — that count must mean real operations
    # (inject / recover), not per-turn correlation IDs. We append to
    # ``task_ids`` only down in the result-payload branch, where we
    # have the actual ``task-<hex>`` allocated by intent_clarification's
    # transition into the inject/recover pipeline.

    # One TUI session ↔ one LangGraph thread. ``conversation_thread_id``
    # is allocated at session creation and reused by every turn —
    # successful inject/recover does NOT reset it (the user can keep
    # talking in the same conversation context after an operation
    # completes). Only ``POST /sessions`` produces a new thread.
    # This is what fixes the "intent_clarification forgot what I
    # said in the previous turn" bug: with a stable thread_id the
    # checkpointer rehydrates the prior ``messages`` (via
    # ``MessagesState``'s ``add_messages`` reducer) so the LLM sees
    # the full conversation, not just the current input.
    thread_id = sess.get("conversation_thread_id") or ""
    if not thread_id:
        # Defensive — shouldn't happen since SessionStore.create
        # populates conversation_thread_id, but a session loaded
        # from a future on-disk format might not have it.
        thread_id = f"conv-{uuid4().hex[:12]}"
        sess["conversation_thread_id"] = thread_id

    is_first_turn = not sess.get("first_turn_done", False)
    sess["first_turn_done"] = True

    # Initial graph state. First turn is the inject_stream-style
    # full payload (session-level fields like kubeconfig /
    # interaction_mode / operation must reach the checkpointer at
    # least once). Subsequent turns mirror Python TUI's
    # ``converse_stream`` selective reset — the checkpointer already
    # carries messages / fault_intent / kubeconfig / etc., we only
    # override what MUST change for THIS turn:
    #
    #   - task_id: fresh per-turn correlation
    #   - input: new user message (load_memory wraps as HumanMessage)
    #   - confirmed_intent="unset": bypasses the short-circuit branch
    #     in intent_clarification that otherwise fires when the
    #     previous turn ended with intent="chat" / "inject" / "recover"
    #   - intent_confidence reset
    #   - pipeline counters / error / replan state reset so a new
    #     injection attempt within the same conversation starts clean
    #
    # We do NOT touch fault_intent, target, params, kubeconfig, or
    # any session-level field — checkpoint values are preserved via
    # LangGraph's per-field merge semantics, and clobbering them
    # would silently undo confirmed parameters mid-dialogue.
    if is_first_turn:
        initial_state = {
            "task_id": turn_id,
            "tui_session_id": sid,
            "interaction_mode": "tui",
            "operation": "inject",
            "target": None,
            "params": {},
            "input": body.input,
            "needs_confirmation": body.permission_mode == "confirm",
            "safety_status": "pending",
            "kubeconfig": settings.kubeconfig_path,
            "kube_context": settings.kube_context,
            "created_at": now_iso(),
            # Phase 3c.2 — pass dry_run through to the agent graph.
            # ``router.route_after_safety_check`` and
            # ``confirmation_gate`` already branch on this flag
            # (state.py declared the field; this wires the request
            # body to it). False default = legacy ``/run`` semantics.
            "dry_run": body.dry_run,
        }
    else:
        initial_state = {
            "task_id": turn_id,
            "input": body.input,
            "confirmed_intent": "unset",
            "intent_confidence": 0.0,
            "safety_status": "pending",
            "agent_loop_count": 0,
            "execute_loop_count": 0,
            "verifier_loop_count": 0,
            "error": None,
            "failure_reason": None,
            "replan_requested": False,
            "replan_count": 0,
            "replan_context": None,
            # Re-assert dry_run per turn — the checkpointer would
            # otherwise carry the previous turn's value forward, so
            # ``/run`` after ``/plan`` would silently inherit dry_run
            # and never actually inject. Always set to body's value.
            "dry_run": body.dry_run,
        }
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }
    graph = agents["inject"]

    # Memory-compaction status pipeline (Phase 4 / 4a).
    #
    # PreReasoningHook synchronously calls compact_memory() (LLM-driven
    # summarisation, 5–15s) inside agent_loop. While that call is in
    # flight the LangGraph stream emits NO events, so the TS TUI used
    # to see a multi-second silent stall and assume the connection had
    # hung. The hook DOES emit ``StatusEvent(source="memory_compression")``
    # to the per-task tracker, but the existing CLI ``/api/v1/status-
    # stream/{task_id}`` endpoint is on a separate channel that the
    # TS TUI doesn't subscribe to.
    #
    # Fix: subscribe a per-turn queue keyed by ``f"tui-{sid}"`` (the
    # hook fans out to this key in addition to the task-id key it's
    # always used). Then concurrently merge the queue with the graph
    # event stream so memory_compaction frames keep flowing when
    # the graph itself is stalled inside the hook.
    from chaos_agent.observability.status_tracker import subscribe as _status_subscribe
    from chaos_agent.observability.status_tracker import unsubscribe as _status_unsubscribe

    _tracker_key = f"tui-{sid}"
    _tracker_queue = _status_subscribe(_tracker_key)

    def _convert_compaction_status(status_evt) -> StreamEvent | None:
        """Translate a status-tracker ``StatusEvent`` whose
        ``source == "memory_compression"`` into the TS-TUI-shaped
        ``StreamEvent(type="memory_compaction", ...)``. Returns None
        for any other source so we don't accidentally relay node /
        tool tracker events that would duplicate the ones LangGraph
        already emits via ``astream_events``.
        """
        if getattr(status_evt, "source", "") != "memory_compression":
            return None
        detail = getattr(status_evt, "detail", None) or {}
        return StreamEvent(
            type="memory_compaction",
            content=getattr(status_evt, "message", ""),
            task_id=turn_id,
            compaction_phase=getattr(status_evt, "phase", ""),
            tokens_before=int(
                detail.get("total_tokens_before")
                or detail.get("tokens_before")
                or 0
            ),
            tokens_after=int(detail.get("tokens_after") or 0),
            messages_compacted=int(
                detail.get("messages_to_compact")
                or detail.get("messages_compacted")
                or 0
            ),
            duration_ms=float(getattr(status_evt, "duration_ms", 0.0) or 0.0),
            layer="llm_summary",
        )

    def _convert_context_size_status(status_evt) -> StreamEvent | None:
        """Translate a ``source == "context_size"`` StatusEvent into a
        ``StreamEvent(type="context_size", ...)`` carrying the four
        numbers the TS TUI's Footer needs to render its live
        ``current/window`` indicator. Returns None for other sources
        so the same status-pump tuple is reused by both converters."""
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

    async def _merged_stream(graph_iter):
        """Yield ``("graph", raw_event)`` and ``("status", status_evt)``
        tuples interleaved as they happen. Two background tasks pump
        each source into a unified queue; the consumer drains the queue
        until ``graph_done`` lands. Status pump cancels cleanly when
        the consumer exits early (disconnect / timeout)."""
        unified: asyncio.Queue = asyncio.Queue()
        graph_done = object()

        async def _graph_pump():
            try:
                async for raw in graph_iter:
                    await unified.put(("graph", raw))
            finally:
                await unified.put(("graph_done", graph_done))

        async def _status_pump():
            try:
                while True:
                    evt = await _tracker_queue.get()
                    await unified.put(("status", evt))
            except asyncio.CancelledError:
                # Normal lifecycle — main loop is done with us.
                pass

        g_task = asyncio.create_task(_graph_pump())
        s_task = asyncio.create_task(_status_pump())
        try:
            while True:
                kind, payload = await unified.get()
                if kind == "graph_done":
                    # Fix C — full drain on graph_done.
                    #
                    # Two sources can still hold pending events:
                    #   (1) ``unified`` itself: status_pump may have
                    #       successfully put a ("status", evt) tuple
                    #       BEFORE graph_pump put graph_done, but the
                    #       main loop's FIFO consumption picked
                    #       graph_done first because asyncio scheduled
                    #       the put-then-cancel sequence in that order.
                    #   (2) ``_tracker_queue``: hook may have emitted
                    #       a "completed" event AFTER status_pump's
                    #       last ``get()`` woke up — the event sits
                    #       in the tracker queue waiting for a pump
                    #       cycle that will never come once we cancel.
                    #
                    # Both paths must drain or the TS TUI's compaction
                    # spinner stays stuck on "started" forever.
                    # Cancel status_pump first to freeze the producer
                    # side; then drain unified, then _tracker_queue.
                    s_task.cancel()
                    try:
                        await s_task
                    except asyncio.CancelledError:
                        pass
                    # Drain (1): events already pumped to unified.
                    while True:
                        try:
                            nk, np = unified.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if nk == "graph_done":
                            continue  # ignore double signal
                        yield nk, np
                    # Drain (2): events left in tracker queue post-cancel.
                    while True:
                        try:
                            evt = _tracker_queue.get_nowait()
                            yield "status", evt
                        except asyncio.QueueEmpty:
                            break
                    # Fix G1 — surface graph_pump exceptions.
                    #
                    # ``_graph_pump`` doesn't catch its own exceptions:
                    # they propagate out of ``async for raw in ...``,
                    # the finally block puts graph_done (so we DO get
                    # here), and the task completes with .exception()
                    # set. The previous code's ``finally: await g_task
                    # except: pass`` silently swallowed those — turning
                    # a real graph error into a clean, eventless turn
                    # exit. Re-raise here so ``event_generator``'s
                    # outer ``except Exception`` produces the user-
                    # facing error SSE event the way it always did
                    # pre-merge.
                    if g_task.done():
                        exc = g_task.exception()
                        if exc is not None:
                            raise exc
                    return
                yield kind, payload
        finally:
            # ``s_task`` is normally already cancelled in the
            # graph_done path above; defensive cancel covers early
            # exits (client disconnect, generator throw, exception
            # in main-loop body before reaching graph_done).
            if not s_task.done():
                s_task.cancel()
                try:
                    await s_task
                except asyncio.CancelledError:
                    pass
            # ``g_task`` is normally done (graph_done was consumed
            # before we got here, so finally has run). For early
            # exits we cancel and drain. Don't suppress non-cancel
            # exceptions — main loop already handled them above for
            # the graph_done path; for the early-exit path the
            # ``event_generator`` outer except will catch anything.
            if not g_task.done():
                g_task.cancel()
                try:
                    await g_task
                except asyncio.CancelledError:
                    pass

    async def event_generator():
        stream_task = asyncio.current_task()
        task_tracker.register(turn_id, stream_task)
        turn_started_monotonic = time.monotonic()
        try:
            # 1. Stream the initial graph invocation, with memory-
            #    compaction status events merged in concurrently
            #    (see _merged_stream comment above).
            async for kind, payload in _merged_stream(
                graph.astream_events(initial_state, config, version="v2"),
            ):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected during turn {turn_id}")
                    return
                if kind == "graph":
                    evt = parse_stream_event(payload)
                    if evt is not None:
                        evt.task_id = turn_id
                        yield evt.to_sse()
                elif kind == "status":
                    compaction_evt = _convert_compaction_status(payload)
                    if compaction_evt is not None:
                        yield compaction_evt.to_sse()
                    ctx_evt = _convert_context_size_status(payload)
                    if ctx_evt is not None:
                        yield ctx_evt.to_sse()

            # 2. Drain any pending interrupts. The inject pipeline has
            #    *two* interrupt() call sites — intent_confirm (Layer 1,
            #    confirms the LLM's parse of user intent before any
            #    expensive planning) and confirmation_gate (Layer 2,
            #    confirms the generated plan + safety status before
            #    actual chaosblade execution). Either or both can fire
            #    in a single turn depending on user replies, and a
            #    rejected Layer 1 short-circuits to END without ever
            #    reaching Layer 2. A simple ``while`` over
            #    ``_extract_pending_interrupt`` handles all cases
            #    uniformly — including any future interrupt nodes added
            #    to the graph — without hardcoding node names here.
            #
            #    ``_extract_pending_interrupt`` reads
            #    ``state.tasks[*].interrupts[*].value`` directly, the
            #    same path ``cli/runner.py`` walks for the in-process
            #    Python TUI. That keeps both front-ends in lockstep on
            #    multi-layer interrupt semantics — fixing this once
            #    fixes both.
            from langgraph.types import Command
            while True:
                current_state = await graph.aget_state(config)
                pending = _extract_pending_interrupt(current_state)
                if pending is None:
                    break

                interrupted_node, payload = pending

                # Emit a structured confirm event. ``payload`` carries
                # the original ``interrupt(value)`` dict so the TS TUI
                # can render fielded forms (intent fields vs plan
                # fields). ``content`` keeps a pre-formatted string for
                # v1 clients that don't read ``payload``.
                yield StreamEvent(
                    type="confirm",
                    content=_content_from_interrupt_payload(payload),
                    node=interrupted_node,
                    task_id=turn_id,
                    payload=payload,
                ).to_sse()

                # interrupt_id == turn_id. Future is set by
                # /sessions/:id/interrupt. We bound the wait so a
                # user-stepping-away case doesn't leak the future
                # nor pin the SSE connection forever. The total budget
                # is configurable (``settings.confirm_wait_timeout``,
                # default 30 min) — long enough for a deliberate human
                # review yet short enough that an abandoned client
                # doesn't keep the future alive indefinitely.
                #
                # Inside the wait we slice on ``_CONFIRM_KEEPALIVE_INTERVAL_S``
                # and yield a ``: keepalive`` SSE comment between
                # slices. Two reasons:
                #   1. Reverse proxies / firewalls / corp NAT often
                #      drop idle TCP connections after 30–60s. Without
                #      a periodic frame the SSE pipe would die mid-wait.
                #      Localhost runs aren't affected, but the same
                #      server is the embedded process for blade-ai
                #      cloud deployments where an LB sits in between.
                #   2. ``asyncio.shield`` keeps ``fut`` alive across the
                #      ``wait_for`` cancellations the slicing introduces;
                #      without shield, the inner timeout would cancel
                #      the future and the next slice would re-register
                #      a fresh one — losing any answer that landed in
                #      the gap.
                fut = store.register_interrupt(turn_id)
                deadline = asyncio.get_event_loop().time() + settings.confirm_wait_timeout
                answer = ""  # always overwritten by the loop's `break` path
                while True:
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        store.cancel_interrupt(turn_id)
                        minutes = settings.confirm_wait_timeout // 60
                        yield StreamEvent(
                            type="error",
                            content=f"Confirmation timed out ({minutes} min)",
                            task_id=turn_id,
                        ).to_sse()
                        yield StreamEvent(type="done", task_id=turn_id).to_sse()
                        return
                    slice_s = min(_CONFIRM_KEEPALIVE_INTERVAL_S, remaining)
                    try:
                        answer = await asyncio.wait_for(
                            asyncio.shield(fut), timeout=slice_s
                        )
                        break
                    except asyncio.TimeoutError:
                        # Still waiting on the user — drop a comment frame
                        # so intermediaries don't reap the connection,
                        # then loop back into the next slice.
                        yield ": keepalive\n\n"
                        continue

                normalised = _normalise_answer(answer)

                # Resume the graph. The next astream_events run drains
                # tokens / tool / phase events until either the next
                # interrupt fires (loop continues) or the graph reaches
                # END (loop exits, fall through to result).
                #
                # Fix B — also wrapped with ``_merged_stream`` so
                # memory-compaction events fired by ``PreReasoningHook``
                # during the post-confirm agent_loop reach the TS TUI.
                # Step 2 is actually the MORE common compaction site
                # because by definition messages have accumulated past
                # the intent_confirm interrupt (so threshold is more
                # likely tripped here than on the very first turn).
                async for kind, payload in _merged_stream(
                    graph.astream_events(
                        Command(resume=normalised), config, version="v2",
                    ),
                ):
                    if await req.is_disconnected():
                        logger.info(
                            f"Client disconnected during resume of {turn_id}"
                        )
                        return
                    if kind == "graph":
                        evt = parse_stream_event(payload)
                        if evt is not None:
                            evt.task_id = turn_id
                            yield evt.to_sse()
                    elif kind == "status":
                        compaction_evt = _convert_compaction_status(payload)
                        if compaction_evt is not None:
                            yield compaction_evt.to_sse()
                        ctx_evt = _convert_context_size_status(payload)
                        if ctx_evt is not None:
                            yield ctx_evt.to_sse()

            # 3. Final-state extraction → ``result`` envelope.
            #    Mirrors inject_stream.py's terminal-state shape so the
            #    TS client's ResultCard parser sees the same data
            #    regardless of which endpoint started the turn. The
            #    ``task_id`` carried in the result payload is read
            #    from final_state.values — i.e. it's the ``task-<hex>``
            #    that the inject/recover pipeline allocated for itself
            #    (in intent_clarification on the inject/recover
            #    transition). Turn.py never invents a ``task-`` id.
            result_payload = await _build_result_payload(
                graph, config, turn_id, turn_started_monotonic
            )
            if result_payload is not None:
                # Pull the real operational task_id straight from the
                # result payload (which read it from final_state.task_id).
                # If it starts with ``task-`` the inject/recover flow
                # actually allocated one; persist that to the on-disk
                # session ``task_ids`` audit trail. Anything else
                # (empty / ``turn-``) means nothing operational was
                # established — skip the persist.
                op_task_id = ""
                data_obj = result_payload.get("data")
                if isinstance(data_obj, dict):
                    candidate = data_obj.get("task_id", "")
                    if isinstance(candidate, str) and candidate.startswith("task-"):
                        op_task_id = candidate
                if op_task_id:
                    # In-memory append so ``/status`` (TS TUI command,
                    # reads ``state.task_ids`` length) reports the
                    # cumulative count of real operations in this
                    # session — multiple injects in the same session
                    # are all listed, never overwritten. SessionStore.add_task
                    # uses ``setdefault(...).append(...)`` so each
                    # call grows the list by one without clobbering
                    # earlier entries.
                    store.add_task(sid, op_task_id)
                    # Mirror to disk session file for the audit trail.
                    # ``tui_session_store.add_task`` is also append-only
                    # (``if task_id not in task_ids: task_ids.append(...)``),
                    # so the on-disk ``task_ids`` array holds every
                    # inject / recover task ID this session has run,
                    # in chronological order.
                    from chaos_agent.memory.tui_session_store import (
                        get_global_tui_session_store,
                    )
                    tui_store = get_global_tui_session_store()
                    if tui_store is not None:
                        try:
                            tui_store.add_task(sid, op_task_id)
                        except Exception as e:
                            logger.warning(
                                f"task_id disk persist failed sid={sid} "
                                f"task={op_task_id}: {e}"
                            )
                yield StreamEvent(
                    type="result",
                    content=json.dumps(result_payload, ensure_ascii=False),
                    task_id=turn_id,
                ).to_sse()

            # 4. Done terminator.
            yield StreamEvent(type="done", task_id=turn_id).to_sse()

        except asyncio.CancelledError:
            logger.info(f"Turn {turn_id} cancelled by client")
            yield StreamEvent(
                type="error",
                content="Turn cancelled",
                task_id=turn_id,
            ).to_sse()
            yield StreamEvent(type="done", task_id=turn_id).to_sse()
            raise
        except Exception as e:
            logger.exception(f"Turn failed for {turn_id}")
            yield StreamEvent(
                type="error",
                content=f"{type(e).__name__}: {e}",
                task_id=turn_id,
            ).to_sse()
            yield StreamEvent(type="done", task_id=turn_id).to_sse()
        finally:
            # Drop any orphaned interrupt future so a client that
            # disconnected mid-confirm doesn't leak it. ``cancel_interrupt``
            # is a no-op when the future is missing or already resolved.
            store.cancel_interrupt(turn_id)
            task_tracker.unregister(turn_id)
            # Detach the per-turn status-tracker queue. ``unsubscribe``
            # tolerates absence / double-close so calling it
            # unconditionally here is safe — keeps the StatusTracker
            # subscriber list from growing unbounded across the
            # process's lifetime.
            try:
                _status_unsubscribe(_tracker_key, _tracker_queue)
            except Exception:
                pass

            # Defensive task-session finalization. ``save_memory``
            # already finalizes on the clean-termination path — this
            # ``finally`` block covers Esc cancel (CancelledError) and
            # unhandled exceptions where save_memory never ran.
            #
            # Skip when the graph is paused at an interrupt: the user
            # may reconnect and resume the turn, in which case
            # finalizing now would (a) seal the JSON file with
            # status="cancelled" prematurely, and (b) drop the entry
            # from ``_active_sessions`` so subsequent
            # ``hook.append_messages`` calls (after resume) silently
            # no-op — the resumed agent_loop / execute_loop messages
            # would never reach the task file. The .json snapshot +
            # .jsonl increment files already exist on disk
            # (``create_session`` flushed them at intent_clarification
            # time), so a paused-but-not-finalized task is safely
            # readable via ``read_session`` and resumable via
            # ``Command(resume=...)``. Letting it sit "active" is
            # honest. The next clean termination (success or explicit
            # rejection) will run finalize through save_memory.
            #
            # Three guards keep this safe:
            #   1. ``has_active`` is the public read of the active-
            #      sessions set — no private attribute access.
            #   2. ``finalize_session`` silently returns when the
            #      task isn't in ``_active_sessions`` — covers
            #      double-call after save_memory already ran.
            #   3. We read ``op_task_id`` from the latest graph state
            #      (NOT ``turn_id`` — turn_id is the per-turn SSE
            #      correlation id, the operational task is the
            #      ``task-<hex>`` allocated by intent_clarification).
            #      No-op when no operational task was ever born.
            try:
                from chaos_agent.memory.session_store import (
                    get_global_session_store,
                )
                _store = get_global_session_store()
                if _store is not None:
                    try:
                        _final = await graph.aget_state(config)
                    except Exception:
                        _final = None
                    _op_tid = ""
                    _state_msgs: list = []
                    if _final and getattr(_final, "values", None):
                        _candidate = _final.values.get("task_id", "")
                        if isinstance(_candidate, str) and _candidate.startswith(
                            ("task-", "recover-")
                        ):
                            _op_tid = _candidate
                        _state_msgs = list(_final.values.get("messages") or [])
                    # Skip finalize when paused at interrupt — user
                    # may resume. ``next`` is non-empty iff the graph
                    # is suspended at an interrupt() call site.
                    paused_at_interrupt = bool(
                        _final and getattr(_final, "next", None)
                    )
                    if _op_tid and _store.has_active(_op_tid):
                        # FLUSH FIRST — always. The PreReasoningHook
                        # writes messages via ``asyncio.create_task``
                        # (fire-and-forget), so on Esc / SSE cancel /
                        # unhandled exception some of those tasks may
                        # not have completed. Plus ToolNode-produced
                        # ToolMessages have no hook of their own and
                        # are normally only flushed when the *next*
                        # iteration's hook fires — Esc'ing between a
                        # tool call and the next agent_loop loses
                        # them entirely. Synchronously appending the
                        # full graph-state ``messages`` list here is
                        # the authoritative "every in-memory message
                        # reaches disk before the file closes" point.
                        # ``append_messages`` dedups by id / content
                        # key so re-passing already-flushed messages
                        # is safe and free.
                        if _state_msgs:
                            try:
                                _store.append_messages(_op_tid, _state_msgs)
                                logger.info(
                                    "Flushed %d state messages to task=%s "
                                    "before finalize gate",
                                    len(_state_msgs), _op_tid,
                                )
                            except Exception:
                                logger.warning(
                                    "Pre-finalize flush failed for task=%s",
                                    _op_tid, exc_info=True,
                                )

                        if not paused_at_interrupt:
                            # Decide status from the same signals
                            # save_memory uses, so the persisted JSON's
                            # ``status`` field doesn't disagree with the
                            # SQLite ``task_state``.
                            _vals = _final.values if _final else {}
                            if _vals.get("blade_uid") and not (
                                _vals.get("error") or _vals.get("failure_reason")
                            ):
                                _final_status = "completed"
                            else:
                                # CancelledError / unhandled error map
                                # to "cancelled" — distinct from
                                # save_memory's "failed" so future
                                # debugging can tell apart "graph
                                # reached save_memory and reported
                                # failure" vs "stream torn down before
                                # save_memory ran".
                                _final_status = "cancelled"
                            # remaining_messages=[] because we already
                            # flushed above; finalize should just seal
                            # status + finished_at without re-running
                            # the dedup loop on the same payload.
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
                            # Paused at interrupt — leave the session
                            # ACTIVE on disk so a future
                            # ``Command(resume=...)`` can keep
                            # appending. The full message flush above
                            # already guarantees every in-memory
                            # message is on disk; status="active"
                            # just signals "more might follow".
                            logger.info(
                                "Paused at interrupt for task=%s "
                                "(next=%s); flushed %d state messages "
                                "and kept session active for resume",
                                _op_tid,
                                list(getattr(_final, "next", []) or []),
                                len(_state_msgs),
                            )
            except Exception:
                logger.warning(
                    "Defensive task-session finalize failed for turn=%s",
                    turn_id, exc_info=True,
                )

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


async def _build_result_payload(
    graph,
    config: dict,
    task_id: str,
    started_monotonic: float,
) -> dict | None:
    """Read final graph state and shape it into a ResultCard envelope.

    Returns ``None`` when nothing operational happened (chat /
    capability Q&A / ambiguous turns with no plan and no blade_uid).
    The TS side renders ``result`` events as an "Injection succeeded"
    card; firing one for a chat reply would surprise the user —
    they typed a greeting, not an operation. The agent's text reply
    is the result for those turns.

    For inject we mirror the rich payload ``inject_stream.py``
    produces so the TS ``ResultCard`` parser doesn't need a
    per-endpoint branch.
    """
    try:
        final_state = await graph.aget_state(config)
    except Exception:
        logger.debug("aget_state failed during result extraction", exc_info=True)
        return None
    if not final_state or not final_state.values:
        return None

    # Defense in depth: never serialize a result envelope while the
    # graph is still paused on an interrupt. Before this guard, a
    # missed pending-interrupt detection in event_generator would let
    # ``_build_result_payload`` read the half-finished state — which
    # always parsed as ``task_state="failed"`` because blade_uid is
    # empty mid-flow — and surfaced as a phantom "Injection failed"
    # ResultCard in the TS TUI. The interrupt-loop above already
    # drains all pending interrupts, but if a future change introduces
    # a new pause point we want this to fail closed (no result) rather
    # than fail open (false failure).
    if final_state.next:
        logger.debug(
            "graph still paused at %s; suppressing result envelope",
            list(final_state.next),
        )
        return None

    values = final_state.values
    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)

    confirmed_intent = values.get("confirmed_intent")
    blade_uid = values.get("blade_uid", "") or ""
    plan_summary = values.get("plan_summary", "") or ""

    # Whitelist what counts as an operational turn worth a result
    # card: only ``inject`` and ``recover`` are user-initiated
    # operations with outcomes the user wants to see. Everything else
    # (``chat``, ``unset`` mid-clarification, missing intent for a
    # pure-text reply) is conversational; the agent's text reply IS
    # the result and showing a green "Injection succeeded" card on
    # top would mislead.
    #
    # Why not the previous heuristic that also checked for empty
    # blade_uid/plan_summary: that incorrectly skipped FAILED inject
    # turns (intent="inject", but no blade_uid/plan_summary because
    # execution didn't make it that far). Failed injects deserve a
    # card showing the failure, not silence.
    if confirmed_intent not in ("inject", "recover"):
        return None

    # Inject turn — full payload.
    task_state = infer_task_state(values)
    if task_state == "injecting":
        task_state = "injected" if blade_uid else "failed"

    skill_name = values.get("skill_name", "") or ""
    params = values.get("params") or {}
    fault_type = ""
    scope = (params.get("scope") or "").strip()
    target = (params.get("target") or "").strip()
    action = (params.get("action") or "").strip()
    if scope and target and action:
        fault_type = f"{scope}-{target}-{action}"
    elif skill_name:
        fault_type = skill_name

    diagnostics = {}
    try:
        diagnostics = extract_ui_diagnostics(values) or {}
    except Exception:
        # The helper touches several fields that may be missing on a
        # short-circuited turn. Treat any failure as "no diagnostics".
        logger.debug("extract_ui_diagnostics failed", exc_info=True)

    # Pull task_id from final state so the inject / recover pipeline's
    # own allocation wins over the per-turn correlation id passed by
    # ``turn.py``. ``intent_clarification`` allocates a ``task-<hex>``
    # the moment the user confirms inject/recover, and downstream
    # nodes inherit it via the LangGraph state. Falling back to the
    # caller's value only matters for legacy CLI paths that already
    # carry a ``task-<hex>`` in state.task_id from the runner.
    state_task_id = values.get("task_id") or ""
    real_task_id = state_task_id if isinstance(state_task_id, str) and state_task_id else task_id

    return {
        "status": "success",
        "data": {
            "task_id": real_task_id,
            "task_state": task_state,
            "fault_type": fault_type,
            "blade_uid": blade_uid,
            "duration_ms": elapsed_ms,
            # P1-6: include the live target spec so the TUI ResultCard
            # can show namespace + names. Without this the user has no
            # way to verify "I actually hit the right pod/node" from
            # the result card alone (had to scroll back to the confirm
            # gate to check).
            "target": values.get("target") or {},
            # Same rationale for params — what we actually executed
            # with, surfaced for confirm-trail audit.
            "params": values.get("params") or {},
            "verification": strip_side_effects(values.get("verification")),
            "side_effects": values.get("verification", {}).get("side_effects")
            if isinstance(values.get("verification"), dict)
            else None,
            **diagnostics,
        },
    }
