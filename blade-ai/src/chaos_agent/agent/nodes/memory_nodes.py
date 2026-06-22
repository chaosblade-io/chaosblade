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
from chaos_agent.agent.operation_outcome import read_inject_verification, read_operation_outcome
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



async def _run_self_evolution(state: AgentState, task_id: str, tracker) -> None:
    """Auto-append experience to AGENT.md when self_evolution is enabled."""
    _evolution_span = None
    try:
        from chaos_agent.observability.tracer import get_trace
        _trace = await get_trace(task_id)
        _evolution_span = _trace.start_span("self_evolution")
    except Exception:
        _evolution_span = None

    try:
        from chaos_agent.agent.experience import append_experience
        from chaos_agent.agent.fault_spec import fault_type_from_state
        fault_type = fault_type_from_state(state)
        verification = read_inject_verification(state) or {}
        outcome = read_operation_outcome(state)
        task_summary = f"Task {task_id}: skill={fault_type}, verification={verification.get('level', 'unknown') if isinstance(verification, dict) else 'unknown'}"
        if outcome.error:
            task_summary += f", error={outcome.error}"

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


async def _generate_postmortem(
    state: AgentState, task_id: str, tracker,
) -> dict | None:
    """Generate postmortem report via LLM when conditions are met.

    All exceptions are swallowed so the result envelope ships unimpeded.
    """
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
            tracker.start(
                StatusCategory.NODE, "postmortem",
                "Generating postmortem (LLM)...",
            )
            # R10 — wire the SAME tracing / OTel callbacks as the main
            # graph LLM so postmortem's token usage flows into
            # ``TaskTrace.total_token_input/output`` + OTel GenAI export.
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
                from chaos_agent.agent.fault_spec import fault_type_from_state
                _spec = read_fault_spec_lazy(state)
                verification = read_inject_verification(state) or {}
                outcome = read_operation_outcome(state)
                header_meta = {
                    "skill_name": fault_type_from_state(state) or "unknown",
                    "namespace": (_spec.namespace if _spec else "") or "unknown",
                    "status": verification.get("level", "unknown"),
                    "duration": _format_duration_ms(
                        outcome.result.get("duration_ms", 0)
                    ) if isinstance(outcome.result, dict) else "",
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
        logger.exception("Postmortem subsystem unexpected error for task %s", task_id)
    # Emit a completion event so the platform UI can show postmortem finished.
    # The tracker.start("postmortem", ...) is emitted inside the try block;
    # this complete pairs with it regardless of success/skip/error.
    try:
        tracker.complete(
            f"Postmortem {'generated' if postmortem_payload else 'skipped'}"
        )
    except Exception:
        pass
    return postmortem_payload


def _infer_failure_detail(state: AgentState) -> dict:
    """Infer failure_detail when task is in a failed state but none was set."""
    outcome = read_operation_outcome(state)
    if outcome.failure_detail:
        return {}
    from chaos_agent.agent.state_helpers import fail_state
    from chaos_agent.agent.verdict import FailureCategory

    error = outcome.error
    verification = read_inject_verification(state)
    replan_count = state.get("replan_count", 0)
    replan_context = state.get("replan_context")
    msgs = state.get("messages", [])
    planning_alternatives = state.get("_planning_alternatives", "")

    if error:
        if replan_count > 0 and replan_context:
            return fail_state(
                FailureCategory.REPLAN_EXHAUSTED,
                f"attempts={replan_count}, last_error={error[:200]}",
                msgs,
                alternatives=planning_alternatives,
            )
        return fail_state(
            FailureCategory.EXECUTION_FAILED,
            error[:300],
            msgs,
            alternatives=planning_alternatives,
        )
    if verification and isinstance(verification, dict):
        l1 = verification.get("layer1", {})
        l2 = verification.get("layer2", {})
        level = verification.get("level", "")
        if level in ("unverified",) or l1.get("status") == "failed" or l2.get("status") == "failed":
            l1_status = l1.get("status", "unknown")
            l2_status = l2.get("status", "unknown")
            return fail_state(
                FailureCategory.VERIFICATION_FAILED,
                f"Layer1={l1_status}, Layer2={l2_status}, level={level}",
                msgs,
                alternatives=planning_alternatives,
            )
    if replan_count > 0 and replan_context and not state.get("blade_uid") and not verification:
        return fail_state(
            FailureCategory.REPLAN_EXHAUSTED,
            f"attempts={replan_count}, injection never succeeded",
            msgs,
            alternatives=planning_alternatives,
        )
    return {}


async def _finalize_session_store(
    state: AgentState, task_id: str, confirmed_intent: str | None, updates: dict,
) -> None:
    """Finalize the per-task SessionStore record."""
    try:
        from chaos_agent.memory.session_store import get_global_session_store
        store = get_global_session_store()
        if store is not None and task_id and task_id != "unknown":
            from chaos_agent.agent.state import infer_task_state
            merged = dict(state)
            merged.update(updates)
            task_state = infer_task_state(merged)
            if task_state == "injecting":
                task_state = "injected" if merged.get("blade_uid") else "failed"

            merged_outcome = read_operation_outcome(merged)
            if confirmed_intent in ("chat", "recover"):
                final_status = "completed"
            elif not merged_outcome.error and (
                merged.get("blade_uid")
                or merged.get("injection_method")
                or task_state in ("injected", "recovered", "partial_recovered")
            ):
                final_status = "completed"
            else:
                final_status = "failed"

            result_summary: str | dict = ""
            try:
                from chaos_agent.server.routes.turn_result import build_inject_data_from_state
                from chaos_agent.models.schemas import build_inject_envelope

                _data = build_inject_data_from_state(merged, task_id)
                result_summary = build_inject_envelope(
                    _data, _data["task_state"], _data.get("error", ""),
                )
            except Exception:
                logger.debug(
                    "build result_summary failed for task=%s",
                    task_id, exc_info=True,
                )
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

    if settings.self_evolution:
        tracker.update("Auto-appending experience to AGENT.md (self_evolution)")
        await _run_self_evolution(state, task_id, tracker)

    tracker.complete("Experiment saved to TaskStore")
    verification = read_inject_verification(state) or {}
    sync_node_status_to_session(state, "save_memory", "Experiment saved to TaskStore",
        detail={"verification_level": verification.get("level", "unknown")})

    postmortem_payload = await _generate_postmortem(state, task_id, tracker)

    # Set finished_at timestamp for the task
    updates = {"finished_at": now_iso()}
    # R11 — ALWAYS write the postmortem field (even when None) to
    # OVERWRITE any leftover value from a prior experiment that shares
    # this LangGraph thread.
    updates["postmortem"] = postmortem_payload

    updates.update(_infer_failure_detail(state))

    # Persist inject_context for cross-session recovery.
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

    await _finalize_session_store(state, task_id, confirmed_intent, updates)

    from chaos_agent.agent.attempt_tracker import end_attempt as _end
    merged_for_attempt = dict(state)
    merged_for_attempt.update(updates)
    operation_outcome = read_operation_outcome(merged_for_attempt)
    _outcome = "failed" if (operation_outcome.failure_detail or operation_outcome.error) else "success"
    end_delta = _end(state, outcome=_outcome)
    if end_delta:
        updates.update(end_delta)
    return updates
