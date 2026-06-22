"""SSE event generator and stream helpers for the /turn endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from chaos_agent.agent.intent_handoff import (
    build_pipeline_handoff_from_intent_state,
    clear_dispatched_operation_payload_update,
    detect_dispatchable_operation,
)
from chaos_agent.agent.operation_summary import (
    build_batch_summary_text,
    build_recover_summary_text,
    build_task_summary_text,
)
from chaos_agent.agent.state_builders import build_inject_initial_state
from chaos_agent.agent.streaming import SSEBatcher, StreamEvent, parse_stream_events
from chaos_agent.agent.task_snapshot import resolve_recover_initial_state
from chaos_agent.agent.skill_identity import has_active_skill
from chaos_agent.config.settings import settings
from chaos_agent.memory.operation_summary_writer import write_operation_summary
from chaos_agent.server.routes.turn_interrupt import (
    ConfirmTimeout,
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
from chaos_agent.utils.time import now_iso

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

async def _merged_stream(graph_iter, tracker_queue: asyncio.Queue):
    """Yield ``("graph", event)`` / ``("status", event)`` tuples from two
    concurrent sources (LangGraph astream_events + status tracker queue)."""
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
                evt = await tracker_queue.get()
                await unified.put(("status", evt))
        except asyncio.CancelledError:
            pass

    g_task = asyncio.create_task(_graph_pump())
    s_task = asyncio.create_task(_status_pump())
    try:
        while True:
            kind, payload = await unified.get()
            if kind == "graph_done":
                s_task.cancel()
                try:
                    await s_task
                except asyncio.CancelledError:
                    pass
                while True:
                    try:
                        nk, np = unified.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if nk == "graph_done":
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
                await s_task
            except asyncio.CancelledError:
                pass
        if not g_task.done():
            g_task.cancel()
            try:
                await g_task
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


class ClientDisconnected(Exception):
    """Raised when the SSE client disconnects mid-stream."""


async def _drain_merged(merged_iter, turn_id, batcher, sidewrite, converters, req, *, source="pipeline"):
    """Consume a merged stream, yielding SSE frames.

    Raises ``ClientDisconnected`` if the client drops the connection.
    """
    async for kind, payload in merged_iter:
        if await req.is_disconnected():
            raise ClientDisconnected()
        if kind == "graph":
            for evt in parse_stream_events(payload):
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
            info_evt = StreamEvent(
                type="token",
                content=f"\n{format_auto_approve_info(node, payload)}\n",
                task_id=ctx.turn_id,
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

            answer, keepalives = await wait_for_confirmation(
                ctx.store, ctx.turn_id, settings.confirm_wait_timeout,
            )
            for ka in keepalives:
                yield ka

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

async def _finalize_task_session(graph, config, turn_id, store_cancel_fn):
    """Flush messages and finalize the task session on the clean-exit path."""
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
        if _final and getattr(_final, "values", None):
            _candidate = _final.values.get("task_id", "")
            if isinstance(_candidate, str) and _candidate.startswith(("task-", "recover-")):
                _op_tid = _candidate
            _state_msgs = list(_final.values.get("messages") or [])
        paused_at_interrupt = bool(_final and getattr(_final, "next", None))
        if _op_tid and _store.has_active(_op_tid):
            if _state_msgs:
                try:
                    _store.append_messages(_op_tid, _state_msgs)
                    logger.info("Flushed %d state messages to task=%s", len(_state_msgs), _op_tid)
                except Exception:
                    logger.warning("Pre-finalize flush failed for task=%s", _op_tid, exc_info=True)
            if not paused_at_interrupt:
                _vals = _final.values if _final else {}
                if _vals.get("blade_uid") and not (_vals.get("error") or _vals.get("failure_reason")):
                    _final_status = "completed"
                else:
                    _final_status = "cancelled"
                _store.finalize_session(_op_tid, remaining_messages=[], status=_final_status)
                logger.info("Defensive finalize for task=%s status=%s", _op_tid, _final_status)
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
    from langchain_core.messages import SystemMessage as _SM
    from chaos_agent.agent.nodes.intent_clarification import bootstrap_task_session

    _handoff_data = build_pipeline_handoff_from_intent_state(
        iv,
        operation="inject",
        task_id=iv.get("task_id", f"task-{uuid4()}"),
        default_tui_session_id=ctx.sid,
    )
    _p_task_id = _handoff_data.task_id
    _handoff = _handoff_data.handoff_summary
    _tui_sid = _handoff_data.tui_session_id

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
    _p_input = build_inject_initial_state(
        task_id=_p_task_id,
        tui_session_id=_tui_sid,
        confirmed_intent="inject",
        fault_spec=_handoff_data.fault_spec,
        needs_confirmation=True,
        interaction_mode="tui",
        kubeconfig=settings.kubeconfig_path,
        kube_context=settings.kube_context,
        kubewiz_cluster_uuid=settings.kubewiz_cluster_uuid,
        kubewiz_profile=settings.kubewiz_profile,
        messages=[_SM(content=_handoff)] if _handoff else [],
        dry_run=ctx.dry_run,
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
            _summary_text = build_task_summary_text(_psv, _p_task_id)
            await write_operation_summary(
                _summary_text,
                intent_graph=ctx.intent_graph,
                thread_id=ctx.thread_id,
                state_update={"pipeline_task_id": _p_task_id},
                tui_session_id=ctx.sid,
                recursion_limit=settings.recursion_limit,
            )
        except Exception:
            logger.debug("Failed to write task summary to Intent Graph", exc_info=True)
    finally:
        if not ctx.dry_run:
            await _clear_dispatched_inject_intent_state(ctx, reason="inject pipeline finalization")


async def _clear_dispatched_inject_intent_state(ctx, *, reason: str) -> None:
    """Clear one-shot inject intent fields after pipeline dispatch.

    Injection execution runs in a separate Pipeline Graph thread. Once the
    pipeline has been dispatched, the Intent Graph must stop carrying
    executable intent fields; otherwise an Esc/disconnect during pipeline
    streaming leaves stale ``fault_spec`` / ``batch_submit_args`` behind and
    the next user turn can re-open an old intent_confirm card.
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
    from langchain_core.messages import SystemMessage as _SM
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store
    from pathlib import Path

    _handoff_data = build_pipeline_handoff_from_intent_state(
        iv,
        operation="batch_inject",
        task_id=f"task-{uuid4()}",
        default_tui_session_id=ctx.sid,
    )
    _p_task_id = _handoff_data.task_id
    _tui_sid = _handoff_data.tui_session_id
    _handoff = _handoff_data.handoff_summary

    _p_config = {
        "configurable": {"thread_id": _p_task_id},
        "recursion_limit": settings.recursion_limit,
    }
    _p_input = build_inject_initial_state(
        task_id=_p_task_id,
        tui_session_id=_tui_sid,
        fault_spec=_handoff_data.fault_spec,
        needs_confirmation=True,
        interaction_mode="tui",
        kubeconfig=settings.kubeconfig_path,
        kube_context=settings.kube_context,
        kubewiz_cluster_uuid=settings.kubewiz_cluster_uuid,
        kubewiz_profile=settings.kubewiz_profile,
        batch_submit_args=_handoff_data.batch_submit_args,
        messages=[_SM(content=_handoff)] if _handoff else [],
        dry_run=False,
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

                _batch_pm_file = _pm_dir / f"batch-{ctx.turn_id}.md"
                _pm_dir.mkdir(parents=True, exist_ok=True)
                _batch_pm_file.write_text("\n".join(_pm_sections), encoding="utf-8")
                _batch_pm_path_str = str(_batch_pm_file)

                _pm_evt = StreamEvent(
                    type="token",
                    content=f"\n📝 批量分析报告: {_batch_pm_path_str}\n",
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
            except Exception:
                logger.warning("Failed to write batch summary to Intent Graph", exc_info=True)

        if _bp_timed_out:
            raise ConfirmTimeout("Batch confirmation timed out")
    finally:
        await _clear_dispatched_inject_intent_state(ctx, reason="batch pipeline finalization")


async def _run_recover(ctx, graph, config, turn_started_monotonic, batcher, sidewrite, converters):
    """Launch and stream the recover graph if intent was classified as recover."""
    from chaos_agent.memory.tui_session_store import get_global_tui_session_store as _get_tui_store

    _recover_final = await graph.aget_state(config)
    _rv = _recover_final.values if _recover_final else {}
    _recover_inject_tid = _rv.get("recover_task_id", "")
    if not (
        _rv.get("confirmed_intent") == "recover"
        and _recover_inject_tid
        and not _recover_final.next
    ):
        return

    recover_graph = ctx.agents.get("recover")
    if recover_graph is None:
        return

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
    if _inj_state and _inj_state.values and (
        _inj_state.values.get("blade_uid")
        or has_active_skill(_inj_state.values)
        or _inj_state.values.get("fault_spec")
    ):
        checkpoint_values = _inj_state.values
    elif _rv.get("blade_uid") or has_active_skill(_rv):
        checkpoint_values = _rv

    _rec_task_id = _rv.get("task_id", f"task-{uuid4()}")

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
            content=f"无法找到实验 {_recover_inject_tid} 的注入状态，恢复已跳过。",
            task_id=ctx.turn_id,
        )
        sidewrite(_no_state_evt)
        yield _no_state_evt.to_sse()
        return

    recover_config = {
        "configurable": {"thread_id": _rec_task_id},
        "recursion_limit": settings.recursion_limit,
    }

    # Bootstrap SessionStore so recover messages persist to memory/tasks/
    from chaos_agent.agent.nodes.intent_clarification import bootstrap_task_session
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
        sv, turn_started_monotonic,
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
            _rec_final = await recover_graph.aget_state(recover_config)
            _rec_msgs = list((_rec_final.values or {}).get("messages", [])) if _rec_final else []
            _rec_data = _rec_result.get("data") if isinstance(_rec_result, dict) else {}
            _rec_task_state = _rec_data.get("task_state") if isinstance(_rec_data, dict) else ""
            _rec_status = "failed" if _rec_task_state == "failed" else "completed"
            _rec_store.finalize_session(
                _rec_task_id,
                remaining_messages=_rec_msgs,
                result_summary=_rec_result if _rec_result is not None else "",
                status=_rec_status,
            )
    except Exception:
        logger.warning("Failed to finalize recover session %s", _rec_task_id, exc_info=True)


# ---------------------------------------------------------------------------
# Checkpoint rollback on cancellation
# ---------------------------------------------------------------------------

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
    try:
        _pre_turn_snap = await ctx.intent_graph.aget_state(ctx.graph_config)
        if _pre_turn_snap and _pre_turn_snap.created_at:
            _pre_turn_checkpoint_id = (
                _pre_turn_snap.config.get("configurable", {}).get("checkpoint_id")
            )
    except Exception:
        logger.debug("Failed to capture pre-turn checkpoint", exc_info=True)

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

        # 2.6 Auto-recover
        _result_graph = ctx.result_graph or ctx.intent_graph
        _result_config = ctx.result_config or ctx.graph_config
        async for sse in _run_recover(ctx, _result_graph, _result_config, turn_started_monotonic, batcher, sidewrite, converters):
            yield sse

        # 3. Result
        _result_graph = ctx.result_graph or ctx.intent_graph
        _result_config = ctx.result_config or ctx.graph_config
        result_payload = None if ctx.dry_run else await build_result_payload(
            _result_graph, _result_config, ctx.turn_id, turn_started_monotonic,
        )
        if result_payload is not None:
            op_task_id = ""
            data_obj = result_payload.get("data")
            if isinstance(data_obj, dict):
                candidate = data_obj.get("task_id", "")
                if isinstance(candidate, str) and candidate.startswith("task-"):
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

        # 4. Done
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()

    except ClientDisconnected:
        logger.info(f"Client disconnected during turn {ctx.turn_id}")
        await _rollback_intent_checkpoint(
            ctx.intent_graph, ctx.thread_id, _pre_turn_checkpoint_id,
        )
        return
    except ConfirmTimeout as cte:
        _timeout_evt = StreamEvent(type="error", content=str(cte), task_id=ctx.turn_id)
        sidewrite(_timeout_evt)
        yield _timeout_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
    except asyncio.CancelledError:
        logger.info(f"Turn {ctx.turn_id} cancelled by client")
        # Rollback intent graph checkpoint to pre-turn state so the next
        # turn doesn't resume from a dirty checkpoint with incomplete AIMessage.
        await _rollback_intent_checkpoint(
            ctx.intent_graph, ctx.thread_id, _pre_turn_checkpoint_id,
        )
        _cancel_evt = StreamEvent(type="error", content="Turn cancelled", task_id=ctx.turn_id)
        sidewrite(_cancel_evt)
        yield _cancel_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
        raise
    except Exception as e:
        logger.exception(f"Turn failed for {ctx.turn_id}")
        _exc_evt = StreamEvent(type="error", content=f"{type(e).__name__}: {e}", task_id=ctx.turn_id)
        sidewrite(_exc_evt)
        yield _exc_evt.to_sse()
        yield StreamEvent(type="done", task_id=ctx.turn_id).to_sse()
    finally:
        _tsm.end_task_span(ctx.turn_id)
        ctx.store.cancel_interrupt(ctx.turn_id)
        ctx.task_tracker.unregister(ctx.turn_id)
        try:
            _status_unsubscribe(ctx.tracker_key, ctx.tracker_queue)
        except Exception:
            pass

        _result_graph = ctx.result_graph or ctx.intent_graph
        _result_config = ctx.result_config or ctx.graph_config
        await _finalize_task_session(_result_graph, _result_config, ctx.turn_id, ctx.store.cancel_interrupt)
