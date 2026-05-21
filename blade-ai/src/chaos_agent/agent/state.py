"""AgentState definition for LangGraph StateGraph."""

from typing import Optional

from langgraph.graph import MessagesState

from chaos_agent.utils.time import now_iso, parse_iso_timestamp


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
    # Side-effect confirmation: L1 passed + container restart destroyed evidence
    # → not a failure, but a valid drill finding (e.g., burn → OOMKill → restart)
    if l1_pass and l2_status == "recovered_before_observation":
        side_effects = verification.get("side_effects") if isinstance(verification, dict) else None
        if side_effects and side_effects.get("container_restarts"):
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


def extract_ui_diagnostics(values: dict) -> dict:
    """Return the UI-visible diagnostic fields for a result envelope payload.

    Centralizes which fields propagate from AgentState into stream events,
    so all production sites (cli/runner.py, server/routes/inject_stream.py)
    surface the same set without recopying boilerplate. Anything that flows
    through here becomes visible in `render_result` via `_read_diagnostic`.
    """
    return {
        "failure_reason": values.get("failure_reason") or "",
        "replan_count": int(values.get("replan_count") or 0),
        "replan_history": list(values.get("replan_history") or []),
        "side_effects": _extract_side_effects(values.get("verification")),
    }


def build_status_data(task_id: str, values: dict) -> dict:
    """Build a complete status data dict from LangGraph checkpoint values.

    Used by both CLI AgentRunner.status() and Server status route.
    """

    skill_name = values.get("skill_name") or ""
    blade_uid = values.get("blade_uid") or ""
    blade_params = values.get("params") or {}
    target = values.get("target") or {}
    verification = values.get("verification")
    error = values.get("error")
    safety_reason = values.get("safety_reason") or ""

    # Infer fault_type from state fields or blade params
    fault_type = ""
    # Priority 1: blade_scope/blade_target/blade_action from structured params
    if values.get("blade_scope") and values.get("blade_target") and values.get("blade_action"):
        fault_type = f"{values['blade_scope']}-{values['blade_target']}-{values['blade_action']}"
    # Priority 2: from blade_params dict (LLM mode)
    if not fault_type and blade_params:
        scope = blade_params.get("scope", "")
        action = blade_params.get("action", "")
        target_action = blade_params.get("target", "")
        if scope and target_action and action:
            fault_type = f"{scope}-{target_action}-{action}"
    # Priority 3: skill_name fallback
    if not fault_type:
        fault_type = skill_name

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

    # Merge failure_reason into error (failure_reason is more descriptive)
    merged_error = values.get("failure_reason") or error or ""

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
        "failure_reason": values.get("failure_reason") or "",
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

    # Task identification
    task_id: str = ""
    tui_session_id: str = ""  # Owning TUI session (empty for non-TUI callers)
    parent_task_id: str = ""  # For recover: the inject task_id being recovered
    operation: str = ""  # inject/recover/chat

    # Skill matching
    skill_name: Optional[str] = None

    # Target specification
    target: Optional[dict] = None  # {namespace, names, labels, resource_type}

    # Fault parameters
    params: Optional[dict] = None

    # Natural language description (NL mode)
    input: Optional[str] = None

    # Safety assessment
    safety_status: str = "pending"  # pending/safe/unsafe/warning/rejected
    safety_reason: Optional[str] = None
    conflict_uids: Optional[list[str]] = None  # UIDs of existing active experiments

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

    # Layer 1 result caches (persisted across ReAct tool_call iterations)
    # Without these, layer1 results are lost when LLM makes tool_calls,
    # causing "Layer1: unknown" on subsequent iterations.
    inject_layer1_cache: Optional[dict] = None
    recover_layer1_cache: Optional[dict] = None

    # Recovery phase tracking (for non-ChaosBlade faults)
    # "layer1_recovery" = LLM-driven recovery execution phase
    # "layer2_verification" = LLM-driven verification phase
    recover_phase: str = "layer1_recovery"

    # Layer 1 iteration count (separate from verifier_loop_count)
    layer1_iteration_count: int = 0

    # Whether Layer 2 context has been added to messages
    # (needed because non-ChaosBlade Layer 2 may start at count > 1)
    layer2_context_added: bool = False

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

    # Failure reason (only set when task result is "failed", None on success)
    failure_reason: Optional[str] = None

    # Injection method tracking (used by verifier to choose verification strategy)
    injection_method: Optional[str] = None  # "host_blade" | "kubectl_exec" | "kubectl_native" | None

    # Tool pod name used during kubectl exec injection (recorded at injection time,
    # used by verifier/recover_verifier to prefer the original pod)
    kubectl_exec_pod_name: Optional[str] = None

    # Skill use-case content (populated during injection, used by Layer 2 verification)
    skill_case_content: Optional[str] = None  # Full content of the matched skill use-case file

    # Injection verification summary (Layer 2 observations from inject phase, used as baseline for recover)
    inject_verification_summary: Optional[str] = None

    # Structured fault parameters (direct mode + LLM mode hints)
    blade_scope: Optional[str] = None    # node / pod / container
    blade_target: Optional[str] = None   # cpu / network / disk / ...
    blade_action: Optional[str] = None   # fullload / delay / loss / ...
    direct: bool = False                 # True: skip LLM, go direct path
    duration: int = 0                    # Fault duration in seconds (user-specified via --duration/-d)
    params_flags: Optional[list] = None  # Boolean bare-key flags ["read", "write"]
    blade_parsed_flags: Optional[dict] = None  # Parsed key params from flags: {"path": "/tmp", "percent": "85", ...}

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
    restart_precheck: Optional[dict] = None  # Fast-path container restart detection result
    reverify_count: int = 0                  # P2: verifier re-verification attempt count
    reverify_gaps: Optional[list[str]] = None # P2: gap types that triggered re-verification
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
    fault_intent: Optional[dict] = None          # Structured fault intent from intent_clarification

    # Dry-Run multi-turn planning (TUI `/plan`).
    # When True: confirmation_gate emits a "what would happen" AIMessage and
    # the router exits to END before any side-effecting node runs. The user
    # can iterate the plan over multiple turns and finally call `/run` (no
    # args) which sets dry_run=False and re-invokes the pipeline.
    dry_run: bool = False
