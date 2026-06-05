"""AgentState definition for LangGraph StateGraph."""

from typing import Annotated, Optional

from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages

from chaos_agent.utils.time import now_iso, parse_iso_timestamp


def _ts_add_messages(left, right):
    """Wrap add_messages to stamp Beijing wall-clock on every incoming message."""
    ts = now_iso()
    if isinstance(right, list):
        for msg in right:
            kwargs = getattr(msg, "additional_kwargs", None)
            if isinstance(kwargs, dict):
                kwargs.setdefault("_ts", ts)
    return add_messages(left, right)


def infer_task_state(values: dict) -> str:
    """Infer the overall task_state from AgentState values.

    Lifecycle states reflecting the two major stages (injection + recovery):
      - injecting: fault injection in progress
      - injected: injection completed, fault is active, awaiting recovery
      - recovering: fault recovery in progress
      - recovered: fault has been fully recovered
      - partial_recovered: fault partially recovered
      - failed: injection or recovery failed
      - rejected: safety check rejected the injection
      - completed: non-injection intents (chat/recover-bridge)
    """
    # Non-injection intents — no fault lifecycle (must check FIRST)
    # "recover" intent in inject_graph is a bridge state (confirmed but
    # actual recovery happens in recover_graph), so it's also "completed".
    if values.get("confirmed_intent") in ("chat", "recover"):
        return "completed"

    operation = values.get("operation", "")
    safety_status = values.get("safety_status", "pending")
    skill_name = values.get("skill_name")
    blade_uid = values.get("blade_uid")
    verification = values.get("verification")
    error = values.get("error")
    result = values.get("result") or {}
    if not isinstance(result, dict):
        result = {}

    # Safety rejection
    if safety_status == "rejected":
        return "rejected"

    # Error — but replan in progress is not a failure
    if error:
        if values.get("replan_count", 0) > 0 and values.get("replan_context"):
            pass  # Replan in progress, continue to normal state inference
        else:
            return "failed"

    # Replan exhaustion: replan was attempted but graph completed without success.
    if values.get("replan_count", 0) > 0 and values.get("replan_context"):
        if not blade_uid and not verification:
            return "failed"

    # Recovery operation
    if operation == "recover":
        recover_verification = values.get("recover_verification")
        if recover_verification:
            if result.get("recovered"):
                # Distinguish full recovery from partial recovery
                recovery_level = result.get("recovery_level", "recovered")
                if recovery_level == "partial":
                    return "partial_recovered"
                return "recovered"
            # Non-ChaosBlade fault recovery: Layer 1 skipped, check Layer 2
            rv = recover_verification if isinstance(recover_verification, dict) else {}
            rl1 = rv.get("layer1", {})
            if rl1.get("status") == "skipped" and rv.get("level") == "recovered":
                return "recovered"
            if rl1.get("status") == "skipped" and rv.get("level") == "partial":
                return "partial_recovered"
            return "failed"
        return "recovering"

    # Injection lifecycle
    if not verification:
        # Still in injection process
        if blade_uid:
            return "injecting"
        if skill_name:
            return "injecting"
        return "injecting"

    # Verification done for injection
    layer1 = verification.get("layer1", {}) if isinstance(verification, dict) else {}
    layer2 = verification.get("layer2", {}) if isinstance(verification, dict) else {}
    l1_status = layer1.get("status")
    l1_pass = l1_status == "passed"
    l1_skip_no_chaos = l1_status == "skipped"  # non-ChaosBlade fault, Layer 1 not applicable
    l2_status = layer2.get("status", "unknown") if isinstance(layer2, dict) else "unknown"
    # ChaosBlade (L1 passed):
    if l1_pass and l2_status in ("passed", "skipped"):
        return "injected"
    # L2 "unknown": LLM didn't produce a clear verification conclusion.
    # Check the level to decide — if the system determined it's "unverified",
    # we cannot confirm the injection worked despite L1 showing Running.
    if l1_pass and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level == "unverified":
            return "failed"
        return "injected"
    # L2 "partial": the LLM's Overall field is the authority
    # on whether the partial result is acceptable (e.g., timing delays) or a real failure.
    if l1_pass and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "injected"
        return "failed"
    # Non-ChaosBlade (L1 skipped): Layer 2 is the ONLY verification layer,
    # so "unknown" is NOT passing — the injection cannot be confirmed.
    if l1_skip_no_chaos and l2_status == "passed":
        return "injected"
    # Non-CB path L2=partial: defer to level (mirrors CB path logic)
    if l1_skip_no_chaos and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "injected"
        return "failed"
    # Side-effect confirmation: L1 passed + evidence destroyed by side-effect
    # → not a failure, but a valid drill finding (e.g., burn → OOMKill → restart)
    if l1_pass and l2_status == "recovered_before_observation":
        side_effects = verification.get("side_effects") if isinstance(verification, dict) else None
        if side_effects and any(v for v in side_effects.values() if v):
            return "injected"
        return "failed"
    return "failed"


def infer_stage(values: dict) -> Optional[str]:
    """Infer the current major stage.

    Returns: injection / recovery / None
    - injection: fault injection in progress or completed (awaiting recovery)
    - recovery: fault recovery in progress or completed
    - None: non-injection intents (chat/recover-bridge) have no fault stage
    """
    # Non-injection intents — no fault stage (must check BEFORE operation)
    # "recover" intent in inject_graph is a bridge state (actual recovery
    # happens in recover_graph), so it has no injection/recovery stage here.
    if values.get("confirmed_intent") in ("chat", "recover"):
        return None

    operation = values.get("operation", "")

    if operation == "recover":
        return "recovery"

    return "injection"


def infer_phase(values: dict) -> str:
    """Infer the current phase within the major stage.

    Injection phases:  planning → safety_check → confirming → executing → verifying → verification_passed / verification_failed / replanning
    Recovery phases:   recovering → verifying → recovered / partial_recovered / verification_failed
    Non-injection intents (chat/recover-bridge): return None (no phase applicable)
    """
    # Non-injection intents — no fault phase applicable in inject_graph context.
    # "recover" intent in inject_graph is a bridge state (actual recovery
    # happens in recover_graph), so it has no injection phase either.
    intent = values.get("confirmed_intent")
    if intent in ("chat", "recover"):
        return None

    operation = values.get("operation", "")
    safety_status = values.get("safety_status", "pending")
    skill_name = values.get("skill_name")
    blade_uid = values.get("blade_uid")
    verification = values.get("verification")
    error = values.get("error")
    needs_confirmation = values.get("needs_confirmation", False)

    if error:
        return "failed"
    if safety_status == "rejected":
        return "rejected"

    # Dry-Run preview (TUI `/plan`): once a plan_summary has been generated and
    # dry_run is still True, surface the dedicated phase so reviewers and the
    # status bar can tell this is a preview, not a real injection.
    if values.get("dry_run") and values.get("plan_summary"):
        return "dry_run_planned"

    # Replan in progress (Phase 2 errored, routed back to Phase 1)
    if values.get("replan_context") and values.get("replan_count", 0) > 0:
        if not blade_uid:
            return "replanning"

    # --- Recovery phases ---
    if operation == "recover":
        recover_verification = values.get("recover_verification")
        if recover_verification:
            result = values.get("result") or {}
            if not isinstance(result, dict):
                result = {}
            if result.get("recovered"):
                recovery_level = result.get("recovery_level", "recovered")
                return "partial_recovered" if recovery_level == "partial" else "recovered"
            return "verification_failed"
        return "recovering"

    # --- Injection phases ---
    if not skill_name and not blade_uid:
        return "planning"
    if not skill_name and blade_uid:
        # blade created but skill not yet identified (edge case)
        return "executing"
    if needs_confirmation:
        return "confirming"
    if safety_status in ("safe", "warning") and not blade_uid:
        return "safety_check"
    if not blade_uid:
        return "planning"
    if not verification:
        return "executing"
    # Verification result
    layer1 = verification.get("layer1", {}) if isinstance(verification, dict) else {}
    layer2 = verification.get("layer2", {}) if isinstance(verification, dict) else {}
    l1_status = layer1.get("status")
    l1_pass = l1_status == "passed"
    l1_skip_no_chaos = l1_status == "skipped"
    l2_status = layer2.get("status", "unknown") if isinstance(layer2, dict) else "unknown"
    # ChaosBlade (L1 passed):
    if l1_pass and l2_status in ("passed", "skipped"):
        return "verification_passed"
    # L2 "unknown": LLM didn't produce a clear conclusion — check level
    if l1_pass and l2_status == "unknown":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level == "unverified":
            return "verification_failed"
        return "verification_passed"
    # L2 "partial": defer to LLM's Overall field
    if l1_pass and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "verification_passed"
        return "verification_failed"
    # Non-ChaosBlade (L1 skipped): L2 is the ONLY verification, "unknown" is NOT passing
    if l1_skip_no_chaos and l2_status in ("passed", "skipped"):
        return "verification_passed"
    # Non-CB path L2=partial: defer to verification level (mirrors CB path logic)
    if l1_skip_no_chaos and l2_status == "partial":
        level = verification.get("level", "unknown") if isinstance(verification, dict) else "unknown"
        if level in ("verified", "partial"):
            return "verification_passed"
        return "verification_failed"
    # Side-effect confirmation: L1 passed + container restart destroyed evidence
    # → not a failure, but a valid drill finding (e.g., burn → OOMKill → restart)
    if l1_pass and l2_status == "recovered_before_observation":
        side_effects = verification.get("side_effects") if isinstance(verification, dict) else None
        if side_effects and side_effects.get("container_restarts"):
            return "verification_passed"
        return "verification_failed"
    return "verification_failed"


def infer_inject_status(task_state: str, operation: str = "") -> str:
    """Infer the injection phase result from task state and operation.

    Returns: success / failed / in_progress / pending
    """
    # Recovery operation means injection already succeeded
    if operation == "recover":
        return "success"

    if task_state in ("injected", "recovering", "recovered", "partial_recovered"):
        return "success"
    if task_state == "injecting":
        return "in_progress"
    if task_state in ("failed", "rejected"):
        return "failed"
    return "pending"


def infer_recover_status(task_state: str, operation: str = "") -> str:
    """Infer the recovery phase result from task state and operation.

    Returns: success / failed / in_progress / pending
    """
    if task_state in ("recovered", "partial_recovered"):
        return "success"
    if task_state == "recovering":
        return "in_progress"
    if task_state == "failed" and operation == "recover":
        return "failed"
    return "pending"


def infer_status(stage: Optional[str], task_state: str, operation: str = "") -> Optional[str]:
    """Infer the current stage's status (unified for injection/recovery).

    Returns: success / failed / in_progress / pending / None
    - injection stage: delegates to infer_inject_status()
    - recovery stage: delegates to infer_recover_status()
    - None (chat intent): returns None (no fault status applicable)
    """
    if stage == "injection":
        return infer_inject_status(task_state, operation)
    elif stage == "recovery":
        return infer_recover_status(task_state, operation)
    return None


def strip_side_effects(verification: dict | None) -> dict | None:
    """Remove internal-only side_effects field from verification dict.

    side_effects is used by infer_phase for result mapping. We strip it
    from the *verification* subdict so older API consumers don't see an
    unexpected nested field, but ``build_status_data`` re-exposes it at
    the top level (``data["side_effects"]``) — UIs need it to surface
    "your fault caused a real container restart" signal to operators.
    """
    if not verification or not isinstance(verification, dict):
        return verification
    v = dict(verification)
    v.pop("side_effects", None)
    return v


def _extract_side_effects(verification: dict | None) -> dict:
    """Pull ``side_effects`` out of verification before it gets stripped.

    Returns a plain dict (possibly empty) so callers can branch on truthiness
    without re-coalescing None. The two known signal shapes are:
      - ``container_restarts``: list of ``{pod, restart_count, reason, note}``
      - any future signal we add (kept generic on purpose)
    """
    if not isinstance(verification, dict):
        return {}
    raw = verification.get("side_effects")
    if not isinstance(raw, dict):
        return {}
    return dict(raw)


def _build_side_effects_summary(verification: dict | None) -> str:
    """Build a one-line summary of all side-effect detection results.

    Enumerates every detector category with its count so the TUI can
    display "what was checked" regardless of whether issues were found.
    The summary is assembled here (backend) so new detectors automatically
    appear without a TUI release.
    """
    from chaos_agent.agent.nodes._side_effect_detectors import _DETECTORS

    if not isinstance(verification, dict):
        return ""
    raw = verification.get("side_effects")
    detected = dict(raw) if isinstance(raw, dict) else {}

    _KEY_LABELS = {
        "container_restarts": "容器重启",
        "evicted_pods": "Pod驱逐",
        "oom_killed_pods": "OOMKill",
        "crash_loop_pods": "CrashLoop",
        "endpoint_removals": "Endpoint移除",
        "hpa_scaling": "HPA扩缩",
        "probe_failures": "探针失败",
        "dependency_errors": "依赖异常",
    }

    parts = []
    for d in _DETECTORS:
        items = detected.get(d.key, [])
        count = len(items) if isinstance(items, list) else 0
        label = _KEY_LABELS.get(d.key, d.key)
        parts.append(f"{label}: {count}")

    total = sum(len(v) for v in detected.values() if isinstance(v, list))
    if total == 0:
        return f"未检测到连带影响 ({', '.join(parts)})"
    return f"检测到 {total} 项连带影响 ({', '.join(parts)})"


def _derive_failure_reason(values: dict) -> str:
    """Derive a failure_reason string from state, preferring failure_detail."""
    reason = values.get("failure_reason") or ""
    if reason:
        return reason
    detail = values.get("failure_detail")
    if not detail or not isinstance(detail, dict):
        return ""
    from chaos_agent.agent.verdict import FailureDetail
    try:
        return FailureDetail.model_validate(detail).to_reason_string()
    except Exception:
        return detail.get("category", "")


def extract_ui_diagnostics(values: dict) -> dict:
    """Return the UI-visible diagnostic fields for a result envelope payload.

    Centralizes which fields propagate from AgentState into stream events,
    so all production sites (cli/runner.py, server/routes/inject_stream.py)
    surface the same set without recopying boilerplate. Anything that flows
    through here becomes visible in `render_result` via `_read_diagnostic`.
    """
    failure_detail = values.get("failure_detail")
    failure_reason = _derive_failure_reason(values)

    verification = values.get("verification")
    return {
        "failure_reason": failure_reason,
        "failure_detail": failure_detail,
        "replan_count": int(values.get("replan_count") or 0),
        "replan_history": list(values.get("replan_history") or []),
        "side_effects": _extract_side_effects(verification),
        "side_effects_summary": _build_side_effects_summary(verification),
    }


def build_status_data(task_id: str, values: dict) -> dict:
    """Build a complete status data dict from LangGraph checkpoint values.

    Used by both CLI AgentRunner.status() and Server status route.
    """

    from chaos_agent.agent.fault_spec import FaultSpec

    skill_name = values.get("skill_name") or ""
    blade_uid = values.get("blade_uid") or ""
    spec = FaultSpec.from_dict(values.get("fault_spec"))
    blade_params = dict(spec.params) if spec else {}
    target = {
        "namespace": spec.namespace if spec else "",
        "names": list(spec.names) if spec else [],
        "labels": dict(spec.labels) if spec else {},
        "resource_type": spec.scope if spec else "",
    }
    verification = values.get("verification")
    error = values.get("error")
    safety_reason = values.get("safety_reason") or ""

    # Infer fault_type from spec; fall back to skill_name
    fault_type = spec.fault_type if (spec and spec.fault_type) else skill_name

    # Timestamps
    created_at = values.get("created_at") or ""
    finished_at = values.get("finished_at") or ""

    # Calculate duration
    duration_ms = 0
    if created_at and finished_at:
        try:
            ct = parse_iso_timestamp(created_at)
            ft = parse_iso_timestamp(finished_at)
            duration_ms = int((ft - ct).total_seconds() * 1000)
        except (ValueError, TypeError):
            pass

    failure_detail = values.get("failure_detail")
    failure_reason_raw = _derive_failure_reason(values)
    merged_error = failure_reason_raw or error or ""

    task_state = infer_task_state(values)
    stage = infer_stage(values)
    status = infer_status(stage, task_state, values.get("operation", ""))

    data = {
        "task_id": task_id,
        "stage": stage,
        "status": status,
        "phase": infer_phase(values),
        "fault_type": fault_type,
        "skill_name": skill_name,
        "target": target,
        "params": blade_params or None,
        "blade_uid": blade_uid,
        "safety_status": values.get("safety_status", "pending"),
        "safety_reason": safety_reason,
        "needs_confirm": values.get("needs_confirmation", False),
        "verification": strip_side_effects(verification),
        "recover_verification": strip_side_effects(values.get("recover_verification")),
        "side_effects": _extract_side_effects(verification),
        "plan_summary": values.get("plan_summary", ""),
        "error": merged_error,
        "failure_reason": failure_reason_raw,
        "failure_detail": failure_detail,
        "intent_confidence": float(values.get("intent_confidence") or 0.0),
        "replan_count": int(values.get("replan_count") or 0),
        "replan_history": list(values.get("replan_history") or []),
        "created_at": created_at,
        "updated_at": now_iso(),
        "finished_at": finished_at,
        "duration_ms": duration_ms,
    }
    if values.get("baseline_data"):
        data["baseline_data"] = values["baseline_data"]

    return data



class AgentState(MessagesState):
    """State for the Chaos Engineering Agent inject/recover graphs."""

    # Override MessagesState.messages to inject wall-clock timestamps
    messages: Annotated[list, _ts_add_messages]

    # Task identification
    task_id: str = ""
    tui_session_id: str = ""  # Owning TUI session (empty for non-TUI callers)
    parent_task_id: str = ""  # For recover: the inject task_id being recovered
    operation: str = ""  # inject/recover/chat

    # Skill matching
    skill_name: Optional[str] = None

    # Natural language description (NL mode entry, used only by entry-
    # point routing — not authoritative for fault context, which lives
    # on ``fault_spec.user_description`` after FaultSpec construction).
    input: Optional[str] = None

    # Safety assessment
    safety_status: str = "pending"  # pending/safe/unsafe/warning/rejected
    safety_reason: Optional[str] = None
    safety_checked_detail: Optional[str] = None
    conflict_uids: Optional[list[str]] = None  # UIDs of existing active experiments
    # E10 — multi-dimensional numeric safety score (0-100 per dim +
    # weighted overall). Stored as dict (not SafetyScore) for the same
    # JSON-roundtrip reason FaultSpec uses dict-form on state.
    safety_score: Optional[dict] = None

    # Execution-level blast radius declared by the LLM in finish_planning.
    # "target-only" | "namespace-wide" | "cluster-wide"
    blast_radius_scope: Optional[str] = None
    blast_radius_detail: Optional[str] = None

    # Confirmation
    needs_confirmation: bool = False

    # Execution plan
    plan: Optional[str] = None
    plan_path: Optional[str] = None  # saved plan file path (memory/plan/{task_id}.md)
    is_complex: Optional[bool] = None  # True if task requires a formal plan document

    # ChaosBlade experiment UID
    blade_uid: Optional[str] = None

    # Inject-phase context for recover LLM (not recorded in session)
    inject_context: Optional[str] = None

    # K8s connection
    kubeconfig: Optional[str] = None
    kube_context: Optional[str] = None

    # Results
    result: Optional[dict] = None
    error: Optional[str] = None

    # Timestamps
    created_at: Optional[str] = None   # ISO 8601, set at task creation
    finished_at: Optional[str] = None  # ISO 8601, set when task finishes
    injection_start_time: Optional[str] = None  # ISO 8601, set when blade_create succeeds

    # Verification (two-layer: layer1=blade_status, layer2=fault-specific)
    verification: Optional[dict] = None
    recover_verification: Optional[dict] = None

    # E2 — structured observation timeline. Each entry:
    #   {iteration: int, timestamp: ISO 8601, tool_call_id: str,
    #    tool_name: str, metrics: dict[str, str]}
    # Populated by PreReasoningHook from ToolMessage extracted_metrics
    # across ALL nodes (inject / recover / verifier), not just verifier —
    # the field name is intentionally generic. Survives [Compressed
    # History] compaction because it lives on state, not on the message
    # list, which is what makes the Phase 3 evidence cross-check in
    # verifier verdict parsing robust to compression.
    metric_observations: Optional[list[dict]] = None

    # Layer 1 result caches (persisted across ReAct tool_call iterations)
    # Without these, layer1 results are lost when LLM makes tool_calls,
    # causing "Layer1: unknown" on subsequent iterations.
    inject_layer1_cache: Optional[dict] = None
    recover_layer1_cache: Optional[dict] = None

    # Recovery phase tracking (for non-ChaosBlade faults)
    # "layer1_recovery" = LLM-driven recovery execution phase
    # "layer2_verification" = LLM-driven verification phase
    recover_phase: str = "layer1_recovery"

    # Recovery Layer 1 type: "deterministic" (host blade_destroy) or "llm_driven"
    # (non-ChaosBlade / kubectl-exec injection). Set by recover_verifier_loop when
    # Layer 1 transitions to Layer 2; consumed by Layer 2 prompt builder and
    # finalize_recover_verification's retry-recovery path.
    recover_layer1_type: Optional[str] = None

    # Layer 1 iteration count (separate from verifier_loop_count)
    layer1_iteration_count: int = 0

    # Whether Layer 2 context has been added to messages
    # (needed because non-ChaosBlade Layer 2 may start at count > 1)
    layer2_context_added: bool = False

    # Scheme B (recover): set by recover_verifier_loop to signal
    # finalize_recover_verification that the verdict was produced on the FIRST
    # Layer 2 turn (before any kubectl verification) → finalize's anti-laziness
    # guard fires once and loops back. Mirrors the old is_first_layer2 guard.
    recover_layer2_first: bool = False

    # Memory (Layer 2-3)
    compressed_summary: Optional[str] = None
    experiment_history: Optional[list] = None
    operational_notes: Optional[str] = None

    # Loop control
    agent_loop_count: int = 0
    execute_loop_count: int = 0
    verifier_loop_count: int = 0

    # Replan control (Phase 2 → Phase 1 feedback loop)
    replan_requested: bool = False                # LLM 或自动检测触发的 replan 请求
    replan_count: int = 0                         # replan 循环次数
    replan_context: Optional[dict] = None         # Phase 2 错误上下文
    replan_history: Optional[list] = None         # 历次 replan 记录

    # Patch C — Wall-clock guard (single source of truth for "when did
    # this turn start"). Stamped at agent_loop_node entry the first
    # time. Router functions consult ``time.time() - pipeline_started_at``
    # against ``settings.max_inject_seconds`` to enforce a turn-level
    # timeout that's independent of iteration counters. ``0.0`` = not
    # yet stamped (the wall-clock guard is a no-op).
    pipeline_started_at: float = 0.0

    # Patch B — Counter for INFRA_TRANSIENT short-retry budget. Each
    # router-detected transient error increments this; ``settings.max_
    # transient_retry`` is the hard cap before SHORT_RETRY is escalated
    # to END_FAILED.
    transient_retry_count: int = 0

    # Patch E — Pipeline attempt tracking. ``pipeline_attempt`` starts
    # at ``0`` and is incremented by ``begin_attempt`` (in
    # chaos_agent.agent.attempt_tracker). ``pipeline_attempts_history``
    # records each attempt's metadata so the TUI / TaskStore can
    # surface "this is attempt #2 because the LLM switched targets"
    # rather than the user seeing what looks like a retry of a failure.
    pipeline_attempt: int = 0
    pipeline_attempts_history: Optional[list] = None

    # Patch D — Target health report from ``safety_check`` (after
    # ``assess_target_health``). Serialised form of ``HealthReport``;
    # see ``chaos_agent.agent.target_health`` for the schema. ``None``
    # when the pre-check is disabled or the scope has no checker.
    # Read by ``confirmation_gate`` / TUI confirm card to surface
    # blocker conditions (DiskPressure, Evicted, …) before the
    # operator approves an inject that's likely to fail.
    target_health_report: Optional[dict] = None
    # E18 — Injection feasibility report (headroom assessment).
    # Shape: FeasibilityReport.to_dict() from chaos_agent.agent.feasibility.
    feasibility_report: Optional[dict] = None

    # Failure reason (only set when task result is "failed", None on success)
    failure_reason: Optional[str] = None
    # E3 — Structured failure detail (FailureDetail.model_dump()). Replaces
    # the freeform failure_reason string with category + context + llm_analysis.
    failure_detail: Optional[dict] = None

    # T6 — Postmortem auto-generation output. Populated by save_memory after
    # the experiment finalises; None when postmortem is disabled, the task
    # belongs to a non-injection intent, the failure category lies outside
    # the postmortem whitelist, or LLM generation timed out / errored.
    # Shape: {"path": str, "markdown": str, "summary": str}.
    # Goes into the result envelope (data.postmortem) verbatim so the TS
    # TUI can render PostmortemSection without a second round-trip.
    postmortem: Optional[dict] = None

    # Injection method tracking (used by verifier to choose verification strategy)
    injection_method: Optional[str] = None  # "host_blade" | "kubectl_exec" | "kubectl_native" | None

    # Tool pod name used during kubectl exec injection (recorded at injection time,
    # used by verifier/recover_verifier to prefer the original pod)
    kubectl_exec_pod_name: Optional[str] = None

    # Skill use-case content (populated during injection, used by Layer 2 verification)
    skill_case_content: Optional[str] = None  # Full content of the matched skill use-case file
    matched_use_case_path: Optional[str] = None  # Catalogue path resolved by match_use_case()

    # Guard flag: extract_planning_metadata sets True when no catalogue
    # case was loaded; conditional edge routes back to agent_loop.
    planning_rejected: bool = False

    # Injection verification summary (Layer 2 observations from inject phase, used as baseline for recover)
    inject_verification_summary: Optional[str] = None

    # ``direct`` mode flag — entry-point routing only, not part of
    # FaultSpec because the spec describes WHAT to inject, not the
    # execution-path choice (direct vs LLM ReAct).
    direct: bool = False                 # True: skip LLM, go direct path
    # Parsed key params from blade flags (runtime artefact of
    # ``execute_loop`` parsing the LLM's ``blade_create`` invocation —
    # downstream verifier consumes ``state.blade_parsed_flags``).
    blade_parsed_flags: Optional[dict] = None  # {"path": "/tmp", "percent": "85", ...}

    # Original replicas for kubectl scale-based faults (resource_name -> replica_count)
    # Used to safely restore replicas after scale-down injection
    original_replicas: Optional[dict] = None  # {"accounting": 3, ...}

    # Pre-injection baseline for direct mode (captured by baseline_capture node)
    baseline_data: Optional[dict] = None

    # FCAT context (collected once at direct_setup / execute_loop entry, reused downstream)
    target_metadata: Optional[dict] = None   # {pod_memory_limit_mb, active_same_action_experiments, ...}
    evidence_snapshot: Optional[dict] = None  # P0: quick evidence after blade_create (ls + df)
    disk_burn_post_check: Optional[dict] = None   # Post-injection I/O throughput verification result
    disk_fill_post_check: Optional[dict] = None    # Post-injection fill file verification result
    se_snapshot: Optional[dict] = None       # Pre-injection side-effect snapshot (SideEffectSnapshot.to_dict())
    reverify_count: int = 0                  # P2: verifier re-verification attempt count
    reverify_gaps: Optional[list[str]] = None # P2: gap types that triggered re-verification
    # Debug pods the verifier's programmatic cleanup has already attempted
    # to delete. Persisted across verifier re-entries (reverify loop, ReAct
    # iterations) so cleanup is exactly-once per pod.
    #
    # Without this, every verifier re-entry re-scans the full message history,
    # re-discovers the same pod names, and re-issues ``kubectl delete``. After
    # the first delete succeeds, subsequent attempts return "NotFound" and
    # inflate the failure-rate stat (observed as 8 spurious NotFound failures
    # in task-712629116b64).
    #
    # ``list[str]`` not ``set[str]``: LangGraph checkpoint requires JSON-
    # serialisable shapes. Stored sorted for deterministic snapshots.
    cleaned_debug_pods: Optional[list[str]] = None
    force_override: bool = False             # P1: CLI --force-override flag

    # Intent clarification (TUI mode)
    confirmed_intent: Optional[str] = None  # "inject" | "recover" | "chat" | None
    interaction_mode: str = "cli"  # "cli" / "tui"
    intent_context: Optional[str] = None         # Intent description text (passed to planning node)
    intent_confidence: float = 0.0               # Confidence score 0.0-1.0
    clarification_round: int = 0                 # Low-confidence clarification round tracking
    dialogue_round: int = 0                      # Overall dialogue round tracking (chat + clarification)
    intent_reasoning: Optional[str] = None       # LLM classification reasoning (audit trail)
    needs_task_selection: bool = False            # RECOVER intent needs user to pick a task
    recover_task_id: Optional[str] = None        # task_id of the inject experiment to recover

    # Dry-Run multi-turn planning (TUI `/plan`).
    # When True: confirmation_gate emits a "what would happen" AIMessage and
    # the router exits to END before any side-effecting node runs. The user
    # can iterate the plan over multiple turns and finally call `/run` (no
    # args) which sets dry_run=False and re-invokes the pipeline.
    dry_run: bool = False

    # Single source of truth for "what fault to inject where".
    # Populated at every entry point (CLI structured / CLI NL / HTTP API
    # / TUI / direct) via ``FaultSpec.from_*`` constructors; rewritten
    # by ``intent_clarification`` once the LLM emits ``submit_fault_intent``
    # in NL flows. Consumers read it via ``read_fault_spec(state)`` and
    # get back a strongly-typed ``FaultSpec`` instance. See
    # ``chaos_agent.agent.fault_spec`` for the dataclass and constructors.
    #
    # Schema (dict form for LangGraph checkpoint compat):
    #   {namespace, scope, names: list[str], labels: dict[str, str],
    #    blade_target, blade_action, params: dict[str, str],
    #    params_flags: list[str], duration_seconds: int,
    #    source: str, user_description: str}
    #
    # ``None`` = no fault context yet (rare; would only happen if a
    # caller bypasses the entry-point constructors).
    fault_spec: Optional[dict] = None

    # Target-drift guard — frozen snapshot of "what the user approved".
    # Populated by ``confirmation_gate`` when the user accepts a plan;
    # consumed by the screener node in front of ``execute_loop``'s
    # ToolNode (see ``chaos_agent.agent.target_guard``). Cleared on
    # TURN_DONE / TURN_ABORTED / replan so the next confirmation_gate
    # freezes a fresh approval. Schema mirrors
    # ``target_guard.ApprovedTarget`` (dict form for LangGraph
    # serialisability):
    #   {scope, namespace, names: list[str], labels: dict[str, str],
    #    is_namespace_wide: bool, blade_target, blade_action,
    #    lock_fault_type: bool}
    # ``None`` = no approval on record (planning phase, or post-cleanup).
    approved_target: Optional[dict] = None

    # Transient routing hint written by the two screeners (one per
    # phase) and consumed by their respective ``route_after_*``
    # dispatcher on the next conditional edge:
    #   - ``phase1_screener``    writes "pass" or "retry"
    #     (consumed by ``route_after_phase1_screener``)
    #   - ``tool_screener``      writes "pass", "replan", or "retry"
    #     (consumed by ``route_after_screener``)
    # The field is shared because (a) the two screeners run in
    # disjoint graph regions (phase 1 = before confirmation, phase 2 =
    # after baseline_capture), so values never collide; (b) every
    # screener invocation overwrites the field at function entry, so
    # no stale value can leak from one pass to another. Not meant for
    # cross-turn persistence.
    screener_route: Optional[str] = None

    # Drift-interrupt rejection counter. Incremented when user rejects a
    # target-change confirmation card. When >= 1, the next drift detection
    # hard-terminates instead of interrupting again.
    drift_reject_count: int = 0

    # Plan-change rejection counter (replan fault type switch).
    # Incremented when user rejects a plan change proposal. When >= 2,
    # plan_change_confirm hard-terminates via fail_state.
    plan_change_reject_count: int = 0

    # Plan builder (interactive guided plan construction via TUI /plan)
    plan_builder_round: int = 0        # Dialogue round counter within plan_builder
    plan_confirmed: bool = False       # submit_plan completed; /run routes to safety_check

    # Batch fault injection (submit_plan with multiple faults)
    # Stores the full submit_plan args when faults[] has more than 1 entry.
    # Single-fault submit_plan does NOT populate this (backward compatible).
    batch_submit_args: Optional[dict] = None

    # Batch execution progress (loop-back within Pipeline Graph)
    current_fault_index: int = 0
    batch_results: Optional[list] = None


class IntentState(MessagesState):
    """State for the Intent Graph (conversation layer).

    Contains only dialogue-level fields. Execution-level fields
    (blade_uid, verification, safety_status, skill_name, etc.)
    live on AgentState in the Pipeline Graph.
    """

    messages: Annotated[list, _ts_add_messages]

    tui_session_id: str = ""
    interaction_mode: str = "tui"

    # Intent recognition
    confirmed_intent: Optional[str] = None
    intent_confidence: float = 0.0
    clarification_round: int = 0
    dialogue_round: int = 0
    intent_reasoning: Optional[str] = None

    # Recover target
    needs_task_selection: bool = False
    recover_task_id: Optional[str] = None

    # Pipeline dispatch
    pipeline_task_id: Optional[str] = None
    pipeline_result_summary: Optional[str] = None
    handoff_summary: Optional[str] = None

    # FaultSpec (converged from intent_clarification)
    fault_spec: Optional[dict] = None
    input: Optional[str] = None

    # Batch fault injection (from submit_batch_intent)
    batch_submit_args: Optional[dict] = None

    # Session-level
    kubeconfig: Optional[str] = None
    kube_context: Optional[str] = None
    needs_confirmation: bool = False
    dry_run: bool = False
    compressed_summary: Optional[str] = None
    operational_notes: Optional[str] = None

    # Task ID (allocated by _allocate_operation_task_id in intent_clarification)
    task_id: str = ""
