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

from chaos_agent.agent.fault_spec import SOURCE_TUI, FaultSpec
from chaos_agent.agent.state import (
    extract_ui_diagnostics,
    infer_task_state,
    strip_side_effects,
)
from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_event
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
      * ``plan_builder`` puts the question text at
        ``payload["question"]``.

    Falling back to a JSON dump is a last-resort defense — every current
    interrupt() call site supplies one of the keys above.
    """
    return (
        payload.get("summary")
        or payload.get("plan_summary")
        or payload.get("question")
        or json.dumps(payload, ensure_ascii=False, indent=2)
    )


def _format_auto_approve_info(node: str, payload: dict) -> str:
    """Format interrupt payload for auto-mode display (token, not card)."""
    lines = [f"[Auto-approved: {node}]"]

    if node == "confirmation_gate":
        fi = payload.get("fault_intent") or {}
        ft = fi.get("fault_type", "")
        if ft:
            lines.append(f"故障: {ft}")
        target = payload.get("target") or {}
        ns = target.get("namespace", "")
        names = target.get("names", [])
        if ns or names:
            lines.append(f"目标: {ns}/{', '.join(names) if names else '*'}")
        params = payload.get("params") or {}
        if params:
            lines.append(f"参数: {', '.join(f'{k}={v}' for k, v in params.items() if v)}")
        safety = payload.get("safety_status", "")
        if safety:
            reason = payload.get("safety_checked_detail") or payload.get("safety_reason") or ""
            lines.append(f"安全: {safety}" + (f" ({reason})" if reason else ""))
        health = payload.get("target_health_report") or {}
        if health:
            lines.append(f"健康: {health.get('overall', '?')} ({health.get('summary', '')})")
        feas = payload.get("feasibility_report") or {}
        if feas and feas.get("severity"):
            lines.append(f"可行性: {feas.get('severity')} ({feas.get('message', '')})")
        score = payload.get("safety_score") or {}
        if score:
            lines.append(f"安全评分: {score.get('overall', '?')}/100 ({score.get('level', '')})")
    elif node == "plan_change_confirm":
        reason = payload.get("reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original.get("fault_type"):
            lines.append(f"原方案: {original['fault_type']}")
        if proposed.get("fault_type"):
            lines.append(f"新方案: {proposed['fault_type']}")
        if reason:
            lines.append(f"原因: {reason}")
    elif node == "tool_screener":
        reason = payload.get("reason", "")
        agent_reason = payload.get("agent_reason", "")
        original = payload.get("original") or {}
        proposed = payload.get("proposed") or {}
        if original:
            ns = original.get("namespace", "")
            names = original.get("names", [])
            lines.append(f"批准目标: {ns}/{', '.join(names) if names else '*'}")
        if proposed:
            ns = proposed.get("namespace", "")
            names = proposed.get("names", [])
            lines.append(f"实际目标: {ns}/{', '.join(names) if names else '*'}")
        if reason:
            lines.append(f"偏移原因: {reason}")
        if agent_reason:
            lines.append(f"Agent 解释: {agent_reason}")
    else:
        content = _content_from_interrupt_payload(payload)
        if content:
            lines.append(content)

    return "\n".join(lines)


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


def _rebuild_inject_verification_summary(verification: dict | None) -> str:
    """Rebuild inject_verification_summary from the stored verification dict."""
    if not verification or not isinstance(verification, dict):
        return ""
    layer2 = verification.get("layer2")
    if not layer2 or not isinstance(layer2, dict):
        return ""
    details = layer2.get("details", "")
    if not details:
        return ""
    return f"Layer2={layer2.get('status', 'unknown')}, Details={details}"


async def _build_recover_initial_from_store(
    task_id: str,
    rec_task_id: str,
    tui_session_id: str,
    agents: dict,
) -> dict | None:
    """Build recover_initial from task_store, bypassing LangGraph checkpoint.

    Used for cross-session TUI recovery where the checkpoint is stored
    under conversation_thread_id (not task_id) and can't be looked up.
    """
    from chaos_agent.persistence.task_store import get_task_store

    store = await get_task_store()
    record = await store.get(task_id)
    if not record or not record.get("blade_uid"):
        return None

    target = record.get("target") or {}
    if isinstance(target, str):
        try:
            target = json.loads(target)
        except (json.JSONDecodeError, TypeError):
            target = {}
    params = record.get("params") or {}
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except (json.JSONDecodeError, TypeError):
            params = {}

    fault_spec = {
        "namespace": target.get("namespace", ""),
        "scope": target.get("resource_type", ""),
        "names": target.get("names", []),
        "labels": target.get("labels", {}),
        "blade_target": "",
        "blade_action": "",
        "params": params,
        "params_flags": [],
        "duration_seconds": 0,
        "source": "task_store_rebuild",
        "user_description": "",
    }

    skill_name = record.get("skill_name", "")
    skill_case_content = ""
    if skill_name:
        try:
            registry = agents.get("skill_registry")
            if registry:
                skill_case_content = registry.activate(skill_name)
        except Exception:
            pass

    return {
        "task_id": rec_task_id,
        "tui_session_id": tui_session_id,
        "parent_task_id": task_id,
        "operation": "recover",
        "blade_uid": record.get("blade_uid", ""),
        "skill_name": skill_name,
        "skill_case_content": skill_case_content,
        "inject_verification_summary": _rebuild_inject_verification_summary(record.get("verification")),
        "inject_context": record.get("inject_context") or "",
        "baseline_data": record.get("baseline_data"),
        "fault_spec": fault_spec,
        "kubeconfig": record.get("kubeconfig") or "",
        "injection_method": record.get("injection_method"),
        "kubectl_exec_pod_name": record.get("kubectl_exec_pod_name"),
        "created_at": str(record.get("gmt_create") or ""),
        "verifier_loop_count": 0,
        "verification": None,
        "recover_verification": None,
        "messages": [],
        "inject_layer1_cache": None,
        "recover_layer1_cache": None,
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

    # First-run gate — lifespan deferred ``create_agent`` because
    # essential LLM config wasn't set yet. The TUI's REPL surfaces
    # this as a redirect into the setup wizard instead of crashing
    # downstream on ``agents['inject']`` dereference.
    if agents is None:
        return StreamingResponse(
            iter([
                StreamEvent(
                    type="error",
                    content="LLM config missing; run the setup wizard first.",
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
        # TUI is always NL — write a placeholder FaultSpec at entry.
        # intent_clarification will rewrite this with the full spec
        # once the LLM converges on a submit_fault_intent tool_call.
        spec = FaultSpec.placeholder_nl(
            user_description=body.input or "",
            source=SOURCE_TUI,
        )
        # Dual-graph model: Intent Graph only needs dialogue-level fields.
        # Pipeline-level fields (operation, safety_status, created_at)
        # are set when Pipeline Graph is launched in the dual-graph block.
        initial_state = {
            "task_id": turn_id,
            "tui_session_id": sid,
            "interaction_mode": "tui",
            "fault_spec": spec.to_dict(),
            "needs_confirmation": True,
            "kubeconfig": settings.kubeconfig_path,
            "kube_context": settings.kube_context,
            "dry_run": body.dry_run,
        }
    else:
        # Dual-graph model: subsequent turns only reset Intent Graph fields.
        # Pipeline fields (agent_loop_count, safety_status, etc.) are
        # irrelevant here — Pipeline Graph starts fresh each time.
        initial_state = {
            "task_id": turn_id,
            "input": body.input,
            "confirmed_intent": "unset",
            "intent_confidence": 0.0,
            "dry_run": body.dry_run,
        }
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": settings.recursion_limit,
    }
    intent_graph = agents["intent"]
    pipeline_graph = agents["pipeline"]
    graph = intent_graph  # Phase 1: Intent Graph

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

    def _convert_postmortem_status(status_evt) -> StreamEvent | None:
        """R17 — translate save_memory's postmortem tracker events into
        ``node_start`` / ``node_end`` SSE frames so the TUI spinner can
        surface the 5-30s "Generating postmortem..." phase. Without
        this, the user sees a silent gap between the verifier finishing
        and the ResultCard appearing.

        Maps StatusPhase → SSE type:
          STARTED   → node_start (sets thoughtSubject = "postmortem")
          RUNNING   → node_start (refresh with new message)
          COMPLETED → node_end   (clear subject)
          FAILED    → node_end   (subject cleared; failure surfaces in
                                  the eventual ResultCard cause field)
        """
        if getattr(status_evt, "source", "") != "postmortem":
            return None
        phase = getattr(status_evt, "phase", "")
        msg = getattr(status_evt, "message", "") or "Generating postmortem"
        # COMPLETED / FAILED → close out the spinner subject
        if phase in ("completed", "failed"):
            return StreamEvent(
                type="node_end", task_id=turn_id, node="postmortem",
                content=msg, phase="save",
            )
        # STARTED / RUNNING → keep / refresh the subject
        return StreamEvent(
            type="node_start", task_id=turn_id, node="postmortem",
            content=msg, phase="save",
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

    # Display Store sidewrite — capture all events for full session recovery.
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

    async def event_generator():
        nonlocal graph, config
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)

        stream_task = asyncio.current_task()
        task_tracker.register(turn_id, stream_task)
        turn_started_monotonic = time.monotonic()
        batcher = SSEBatcher(
            flush_interval_ms=settings.sse_batch_interval_ms,
            flush_chars=settings.sse_batch_chars,
        )
        try:
            _tsm.start_task_span(turn_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(turn_id)
            try:
                _ts = _get_tui_store()
                if _ts is not None and sid:
                    _ts.append_event(sid, {
                        "ts": now_iso(),
                        "source": "user",
                        "task_id": turn_id,
                        "event_type": "user_input",
                        "data": {"content": body.input},
                    })
            except Exception:
                pass
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
                        _sidewrite(evt)
                        for sse in batcher.feed(evt):
                            yield sse
                elif kind == "status":
                    for sse in batcher.flush():
                        yield sse
                    compaction_evt = _convert_compaction_status(payload)
                    if compaction_evt is not None:
                        _sidewrite(compaction_evt)
                        yield compaction_evt.to_sse()
                    ctx_evt = _convert_context_size_status(payload)
                    if ctx_evt is not None:
                        _sidewrite(ctx_evt)
                        yield ctx_evt.to_sse()
                    pm_evt = _convert_postmortem_status(payload)
                    if pm_evt is not None:
                        _sidewrite(pm_evt)
                        yield pm_evt.to_sse()
            for sse in batcher.flush():
                yield sse

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
                _confirm_evt = StreamEvent(
                    type="confirm",
                    content=_content_from_interrupt_payload(payload),
                    node=interrupted_node,
                    task_id=turn_id,
                    payload=payload,
                )
                _sidewrite(_confirm_evt)
                yield _confirm_evt.to_sse()

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
                        _timeout_evt = StreamEvent(
                            type="error",
                            content=f"Confirmation timed out ({minutes} min)",
                            task_id=turn_id,
                        )
                        _sidewrite(_timeout_evt)
                        yield _timeout_evt.to_sse()
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

                # plan_builder passes through raw selection ("A", "B", free text);
                # other nodes normalize to "approved"/"rejected".
                if interrupted_node == "plan_builder":
                    normalised = answer
                else:
                    normalised = _normalise_answer(answer)

                try:
                    _ts = _get_tui_store()
                    if _ts is not None and sid:
                        _ts.append_event(sid, {
                            "ts": now_iso(),
                            "source": "user",
                            "task_id": turn_id,
                            "event_type": "confirm_answer",
                            "data": {"content": normalised},
                        })
                except Exception:
                    pass

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
                            _sidewrite(evt)
                            for sse in batcher.feed(evt):
                                yield sse
                    elif kind == "status":
                        for sse in batcher.flush():
                            yield sse
                        compaction_evt = _convert_compaction_status(payload)
                        if compaction_evt is not None:
                            _sidewrite(compaction_evt)
                            yield compaction_evt.to_sse()
                        ctx_evt = _convert_context_size_status(payload)
                        if ctx_evt is not None:
                            _sidewrite(ctx_evt)
                            yield ctx_evt.to_sse()
                for sse in batcher.flush():
                    yield sse

            # 2.5 Dual-graph: if Intent Graph confirmed inject, launch Pipeline Graph
            _intent_final = await graph.aget_state(config)
            _iv = _intent_final.values if _intent_final else {}
            _confirmed = _iv.get("confirmed_intent")

            # Batch inject → launch Pipeline Graph with batch_submit_args
            if (
                _confirmed == "batch_inject"
                and _iv.get("batch_submit_args")
                and not _intent_final.next
            ):
                _p_task_id = f"task-{uuid4().hex[:12]}"
                _tui_sid = _iv.get("tui_session_id", "") or sid
                _handoff = _iv.get("handoff_summary", "")

                from langchain_core.messages import SystemMessage as _SM

                _p_config = {
                    "configurable": {"thread_id": _p_task_id},
                    "recursion_limit": settings.recursion_limit,
                }
                _p_input = {
                    "task_id": _p_task_id,
                    "tui_session_id": _tui_sid,
                    "operation": "inject",
                    "interaction_mode": "tui",
                    "kubeconfig": settings.kubeconfig_path,
                    "kube_context": settings.kube_context,
                    "batch_submit_args": _iv.get("batch_submit_args"),
                    "fault_spec": _iv.get("fault_spec"),
                    "needs_confirmation": True,
                    "messages": [_SM(content=_handoff)] if _handoff else [],
                    "created_at": now_iso(),
                    "dry_run": False,
                }

                # Stream Pipeline Graph (batch_setup → agent_loop → full pipeline per fault)
                async for kind, payload in _merged_stream(
                    pipeline_graph.astream_events(_p_input, _p_config, version="v2"),
                ):
                    if await req.is_disconnected():
                        break
                    if kind == "graph":
                        evt = parse_stream_event(payload)
                        if evt is not None:
                            evt.task_id = turn_id
                            _sidewrite(evt)
                            for sse in batcher.feed(evt):
                                yield sse
                    elif kind == "status":
                        for sse in batcher.flush():
                            yield sse
                        compaction_evt = _convert_compaction_status(payload)
                        if compaction_evt is not None:
                            _sidewrite(compaction_evt)
                            yield compaction_evt.to_sse()
                        ctx_evt = _convert_context_size_status(payload)
                        if ctx_evt is not None:
                            _sidewrite(ctx_evt)
                            yield ctx_evt.to_sse()
                for sse in batcher.flush():
                    yield sse

                # Handle interrupts (confirmation_gate for each batch fault).
                while True:
                    _bp_cur = await pipeline_graph.aget_state(_p_config)
                    if not (_bp_cur and _bp_cur.next):
                        break
                    _bp_pending = _extract_pending_interrupt(_bp_cur)
                    if _bp_pending is None:
                        break
                    _bp_node, _bp_payload = _bp_pending

                    _bp_auto = body.permission_mode != "confirm"
                    if _bp_auto and _bp_node in ("confirmation_gate", "plan_change_confirm", "tool_screener"):
                        # Auto mode: show info as token (read-only) instead of
                        # confirm (which triggers TUI waiting_confirmation state).
                        _bp_info_evt = StreamEvent(
                            type="token",
                            content=f"\n{_format_auto_approve_info(_bp_node, _bp_payload)}\n",
                            task_id=turn_id,
                        )
                        _sidewrite(_bp_info_evt)
                        yield _bp_info_evt.to_sse()
                        _bp_normalised = "approved"
                    else:
                        _bp_confirm_evt = StreamEvent(
                            type="confirm",
                            content=_content_from_interrupt_payload(_bp_payload),
                            node=_bp_node, task_id=turn_id,
                            payload=_bp_payload,
                        )
                        _sidewrite(_bp_confirm_evt)
                        yield _bp_confirm_evt.to_sse()

                        _bp_fut = store.register_interrupt(turn_id)
                        _bp_deadline = asyncio.get_event_loop().time() + settings.confirm_wait_timeout
                        _bp_answer = ""
                        _bp_timed_out = False
                        while True:
                            _bp_remaining = _bp_deadline - asyncio.get_event_loop().time()
                            if _bp_remaining <= 0:
                                store.cancel_interrupt(turn_id)
                                _bp_timed_out = True
                                break
                            _bp_slice = min(_CONFIRM_KEEPALIVE_INTERVAL_S, _bp_remaining)
                            try:
                                _bp_answer = await asyncio.wait_for(
                                    asyncio.shield(_bp_fut), timeout=_bp_slice,
                                )
                                break
                            except asyncio.TimeoutError:
                                yield ": keepalive\n\n"
                                continue
                        if _bp_timed_out:
                            break
                        if _bp_node == "plan_builder":
                            _bp_normalised = _bp_answer
                        else:
                            _bp_normalised = _normalise_answer(_bp_answer)

                    try:
                        _bp_ts = _get_tui_store()
                        if _bp_ts is not None and sid:
                            _bp_ts.append_event(sid, {
                                "ts": now_iso(), "source": "user",
                                "task_id": turn_id, "event_type": "confirm_answer",
                                "data": {"content": _bp_normalised},
                            })
                    except Exception:
                        pass

                    async for kind, payload in _merged_stream(
                        pipeline_graph.astream_events(
                            Command(resume=_bp_normalised), _p_config, version="v2",
                        ),
                    ):
                        if await req.is_disconnected():
                            break
                        if kind == "graph":
                            evt = parse_stream_event(payload)
                            if evt is not None:
                                evt.task_id = turn_id
                                _sidewrite(evt)
                                for sse in batcher.feed(evt):
                                    yield sse
                        elif kind == "status":
                            for sse in batcher.flush():
                                yield sse
                    for sse in batcher.flush():
                        yield sse

                # Batch post-processing: register task_ids + write summary
                _bp_final = await pipeline_graph.aget_state(_p_config)
                _bpv = _bp_final.values if _bp_final else {}
                _batch_results = _bpv.get("batch_results") or []

                for _br in _batch_results:
                    _br_tid = _br.get("task_id", "")
                    if _br_tid:
                        store.add_task(sid, _br_tid)
                        _br_tui = _get_tui_store()
                        if _br_tui is not None:
                            try:
                                _br_tui.add_task(sid, _br_tid)
                            except Exception:
                                pass

                # Batch summary → Intent Graph + aggregated postmortem file
                if _batch_results:
                    # 1. Aggregate per-fault postmortems into one batch report
                    _batch_pm_path_str = ""
                    try:
                        from pathlib import Path
                        _pm_dir = Path(settings.resolved_memory_dir).parent / "postmortems"
                        _pm_sections = [
                            f"# 批量故障注入分析报告\n",
                            f"共 {len(_batch_results)} 个故障\n",
                        ]
                        for _bi, _br in enumerate(_batch_results):
                            _br_tid = _br.get("task_id", "")
                            _br_ft = _br.get("fault_type", "unknown")
                            _br_ts = _br.get("task_state", "unknown")
                            _pm = _br.get("postmortem")
                            _pm_sections.append(f"---\n\n## 故障 {_bi+1}: {_br_ft} → {_br_ts}\n")
                            _pm_sections.append(f"task_id: `{_br_tid}`\n")
                            if _pm and isinstance(_pm, dict) and _pm.get("markdown"):
                                _pm_sections.append(_pm["markdown"])
                            else:
                                _pm_path = _pm_dir / f"{_br_tid}.md" if _br_tid else None
                                if _pm_path and _pm_path.exists():
                                    _pm_sections.append(_pm_path.read_text(encoding="utf-8"))
                                else:
                                    _pm_sections.append("*事后分析未生成*\n")

                        _batch_pm_file = _pm_dir / f"batch-{turn_id}.md"
                        _pm_dir.mkdir(parents=True, exist_ok=True)
                        _batch_pm_file.write_text("\n".join(_pm_sections), encoding="utf-8")
                        _batch_pm_path_str = str(_batch_pm_file)

                        _pm_evt = StreamEvent(
                            type="token",
                            content=f"\n📝 批量分析报告: {_batch_pm_path_str}\n",
                            task_id=turn_id,
                        )
                        _sidewrite(_pm_evt)
                        yield _pm_evt.to_sse()
                    except Exception:
                        logger.warning("Failed to write batch postmortem report", exc_info=True)

                    # 2. Write summary + report path to Intent Graph (single write)
                    try:
                        _bs_parts = [f"[Batch Summary] {len(_batch_results)} faults"]
                        for _bi, _br in enumerate(_batch_results):
                            _bs_ok = _br.get("task_state") in ("injected",)
                            _bs_parts.append(
                                f"  {_bi+1}. {_br.get('fault_type','')} "
                                f"→ {_br.get('task_state','unknown')} "
                                f"{'✓' if _bs_ok else '✗'} "
                                f"(task={_br.get('task_id','')})"
                            )
                        if _batch_pm_path_str:
                            _bs_parts.append(f"批量分析报告: {_batch_pm_path_str}")
                        from langchain_core.messages import SystemMessage as _BsSM
                        await intent_graph.aupdate_state(
                            {"configurable": {"thread_id": thread_id},
                             "recursion_limit": settings.recursion_limit},
                            {
                                "messages": [_BsSM(content="\n".join(_bs_parts))],
                                "batch_submit_args": None,
                            },
                            as_node="save_dialogue",
                        )
                    except Exception:
                        logger.warning("Failed to write batch summary to Intent Graph", exc_info=True)

            elif (
                _confirmed == "inject"
                and _iv.get("fault_spec")
                and not _intent_final.next
            ):
                _p_task_id = _iv.get("task_id", f"task-{uuid4().hex[:12]}")
                _handoff = _iv.get("handoff_summary", "")
                _tui_sid = _iv.get("tui_session_id", "") or sid

                from chaos_agent.agent.nodes.intent_clarification import bootstrap_task_session
                from langchain_core.messages import SystemMessage as _SM
                if _p_task_id:
                    bootstrap_task_session(
                        _p_task_id, operation="inject",
                        tui_session_id=_tui_sid,
                        handoff_message=_SM(content=_handoff) if _handoff else None,
                    )

                _p_config = {
                    "configurable": {"thread_id": _p_task_id},
                    "recursion_limit": settings.recursion_limit,
                }
                _p_input = {
                    "task_id": _p_task_id,
                    "tui_session_id": _tui_sid,
                    "operation": "inject",
                    "confirmed_intent": "inject",
                    "fault_spec": _iv.get("fault_spec"),
                    "needs_confirmation": True,
                    "interaction_mode": "tui",
                    "kubeconfig": settings.kubeconfig_path,
                    "kube_context": settings.kube_context,
                    "messages": [_SM(content=_handoff)] if _handoff else [],
                    "safety_status": "pending",
                    "created_at": now_iso(),
                    "dry_run": body.dry_run,
                }

                # Stream Pipeline Graph with merged status
                async for kind, payload in _merged_stream(
                    pipeline_graph.astream_events(_p_input, _p_config, version="v2"),
                ):
                    if await req.is_disconnected():
                        return
                    if kind == "graph":
                        evt = parse_stream_event(payload)
                        if evt is not None:
                            evt.task_id = turn_id
                            _sidewrite(evt)
                            for sse in batcher.feed(evt):
                                yield sse
                    elif kind == "status":
                        for sse in batcher.flush():
                            yield sse
                        compaction_evt = _convert_compaction_status(payload)
                        if compaction_evt is not None:
                            _sidewrite(compaction_evt)
                            yield compaction_evt.to_sse()
                        ctx_evt = _convert_context_size_status(payload)
                        if ctx_evt is not None:
                            _sidewrite(ctx_evt)
                            yield ctx_evt.to_sse()
                for sse in batcher.flush():
                    yield sse

                # Handle Pipeline Graph interrupts (confirmation_gate)
                while True:
                    _p_cur = await pipeline_graph.aget_state(_p_config)
                    if not (_p_cur and _p_cur.next):
                        break
                    _p_pending = _extract_pending_interrupt(_p_cur)
                    if _p_pending is None:
                        break
                    _p_node, _p_payload = _p_pending

                    _p_auto = body.permission_mode != "confirm"
                    if _p_auto and _p_node in ("confirmation_gate", "plan_change_confirm", "tool_screener"):
                        _p_info_evt = StreamEvent(
                            type="token",
                            content=f"\n{_format_auto_approve_info(_p_node, _p_payload)}\n",
                            task_id=turn_id,
                        )
                        _sidewrite(_p_info_evt)
                        yield _p_info_evt.to_sse()
                        _p_normalised = "approved"
                    else:
                        _p_confirm_evt = StreamEvent(
                            type="confirm",
                            content=_content_from_interrupt_payload(_p_payload),
                            node=_p_node,
                            task_id=turn_id,
                            payload=_p_payload,
                        )
                        _sidewrite(_p_confirm_evt)
                        yield _p_confirm_evt.to_sse()

                        _p_fut = store.register_interrupt(turn_id)
                        _p_deadline = asyncio.get_event_loop().time() + settings.confirm_wait_timeout
                        _p_answer = ""
                        while True:
                            _p_remaining = _p_deadline - asyncio.get_event_loop().time()
                            if _p_remaining <= 0:
                                store.cancel_interrupt(turn_id)
                                yield StreamEvent(type="error", content="Confirmation timed out", task_id=turn_id).to_sse()
                                yield StreamEvent(type="done", task_id=turn_id).to_sse()
                                return
                            try:
                                _p_answer = await asyncio.wait_for(asyncio.shield(_p_fut), timeout=min(_CONFIRM_KEEPALIVE_INTERVAL_S, _p_remaining))
                                break
                            except asyncio.TimeoutError:
                                yield ": keepalive\n\n"
                                continue

                        _p_normalised = _normalise_answer(_p_answer) if _p_node != "plan_builder" else _p_answer

                    try:
                        _ts2 = _get_tui_store()
                        if _ts2 is not None and sid:
                            _ts2.append_event(sid, {
                                "ts": now_iso(), "source": "user",
                                "task_id": turn_id, "event_type": "confirm_answer",
                                "data": {"content": _p_normalised},
                            })
                    except Exception:
                        pass

                    async for kind, payload in _merged_stream(
                        pipeline_graph.astream_events(
                            Command(resume=_p_normalised), _p_config, version="v2",
                        ),
                    ):
                        if await req.is_disconnected():
                            return
                        if kind == "graph":
                            evt = parse_stream_event(payload)
                            if evt is not None:
                                evt.task_id = turn_id
                                _sidewrite(evt)
                                for sse in batcher.feed(evt):
                                    yield sse
                        elif kind == "status":
                            for sse in batcher.flush():
                                yield sse
                    for sse in batcher.flush():
                        yield sse

                # Dry-run (plan_builder path): skip result extraction
                if body.dry_run:
                    pass  # plan preview already streamed as tokens
                else:
                    # Swap graph/config for result extraction below
                    graph = pipeline_graph
                    config = _p_config
                    store.add_task(sid, _p_task_id)

                    # Write task summary back to Intent Graph
                    try:
                        _pfinal_for_summary = await pipeline_graph.aget_state(_p_config)
                        _psv = _pfinal_for_summary.values if _pfinal_for_summary else {}
                        _p_ts = infer_task_state(_psv) if _psv else "unknown"
                        from chaos_agent.agent.fault_spec import read_fault_spec as _rfs
                        from chaos_agent.agent.state import extract_ui_diagnostics as _ext_diag
                        from chaos_agent.memory.session_store import build_verification_simple as _bvs
                        _p_spec = _rfs(_psv) if _psv else None
                        _p_ft = (_p_spec.fault_type if _p_spec and _p_spec.fault_type else _psv.get("skill_name", ""))
                        _p_ns = _p_spec.namespace if _p_spec else ""
                        _p_names = ", ".join(_p_spec.names) if _p_spec and _p_spec.names else ""
                        _p_uid = _psv.get("blade_uid", "")
                        _p_verif = _psv.get("verification")
                        _p_vs = _bvs(_p_verif) if _p_verif else None
                        _p_diag = _ext_diag(_psv)

                        _p_parts = [
                            f"[Task Summary] task_id={_p_task_id}",
                            f"类型: {_p_ft} | 目标: {_p_ns}/{_p_names}",
                            f"结果: {_p_ts} | blade_uid: {_p_uid}",
                        ]
                        if _p_vs:
                            _p_parts.append(f"验证: {_p_vs.get('level','?')} (L1={_p_vs.get('layer1',{}).get('status','?')}, L2={_p_vs.get('layer2',{}).get('status','?')})")
                        if _p_diag.get("side_effects_summary"):
                            _p_parts.append(f"副作用: {_p_diag['side_effects_summary']}")
                        if _p_diag.get("failure_reason"):
                            _p_parts.append(f"失败原因: {_p_diag['failure_reason']}")
                        from langchain_core.messages import SystemMessage as _SummarySM
                        _summary_text = "\n".join(_p_parts)
                        await intent_graph.aupdate_state(
                            {"configurable": {"thread_id": thread_id}, "recursion_limit": settings.recursion_limit},
                            {"messages": [_SummarySM(content=_summary_text)], "pipeline_task_id": _p_task_id},
                            as_node="save_dialogue",
                        )
                        _tui_s = _get_tui_store()
                        if _tui_s and sid:
                            _tui_s.append_dialogue(sid, [_SummarySM(content=_summary_text)])
                    except Exception:
                        logger.debug("Failed to write task summary to Intent Graph", exc_info=True)

            # 2.6 Auto-recover: when Intent Graph classified recover intent
            _recover_final = await graph.aget_state(config)
            _rv = _recover_final.values if _recover_final else {}
            _recover_inject_tid = _rv.get("recover_task_id", "")
            if (
                _rv.get("confirmed_intent") == "recover"
                and _recover_inject_tid
                and not _recover_final.next  # not paused at interrupt
            ):
                recover_graph = agents.get("recover")
                if recover_graph is not None:
                    # Resolve inject state for the target experiment.
                    # Priority 1: aget_state by task_id — authoritative for
                    # CLI-originated experiments (thread_id = task_id).
                    # Priority 2: current conversation state (_rv) — for
                    # same-session TUI recovery where the inject checkpoint
                    # lives under conversation_thread_id, not task_id.
                    sv = None
                    _inj_config = {
                        "configurable": {"thread_id": _recover_inject_tid},
                        "recursion_limit": settings.recursion_limit,
                    }
                    try:
                        _inj_state = await agents["pipeline"].aget_state(_inj_config)
                    except Exception:
                        _inj_state = None
                    if _inj_state and _inj_state.values and _inj_state.values.get("blade_uid"):
                        sv = _inj_state.values
                    elif _rv.get("blade_uid"):
                        sv = _rv
                    if sv:
                        from chaos_agent.utils.inject_context import build_inject_context

                        _rec_task_id = _rv.get("task_id", f"task-{uuid4().hex[:12]}")
                        recover_initial = {
                            "task_id": _rec_task_id,
                            "tui_session_id": sv.get("tui_session_id", ""),
                            "parent_task_id": _recover_inject_tid,
                            "operation": "recover",
                            "blade_uid": sv.get("blade_uid", ""),
                            "skill_name": sv.get("skill_name", ""),
                            "skill_case_content": sv.get("skill_case_content", ""),
                            "inject_verification_summary": sv.get("inject_verification_summary", ""),
                            "inject_context": build_inject_context(sv.get("messages", [])),
                            "fault_spec": sv.get("fault_spec"),
                            "kubeconfig": sv.get("kubeconfig", ""),
                            "injection_method": sv.get("injection_method"),
                            "kubectl_exec_pod_name": sv.get("kubectl_exec_pod_name"),
                            "created_at": sv.get("created_at", ""),
                            "verifier_loop_count": 0,
                            "verification": None,
                            "recover_verification": None,
                            "messages": [],
                            "inject_layer1_cache": None,
                            "recover_layer1_cache": None,
                            "error": None,
                            "failure_reason": None,
                            "failure_detail": None,
                        }
                        recover_config = {
                            "configurable": {"thread_id": _rec_task_id},
                            "recursion_limit": settings.recursion_limit,
                        }

                        # Stream recover_graph events
                        async for kind, payload in _merged_stream(
                            recover_graph.astream_events(
                                recover_initial, recover_config, version="v2"
                            ),
                        ):
                            if await req.is_disconnected():
                                return
                            if kind == "graph":
                                evt = parse_stream_event(payload)
                                if evt is not None:
                                    evt.task_id = turn_id
                                    _sidewrite(evt, source="recover")
                                    for sse in batcher.feed(evt):
                                        yield sse
                            elif kind == "status":
                                for sse in batcher.flush():
                                    yield sse
                        for sse in batcher.flush():
                            yield sse

                        # Emit recover result card
                        _rec_result = await _build_recover_result_payload(
                            recover_graph, recover_config,
                            _rec_task_id, _recover_inject_tid,
                            sv, turn_started_monotonic,
                        )
                        if _rec_result is not None:
                            store.add_task(sid, _rec_task_id)
                            from chaos_agent.memory.tui_session_store import (
                                get_global_tui_session_store,
                            )
                            _tui_store = get_global_tui_session_store()
                            if _tui_store is not None:
                                try:
                                    _tui_store.add_task(sid, _rec_task_id)
                                except Exception:
                                    logger.warning(
                                        "recover task_id disk persist failed "
                                        "sid=%s task=%s", sid, _rec_task_id,
                                    )
                            _rec_evt = StreamEvent(
                                type="result",
                                content=json.dumps(_rec_result, ensure_ascii=False),
                                task_id=turn_id,
                            )
                            _sidewrite(_rec_evt, source="recover")
                            yield _rec_evt.to_sse()
                    else:
                        # Priority 3: rebuild from task_store (cross-session TUI)
                        _rec_task_id = _rv.get("task_id", f"task-{uuid4().hex[:12]}")
                        recover_initial = await _build_recover_initial_from_store(
                            _recover_inject_tid, _rec_task_id, sid, agents,
                        )
                        if recover_initial is not None:
                            recover_config = {
                                "configurable": {"thread_id": _rec_task_id},
                                "recursion_limit": settings.recursion_limit,
                            }
                            async for kind, payload in _merged_stream(
                                recover_graph.astream_events(
                                    recover_initial, recover_config, version="v2"
                                ),
                            ):
                                if await req.is_disconnected():
                                    return
                                if kind == "graph":
                                    evt = parse_stream_event(payload)
                                    if evt is not None:
                                        evt.task_id = turn_id
                                        _sidewrite(evt, source="recover")
                                        for sse in batcher.feed(evt):
                                            yield sse
                                elif kind == "status":
                                    for sse in batcher.flush():
                                        yield sse
                            for sse in batcher.flush():
                                yield sse

                            _rec_result = await _build_recover_result_payload(
                                recover_graph, recover_config,
                                _rec_task_id, _recover_inject_tid,
                                recover_initial, turn_started_monotonic,
                            )
                            if _rec_result is not None:
                                store.add_task(sid, _rec_task_id)
                                from chaos_agent.memory.tui_session_store import (
                                    get_global_tui_session_store,
                                )
                                _tui_store = get_global_tui_session_store()
                                if _tui_store is not None:
                                    try:
                                        _tui_store.add_task(sid, _rec_task_id)
                                    except Exception:
                                        logger.warning(
                                            "recover task_id disk persist failed "
                                            "sid=%s task=%s", sid, _rec_task_id,
                                        )
                                _rec_evt2 = StreamEvent(
                                    type="result",
                                    content=json.dumps(_rec_result, ensure_ascii=False),
                                    task_id=turn_id,
                                )
                                _sidewrite(_rec_evt2, source="recover")
                                yield _rec_evt2.to_sse()
                        else:
                            logger.warning(
                                "Auto-recover: no inject state found for %s",
                                _recover_inject_tid,
                            )
                            _no_state_evt = StreamEvent(
                                type="error",
                                content=f"无法找到实验 {_recover_inject_tid} 的注入状态，恢复已跳过。",
                                task_id=turn_id,
                            )
                            _sidewrite(_no_state_evt)
                            yield _no_state_evt.to_sse()

            # 3. Final-state extraction → ``result`` envelope.
            #    Mirrors inject_stream.py's terminal-state shape so the
            #    TS client's ResultCard parser sees the same data
            #    regardless of which endpoint started the turn. The
            #    ``task_id`` carried in the result payload is read
            #    from final_state.values — i.e. it's the ``task-<hex>``
            #    that the inject/recover pipeline allocated for itself
            #    (in intent_clarification on the inject/recover
            #    transition). Turn.py never invents a ``task-`` id.
            result_payload = None if body.dry_run else await _build_result_payload(
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
                _result_evt = StreamEvent(
                    type="result",
                    content=json.dumps(result_payload, ensure_ascii=False),
                    task_id=turn_id,
                )
                _sidewrite(_result_evt)
                yield _result_evt.to_sse()

            # 4. Done terminator.
            yield StreamEvent(type="done", task_id=turn_id).to_sse()

        except asyncio.CancelledError:
            logger.info(f"Turn {turn_id} cancelled by client")
            _cancel_evt = StreamEvent(
                type="error", content="Turn cancelled", task_id=turn_id,
            )
            _sidewrite(_cancel_evt)
            yield _cancel_evt.to_sse()
            yield StreamEvent(type="done", task_id=turn_id).to_sse()
            raise
        except Exception as e:
            logger.exception(f"Turn failed for {turn_id}")
            _exc_evt = StreamEvent(
                type="error",
                content=f"{type(e).__name__}: {e}",
                task_id=turn_id,
            )
            _sidewrite(_exc_evt)
            yield _exc_evt.to_sse()
            yield StreamEvent(type="done", task_id=turn_id).to_sse()
        finally:
            _tsm.end_task_span(turn_id)
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
    # card: only ``inject`` is a user-initiated operation with outcomes
    # the user wants to see in the inject graph. Everything else
    # (``chat``, ``unset`` mid-clarification, missing intent for a
    # pure-text reply) is conversational; the agent's text reply IS
    # the result and showing a green "Injection succeeded" card on
    # top would mislead.
    #
    # ``recover`` in inject_graph is a BRIDGE state — intent was
    # classified but the actual recover pipeline runs separately
    # (recover_graph launched by TUI ConversationController). The
    # state still carries residual blade_uid/verification/postmortem
    # from the previous inject turn (checkpoint inheritance), so
    # emitting a result card here would re-display the old inject's
    # failure card. Suppress it.
    #
    # Why not the previous heuristic that also checked for empty
    # blade_uid/plan_summary: that incorrectly skipped FAILED inject
    # turns (intent="inject", but no blade_uid/plan_summary because
    # execution didn't make it that far). Failed injects deserve a
    # card showing the failure, not silence.
    if confirmed_intent != "inject":
        return None

    # Inject turn — full payload.
    task_state = infer_task_state(values)
    if task_state == "injecting":
        task_state = "injected" if blade_uid else "failed"

    skill_name = values.get("skill_name", "") or ""
    # Project fault_spec to legacy shapes used by the response envelope.
    from chaos_agent.agent.fault_spec import (
        legacy_params_dict, legacy_target_dict, read_fault_spec,
    )
    spec = read_fault_spec(values)
    params = legacy_params_dict(values)
    fault_type = ""
    if spec and spec.fault_type:
        fault_type = spec.fault_type
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
            "target": legacy_target_dict(values),
            # Same rationale for params — what we actually executed
            # with, surfaced for confirm-trail audit.
            "params": params,
            "verification": strip_side_effects(values.get("verification")),
            "side_effects": values.get("verification", {}).get("side_effects")
            if isinstance(values.get("verification"), dict)
            else None,
            # T6 — postmortem payload from save_memory (None when not
            # generated: disabled / non-inject intent / non-whitelist
            # failure / LLM timeout). TS TUI tolerates absence.
            "postmortem": values.get("postmortem"),
            **diagnostics,
        },
    }


async def _build_recover_result_payload(
    recover_graph,
    recover_config: dict,
    recover_task_id: str,
    inject_task_id: str,
    inject_state_values: dict,
    started_monotonic: float,
) -> dict | None:
    """Build a result card payload for recover_graph completion."""
    try:
        final = await recover_graph.aget_state(recover_config)
    except Exception:
        return None
    if not final or not final.values:
        return None

    values = final.values
    elapsed_ms = int((time.monotonic() - started_monotonic) * 1000)

    is_recovered = False
    recovery_level = "failed"
    result_dict = values.get("result")
    if isinstance(result_dict, dict):
        is_recovered = result_dict.get("recovered", False)
        recovery_level = result_dict.get("recovery_level", "recovered" if is_recovered else "failed")

    task_state = recovery_level if is_recovered else "failed"

    blade_uid = inject_state_values.get("blade_uid", "")
    skill_name = inject_state_values.get("skill_name", "")
    from chaos_agent.agent.fault_spec import legacy_target_dict
    target = legacy_target_dict(inject_state_values)

    return {
        "status": "success",
        "data": {
            "task_id": recover_task_id,
            "task_state": task_state,
            "fault_type": skill_name,
            "blade_uid": blade_uid,
            "duration_ms": elapsed_ms,
            "target": target,
            "params": {},
            "verification": strip_side_effects(values.get("recover_verification")),
        },
    }
