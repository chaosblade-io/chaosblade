"""AgentState definition for LangGraph StateGraph."""

from typing import Annotated, Optional

from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages

from chaos_agent.agent.operation_outcome import (
    read_failure_reason,
    read_inject_verification,
    read_operation_outcome,
    read_recover_verification,
)
from chaos_agent.agent.skill_identity import read_active_skill_name
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
    active_skill_name = read_active_skill_name(values)
    blade_uid = values.get("blade_uid")
    verification = read_inject_verification(values)
    outcome = read_operation_outcome(values)
    error = outcome.error
    result = outcome.result or {}
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
        recover_verification = read_recover_verification(values)
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
        if active_skill_name:
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
    active_skill_name = read_active_skill_name(values)
    blade_uid = values.get("blade_uid")
    verification = read_inject_verification(values)
    outcome = read_operation_outcome(values)
    error = outcome.error
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
        recover_verification = read_recover_verification(values)
        if recover_verification:
            result = outcome.result or {}
            if not isinstance(result, dict):
                result = {}
            if result.get("recovered"):
                recovery_level = result.get("recovery_level", "recovered")
                return "partial_recovered" if recovery_level == "partial" else "recovered"
            return "verification_failed"
        return "recovering"

    # --- Injection phases ---
    if not active_skill_name and not blade_uid:
        return "planning"
    if not active_skill_name and blade_uid:
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
    return read_failure_reason(values)


def extract_ui_diagnostics(values: dict) -> dict:
    """Return the UI-visible diagnostic fields for a result envelope payload.

    Centralizes which fields propagate from AgentState into stream events,
    so all production sites (cli/runner.py, server/routes/inject_stream.py)
    surface the same set without recopying boilerplate. Anything that flows
    through here becomes visible in `render_result` via `_read_diagnostic`.
    """
    outcome = read_operation_outcome(values)
    failure_detail = outcome.failure_detail
    failure_reason = outcome.failure_reason

    verification = read_inject_verification(values)
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

    from chaos_agent.agent.fault_spec import (
        fault_type_from_state,
        legacy_params_dict,
        legacy_target_dict,
    )

    active_skill_name = read_active_skill_name(values)
    blade_uid = values.get("blade_uid") or ""
    blade_params = legacy_params_dict(values)
    target = legacy_target_dict(values)
    verification = read_inject_verification(values)
    safety_reason = values.get("safety_reason") or ""

    fault_type = fault_type_from_state(values)

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

    outcome = read_operation_outcome(values)
    failure_detail = outcome.failure_detail
    failure_reason_raw = outcome.failure_reason
    merged_error = outcome.error

    task_state = infer_task_state(values)
    stage = infer_stage(values)
    status = infer_status(stage, task_state, values.get("operation", ""))

    data = {
        "task_id": task_id,
        "stage": stage,
        "status": status,
        "phase": infer_phase(values),
        "fault_type": fault_type,
        "skill_name": active_skill_name,
        "active_skill_name": active_skill_name,
        "target": target,
        "params": blade_params or None,
        "blade_uid": blade_uid,
        "safety_status": values.get("safety_status", "pending"),
        "safety_reason": safety_reason,
        "needs_confirm": values.get("needs_confirmation", False),
        "verification": strip_side_effects(verification),
        "recover_verification": strip_side_effects(read_recover_verification(values)),
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
    """State for the Chaos Engineering Agent inject/recover graphs.

    Fields are grouped by lifecycle phase. Within each group, fields
    appear in the order they are typically populated.
    """

    # ── Core Identity ──────────────────────────────────────────────
    messages: Annotated[list, _ts_add_messages]
    task_id: str = ""
    tui_session_id: str = ""             # Owning TUI session (empty for non-TUI callers)
    parent_task_id: str = ""             # For recover: the inject task_id being recovered
    operation: str = ""                  # inject / recover / chat

    # ── Intent & Input ─────────────────────────────────────────────
    input: Optional[str] = None          # NL description (entry-point routing only)
    confirmed_intent: Optional[str] = None   # "inject" | "recover" | "chat" | None
    interaction_mode: str = "cli"        # "cli" / "tui"
    intent_context: Optional[str] = None     # Intent description text (passed to planning node)
    intent_confidence: float = 0.0       # Confidence score 0.0-1.0
    clarification_round: int = 0         # Low-confidence clarification round tracking
    dialogue_round: int = 0              # Overall dialogue round tracking (chat + clarification)
    intent_reasoning: Optional[str] = None   # LLM classification reasoning (audit trail)
    needs_task_selection: bool = False    # RECOVER intent needs user to pick a task
    recover_task_id: Optional[str] = None    # task_id of the inject experiment to recover
    dry_run: bool = False                # TUI /plan dry-run mode

    # ── Planning ───────────────────────────────────────────────────
    skill_name: Optional[str] = None
    fault_spec: Optional[dict] = None    # FaultSpec dict (see chaos_agent.agent.fault_spec)
    skill_case_content: Optional[str] = None     # Full content of the matched skill use-case file
    matched_use_case_path: Optional[str] = None  # Catalogue path resolved by match_use_case()
    plan: Optional[str] = None
    plan_summary: str = ""               # Human-facing execution preview / dry-run summary
    plan_path: Optional[str] = None      # saved plan file path (memory/plan/{task_id}.md)
    is_complex: Optional[bool] = None    # True if task requires a formal plan document
    planning_rejected: bool = False      # No catalogue case loaded; edge routes back
    _planning_rejection_reason: Optional[str] = None  # LLM rejection_reason for fail diagnosis
    _planning_alternatives: str = ""     # LLM-proposed alternatives after planning rejection
    _catalogue_rejection_nudged: bool = False  # Guard: nudge only once before accepting rejection
    plan_builder_round: int = 0          # Dialogue round counter within plan_builder
    plan_confirmed: bool = False         # submit_plan completed; /run routes to safety_check

    # ── Safety ─────────────────────────────────────────────────────
    safety_status: str = "pending"       # pending / safe / unsafe / warning / rejected
    safety_reason: Optional[str] = None
    safety_checked_detail: Optional[str] = None
    conflict_uids: Optional[list[str]] = None    # UIDs of existing active experiments
    safety_score: Optional[dict] = None          # Multi-dimensional numeric safety score (dict form)
    blast_radius_scope: Optional[str] = None     # "target-only" | "namespace-wide" | "cluster-wide"
    blast_radius_detail: Optional[str] = None
    target_health_report: Optional[dict] = None  # HealthReport dict (see chaos_agent.agent.target_health)
    feasibility_report: Optional[dict] = None    # FeasibilityReport dict (see chaos_agent.agent.feasibility)

    # ── Confirmation ───────────────────────────────────────────────
    needs_confirmation: bool = False
    approved_target: Optional[dict] = None   # ApprovedTarget dict (see chaos_agent.agent.target_guard)
    drift_reject_count: int = 0          # Target-change rejection counter
    plan_change_reject_count: int = 0    # Replan fault type switch rejection counter
    screener_route: Optional[str] = None # Transient routing hint: "pass" / "retry" / "replan"

    # ── Execution ──────────────────────────────────────────────────
    blade_uid: Optional[str] = None      # ChaosBlade experiment UID
    injection_method: Optional[str] = None   # "host_blade" | "kubectl_exec" | "kubectl_native"
    kubectl_exec_pod_name: Optional[str] = None  # Tool pod used during kubectl exec injection
    blade_parsed_flags: Optional[dict] = None    # {"path": "/tmp", "percent": "85", ...}
    direct: bool = False                 # True: skip LLM, go direct path
    original_replicas: Optional[dict] = None     # kubectl scale-based faults: {resource -> count}
    kubeconfig: Optional[str] = None
    kube_context: Optional[str] = None
    kubewiz_cluster_uuid: Optional[str] = None
    kubewiz_profile: Optional[str] = None
    inject_context: Optional[str] = None     # Inject-phase context for recover LLM
    baseline_data: Optional[dict] = None     # Pre-injection baseline (from baseline_capture node)
    target_metadata: Optional[dict] = None   # {pod_memory_limit_mb, active_same_action_experiments, ...}
    evidence_snapshot: Optional[dict] = None  # P0: quick evidence after blade_create (ls + df)
    disk_burn_post_check: Optional[dict] = None   # Post-injection I/O throughput verification
    disk_fill_post_check: Optional[dict] = None   # Post-injection fill file verification
    se_snapshot: Optional[dict] = None       # Pre-injection side-effect snapshot
    force_override: bool = False             # CLI --force-override flag
    _execute_text_nudged: bool = False       # Guard: nudge execute_loop only once for text-only exit
    _kubectl_step_nudged: bool = False       # Guard: nudge kubectl-native incomplete steps only once
    batch_submit_args: Optional[dict] = None     # Multi-fault submit_plan args
    current_fault_index: int = 0
    batch_results: Optional[list] = None

    # ── Verification ───────────────────────────────────────────────
    verification: Optional[dict] = None          # Two-layer: layer1=blade_status, layer2=fault-specific
    recover_verification: Optional[dict] = None
    inject_layer1_cache: Optional[dict] = None   # Persisted across ReAct iterations
    recover_layer1_cache: Optional[dict] = None
    metric_observations: Optional[list[dict]] = None  # Structured observation timeline (all nodes)
    inject_verification_summary: Optional[str] = None  # Layer 2 observations for recover baseline
    reverify_count: int = 0              # Re-verification attempt count
    reverify_gaps: Optional[list[str]] = None    # Gap types that triggered re-verification
    cleaned_debug_pods: Optional[list[str]] = None   # Debug pods already cleaned up (exactly-once)

    # ── Recovery ───────────────────────────────────────────────────
    recover_phase: str = "layer1_recovery"   # "layer1_recovery" | "layer2_verification"
    recover_layer1_type: Optional[str] = None    # "deterministic" | "llm_driven"
    layer1_iteration_count: int = 0
    layer2_context_added: bool = False       # Non-ChaosBlade Layer 2 may start at count > 1
    recover_layer2_first: bool = False       # Verdict on first Layer 2 turn (anti-laziness guard)

    # ── Loop Control ───────────────────────────────────────────────
    agent_loop_count: int = 0
    execute_loop_count: int = 0
    verifier_loop_count: int = 0
    pipeline_started_at: float = 0.0     # Wall-clock guard (0.0 = not yet stamped)
    transient_retry_count: int = 0       # INFRA_TRANSIENT short-retry budget
    pipeline_attempt: int = 0            # Attempt tracking (incremented by begin_attempt)
    pipeline_attempts_history: Optional[list] = None
    replan_requested: bool = False
    replan_count: int = 0
    replan_context: Optional[dict] = None
    replan_history: Optional[list] = None
    _replan_loop_reset: Optional[int] = None  # Tracks which replan_count has been loop-reset

    # ── Results ────────────────────────────────────────────────────
    result: Optional[dict] = None
    error: Optional[str] = None
    failure_reason: Optional[str] = None
    failure_detail: Optional[dict] = None    # FailureDetail dict (category + context + llm_analysis)
    postmortem: Optional[dict] = None        # {"path": str, "markdown": str, "summary": str}
    created_at: Optional[str] = None         # ISO 8601
    finished_at: Optional[str] = None        # ISO 8601
    injection_start_time: Optional[str] = None   # ISO 8601, set when blade_create succeeds

    # ── Memory ─────────────────────────────────────────────────────
    compressed_summary: Optional[str] = None
    experiment_history: Optional[list] = None
    operational_notes: Optional[str] = None


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
    kubewiz_cluster_uuid: Optional[str] = None
    kubewiz_profile: Optional[str] = None
    needs_confirmation: bool = False
    dry_run: bool = False
    compressed_summary: Optional[str] = None
    operational_notes: Optional[str] = None

    # Task ID (allocated by _allocate_operation_task_id in intent_clarification)
    task_id: str = ""
