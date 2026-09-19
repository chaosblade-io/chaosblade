"""Memory nodes: load and save operational/session memory within the graph."""

import logging
from uuid import uuid4

from langchain_core.messages import HumanMessage

from chaos_agent.agent.node_names import MEMORY_NODE

from chaos_agent.agent.nodes.store._store_sync import sync_to_store, sync_node_status_to_session
from chaos_agent.persistence.task_identity import is_real_task_id
from chaos_agent.agent.result.operation_outcome import read_inject_verification, read_operation_outcome
from chaos_agent.agent.result.verdict import INJECT_VETO_VALUES
from chaos_agent.agent.state import AgentState, has_active_fault
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
    task_id = state.get("task_id", "") or ""
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

    # Reset module-level time_wait state so it doesn't leak from a
    # previous task (the globals persist for the lifetime of the
    # process, which spans many tasks in server mode).
    from chaos_agent.tools.wait import reset_wait_state
    reset_wait_state()

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
    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    _spec = read_fault_spec(state)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        namespace = _spec.namespace if _spec else ""
        # Multi-tenant isolation: only query the current tenant's active experiments
        _tenant_id = state.get("tenant_id", "") or ""
        # Workspace axis (platform mode): empty locally = unfiltered, same contract.
        _workspace_id = state.get("workspace_id", "") or ""
        active = await store.query_active(namespace=namespace, tenant_id=_tenant_id, workspace_id=_workspace_id)
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
    # structured synthetic prompt (no input, complete spec).
    nl_description = state.get("input") or (_spec.user_description if _spec else "")
    if nl_description:
        # Assign an explicit id BEFORE the early session-store append below.
        # Without it, the pre-reducer serialization has no id (content-based
        # dedup key) while the post-reducer copy carries a LangGraph-assigned
        # UUID (id-based dedup key) — the key mismatch defeats dedup and the
        # message is recorded twice in the task JSONL.
        updates["messages"] = [HumanMessage(content=nl_description, id=str(uuid4()))]
    elif _spec and _spec.is_complete:
        # Structured entry (no NL input, structured spec) — synthesise a
        # HumanMessage from the spec so the agent has a clear request.
        parts = [f"Execute fault injection: {_spec.scope}-{_spec.fault_target}-{_spec.fault_action}"]
        if _spec.namespace:
            parts.append(f"Target namespace: {_spec.namespace}")
        if _spec.names:
            parts.append(f"Target names: {', '.join(_spec.names)}")
        if _spec.params:
            param_str = ", ".join(f"{k}={v}" for k, v in _spec.params.items() if v)
            if param_str:
                parts.append(f"Parameters: {param_str}")
        kubeconfig = state.get("kubeconfig") or ""
        if kubeconfig:
            parts.append(f"kubeconfig: {kubeconfig}")
        updates["messages"] = [HumanMessage(content="\n".join(parts), id=str(uuid4()))]

    # Record HumanMessages to session store immediately so they appear
    # in correct chronological order (before execute_loop's ToolMessages).
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
    CLI (structured + NL) and TUI after Intent Graph confirms inject.
    """
    task_id = state.get("task_id", "") or ""
    memory_dir = settings.resolved_memory_dir
    updates: dict = {"approved_target": None, "screener_route": None}

    # Reset module-level time_wait state so it doesn't leak from a
    # previous task (same rationale as load_memory).
    from chaos_agent.tools.wait import reset_wait_state
    reset_wait_state()

    tracker = get_tracker(task_id)
    tracker.start(StatusCategory.NODE, "pipeline_init", "Loading operational context")

    try:
        op_memory = OperationalMemory(memory_dir / "MEMORY.md")
        updates["operational_notes"] = op_memory.read()
    except Exception as e:
        logger.warning(f"Failed to load operational memory: {e}")
        updates["operational_notes"] = ""

    from chaos_agent.agent.spec.fault_spec import read_fault_spec
    _spec = read_fault_spec(state)
    try:
        from chaos_agent.persistence.task_store import get_task_store
        store = await get_task_store()
        namespace = _spec.namespace if _spec else ""
        # Multi-tenant isolation: only query the current tenant's active experiments
        _tenant_id = state.get("tenant_id", "") or ""
        # Workspace axis (platform mode): empty locally = unfiltered, same contract.
        _workspace_id = state.get("workspace_id", "") or ""
        updates["experiment_history"] = await store.query_active(namespace=namespace, tenant_id=_tenant_id, workspace_id=_workspace_id)
    except Exception as e:
        logger.warning(f"Failed to load experiment history: {e}")
        updates["experiment_history"] = []

    nl_description = state.get("input") or (_spec.user_description if _spec else "")
    if nl_description:
        # Explicit id — same dedup-key rationale as load_memory above.
        updates["messages"] = [HumanMessage(content=nl_description, id=str(uuid4()))]
    elif _spec and _spec.is_complete:
        parts = [f"Execute fault injection: {_spec.scope}-{_spec.fault_target}-{_spec.fault_action}"]
        if _spec.namespace:
            parts.append(f"Target namespace: {_spec.namespace}")
        if _spec.names:
            parts.append(f"Target names: {', '.join(_spec.names)}")
        if _spec.params:
            param_str = ", ".join(f"{k}={v}" for k, v in _spec.params.items() if v)
            if param_str:
                parts.append(f"Parameters: {param_str}")
        kubeconfig = state.get("kubeconfig") or ""
        if kubeconfig:
            parts.append(f"kubeconfig: {kubeconfig}")
        updates["messages"] = [HumanMessage(content="\n".join(parts), id=str(uuid4()))]

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
    """Auto-append experience to EXPERIENCE.md when self_evolution is enabled."""
    _evolution_span = None
    try:
        from chaos_agent.observability.tracer import get_trace
        _trace = await get_trace(task_id)
        _evolution_span = _trace.start_span("self_evolution")
    except Exception:
        _evolution_span = None

    try:
        from chaos_agent.agent.experience import append_experience
        from chaos_agent.agent.spec.fault_spec import fault_type_from_state
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


def _infer_failure_detail(state: AgentState) -> dict:
    """Infer failure_detail when task is in a failed state but none was set."""
    outcome = read_operation_outcome(state)
    if outcome.failure_detail:
        return {}
    from chaos_agent.agent.state_mgmt.state_helpers import fail_state
    from chaos_agent.agent.result.verdict import FailureCategory

    error = outcome.error
    verification = read_inject_verification(state)
    replan_count = state.get("replan_count", 0)
    verify_replan_count = state.get("verify_replan_count", 0)
    replan_context = state.get("replan_context")
    _any_replan = replan_count > 0 or verify_replan_count > 0
    msgs = state.get("messages", [])
    planning_alternatives = state.get("_planning_alternatives", "")

    if error:
        if _any_replan and replan_context:
            return fail_state(
                FailureCategory.REPLAN_EXHAUSTED,
                f"attempts={replan_count + verify_replan_count}, last_error={error[:200]}",
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
        l1_status = l1.get("status", "unknown")
        l2_status = l2.get("status", "unknown")
        # Defense-in-depth exemption: an expired Layer1 record (Destroyed before
        # or after its timeout) only proves the RECORD is gone, not that the
        # fault never took effect. When Layer 2 independently verified the fault
        # effects on the cluster (verified + passed), the Layer1 failure must
        # not veto the verdict (task inject-e47de3e8: executor cleanup destroyed
        # the record, Layer2 verified the restarts, task was wrongly failed).
        layer1_expired_overridden = (
            l1_status == "failed"
            and bool(l1.get("expired"))
            and l2_status == "passed"
            and level == "verified"
        )
        if (
            # Honest ignorance is never success (round-15: the unverified
            # veto was a bare single-word tuple hand copy).
            level in INJECT_VETO_VALUES
            or (l1_status == "failed" and not layer1_expired_overridden)
            or l2_status == "failed"
        ):
            return fail_state(
                FailureCategory.VERIFICATION_FAILED,
                f"Layer1={l1_status}, Layer2={l2_status}, level={level}",
                msgs,
                alternatives=planning_alternatives,
            )
    if _any_replan and replan_context and not has_active_fault(state) and not verification:
        return fail_state(
            FailureCategory.REPLAN_EXHAUSTED,
            f"attempts={replan_count + verify_replan_count}, injection never succeeded",
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
        if store is not None and is_real_task_id(task_id):
            merged = dict(state)
            merged.update(updates)

            result_summary: str | dict = ""
            _data: dict | None = None
            try:
                from chaos_agent.agent.result.operation_result import build_inject_data_from_state
                from chaos_agent.memory.session_finalizer import (
                    build_inject_session_summary,
                )

                _data = build_inject_data_from_state(merged, task_id)
                result_summary = build_inject_session_summary(_data)
            except Exception:
                logger.debug(
                    "build result_summary failed for task=%s",
                    task_id, exc_info=True,
                )

            # Derive the session status from the same canonical result
            # projection the defensive finalize uses (task-ff057e7f):
            # ``build_inject_data_from_state`` applies the fail-closed
            # ``terminal_task_state`` and ``inject_session_status`` maps
            # it to a status. A experiment_uid proves a creation request was
            # accepted, not that the fault took effect, so it must never
            # upgrade a run without a verdict to "completed" — the old
            # experiment_uid-leniency here contradicted the result_summary
            # written by this very function.
            from chaos_agent.memory.session_finalizer import inject_session_status
            if confirmed_intent in ("chat", "recover"):
                final_status = "completed"
            elif _data is not None:
                final_status = inject_session_status(_data)
            else:
                final_status = "failed"
            full_messages = list(state.get("messages") or [])
            store.finalize_session(
                task_id,
                remaining_messages=full_messages,
                result_summary=result_summary,
                status=final_status,
                # The working ledger lives only in LangGraph state unless
                # handed over here — finalize_session supports the field
                # and the task schema carries it, but no caller passed it,
                # so every archived task showed progress_ledger=null even
                # when update_progress had been used.
                progress_ledger=merged.get("progress_ledger"),
            )
            # Sync the frozen model name into the metric store. finalize_session
            # snapshots ``settings.model_name`` (or the result-carried value for
            # multi-model runs) into the session record; the task_details row is
            # the only place the metric chain (blade-ai metric / TUI review card /
            # trace preview) can read it from. Fire-and-forget: a sync failure
            # must never break save_memory.
            try:
                from chaos_agent.persistence.task_store import get_task_store

                frozen = (store.read_session(task_id) or {}).get("model_name") or ""
                if frozen:
                    task_store = await get_task_store()
                    await task_store.upsert(task_id, model_name=frozen)
            except Exception:
                logger.debug(
                    "model_name sync to TaskStore failed for %s (non-critical)",
                    task_id, exc_info=True,
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
    task_id = state.get("task_id", "") or ""
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
        tracker.update("Auto-appending experience to EXPERIENCE.md (self_evolution)")
        await _run_self_evolution(state, task_id, tracker)

    tracker.complete("Experiment saved to TaskStore")
    verification = read_inject_verification(state) or {}
    sync_node_status_to_session(state, "save_memory", "Experiment saved to TaskStore",
        detail={"verification_level": verification.get("level", "unknown")})

    inferred_failure = _infer_failure_detail(state)

    # Set finished_at timestamp for the task
    updates = {"finished_at": now_iso()}
    # R11 — ALWAYS write the postmortem / issue-report fields (even when
    # None) to OVERWRITE any leftover value from a prior experiment that
    # shares this LangGraph thread. The artifacts are produced upstream
    # by ``terminal_reports`` (runs on every experiment terminal path
    # ahead of this node); pass them through so the single
    # ``sync_to_store`` below persists them. Read through the canonical
    # outcome reader (state-field contract: terminal outcome fields are
    # high-risk direct reads).
    _report_outcome = read_operation_outcome(state)
    updates["postmortem"] = _report_outcome.postmortem
    # Same R11 overwrite contract for the issue-report payload.
    updates["issue_report"] = _report_outcome.issue_report

    updates.update(inferred_failure)

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
