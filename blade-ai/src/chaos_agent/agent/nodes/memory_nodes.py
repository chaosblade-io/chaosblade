"""Memory nodes: load and save operational/session memory within the graph."""

import logging

from langchain_core.messages import HumanMessage

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
    updates = {}

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

    # Load experiment history for the target (from TaskStore)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        target = state.get("target") or {}
        namespace = target.get("namespace", "")
        active = await store.query_active(namespace=namespace)
        updates["experiment_history"] = active
    except Exception as e:
        logger.warning(f"Failed to load experiment history: {e}")
        updates["experiment_history"] = []

    tracker.complete("Memory loaded")
    sync_node_status_to_session(state, "load_memory", "Memory loaded")

    # If NL description is present, inject it as a HumanMessage so the Agent can see it
    nl_description = state.get("input")
    if nl_description:
        updates["messages"] = [HumanMessage(content=nl_description)]
    elif state.get("blade_scope") and state.get("blade_target") and state.get("blade_action"):
        # Structured mode: synthesize HumanMessage from blade parameters
        # so the LLM receives a clear fault injection request
        scope = state["blade_scope"]
        target = state["blade_target"]
        action = state["blade_action"]
        target_info = state.get("target") or {}
        ns = target_info.get("namespace", "")
        names = target_info.get("names", [])
        parts = [f"执行故障注入：{scope}-{target}-{action}"]
        if ns:
            parts.append(f"目标命名空间: {ns}")
        if names:
            names_str = ", ".join(names) if isinstance(names, list) else names
            parts.append(f"目标名称: {names_str}")
        params = state.get("params") or {}
        if params:
            param_str = ", ".join(f"{k}={v}" for k, v in params.items() if v)
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
            _store.append_messages(_tid, msgs)

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

    # Set finished_at timestamp for the task
    updates = {"finished_at": now_iso()}

    # Infer failure_reason if not already set but task is in a failed state
    if not state.get("failure_reason"):
        from chaos_agent.errors import FailureReason, enrich_failure_reason
        error = state.get("error")
        verification = state.get("verification")
        replan_count = state.get("replan_count", 0)
        replan_context = state.get("replan_context")
        safety_status = state.get("safety_status", "")
        msgs = state.get("messages", [])

        if error:
            # Replan exhaustion: had replan context but still failed
            if replan_count > 0 and replan_context:
                base = (
                    f"{FailureReason.REPLAN_EXHAUSTED.value}: "
                    f"Replan exhausted after {replan_count} attempt(s), "
                    f"last error: {error[:200]}"
                )
                updates["failure_reason"] = enrich_failure_reason(base, msgs)
            else:
                base = f"{FailureReason.EXECUTION_FAILED.value}: {error[:300]}"
                updates["failure_reason"] = enrich_failure_reason(base, msgs)
        elif verification and isinstance(verification, dict):
            # Verification failed (reached save_memory via verifier → "done")
            l1 = verification.get("layer1", {})
            l2 = verification.get("layer2", {})
            level = verification.get("level", "")
            if level in ("unverified",) or l1.get("status") == "failed" or l2.get("status") == "failed":
                l1_status = l1.get("status", "unknown")
                l2_status = l2.get("status", "unknown")
                base = (
                    f"{FailureReason.VERIFICATION_FAILED.value}: "
                    f"Layer1={l1_status}, Layer2={l2_status}, level={level}"
                )
                updates["failure_reason"] = enrich_failure_reason(base, msgs)
        # Replan exhaustion but error was cleared (moved to replan_context during replan).
        # Without this, the response would have result="failed" but error="", which is confusing.
        elif replan_count > 0 and replan_context and not state.get("blade_uid") and not verification:
            base = (
                f"{FailureReason.REPLAN_EXHAUSTED.value}: "
                f"Replan exhausted after {replan_count} attempt(s), "
                f"injection never succeeded"
            )
            updates["failure_reason"] = enrich_failure_reason(base, msgs)

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
            elif state.get("blade_uid") and not (
                state.get("error") or updates.get("failure_reason")
            ):
                final_status = "completed"
            else:
                final_status = "failed"
            # Build a human-readable result_summary from verification
            # so the JSON snapshot at rest carries the same outcome
            # signal the live ResultCard surfaces.
            result_summary_str = ""
            try:
                from chaos_agent.memory.session_store import (
                    build_result_summary,
                )
                _v = state.get("verification") or {}
                if isinstance(_v, dict):
                    result_summary_str = build_result_summary(_v)
            except Exception:
                logger.debug(
                    "build_result_summary failed for task=%s",
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
                result_summary=result_summary_str,
                status=final_status,
            )
    except Exception:
        logger.warning(
            "Failed to finalize task session for %s in save_memory; "
            "turn.py finally block will retry the finalize.",
            task_id, exc_info=True,
        )

    return updates
