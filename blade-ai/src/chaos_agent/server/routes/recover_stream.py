"""POST /api/v1/recover-stream - SSE streaming recover endpoint."""

import anyio
import asyncio
import json
import logging
import time

from fastapi import Request
from fastapi.responses import StreamingResponse

from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_event
from chaos_agent.config.settings import settings
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_RECOVER_PAYLOAD,
    finalize_recover_session,
)
from chaos_agent.persistence.task_identity import new_recover_task_id
from chaos_agent.server.routes import recover_router
from chaos_agent.server.routes.recover_common import (
    RecoverSetupError,
    build_recover_initial_state,
)
from chaos_agent.server.routes.stream_abort import (
    ABORT_SQLITE_CEILING_S,
    ClientDisconnected,
    abort_row_word,
    write_aborted_task_row,
)
from chaos_agent.server.routes.turn_result import build_recover_result_payload
from chaos_agent.server.schemas import RecoverRequest

logger = logging.getLogger(__name__)


@recover_router.post("/recover-stream")
async def recover_stream(request: RecoverRequest, req: Request):
    """Recover a fault with real-time SSE streaming.

    Returns a Server-Sent Events stream with events:
    - token: LLM output tokens (recover verifier reasoning)
    - thinking: LLM thinking tokens
    - tool_start/tool_end: Tool invocations (kubectl, blade_status)
    - node_start/node_end: Graph node transitions
    - result: Final result envelope
    - error: Error message
    - done: Stream complete sentinel
    """
    inject_task_id = request.task_id
    record_task_id = new_recover_task_id()
    agents = req.app.state.agents
    task_tracker = req.app.state.task_tracker
    req_id = getattr(req.state, "request_id", "")

    sse_headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }

    if task_tracker.is_shutting_down:
        return StreamingResponse(
            iter([
                StreamEvent(type="error", content="Server is shutting down", task_id=record_task_id).to_sse(),
                StreamEvent(type="done", task_id=record_task_id).to_sse(),
            ]),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    if agents is None:
        return StreamingResponse(
            iter([
                StreamEvent(type="error", content="LLM config missing; run the setup wizard first.", task_id=record_task_id).to_sse(),
                StreamEvent(type="done", task_id=record_task_id).to_sse(),
            ]),
            media_type="text/event-stream",
            headers=sse_headers,
        )

    async def event_generator():
        from chaos_agent.observability.otel_genai import get_task_span_manager
        from chaos_agent.observability import status_tracker as _st_mod
        _tsm = get_task_span_manager()
        _otel_cb = getattr(_st_mod, "_otel_callback", None)

        stream_task = asyncio.current_task()
        task_tracker.register(record_task_id, stream_task)
        batcher = SSEBatcher(
            flush_interval_ms=settings.sse_batch_interval_ms,
            flush_chars=settings.sse_batch_chars,
        )
        session_store = None
        state_values = {}
        recover_config = None
        recover_graph = None
        # Declared up front: an interruption can happen before the assignment
        # below, and the interruption record needs it to find the TUI session.
        inject_tui_session_id = ""
        # One record per recovery: a failure after the outcome record landed must
        # not append a contradicting interruption note on top of it.
        record_written = False
        # The abort-cause memo (round-57 F1'): the finally fallback's
        # session finalize runs for EVERY abort cause (the cleanup above
        # never finalizes the session), and its word must be classified —
        # the turn twin's own G5 lesson: a hard-coded word on a
        # every-cause path mis-words every cause it was not written for.
        abort_cause = ""
        started_monotonic = time.monotonic()

        async def _write_recover_interrupted(cause: str, error_detail: str = "") -> None:
            """Mirror an interruption record for this recovery to the intent graph.

            Recovery is the operation whose interruption matters most: the fault
            is still live by definition until recovery finishes, and the
            context-isolated intent graph would otherwise learn nothing. Reads
            the recover graph's ledger, so it reflects however far recovery got.
            """
            if record_written:
                return
            try:
                from chaos_agent.agent.result.operation_summary import (
                    build_interrupted_record,
                )
                from chaos_agent.memory.operation_summary_writer import (
                    write_operation_summary,
                )
                from chaos_agent.server.routes.sessions import (
                    get_store as get_tui_session_store,
                )

                values = {}
                if recover_graph is not None and recover_config is not None:
                    snapshot = await recover_graph.aget_state(recover_config)
                    if snapshot and snapshot.values:
                        values = snapshot.values

                index_store = get_tui_session_store()
                meta = index_store.get(inject_tui_session_id) if inject_tui_session_id else None
                thread_id = (
                    meta.get("conversation_thread_id") if isinstance(meta, dict) else ""
                )
                await write_operation_summary(
                    build_interrupted_record(
                        values, record_task_id, cause=cause, error_detail=error_detail,
                    ),
                    intent_graph=agents.get("intent") if isinstance(agents, dict) else None,
                    thread_id=thread_id,
                    tui_session_id=inject_tui_session_id,
                    session_index_store=index_store,
                    task_id=record_task_id,
                    recursion_limit=settings.recursion_limit,
                    raise_graph_error=False,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # Warning, not debug (round-52, same family as the turn
                # stream's record fix): this record is the ONLY memory
                # that the fault may still be live — recovery was
                # interrupted before it finished — and at debug level
                # its loss was invisible in production.
                logger.warning(
                    "Failed to write recover interruption record for %s (cause=%s)",
                    record_task_id, cause, exc_info=True,
                )

        async def _abort_recover_cleanup(cause: str, error_detail: str = "") -> None:
            """Single-source abort cleanup for this recovery stream (round-52).

            Also assigns the abort-cause memo (round-57): every handler
            routes through here, so this is the one place the finally
            fallback can learn the cause from.

            The recover twin of ``turn_event_stream._abort_turn_cleanup``.
            Starlette serves this generator inside an anyio task group: a
            client disconnect cancels the task-group SCOPE — level-based
            cancellation that re-delivers CancelledError at every await
            suspension — so an unshielded cleanup await dies at its first
            suspension. This stream's own handlers carried exactly that
            defect family (round-48 found it on the turn twin; this module
            was never swept): the user-cancel handler used an
            ``asyncio.shield`` OUTER await — the form round-48 proved
            ineffective, the shield only keeps its background job alive —
            and the internal-error handler used a bare await.

            Decision table:
              shield — always: the whole body. Ceiling since round-54
                       (G7/F5): the old no-ceiling ruling was
                       checkpointer-shaped (AsyncSqliteSaver, verified r53)
                       and this chain carries aget_state — a checkpointer
                       READ — so a future remote checkpointer re-opens the
                       r49 hang surface; the SQLite-class bound is generous
                       (30s ≫ any local write) and a hit is bounded
                       abandon.
              record — every cause: an interrupted recovery's "fault may
                       still be live" memory (``record_written`` guards the
                       double write on the completed-then-crashed path).
              row    — every cause (round-54 G4): the recover task row's
                       terminal write via the shared guarded helper and
                       the shared taxonomy (abort_row_word — interrupt
                       causes "cancelled", internal_error "failed").
                       Round-56 found this arm carrying its own inline
                       cause→word conditional with the OPPOSITE
                       unknown-cause polarity (unknown → "cancelled",
                       fail-open — an unknown cause understated as a
                       user cancel; the shared taxonomy is fail-closed).
                       The r53 triad's cancel-exit-only wiring left
                       this module's rows zombie at the last mid-graph
                       upsert. Ordered AFTER the record (the record is
                       the live-fault memory; the row is the derived
                       user-facing fact).
            """
            with anyio.CancelScope(shield=True):
                nonlocal abort_cause
                abort_cause = cause
                try:
                    with anyio.fail_after(ABORT_SQLITE_CEILING_S):
                        await _write_recover_interrupted(cause, error_detail)
                        await write_aborted_task_row(
                            record_task_id, abort_row_word(cause),
                        )
                except TimeoutError:
                    logger.warning(
                        "Recover stream %s abort cleanup exceeded %.0fs "
                        "ceiling; remaining abort writes abandoned",
                        record_task_id, ABORT_SQLITE_CEILING_S,
                    )

        try:
            # 1. Build initial state from inject checkpoint
            try:
                initial_state, state_values = await build_recover_initial_state(
                    agents, inject_task_id, record_task_id, req_id,
                )
            except RecoverSetupError as e:
                yield StreamEvent(
                    type="error",
                    content=e.envelope.get("message", "Task not found"),
                    task_id=record_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                return

            inject_tui_session_id = initial_state.get("tui_session_id", "")

            # 2. Create session for recording
            session_store = agents.get("session_store")
            if session_store:
                inject_messages = state_values.get("messages", [])
                session_store.create_session(
                    record_task_id,
                    operation="recover",
                    tui_session_id=inject_tui_session_id,
                    parent_task_id=inject_task_id,
                    baseline_messages=inject_messages,
                )

            # 3. Stream recover graph
            recover_config = {
                "configurable": {"thread_id": record_task_id},
                "recursion_limit": settings.recursion_limit,
            }
            recover_graph = agents.get("recover")
            if recover_graph is None:
                yield StreamEvent(
                    type="error",
                    content="Recover graph not available",
                    task_id=record_task_id,
                ).to_sse()
                yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                return

            _tsm.start_task_span(record_task_id)
            if _otel_cb is not None:
                _otel_cb.set_task_id(record_task_id)

            async for raw_event in recover_graph.astream_events(
                initial_state, recover_config, version="v2"
            ):
                if await req.is_disconnected():
                    logger.info(f"Client disconnected, aborting recover stream {record_task_id}")
                    # Round-54 G2: the poll is an ABORT EXIT, not a silent
                    # break. Before, it fell through to result extraction —
                    # a half-run recovery could write a COMPLETED-LOOKING
                    # summary record (record_written=True) on the stream
                    # whose interrupted record is the ONLY "fault may still
                    # be live" memory. The ClientDisconnected handler gives
                    # the poll-loser path the cancel exit's semantics; which
                    # one a disconnect gets must not be decided by the
                    # poll-vs-cancel race (r48: the cancel usually wins).
                    raise ClientDisconnected()
                stream_evt = parse_stream_event(raw_event)
                if stream_evt is not None:
                    stream_evt.task_id = record_task_id
                    for sse in batcher.feed(stream_evt):
                        yield sse
            for sse in batcher.flush():
                yield sse

            # 4. Extract final result
            final_state = await recover_graph.aget_state(recover_config)
            if final_state and final_state.values:
                result_payload = await build_recover_result_payload(
                    recover_graph,
                    recover_config,
                    record_task_id,
                    inject_task_id,
                    state_values,
                    started_monotonic,
                )
                if result_payload is None:
                    yield StreamEvent(
                        type="error",
                        content="Recover graph completed but no result payload was available",
                        task_id=record_task_id,
                    ).to_sse()
                    yield StreamEvent(type="done", task_id=record_task_id).to_sse()
                    return

                try:
                    from chaos_agent.agent.result.operation_summary import (
                        append_ledger_process_detail,
                        build_recover_summary_text,
                    )
                    from chaos_agent.memory.operation_summary_writer import write_operation_summary
                    from chaos_agent.server.routes.sessions import get_store as get_tui_session_store

                    summary_text = build_recover_summary_text(
                        result_payload,
                        inject_task_id,
                        state_values,
                    )
                    # ONE combined record: append the recover ledger's process
                    # detail (what recovery established / did) below the summary,
                    # from the recover graph's final state.
                    summary_text = append_ledger_process_detail(
                        summary_text,
                        final_state.values if final_state else None,
                    )
                    if summary_text:
                        tui_session_index_store = get_tui_session_store()
                        session_meta = (
                            tui_session_index_store.get(inject_tui_session_id)
                            if inject_tui_session_id
                            else None
                        )
                        thread_id = (
                            session_meta.get("conversation_thread_id")
                            if isinstance(session_meta, dict)
                            else ""
                        )
                        intent_graph = agents.get("intent") if isinstance(agents, dict) else None
                        await write_operation_summary(
                            summary_text,
                            intent_graph=intent_graph,
                            thread_id=thread_id,
                            state_update={
                                "confirmed_intent": None,
                                "recover_task_id": None,
                                "pipeline_task_id": record_task_id,
                            },
                            tui_session_id=inject_tui_session_id,
                            session_index_store=tui_session_index_store,
                            task_id=record_task_id,
                            recursion_limit=settings.recursion_limit,
                            raise_graph_error=False,
                        )
                        record_written = True
                except Exception:
                    logger.warning(
                        "Failed to persist recover-stream summary for %s",
                        record_task_id,
                        exc_info=True,
                    )

                await finalize_recover_session(
                    session_store,
                    recover_graph,
                    recover_config,
                    record_task_id,
                    inject_task_id,
                    state_values,
                    result_payload=result_payload,
                    result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
                )

                yield StreamEvent(
                    type="result",
                    content=json.dumps(result_payload, ensure_ascii=False),
                    task_id=record_task_id,
                ).to_sse()
            else:
                yield StreamEvent(
                    type="error",
                    content="Recover graph completed but no state available",
                    task_id=record_task_id,
                ).to_sse()

            yield StreamEvent(type="done", task_id=record_task_id).to_sse()

        except asyncio.CancelledError:
            # ``CancelledError`` derives from BaseException, so the handler below
            # never saw it and a cancelled recovery left no record at all — the
            # worst case, since the fault is live by definition until recovery
            # completes. Routed through the single-source shielded cleanup since
            # round-52 (the old asyncio.shield outer await here was the exact
            # form round-48 proved ineffective on the turn twin).
            logger.info(f"Recover stream cancelled for task {inject_task_id}")
            await _abort_recover_cleanup("user_cancel")
            raise
        except ClientDisconnected:
            # Round-54 G2: the poll-detected disconnect — the exit the
            # poll-vs-cancel RACE decides (r48: the scope cancel usually
            # wins a real disconnect, but the poll wins server-side cancels
            # and fast/proxied disconnects). Same abort semantics as the
            # cancel exit above: the interrupted record (cause
            # "disconnected") and the recover row's terminal word, all
            # through the single-source shielded cleanup.
            logger.info(f"Recover stream disconnected for task {inject_task_id}")
            await _abort_recover_cleanup("disconnected")
            return
        except Exception as e:
            logger.exception(f"Recover stream failed for task {inject_task_id}")
            # Before the terminating events: the consumer stops iterating at
            # ``done`` and anything after the final yield would never run.
            # Single-source shielded cleanup since round-52 — a cancellation
            # landing mid-record (error plus disconnect) must not lose the
            # record: the fault is live by definition.
            await _abort_recover_cleanup(
                "internal_error", f"{type(e).__name__}: {e}",
            )
            yield StreamEvent(
                type="error",
                content=f"Recovery failed: {type(e).__name__}: {e}",
                task_id=record_task_id,
            ).to_sse()
            yield StreamEvent(type="done", task_id=record_task_id).to_sse()
        finally:
            _tsm.end_task_span(record_task_id)
            if session_store and session_store.has_active(record_task_id):
                # Shielded for the same reason as the turn stream's terminal
                # finalize (round-48 site 4): under a real client disconnect
                # the task-group scope cancellation is level-based and a bare
                # await here dies at its first suspension — leaving the recover
                # session row stuck active forever (this finalize is the ONLY
                # writer of the terminal status on the abort path). Ceiling
                # since round-54 (G7): same checkpointer-shaped ruling as the
                # abort chain above — aget_state rides this shield too.
                with anyio.CancelScope(shield=True):
                    try:
                        with anyio.fail_after(ABORT_SQLITE_CEILING_S):
                            await finalize_recover_session(
                                session_store,
                                recover_graph,
                                recover_config,
                                record_task_id,
                                inject_task_id,
                                state_values,
                                result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
                                # Round-57 F1': the abort path's ONLY session
                                # writer is this fallback, and no result payload
                                # exists on it — so default_status IS the session
                                # word. Classified, not hard-coded: a
                                # user-cancelled recovery records "cancelled",
                                # same word its TaskStore row already carries
                                # (the memo is empty only when the run never
                                # reached an abort handler — fail-closed).
                                default_status=abort_row_word(
                                    abort_cause or "internal_error",
                                ),
                                error_log_level="debug",
                            )
                    except TimeoutError:
                        logger.warning(
                            "Recover stream %s terminal finalize exceeded "
                            "%.0fs ceiling; remaining terminal writes abandoned",
                            record_task_id, ABORT_SQLITE_CEILING_S,
                        )
            task_tracker.unregister(record_task_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers=sse_headers,
    )
