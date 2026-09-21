"""POST /api/v1/inject-stream - SSE streaming inject endpoint."""

import anyio
import asyncio
import json
import logging

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.agent.spec.fault_spec import DurationParamError, FaultSpec
from chaos_agent.agent.state_mgmt.state_builders import build_inject_initial_state
from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_DATA_ENVELOPE,
    finalize_inject_session,
)
from chaos_agent.persistence.task_identity import new_inject_task_id
from chaos_agent.models.schemas import JSONEnvelope
from chaos_agent.server.routes import inject_router
from chaos_agent.server.routes.stream_abort import (
    ABORT_SQLITE_CEILING_S,
    ClientDisconnected,
    abort_row_word,
    write_aborted_task_row,
)
from chaos_agent.server.schemas import InjectRequest
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


# Round-52: abort-cleanup ceiling for the inject stream's rollback arm.
# auto_rollback dispatches blade-destroy subprocesses (vehicle-class
# cleanup, the same hang surface as the turn stream's kubectl-delete
# artifact cleanup — round-49), NOT millisecond SQLite writes — so the
# shield that carries it through a scope cancel needs the same bounded
# abandon instead of holding the task group hostage on a pathological
# hang.
_ROLLBACK_CEILING_S = 90.0


@inject_router.post("/inject-stream")
async def inject_stream(request: InjectRequest, req: Request):
    """Inject a fault with real-time SSE streaming.

    Returns a Server-Sent Events stream with events:
    - token: LLM output tokens
    - tool_start/tool_end: Tool invocations
    - confirm: Paused at confirmation gate
    - result: Final result envelope
    - error: Error message
    """
    task_id = new_inject_task_id()
    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker

    # Check if server is shutting down
    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([StreamEvent(type="error", content="Server is shutting down", task_id=task_id).to_sse()]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # First-run gate — see inject.py for the rationale.
    if agents is None:
        return StreamingResponse(
            iter([StreamEvent(
                type="error",
                content="LLM config missing; run the setup wizard first.",
                task_id=task_id,
            ).to_sse()]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )

    # Runtime override
    if request.kubeconfig:
        settings.kubeconfig_path = request.kubeconfig
    if request.context:
        settings.kube_context = request.context

    # Build initial state — FaultSpec is the single source of truth.
    try:
        spec = FaultSpec.from_http_request(request)
    except DurationParamError as e:
        # Duration contract violation is a client input error — emit one SSE
        # error event instead of an unhandled 500 mid-stream setup.
        return StreamingResponse(
            iter([StreamEvent(type="error", content=str(e), task_id=task_id).to_sse()]),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
    initial_state = build_inject_initial_state(
        task_id=task_id,
        fault_spec=spec,
        needs_confirmation=request.confirm,
        kubeconfig=request.kubeconfig or settings.kubeconfig_path,
        kube_context=request.context or settings.kube_context,
        kubewiz_cluster_uuid=getattr(request, "cluster_uuid", "") or settings.kubewiz_cluster_uuid,
        kubewiz_profile=getattr(request, "profile", "") or settings.kubewiz_profile,
        kube_connection_mode=getattr(request, "kube_connection_mode", "") or settings.kube_connection_mode,
        host_name=getattr(request, "host_name", "") or getattr(settings, "host_name", ""),
        ssh_host=getattr(request, "ssh_host", "") or getattr(settings, "ssh_host", ""),
        ssh_user=getattr(request, "ssh_user", "") or getattr(settings, "ssh_user", ""),
        ssh_key_path=getattr(request, "ssh_key_path", "") or getattr(settings, "ssh_key_path", ""),
        ssh_port=getattr(request, "ssh_port", None) or getattr(settings, "ssh_port", None),
    )

    config = {"configurable": {"thread_id": task_id}, "recursion_limit": settings.recursion_limit}
    graph = agents["pipeline"]

    # Create session for recording
    session_store = agents.get("session_store")
    if session_store:
        session_store.create_session(task_id, operation="inject")

    async def event_generator():
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        from chaos_agent.cli.session_finalize import auto_rollback
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)

        # Register with task tracker for graceful shutdown
        stream_task = asyncio.current_task()
        task_tracker.register(task_id, stream_task)
        batcher = SSEBatcher(
            flush_interval_ms=settings.sse_batch_interval_ms,
            flush_chars=settings.sse_batch_chars,
        )
        # Set on the CancelledError / ClientDisconnected exits (round-53,
        # widened round-54): the finally block's terminal writes key on
        # it — a KNOWN "cancelled" terminal state, not the inference
        # finalize would otherwise apply to an unknown state (the turn
        # stream's same triad: flag → TaskStore row → finalize
        # status_override). Round-54 G1: the POLL-detected disconnect
        # sets the SAME flag — which semantics a disconnect gets must
        # not be decided by the poll-vs-cancel race (r48: the cancel
        # usually wins, but the poll wins sometimes).
        _stream_cancelled = False
        # Set on the internal-error exit (round-54 G4): the crash path's
        # terminal word. The r53 triad was wired for the cancel exits
        # only — a crashed run's TaskStore row stayed a zombie at its
        # last mid-graph upsert ("injecting" forever in /tasks; no
        # later writer comes on the abort path).
        _stream_failed = False

        async def _abort_inject_stream_cleanup() -> str:
            """Single-source abort cleanup for this inject stream (round-52).

            The inject twin of the turn/recover single sources. A real
            client disconnect cancels the StreamingResponse task-group SCOPE
            (level-based cancellation), and this stream's error path carried
            the round-48 defect family unswept: a bare ``await
            auto_rollback(graph, config)`` in the except handler — the
            orphan-fault safety net (blade-family UIDs destroyed by kind)
            died at its first suspension exactly when it mattered, leaving
            committed faults live in the cluster with no one left to roll
            them back.

            Decision table:
              shield  — always: the whole body (the only form that carries
                        an await chain through level-based cancellation).
              ceiling — 90s: auto_rollback is vehicle-class (blade-destroy
                        subprocesses behind a live-liability sweep), the same
                        hang surface that earned the turn stream's cancel
                        exit its ceiling (round-49). A hit abandons the
                        rollback with a loud warning — bounded loss — the
                        explicit recover graph remains the fallback.
            """
            with anyio.CancelScope(shield=True):
                try:
                    with anyio.fail_after(_ROLLBACK_CEILING_S):
                        return await auto_rollback(graph, config)
                except TimeoutError:
                    logger.warning(
                        "Inject stream %s rollback exceeded %.0fs ceiling; "
                        "orphan-fault rollback abandoned (recover graph "
                        "remains the fallback)",
                        task_id, _ROLLBACK_CEILING_S,
                    )
                    return ""

        try:
            _tsm.start_task_span(task_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(task_id)
            # Stream first invoke
            async for raw_event in graph.astream_events(initial_state, config, version="v2"):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected, aborting stream for task {task_id}")
                    # Round-54 G1: the poll is an ABORT EXIT, not a silent
                    # break. Before, it fell through to the normal
                    # completion path — no cancelled flag, no row write,
                    # no override — and kept driving the unattended
                    # auto-approve resume below with a dead client. The
                    # ClientDisconnected handler gives the poll-loser path
                    # the same terminal semantics the scope-cancel exit
                    # has; which one a disconnect gets must not be decided
                    # by the race (r48: the cancel usually wins).
                    raise ClientDisconnected()
                stream_evt = parse_stream_event(raw_event)
                if stream_evt is not None:
                    stream_evt.task_id = task_id
                    for sse in batcher.feed(stream_evt):
                        yield sse
            for sse in batcher.flush():
                yield sse

            # Check if paused at confirmation_gate
            current_state = await graph.aget_state(config)
            if current_state and current_state.next:
                next_nodes = list(current_state.next)
                if "confirmation_gate" in next_nodes:
                    plan_summary = ""
                    interrupt_payload = None
                    if current_state.values:
                        plan_summary = current_state.values.get("plan_summary", "")
                    for t in (current_state.tasks or []):
                        if getattr(t, "interrupts", None):
                            interrupt_payload = t.interrupts[0].value
                            break
                    # The confirm event carries the FULL interrupt payload
                    # (mechanism_writes entries verbatim when the case
                    # contract is widened) so interactive clients render
                    # what the case legislated, not just the plan prose.
                    yield StreamEvent(
                        type="confirm",
                        content=plan_summary,
                        node="confirmation_gate",
                        task_id=task_id,
                        payload=interrupt_payload,
                    ).to_sse()

                    if not request.confirm:
                        # Unattended auto-approve decides through the shared
                        # boundary helper (AUTO delegation: always
                        # "approved" — the manifest is the authority, the
                        # guard enforces the per-name boundary). Delegating
                        # a WIDENED contract is an auditable event: emit
                        # ``auto_approved`` with the full payload so the
                        # manifest entries ride the stream verbatim — the
                        # same event the CLI streaming path emits.
                        from langgraph.types import Command
                        from chaos_agent.agent.nodes.gates._write_set_boundary import (
                            unattended_resume_value,
                            widened_auto_approval_payload,
                        )

                        interrupt_info = None
                        for t in (current_state.tasks or []):
                            if getattr(t, "interrupts", None):
                                interrupt_info = t.interrupts[0].value
                                break

                        _widened = widened_auto_approval_payload(interrupt_info)
                        if _widened is not None:
                            for sse in batcher.feed(StreamEvent(
                                type="auto_approved",
                                content=(
                                    "[Auto-approved: confirmation_gate] "
                                    "widened write-set contract "
                                    "(case manifest mechanism_writes) — "
                                    "see payload for the entries"
                                ),
                                node="confirmation_gate",
                                task_id=task_id,
                                payload=_widened,
                            )):
                                yield sse
                            for sse in batcher.flush():
                                yield sse

                        async for raw_event in graph.astream_events(
                            Command(resume=unattended_resume_value(interrupt_info)), config, version="v2"
                        ):
                            if await req.is_disconnected():
                                logger.info(f"Client disconnected during confirm flow for task {task_id}")
                                # Round-54 G1: same ruling as the first poll
                                # — the confirm-resume loop runs with a dead
                                # client otherwise, and its break used to
                                # fall into result extraction on the dirty
                                # half-run state.
                                raise ClientDisconnected()
                            stream_evt = parse_stream_event(raw_event)
                            if stream_evt is not None:
                                stream_evt.task_id = task_id
                                for sse in batcher.feed(stream_evt):
                                    yield sse
                        for sse in batcher.flush():
                            yield sse

            # Extract final result
            final_state = await graph.aget_state(config)
            if final_state and final_state.values:
                values = final_state.values

                # Non-injection intent completed via intent_clarification (TUI mode)
                confirmed_intent = values.get("confirmed_intent")
                if confirmed_intent in ("chat", "recover"):
                    yield StreamEvent(
                        type="result",
                        content=json.dumps(JSONEnvelope.ok(
                            data={
                                "task_id": task_id,
                                "result": "completed",
                                "confirmed_intent": confirmed_intent,
                            },
                            request_id=getattr(req.state, "request_id", ""),
                        ), ensure_ascii=False),
                        task_id=task_id,
                    ).to_sse()
                    return

                # Fault injection result. ``snapshot=final_state`` makes the
                # engine the authority on pause (round-64 F3): with
                # ``confirm=true`` this route emits the ``confirm`` event
                # above and leaves the graph PARKED, so the projection must
                # read ``waiting_input``, not the fail-closed ``failed`` the
                # values-only path produced. ``needs_confirm`` /
                # ``plan_summary`` now ride the single-source projection —
                # the hand-patched copies this route used to add after the
                # call are gone (they were one of the six divergent shapes).
                from chaos_agent.agent.result.operation_result import build_inject_data_from_state
                _data = build_inject_data_from_state(values, task_id, snapshot=final_state)
                _data["created_at"] = now_iso()

                yield StreamEvent(
                    type="result",
                    content=json.dumps(JSONEnvelope.ok(
                        data=_data,
                        request_id=getattr(req.state, "request_id", ""),
                    ), ensure_ascii=False),
                    task_id=task_id,
                ).to_sse()
            else:
                yield StreamEvent(
                    type="error",
                    content="Graph completed but no state available",
                    task_id=task_id,
                ).to_sse()

        except asyncio.CancelledError:
            # Round-53: the disconnect exit now carries terminal semantics.
            # Starlette serves this generator inside an anyio task group, so
            # a real client disconnect arrives HERE as a scope-level
            # cancellation — before round-53 it fell straight through to the
            # finally, leaving the session status to finalize's inference
            # (an unreadable state mapped to "completed" — the lie P-B
            # closes — and a half-run one to "failed" without the cancelled
            # semantics) and the TaskStore row a zombie at its last mid-graph
            # upsert (the turn aborts write update_task_state("cancelled")).
            # NO rollback on this exit: "crash rolls, interrupt self-expires"
            # is the project-wide stance (every auto_rollback call site sits
            # on an except-Exception path; the fault's own --timeout plus the
            # recover graph are the interrupt-side safety net — probe r53 D).
            _stream_cancelled = True
            logger.info(f"Inject stream cancelled for task {task_id}")
            raise
        except ClientDisconnected:
            # Round-54 G1: the poll-detected disconnect — the exit the
            # poll-vs-cancel RACE decides (r48: the scope cancel usually
            # wins a real disconnect, but the poll wins server-side
            # cancels and fast/proxied disconnects). Same terminal
            # semantics as the cancel exit above (flag → row → finalize
            # override), same no-rollback stance (interrupt, not crash).
            _stream_cancelled = True
            logger.info(f"Client disconnected, aborting inject stream for task {task_id}")
            return
        except Exception as e:
            logger.exception(f"Stream inject failed for task {task_id}")
            _stream_failed = True

            # Auto-rollback: dispatched by fault-handle kind through the
            # provider registry (blade-family UIDs are destroyed; UID-less
            # native carriers decline — their safety net is the injection's
            # own ``--timeout``/``--runtime`` self-termination plus the
            # explicit recover graph (LLM reads the skill-case reverse
            # command)). Shared seam with the CLI runner — the single tested
            # implementation lives in cli/session_finalize.py.
            # Single-source shielded cleanup since round-52: this handler
            # runs inside the Starlette task group, and a disconnect landing
            # mid-rollback (error plus disconnect) used to kill the rollback
            # at its first suspension — orphaning committed faults.
            rollback_info = await _abort_inject_stream_cleanup()

            yield StreamEvent(
                type="error",
                content=f"Inject failed: {type(e).__name__}: {e}{rollback_info}",
                task_id=task_id,
            ).to_sse()
        finally:
            _tsm.end_task_span(task_id)
            # Finalize session: flush remaining messages from final graph state.
            # Shielded for the same reason as the turn/recover terminal finalize
            # (round-48 site 4): under a real client disconnect the scope
            # cancellation is level-based and a bare await here dies at its
            # first suspension — leaving this inject session row stuck active
            # forever (the finalize is the ONLY writer of the terminal status
            # on the abort path). Ceiling since round-54 (G7/F5): the old
            # no-ceiling ruling was checkpointer-shaped (AsyncSqliteSaver,
            # verified r53) and this chain carries aget_state — a checkpointer
            # READ — so a future remote checkpointer re-opens the r49 hang
            # surface; the SQLite-class bound is generous (30s ≫ any local
            # write) and a hit is bounded abandon, the vehicle-class ceiling's
            # same trade.
            with anyio.CancelScope(shield=True):
                try:
                    with anyio.fail_after(ABORT_SQLITE_CEILING_S):
                        if _stream_cancelled or _stream_failed:
                            # Terminal write for the TaskStore row, BEFORE the
                            # session finalize (the turn stream's own ordering
                            # rationale: the row is the user-facing fact —
                            # /tasks, the audit record — the session record is
                            # derived, and a failure in the latter must not
                            # swallow the former). No later writer comes on
                            # the abort path, and inference cannot derive the
                            # abort word on its own. Guarded (G6): a row that
                            # already reached its OWN terminal word (the
                            # pipeline completed before the abort raced in)
                            # keeps its verdict — "completed" must not be
                            # rewritten to "cancelled"/"failed".
                            # Round-56: the exit flags encode the cause CLASS
                            # (both interrupt exits set _stream_cancelled;
                            # only the crash handler sets _stream_failed), so
                            # the representative cause routes the word
                            # through the SHARED taxonomy — the inline
                            # flag→word conditional this site used to carry
                            # was the r54 G4 defect shape, flag edition.
                            await write_aborted_task_row(
                                task_id,
                                abort_row_word(
                                    "user_cancel" if _stream_cancelled
                                    else "internal_error",
                                ),
                            )
                        await finalize_inject_session(
                            session_store,
                            graph,
                            config,
                            task_id,
                            result_summary_mode=RESULT_SUMMARY_DATA_ENVELOPE,
                            # Round-57 F2': the word VALUE comes from the
                            # shared taxonomy (the flag encodes the cause
                            # CLASS — both interrupt exits set it). None on
                            # the other exits stays deliberate, for a
                            # stronger reason than the original one: the
                            # crash arm's inference path is itself
                            # fail-closed (build_inject_data_from_state →
                            # terminal_task_state maps a verdict-less run to
                            # "failed" — the same word the classified write
                            # would land), and a reached verdict keeps
                            # itself either way (round-62 P8's gate inside
                            # the finalizer now backstops the flag arm;
                            # r63 re-audit confirmed all three crash cells
                            # — verdict / mid-flight / empty state — map
                            # identically under both arms, so the None arm
                            # stays with zero behavioral delta).
                            # Round-62 R62-1/P8: the flag arm is ALSO gated
                            # now — no longer by this call site but inside
                            # finalize_inject_session itself (the single-
                            # source G6 on the session surface): a run that
                            # reached its own verdict keeps it even when the
                            # interrupt raced in during result extraction,
                            # so the override passed here only lands when
                            # the graph is genuinely mid-flight.
                            status_override=(
                                abort_row_word("user_cancel")
                                if _stream_cancelled
                                else None
                            ),
                        )
                except TimeoutError:
                    logger.warning(
                        "Inject stream %s terminal finalize exceeded %.0fs "
                        "ceiling; remaining terminal writes abandoned",
                        task_id, ABORT_SQLITE_CEILING_S,
                    )
            task_tracker.unregister(task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
