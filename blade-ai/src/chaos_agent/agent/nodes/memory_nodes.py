"""Memory nodes: load and save operational/session memory within the graph."""

import asyncio
import logging

from langchain_core.messages import HumanMessage

from chaos_agent.agent.node_names import MEMORY_NODE


def _format_duration_ms(ms) -> str:
    """Render a duration in ms as ``Ns`` / ``Nm Ns``; empty when unknown."""
    try:
        ms_int = int(ms or 0)
    except (TypeError, ValueError):
        return ""
    if ms_int <= 0:
        return ""
    seconds = ms_int // 1000
    if seconds < 60:
        return f"{seconds}s"
    return f"{seconds // 60}m {seconds % 60}s"


def read_fault_spec_lazy(state):
    """Defer fault_spec import to avoid eager top-level cycle."""
    from chaos_agent.agent.fault_spec import read_fault_spec
    return read_fault_spec(state)

from chaos_agent.agent.nodes._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.agent.state import AgentState
from chaos_agent.config.settings import settings
from chaos_agent.memory.operational_memory import OperationalMemory
from chaos_agent.observability.status_tracker import (
    get_tracker,
    StatusCategory,
)
from chaos_agent.utils.time import now_iso

logger = logging.getLogger(__name__)


async def load_memory(state: AgentState) -> dict:
    """Load operational memory and experiment history into state.

    This is the first node in the inject graph, providing context
    from Layer 3 (Operational Memory) to the agent.
    """
    task_id = state.get("task_id", "unknown")
    working_dir = settings.working_dir
    memory_dir = settings.resolved_memory_dir
    # Wipe per-turn transient fields that should NOT bleed across turns
    # via the LangGraph checkpoint. ``approved_target`` is the most
    # important one — without this clear, a chat-only follow-up turn
    # could inherit the previous inject turn's frozen approval and
    # cause the screener to false-positive on read-only or unrelated
    # tool_calls. confirmation_gate refreezes a fresh approval on the
    # next user approve, so wiping at turn-start is safe.
    updates: dict = {"approved_target": None, "screener_route": None}

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "load_memory",
        "Loading operational memory and experiment history",
    )

    # Load MEMORY.md operational notes
    try:
        memory_path = memory_dir / "MEMORY.md"
        op_memory = OperationalMemory(memory_path)
        updates["operational_notes"] = op_memory.read()
    except Exception as e:
        logger.warning(f"Failed to load operational memory: {e}")
        updates["operational_notes"] = ""

    # Load experiment history for the target (from TaskStore) — namespace
    # comes from the FaultSpec written at entry.
    from chaos_agent.agent.fault_spec import read_fault_spec
    _spec = read_fault_spec(state)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        namespace = _spec.namespace if _spec else ""
        active = await store.query_active(namespace=namespace)
        updates["experiment_history"] = active
    except Exception as e:
        logger.warning(f"Failed to load experiment history: {e}")
        updates["experiment_history"] = []

    tracker.complete("Memory loaded")
    sync_node_status_to_session(state, "load_memory", "Memory loaded")

    # Per-turn chat input takes priority — ``state.input`` is set by
    # entry points on every invocation (TUI first turn, TUI continuing
    # turn, CLI NL re-invocation). Falls through to FaultSpec's
    # ``user_description`` (NL placeholder seed) and finally to the
    # structured synthetic prompt for direct mode (no input, complete
    # spec).
    nl_description = state.get("input") or (_spec.user_description if _spec else "")
    if nl_description:
        updates["messages"] = [HumanMessage(content=nl_description)]
    elif _spec and _spec.is_complete:
        # Direct mode (no NL input, structured spec) — synthesise a
        # HumanMessage from the spec so the agent has a clear request.
        parts = [f"执行故障注入：{_spec.scope}-{_spec.blade_target}-{_spec.blade_action}"]
        if _spec.namespace:
            parts.append(f"目标命名空间: {_spec.namespace}")
        if _spec.names:
            parts.append(f"目标名称: {', '.join(_spec.names)}")
        if _spec.params:
            param_str = ", ".join(f"{k}={v}" for k, v in _spec.params.items() if v)
            if param_str:
                parts.append(f"参数: {param_str}")
        kubeconfig = state.get("kubeconfig") or ""
        if kubeconfig:
            parts.append(f"kubeconfig: {kubeconfig}")
        updates["messages"] = [HumanMessage(content="\n".join(parts))]

    # Record HumanMessages to session store immediately so they appear
    # in correct chronological order (before direct_execute's ToolMessages).
    # Without this, finalize_session appends them after all already-recorded
    # messages, causing ordering mismatch.
    msgs = updates.get("messages")
    if msgs:
        from chaos_agent.memory.session_store import get_global_session_store
        _store = get_global_session_store()
        _tid = state.get("task_id", "")
        if _store and _tid:
            _store.append_messages(_tid, msgs, node_name=MEMORY_NODE)

    await sync_to_store(state, updates)
    return updates


async def pipeline_init(state: AgentState) -> dict:
    """Entry node for Pipeline Graph — load operational context.

    Equivalent to load_memory but without intent routing. Used by
    CLI (direct + NL) and TUI after Intent Graph confirms inject.
    """
    task_id = state.get("task_id", "unknown")
    memory_dir = settings.resolved_memory_dir
    updates: dict = {"approved_target": None, "screener_route": None}

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "pipeline_init", "Loading operational context")

    try:
        op_memory = OperationalMemory(memory_dir / "MEMORY.md")
        updates["operational_notes"] = op_memory.read()
    except Exception as e:
        logger.warning(f"Failed to load operational memory: {e}")
        updates["operational_notes"] = ""

    from chaos_agent.agent.fault_spec import read_fault_spec
    _spec = read_fault_spec(state)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        namespace = _spec.namespace if _spec else ""
        updates["experiment_history"] = await store.query_active(namespace=namespace)
    except Exception as e:
        logger.warning(f"Failed to load experiment history: {e}")
        updates["experiment_history"] = []

    nl_description = state.get("input") or (_spec.user_description if _spec else "")
    if nl_description:
        updates["messages"] = [HumanMessage(content=nl_description)]
    elif _spec and _spec.is_complete:
        parts = [f"执行故障注入：{_spec.scope}-{_spec.blade_target}-{_spec.blade_action}"]
        if _spec.namespace:
            parts.append(f"目标命名空间: {_spec.namespace}")
        if _spec.names:
            parts.append(f"目标名称: {', '.join(_spec.names)}")
        if _spec.params:
            param_str = ", ".join(f"{k}={v}" for k, v in _spec.params.items() if v)
            if param_str:
                parts.append(f"参数: {param_str}")
        kubeconfig = state.get("kubeconfig") or ""
        if kubeconfig:
            parts.append(f"kubeconfig: {kubeconfig}")
        updates["messages"] = [HumanMessage(content="\n".join(parts))]

    msgs = updates.get("messages")
    if msgs:
        from chaos_agent.memory.session_store import get_global_session_store
        _store = get_global_session_store()
        _tid = state.get("task_id", "")
        if _store and _tid:
            _store.append_messages(_tid, msgs, node_name=MEMORY_NODE)

    tracker.complete("Pipeline context loaded")
    sync_node_status_to_session(state, "pipeline_init", "Context loaded")
    await sync_to_store(state, updates)
    return updates


async def save_memory(state: AgentState) -> dict:
    """Save experiment results to history and optionally update MEMORY.md.

    This is the last node in the inject graph, persisting results
    to Layer 3 (Operational Memory).

    For non-injection intents (chat, query, explore, recover-bridge),
    only persists task metadata and timestamps — skips self-evolution
    and failure_reason inference (no fault experiment to record).
    """
    task_id = state.get("task_id", "unknown")
    confirmed_intent = state.get("confirmed_intent")

    tracker = get_tracker(task_id)
    tracker.start(
        StatusCategory.NODE,
        "save_memory",
        "Saving experiment results to memory",
    )

    # Non-injection intents: lightweight save (no fault experiment to record)
    if confirmed_intent in ("chat", "recover"):
        updates = {"finished_at": now_iso()}
        tracker.complete("Non-injection intent saved")
        sync_node_status_to_session(state, "save_memory", "Non-injection intent saved")
        # Patch E — close out the current attempt (if any) so the
        # history entry has end_at + outcome populated. Idempotent
        # for chat / recover where no attempt was started.
        from chaos_agent.agent.attempt_tracker import end_attempt as _end
        updates.update(_end(state, outcome="success"))
        await sync_to_store(state, updates)
        return updates

    working_dir = settings.working_dir
    memory_dir = settings.resolved_memory_dir
    result = state.get("result") or {}

    # Experiment record is now persisted by sync_to_store in each node
    # No need to write to history.jsonl separately

    # Self-evolution: auto-append experience when enabled
    if settings.self_evolution:
        tracker.update("Auto-appending experience to AGENT.md (self_evolution)")
        _evolution_span = None
        try:
            from chaos_agent.observability.tracer import get_trace
            _trace = await get_trace(task_id)
            _evolution_span = _trace.start_span("self_evolution")
        except Exception:
            _evolution_span = None

        try:
            from chaos_agent.agent.experience import append_experience
            skill_name = state.get("skill_name", "")
            verification = state.get("verification", {})
            error = state.get("error", "")
            task_summary = f"Task {task_id}: skill={skill_name}, verification={verification.get('level', 'unknown') if isinstance(verification, dict) else 'unknown'}"
            if error:
                task_summary += f", error={error}"

            result = append_experience(task_summary, dict(state))

            if result["status"] == "appended":
                logger.info(
                    "self_evolution appended experience: category=%s, preview=%s",
                    result["category"],
                    result["entry_preview"][:80],
                )
                tracker.update(
                    f"Experience appended to [{result['category']}]",
                    detail={
                        "self_evolution": {
                            "status": "appended",
                            "category": result["category"],
                            "reason": result["reason"],
                        },
                    },
                )
            else:
                logger.info(
                    "self_evolution skipped: %s",
                    result["reason"],
                )
                tracker.update(
                    "Experience append skipped (routine task)",
                    detail={
                        "self_evolution": {
                            "status": "skipped",
                            "reason": result["reason"],
                        },
                    },
                )

            if _evolution_span is not None:
                _evolution_span.detail = result
                await _trace.end_span(_evolution_span)

        except Exception as e:
            logger.warning("Failed to append experience (self_evolution): %s", e)
            tracker.update(
                f"Self-evolution failed: {e}",
                detail={"self_evolution": {"status": "error", "error": str(e)}},
            )
            if _evolution_span is not None:
                try:
                    await _trace.end_span(_evolution_span, error=str(e))
                except Exception:
                    pass

    tracker.complete("Experiment saved to TaskStore")
    sync_node_status_to_session(state, "save_memory", "Experiment saved to TaskStore",
        detail={"verification_level": (state.get("verification") or {}).get("level", "unknown")})

    # T6 — postmortem auto-generation. Gated by settings + experiment
    # outcome (only real injections / qualifying failures get the LLM
    # call). All exceptions are swallowed and degrade to postmortem=None
    # so the result envelope still ships unimpeded.
    postmortem_payload: dict | None = None
    try:
        from chaos_agent.agent.postmortem import (
            build_postmortem_context,
            generate_postmortem,
            save_postmortem,
            should_generate_postmortem,
        )
        from chaos_agent.agent.postmortem.generator import make_summary

        if should_generate_postmortem(dict(state), settings):
            # R17 — use a dedicated source ("postmortem") instead of
            # piggybacking on "save_memory" so turn.py's
            # ``_convert_postmortem_status`` can pick this signal out
            # of the StatusEvent stream and surface it to the TUI as a
            # visible spinner phase. Without a dedicated source the
            # status would be indistinguishable from generic save_memory
            # tracker events (which are dropped by the converter
            # whitelist) and the user would see a silent 5-30s pause.
            tracker.start(
                StatusCategory.NODE, "postmortem",
                "Generating postmortem (LLM)...",
            )
            # R10 — wire the SAME tracing / OTel callbacks as the main
            # graph LLM so postmortem's token usage flows into
            # ``TaskTrace.total_token_input/output`` + OTel GenAI export.
            # Without this, the TUI Footer's per-turn token counter
            # under-reports by 1-3K and monitoring dashboards are blind
            # to this LLM call entirely.
            from chaos_agent.agent.factory import make_llm
            from chaos_agent.observability import status_tracker as _st_mod
            _pm_callbacks: list = []
            _trace_cb = getattr(_st_mod, "_tracing_callback", None)
            if _trace_cb is not None:
                _pm_callbacks.append(_trace_cb)
            _otel_cb = getattr(_st_mod, "_otel_callback", None)
            if _otel_cb is not None:
                _pm_callbacks.append(_otel_cb)
            pm_llm = make_llm(callbacks=_pm_callbacks or None)
            context = build_postmortem_context(
                dict(state),
                max_messages=settings.postmortem_max_messages,
            )
            try:
                markdown_body = await generate_postmortem(
                    context, pm_llm,
                    timeout=settings.postmortem_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Postmortem LLM call timed out after %ds for task %s",
                    settings.postmortem_timeout_seconds, task_id,
                )
                tracker.update("Postmortem skipped (timeout)")
                markdown_body = ""
            except Exception as e:
                logger.warning(
                    "Postmortem LLM call failed for task %s: %s", task_id, e,
                )
                tracker.update(f"Postmortem skipped ({type(e).__name__})")
                markdown_body = ""

            if markdown_body:
                # Header metadata derived from the same state the LLM
                # used — keeps the on-disk file self-describing.
                _spec = read_fault_spec_lazy(state)
                header_meta = {
                    "skill_name": state.get("skill_name", "") or "unknown",
                    "namespace": (_spec.namespace if _spec else "") or "unknown",
                    "status": (state.get("verification") or {}).get("level", "unknown") if isinstance(state.get("verification"), dict) else "unknown",
                    "duration": _format_duration_ms((state.get("result") or {}).get("duration_ms", 0)) if isinstance(state.get("result"), dict) else "",
                    "generated_at": now_iso(),
                }
                try:
                    pm_path = save_postmortem(
                        task_id, markdown_body, header_meta=header_meta,
                    )
                    postmortem_payload = {
                        "path": str(pm_path),
                        "markdown": markdown_body,
                        "summary": make_summary(markdown_body),
                    }
                    tracker.update(
                        f"Postmortem saved ({len(markdown_body)} chars)",
                    )
                except Exception as e:
                    logger.warning(
                        "Postmortem write failed for task %s: %s", task_id, e,
                    )
                    tracker.update("Postmortem skipped (write error)")
    except Exception:
        # Outermost catch — postmortem subsystem must NEVER crash
        # save_memory. Any import error / unexpected exception lands here.
        logger.exception("Postmortem subsystem unexpected error for task %s", task_id)

    # Set finished_at timestamp for the task
    updates = {"finished_at": now_iso()}
    # R11 — ALWAYS write the postmortem field (even when None) to
    # OVERWRITE any leftover value from a prior experiment that shares
    # this LangGraph thread (server mode uses ``conv-<sid>`` as the
    # thread_id, so state.postmortem persists across inject runs within
    # one TUI session). Without this, a subsequent SAFETY_REJECTED /
    # USER_REJECTED inject that skips postmortem generation would
    # inherit the previous experiment's postmortem dict and surface it
    # on the current ResultCard — a serious data-correctness bug.
    updates["postmortem"] = postmortem_payload

    # Infer failure_detail if not already set but task is in a failed state
    if not state.get("failure_detail"):
        from chaos_agent.agent.state_helpers import fail_state
        from chaos_agent.agent.verdict import FailureCategory

        error = state.get("error")
        verification = state.get("verification")
        replan_count = state.get("replan_count", 0)
        replan_context = state.get("replan_context")
        msgs = state.get("messages", [])

        if error:
            if replan_count > 0 and replan_context:
                _fs = fail_state(
                    FailureCategory.REPLAN_EXHAUSTED,
                    f"attempts={replan_count}, last_error={error[:200]}",
                    msgs,
                )
            else:
                _fs = fail_state(
                    FailureCategory.EXECUTION_FAILED,
                    error[:300],
                    msgs,
                )
            updates.update(_fs)
        elif verification and isinstance(verification, dict):
            l1 = verification.get("layer1", {})
            l2 = verification.get("layer2", {})
            level = verification.get("level", "")
            if level in ("unverified",) or l1.get("status") == "failed" or l2.get("status") == "failed":
                l1_status = l1.get("status", "unknown")
                l2_status = l2.get("status", "unknown")
                _fs = fail_state(
                    FailureCategory.VERIFICATION_FAILED,
                    f"Layer1={l1_status}, Layer2={l2_status}, level={level}",
                    msgs,
                )
                updates.update(_fs)
        elif replan_count > 0 and replan_context and not state.get("blade_uid") and not verification:
            _fs = fail_state(
                FailureCategory.REPLAN_EXHAUSTED,
                f"attempts={replan_count}, injection never succeeded",
                msgs,
            )
            updates.update(_fs)

    # Persist inject_context for cross-session recovery.
    # Task_store has the column but inject flow never populates it;
    # compute it here where messages are complete.
    if not state.get("inject_context"):
        try:
            from chaos_agent.utils.inject_context import build_inject_context
            _msgs = state.get("messages", [])
            _ctx = build_inject_context(_msgs)
            if _ctx:
                updates["inject_context"] = _ctx
        except Exception:
            pass

    await sync_to_store(state, updates)

    # Finalize the per-task SessionStore record so the on-disk
    # ``memory/tasks/<task_id>.json`` is closed out atomically with
    # the SQLite metadata. This is the clean-termination path —
    # turn.py also defensively finalizes in its ``finally`` block to
    # cover Esc / CancelledError / Exception paths that bypass
    # save_memory entirely. ``finalize_session`` is idempotent
    # (silent return when the task isn't in ``_active_sessions``),
    # so the double-call is safe.
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        store = get_global_session_store()
        if store is not None and task_id and task_id != "unknown":
            # Status string mirrors the task lifecycle tags used by
            # SessionStore: "completed" for chat / recover-bridge /
            # successful inject; "failed" when the run terminated in
            # a failure state. Keeping the strings aligned with
            # ``infer_task_state`` makes downstream filtering trivial.
            if confirmed_intent in ("chat", "recover"):
                final_status = "completed"
            elif not (state.get("error") or updates.get("error")) and (
                state.get("blade_uid")
                or state.get("injection_method")
                or task_state in ("injected", "recovered", "partial_recovered")
            ):
                final_status = "completed"
            else:
                final_status = "failed"
            # Build a structured result_summary envelope (matching
            # inject.py / inject_stream.py / recover.py format) so the
            # JSON snapshot at rest carries the same data the live
            # ResultCard surfaces via SSE.
            result_summary: str | dict = ""
            try:
                from chaos_agent.agent.state import infer_task_state, extract_ui_diagnostics
                from chaos_agent.agent.fault_spec import legacy_target_dict, legacy_params_dict
                from chaos_agent.memory.session_store import build_verification_simple
                from chaos_agent.models.schemas import build_inject_envelope

                merged = dict(state)
                merged.update(updates)

                task_state = infer_task_state(merged)
                if task_state == "injecting":
                    task_state = "injected" if merged.get("blade_uid") else "failed"

                result_target = legacy_target_dict(merged)
                blade_params = legacy_params_dict(merged)
                skill_name_fin = merged.get("skill_name", "")
                blade_uid = merged.get("blade_uid", "")

                fault_type = ""
                if blade_params:
                    _s = blade_params.get("scope", "")
                    _a = blade_params.get("action", "")
                    _t = blade_params.get("target", "")
                    if _s and _t and _a:
                        fault_type = f"{_s}-{_t}-{_a}"
                if not fault_type:
                    fault_type = skill_name_fin

                names = result_target.get("names", [])
                ns = result_target.get("namespace", "") or blade_params.get("namespace", "")
                verification = merged.get("verification")
                failure_reason = (
                    (merged.get("failure_detail") or {}).get("context", "")
                    if merged.get("failure_detail")
                    else (merged.get("error") or "")
                )

                result_summary = build_inject_envelope(
                    {
                        "task_id": task_id,
                        "task_state": task_state,
                        "fault_type": fault_type,
                        "blade_uid": blade_uid,
                        "targets": [{"name": n, "namespace": ns} for n in names],
                        "verification": build_verification_simple(verification) if verification else None,
                        **extract_ui_diagnostics(merged),
                    },
                    task_state,
                    failure_reason,
                )
            except Exception:
                logger.debug(
                    "build result_summary failed for task=%s",
                    task_id, exc_info=True,
                )
            # Flush the FULL conversation history to disk before
            # sealing the file. ``state.messages`` carries every
            # message produced this turn — including ToolMessages
            # from phase1/phase2 ToolNodes (which have no hook of
            # their own) and the most recent AIMessage that the
            # next-iteration hook would have caught (but won't,
            # because save_memory is the terminal node). The hook's
            # fire-and-forget ``asyncio.create_task`` flow racily
            # writes a subset; this synchronous append makes finalize
            # the authoritative point where every in-memory message
            # is guaranteed on disk. ``append_messages`` dedups by
            # message id / fallback dedup key, so re-passing
            # already-written messages is a no-op.
            full_messages = list(state.get("messages") or [])
            store.finalize_session(
                task_id,
                remaining_messages=full_messages,
                result_summary=result_summary,
                status=final_status,
            )
    except Exception:
        logger.warning(
            "Failed to finalize task session for %s in save_memory; "
            "turn.py finally block will retry the finalize.",
            task_id, exc_info=True,
        )

    from chaos_agent.agent.attempt_tracker import end_attempt as _end
    _outcome = "failed" if (state.get("failure_detail") or state.get("error")) else "success"
    end_delta = _end(state, outcome=_outcome)
    if end_delta:
        # Merge into the existing updates dict so both the legacy
        # ``finished_at`` write and the attempt-close land in the same
        # state mutation.
        updates.update(end_delta)
    return updates
