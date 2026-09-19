"""Tests for AgentState definition."""

from chaos_agent.agent.state import (
    AgentState,
    build_status_data,
    has_active_fault,
    infer_inject_status,
    infer_phase,
    infer_recover_status,
    infer_task_state,
    materialize_fault_handle,
    terminal_task_state,
)
from chaos_agent.agent.state_mgmt.state_lifecycle import (
    STATE_DURABLE_FACT_FIELDS,
    STATE_FIELD_GROUPS,
    STATE_FIELD_POLICIES,
    ensure_recover_runtime_defaults,
    iter_state_fields,
    per_fault_reset_state,
    recover_reset_state,
    replan_reset_state,
    state_field_policy,
    state_field_group,
)


class TestAgentStateDefaults:
    """Test default field values using dict-style access (LangGraph convention)."""

    def test_default_task_id(self):
        state = AgentState()
        assert state.get("task_id", "") == ""

    def test_default_operation(self):
        state = AgentState()
        assert state.get("operation", "") == ""

    def test_default_safety_status(self):
        state = AgentState()
        assert state.get("safety_status", "pending") == "pending"

    def test_default_needs_confirmation(self):
        state = AgentState()
        assert state.get("needs_confirmation", False) is False

    def test_default_loop_counters(self):
        state = AgentState()
        assert state.get("agent_loop_count", 0) == 0
        assert state.get("execute_loop_count", 0) == 0

    def test_default_optional_fields(self):
        state = AgentState()
        assert state.get("skill_name") is None
        assert state.get("target") is None
        assert state.get("params") is None
        assert state.get("plan") is None
        assert state.get("blade_uid") is None
        assert state.get("result") is None
        assert state.get("error") is None
        assert state.get("compressed_summary") is None
        assert state.get("experiment_history") is None
        assert state.get("operational_notes") is None
        assert state.get("blade_scope") is None
        assert state.get("blade_target") is None
        assert state.get("blade_action") is None
        assert state.get("params_flags") is None

    def test_declares_runtime_checkpoint_fields(self):
        annotations = AgentState.__annotations__
        for field in (
            "plan_summary",
            "_planning_alternatives",
            "_catalogue_rejection_nudged",
            "_execute_text_stall_count",
            "_injection_selfcheck_nudged",
        ):
            assert field in annotations

    def test_all_agent_state_fields_are_lifecycle_classified(self):
        annotations = set(AgentState.__annotations__)
        classified = set(iter_state_fields())

        assert annotations - classified == set()
        assert classified - annotations == set()

    def test_state_field_groups_do_not_overlap(self):
        seen = {}
        duplicates = {}
        for group, fields in STATE_FIELD_GROUPS.items():
            for field in fields:
                if field in seen:
                    duplicates.setdefault(field, [seen[field]]).append(group)
                seen[field] = group

        assert duplicates == {}
        assert state_field_group("experiment_uid") == "execution"
        assert state_field_group("recover_verification") == "verification"

    def test_durable_facts_are_not_cleared_by_per_fault_reset(self):
        reset_fields = set(per_fault_reset_state())
        durable_fields = set(STATE_DURABLE_FACT_FIELDS)
        classified = set(iter_state_fields())

        assert reset_fields - classified == set()
        assert "experiment_uid" in reset_fields
        assert "verification" in reset_fields
        assert "task_id" not in reset_fields
        assert "kubeconfig" not in reset_fields
        assert "batch_results" not in reset_fields
        assert "created_at" not in reset_fields
        assert "task_id" in durable_fields
        assert "fault_spec" in durable_fields

    def test_state_field_policies_match_lifecycle_tables(self):
        assert set(STATE_FIELD_POLICIES) == set(iter_state_fields())
        assert tuple(STATE_FIELD_POLICIES) == iter_state_fields()
        assert STATE_DURABLE_FACT_FIELDS == tuple(
            name
            for name, policy in STATE_FIELD_POLICIES.items()
            if policy.durable
        )
        assert set(per_fault_reset_state()) == {
            name
            for name, policy in STATE_FIELD_POLICIES.items()
            if policy.reset_on_batch_fault
        }
        assert set(recover_reset_state()) == {
            name
            for name, policy in STATE_FIELD_POLICIES.items()
            if policy.reset_on_recover
        }
        assert set(replan_reset_state()) == {
            name
            for name, policy in STATE_FIELD_POLICIES.items()
            if policy.reset_on_replan
        }

        experiment_policy = state_field_policy("experiment_uid")
        assert experiment_policy is not None
        assert experiment_policy.group == "execution"
        assert experiment_policy.durable is True
        assert experiment_policy.reset_on_batch_fault is True
        assert experiment_policy.reset_on_recover is False

        recover_policy = state_field_policy("recover_verification")
        assert recover_policy is not None
        assert recover_policy.group == "verification"
        assert recover_policy.reset_on_batch_fault is True
        assert recover_policy.reset_on_recover is True

        # B76 review G/H — the liability ledger's two wings. Both are
        # append-only for the task lifetime and cross EVERY boundary
        # (batch advance, recover entry): owned is the birth registry,
        # retired is the only death proof for framework-side destroys
        # (no ToolMessage) — a reset on either boundary resurrects a
        # destroyed experiment in live_liability_uids and the next sweep
        # repeat-destroys it (probe_b76_round8.py H4).
        owned_policy = state_field_policy("owned_experiment_uids")
        assert owned_policy is not None
        assert owned_policy.group == "execution"
        assert owned_policy.durable is True
        assert owned_policy.reset_on_batch_fault is False
        assert owned_policy.reset_on_recover is False

        retired_policy = state_field_policy("retired_experiment_uids")
        assert retired_policy is not None
        assert retired_policy.group == "execution"
        assert retired_policy.durable is True
        assert retired_policy.reset_on_batch_fault is False
        assert retired_policy.reset_on_recover is False

    def test_replan_reset_state_pins_w56_5_attempt_scoped_keys(self):
        """W-56-5 defect b — the replan seam reset is registry-driven.

        The agent_loop replan entry once hand-maintained this list and
        forgot safety_reason/error/failure_reason/failure_detail, letting
        attempt 1's terminal residue strangle attempt 2 on its first route
        (task #56). The exact key/value set is pinned here: shrinking it
        resurrects the leak, growing it must be a deliberate lifecycle
        decision (declare ``replan=`` on the policy, not a side list).
        """
        assert replan_reset_state() == {
            # Safety verdict of the previous attempt — re-evaluated fresh.
            "safety_status": "pending",
            "safety_reason": None,
            "blast_radius_scope": None,
            "blast_radius_detail": None,
            # Confirmation handshake — re-frozen for the corrected plan.
            "needs_confirmation": False,
            "replan_requested": False,
            # Terminal residue — already captured in replan_history; a
            # stale error short-circuits should_continue_agent_loop.
            "error": None,
            "failure_reason": None,
            "failure_detail": None,
        }
        # Cross-boundary independence: the replan group is a distinct
        # semantic slot — batch/recover membership does not imply replan
        # membership (e.g. drift_reject_count resets per batch but
        # deliberately accumulates across attempts).
        assert "drift_reject_count" not in replan_reset_state()
        assert "drift_reject_count" in per_fault_reset_state()
        for policy in STATE_FIELD_POLICIES.values():
            if policy.reset_on_replan:
                assert policy.reset_on_batch_fault, (
                    f"{policy.name}: attempt-scoped reset without "
                    "batch-scoped reset is a lifecycle smell"
                )

    def test_liability_ledger_survives_batch_and_recover_resets(self):
        """B76 review H — the death proof must outlive the batch boundary.

        batch_setup wipes messages (REMOVE_ALL_MESSAGES), so after a batch
        advance the retired registry is the ONLY evidence that a
        framework-destroyed UID is dead; if the reset delta also cleared it,
        the live view would resurrect the UID for the next fault's sweep.
        """
        from chaos_agent.agent.state import live_liability_uids

        assert "owned_experiment_uids" not in per_fault_reset_state()
        assert "retired_experiment_uids" not in per_fault_reset_state()
        assert "owned_experiment_uids" not in recover_reset_state()
        assert "retired_experiment_uids" not in recover_reset_state()

        # Post-batch shape: both wings retained, message evidence wiped.
        post_batch = {
            "messages": [],
            "owned_experiment_uids": ["uid-e1", "uid-e2"],
            "retired_experiment_uids": ["uid-e1"],
        }
        assert live_liability_uids(post_batch) == ["uid-e2"]

    def test_recover_reset_fields_are_lifecycle_classified(self):
        reset_fields = set(recover_reset_state())
        classified = set(iter_state_fields())

        assert reset_fields - classified == set()
        assert "messages" in reset_fields
        assert "verification" in reset_fields
        assert "recover_verification" in reset_fields
        assert "blade_uid" not in reset_fields
        assert "fault_spec" not in reset_fields

    def test_reset_defaults_are_not_shared_between_calls(self):
        first = recover_reset_state()
        second = recover_reset_state()

        first["messages"].append("stale")

        assert second["messages"] == []

    def test_ensure_recover_runtime_defaults_does_not_share_mutable_defaults(self):
        first = ensure_recover_runtime_defaults({"task_id": "recover-a"})
        second = ensure_recover_runtime_defaults({"task_id": "recover-b"})

        first["messages"].append("stale")

        assert second["messages"] == []


class TestAgentStateFields:
    """Test field assignment via constructor and dict-style access."""

    def test_set_task_id(self):
        state = AgentState(task_id="task-123")
        assert state.get("task_id", "") == "task-123"

    def test_set_operation(self):
        state = AgentState(operation="inject")
        assert state.get("operation", "") == "inject"

    def test_set_safety_status(self):
        state = AgentState(safety_status="safe")
        assert state.get("safety_status", "") == "safe"

    def test_set_target(self):
        target = {"namespace": "default", "names": ["pod1"], "resource_type": "pod"}
        state = AgentState(target=target)
        assert state.get("target", {})["namespace"] == "default"

    def test_loop_counter_increment(self):
        state = AgentState()
        assert state.get("agent_loop_count", 0) == 0
        state["agent_loop_count"] = 1
        assert state.get("agent_loop_count", 0) == 1


class TestInferTaskState:
    """Test infer_task_state() logic for ChaosBlade vs non-ChaosBlade faults."""

    def test_l1_passed_l2_unknown_returns_injected(self):
        """ChaosBlade: L1=passed + L2=unknown → injected (partial verification OK)."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "injected"

    def test_l1_skipped_l2_unknown_returns_unverified(self):
        """Non-CB: L1=skipped + L2=unknown + level=unverified → unverified.

        Honest ignorance (verification ran, evidence unavailable) is a distinct
        knowledge claim — not counter-evidence (failed), not success (injected).
        Previously fell through to "failed".
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "unverified"

    def test_l1_passed_l2_unknown_unverified_returns_unverified(self):
        """CB: L1=passed + L2=unknown + level=unverified → unverified.

        L1 shows the experiment Running, but the verifier honestly reports it
        could not observe the effect. Reporting "failed" would claim
        counter-evidence that does not exist.
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "unverified"

    def test_l1_warning_l2_unknown_unverified_returns_unverified(self):
        """CB warning path (e.g., CLI timeout): mirrors the passed path."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "warning"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "unverified"

    def test_l2_unknown_level_unknown_returns_unverified(self):
        """Verifier silence (level=unknown) no longer outranks honesty.

        l2=unknown + level=unknown used to map to "injected" — silence scored
        better than an honest "unverified". Both now land on "unverified".
        """
        state = {
            "operation": "inject",
            "verification": {
                "level": "unknown",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "unverified"

    def test_l1_skipped_l2_passed_returns_injected(self):
        """Non-ChaosBlade: L1=skipped + L2=passed → injected (verified)."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "verified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_task_state(state) == "injected"

    def test_l1_skipped_l2_skipped_returns_failed(self):
        """Non-ChaosBlade: L1=skipped + L2=skipped → failed (no verification at all)."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "partial",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "skipped"},
            },
        }
        assert infer_task_state(state) == "failed"

    def test_l1_skipped_l2_failed_returns_failed(self):
        """Non-ChaosBlade: L1=skipped + L2=failed → failed."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "failed"},
            },
        }
        assert infer_task_state(state) == "failed"

    def test_replan_exhausted_no_experiment_uid_returns_failed(self):
        """Replan was attempted but graph completed without experiment_uid or verification → failed."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "replan_count": 2,
            "replan_context": {"error_summary": "blade_create failed"},
        }
        assert infer_task_state(state) == "failed"

    def test_replan_exhausted_with_experiment_uid_returns_injecting(self):
        """Replan was attempted but experiment_uid exists (partial success) → injecting."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "experiment_uid": "abc123",
            "replan_count": 2,
            "replan_context": {"error_summary": "partial failure"},
        }
        assert infer_task_state(state) == "injecting"

    def test_replan_exhausted_with_verification_returns_injected(self):
        """Replan was attempted and eventually succeeded (verification present) → injected."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "experiment_uid": "abc123",
            "replan_count": 1,
            "replan_context": {"error_summary": "previous attempt failed"},
            "verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "injected"

    def test_replan_exhausted_with_chat_confirmed_intent_returns_failed(self):
        """confirmed_intent=chat takes priority over replan_context → completed.
        Non-injection intents have no fault lifecycle, so replan exhaustion
        is irrelevant — the task is simply completed as a chat interaction.
        Note: This synthetic state (chat intent + replan_context) cannot
        occur in real execution paths since non-injection intents get
        their own fresh task_id without inherited replan state."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "replan_count": 1,
            "replan_context": {"error_summary": "blade_create failed"},
            "confirmed_intent": "chat",
        }
        assert infer_task_state(state) == "completed"

    def test_normal_chat_still_works(self):
        """NL mode chat (has input) with confirmed_intent=chat → completed."""
        state = {
            "operation": "inject",
            "input": "What is chaos engineering?",
            "confirmed_intent": "chat",
        }
        assert infer_task_state(state) == "completed"

    def test_dry_run_preview_returns_completed(self):
        """Round-61 R61-4/R61-4b: the /plan preview terminates at
        route_after_confirmation ("end" for dry_run) with no verification
        — its own deliverable IS the plan, so the terminal word is
        'completed', not the 'injecting'→'failed' cascade. The pipeline
        input carries ``dry_run`` straight from the TUI /plan context."""
        state = {
            "operation": "inject",
            "confirmed_intent": "inject",
            "dry_run": True,
            "plan_summary": "## Plan\n- cpu fullload 80%",
            "needs_confirmation": False,
            "fault_spec": {"target": "app=x", "action": "cpu-fullload"},
        }
        assert infer_task_state(state) == "completed"

    def test_real_run_plan_summary_shape_not_swallowed(self):
        """Round-61 P7 collision guard: planning writes ``plan_summary``
        for REAL runs too (extract_planning_metadata feeds the confirm
        card), so once the gate closes needs_confirmation a real run
        aborted before its verification carries the SAME plan_summary
        shape — minus ``dry_run``. It must NOT infer 'completed': a run
        that never reached its verdict is 'injecting' (terminal 'failed').
        This is exactly what a shape-only gate (no dry_run discriminator)
        would get wrong."""
        state = {
            "operation": "inject",
            "confirmed_intent": "inject",
            "plan_summary": "## Plan\n- cpu fullload 80%",
            "needs_confirmation": False,
            "fault_spec": {"target": "app=x", "action": "cpu-fullload"},
        }
        assert infer_task_state(state) == "injecting"

    def test_dry_run_with_committed_fault_not_completed(self):
        """The dry_run branch never outranks a committed fault: a
        dry_run=True thread that somehow carries a fault handle stays on
        the fault lifecycle (injecting without a verdict), so the branch
        cannot mask a live injection behind the preview's completion."""
        state = {
            "operation": "inject",
            "confirmed_intent": "inject",
            "dry_run": True,
            "plan_summary": "## Plan\n- cpu fullload 80%",
            "needs_confirmation": False,
            "experiment_uid": "exp-123",
        }
        assert infer_task_state(state) == "injecting"

    def test_dry_run_with_verification_follows_verification(self):
        """A dry_run thread that nonetheless carries a verification verdict
        is governed by the verdict (injected), not by the preview branch —
        the discriminator only completes runs with NOTHING else on record."""
        state = {
            "operation": "inject",
            "confirmed_intent": "inject",
            "dry_run": True,
            "plan_summary": "## Plan\n- cpu fullload 80%",
            "needs_confirmation": False,
            "verification": {
                "level": "verified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_task_state(state) == "injected"


class TestInferPhase:
    """Test infer_phase() logic for ChaosBlade vs non-ChaosBlade verification.

    Note: phase presence gates on has_active_fault() (a materialized fault
    handle — either an explicit fault_handle or derivable attribution
    facts), never on a carrier field spelling directly.
    """

    def test_l1_passed_l2_unknown_returns_verification_passed(self):
        """ChaosBlade: L1=passed + L2=unknown → verification_passed."""
        state = {
            "operation": "inject",
            "experiment_uid": "abc123",
            "skill_name": "cpu-stress",
            "verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_phase(state) == "verification_passed"

    def test_l1_passed_l2_passed_returns_verification_passed(self):
        """ChaosBlade: L1=passed + L2=passed → verification_passed."""
        state = {
            "operation": "inject",
            "experiment_uid": "abc123",
            "skill_name": "cpu-stress",
            "verification": {
                "level": "verified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_phase(state) == "verification_passed"

    def test_l1_passed_l2_failed_returns_verification_failed(self):
        """ChaosBlade: L1=passed + L2=failed → verification_failed."""
        state = {
            "operation": "inject",
            "experiment_uid": "abc123",
            "skill_name": "cpu-stress",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "failed"},
            },
        }
        assert infer_phase(state) == "verification_failed"

    def test_non_attributed_no_experiment_uid_returns_planning(self):
        """No experiment_uid AND no attributed method → infer_phase returns
        'planning': nothing claims a committed fault, so a native handle is
        never fabricated for an injection that never happened."""
        state = {
            "operation": "inject",
            "skill_name": "pvc-pending",
            "experiment_uid": "",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_phase(state) == "planning"

    def test_native_attributed_fault_is_not_stuck_planning(self):
        """Regression: phase gates used to key on ``blade_uid`` alone, which
        stranded attributed native faults (no UID) in planning/safety_check.
        An attributed kubectl-native injection committed a fault, so it must
        progress to executing."""
        state = {
            "operation": "inject",
            "skill_name": "pvc-pending",
            "injection_method": "kubectl_native",
            "experiment_uid": "",
            "safety_status": "safe",
        }
        assert infer_phase(state) == "executing"

    def test_native_attributed_fault_reaches_verification_section(self):
        """Regression: an attributed native fault with a verdict must reach
        the verification-result section (L1 skipped → L2 decides)."""
        base = {
            "operation": "inject",
            "skill_name": "pvc-pending",
            "injection_method": "kubectl_native",
            "experiment_uid": "",
        }
        passed = {
            **base,
            "verification": {
                "level": "verified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_phase(passed) == "verification_passed"
        failed = {
            **base,
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "failed"},
            },
        }
        assert infer_phase(failed) == "verification_failed"

    def test_recover_phase_honors_verification_over_result_mirror(self):
        """Round-16 S3: a mirror/verification divergence must not split
        phase from task_state. Before the fix this branch read the result
        mirror (the FOURTH parallel copy of the recover verdict mapping)
        while every task_state reader honoured the verification dict
        (D4) — mirror=partial + verification=recovered yielded
        task_state="recovered" but phase="partial_recovered" for the
        SAME state."""
        diverged = {
            "operation": "recover",
            "result": {"recovered": True, "recovery_level": "partial"},
            "recover_verification": {
                "level": "recovered",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
        }
        assert infer_phase(diverged) == "recovered"

    def test_recover_phase_partial_verification_beats_mirror_claim(self):
        """The mirror direction too: mirror claims full recovery, the
        verification authority says partial — phase follows the
        authority (single source), not the mirror."""
        diverged = {
            "operation": "recover",
            "result": {"recovered": True, "recovery_level": "recovered"},
            "recover_verification": {
                "level": "partial",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_phase(diverged) == "partial_recovered"


class TestFaultHandlePredicate:
    """``materialize_fault_handle`` / ``has_active_fault`` — the carrier-neutral
    fault-presence predicate every gate/judgement keys on."""

    def test_blade_legacy_fields_materialize_blade_handle(self):
        # [已翻转] phase-14 G4 EOL 后：provider 侧旧键 fallback 读取拆除
        # （design 五文件清单之外的盘点盲区），仅旧键 attribution 不再产
        # 生 handle——旧键视作不存在。新键 attribution 走同族测试（下）。
        state = {"blade_uid": "uid-9", "injection_method": "host_blade"}
        assert materialize_fault_handle(state) is None
        assert not has_active_fault(state)

    def test_native_attribution_materializes_native_handle_without_uid(self):
        state = {"injection_method": "kubectl_native"}
        assert materialize_fault_handle(state) == {
            "kind": "native", "method": "kubectl_native",
        }
        assert has_active_fault(state)

    def test_existing_handle_wins_over_legacy_derivation(self):
        state = {
            "fault_handle": {"kind": "native", "method": "host_native"},
            "experiment_uid": "uid-legacy",
        }
        assert materialize_fault_handle(state) == {
            "kind": "native", "method": "host_native",
        }

    def test_empty_state_has_no_active_fault(self):
        assert materialize_fault_handle({}) is None
        assert not has_active_fault({})

    def test_unattributed_uid_still_claimed_in_registration_order(self):
        """Pre-handle checkpoints may carry a UID without a method: the
        hydration seam must still claim it (ChaosBlade first in precedence)."""
        state = {"experiment_uid": "uid-orphan"}
        assert materialize_fault_handle(state) == {
            "kind": "experiment_uid", "value": "uid-orphan", "method": "",
        }

    def test_combo_facts_materialize_the_native_attribution(self):
        """Combo task (blade experiment live + native attribution): the plain
        materialization reports the NATIVE attribution — attribution is the
        answer to "who owns this fault". The recover dispatch intentionally
        disagrees (its claim-1 experiment handle carries the blade uid); that
        combo-safe split is pinned in test_registry.py and the finalize uid
        contract test."""
        state = {"experiment_uid": "uid-combo", "injection_method": "kubectl_native"}
        assert materialize_fault_handle(state) == {
            "kind": "native", "method": "kubectl_native",
        }

    def test_committed_semantics_survives_proven_destroy(self):
        """Round-25 contract pin: the predicate is COMMITTED, not live. The
        handle projection has no death axis (it mirrors the attribution
        slots, which no destroy path clears), so the post-destroy steady
        state — corpse slot + landed retired ledger + proven destroy pair
        — keeps materializing a live-shaped handle. This is by design:
        recovery targets, postmortems and summaries need the committed
        identity long after the death. A consumer answering the LIVE
        question must gate on live_liability_uids instead (the twins
        verdict on this exact dict is the pin's other half)."""
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.state import live_liability_uids

        uid = "deadbeef00000001"
        dead_state = {
            "experiment_uid": uid,
            "injection_method": "chaosblade",
            "retired_experiment_uids": [uid],
            "messages": [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "blade_destroy",
                        "args": {"uid": uid},
                        "id": "tc-r25-pin",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"success"}',
                    name="blade_destroy",
                    tool_call_id="tc-r25-pin",
                ),
            ],
        }
        # Committed half: the handle stays live-shaped after the destroy.
        assert has_active_fault(dead_state) is True
        assert materialize_fault_handle(dead_state) == {
            "kind": "experiment_uid", "value": uid, "method": "chaosblade",
        }
        # Live half (the twins verdict): the liability primitive convicts
        # the same dict — live-semantics consumers gate on THIS, never on
        # the committed predicate alone.
        assert live_liability_uids(dead_state) == []

    def test_live_predicate_carrier_and_lifecycle_matrix(self):
        """Round-28: ``has_live_fault`` — the LIVE twin, single-sourced.

        The matrix the round-25 pin implied but never had a public
        predicate for: never-injected False; a native carrier True (no
        death oracle exists — the committed verdict, conservative); a live
        experiment True; the post-destroy steady state False while the
        committed twin stays True on the exact same dict (the split
        round-25 legislated, now reachable without re-assembling the
        gate inline at every consumer)."""
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.state import has_live_fault

        # Never-injected: no provider claims the facts.
        assert has_live_fault({}) is False
        assert has_live_fault({"messages": []}) is False

        # Native carrier: UID-less, no death oracle — committed verdict.
        native = {
            "injection_method": "kubectl_native",
            "fault_handle": {"kind": "native", "method": "kubectl_native"},
            "messages": [],
        }
        assert has_live_fault(native) is True

        uid = "deadbeef00000002"
        create_pair = [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": "tc-r28-live",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id="tc-r28-live",
            ),
        ]
        # Live experiment: the create receipt proves the birth, nothing
        # proves death.
        live = {
            "experiment_uid": uid,
            "injection_method": "kubectl_exec",
            "messages": create_pair,
        }
        assert has_live_fault(live) is True

        # Post-destroy steady state: slot corpse + landed retired ledger
        # + proven destroy pair. The twins split exactly here.
        dead = {
            **live,
            "retired_experiment_uids": [uid],
            "messages": create_pair + [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "blade_destroy",
                        "args": {"uid": uid},
                        "id": "tc-r28-kill",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"success"}',
                    name="blade_destroy",
                    tool_call_id="tc-r28-kill",
                ),
            ],
        }
        assert has_live_fault(dead) is False
        assert has_active_fault(dead) is True

    def test_live_predicate_hydrates_the_seam_aftermath_shapes(self):
        """Round-28: the replan-seam aftermath reaches the oracle through
        hydration.

        The aftermath shapes carry NO stored handle and NO method (the
        seam keeps the UID, clears the method for re-detection). The
        predicate materializes FIRST, so the provider claims the bare UID
        slot and the carrier split still reaches the liability oracle:
        the PROTECTED shape (a live experiment that survived the seam —
        the reason keep_experiment_uid exists) keeps its live verdict;
        the corpse flavor reads False. This hydration lane is exactly
        what the round-25 inline emergency gate missed — a
        stored-handle-less corpse used to fall into the committed branch
        (pinned in test_emergency_recover_gate.py)."""
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.state import has_live_fault

        uid = "deadbeef00000003"
        create_pair = [
            AIMessage(
                content="",
                tool_calls=[{
                    "name": "blade_create",
                    "args": {"command": "create k8s pod-cpu fullload"},
                    "id": "tc-r28-after",
                    "type": "tool_call",
                }],
            ),
            ToolMessage(
                content='{"code":200,"success":true,"result":"%s"}' % uid,
                name="blade_create",
                tool_call_id="tc-r28-after",
            ),
        ]
        # LIVE aftermath — keep=True exists to protect this shape.
        live_aftermath = {"experiment_uid": uid, "messages": create_pair}
        assert has_live_fault(live_aftermath) is True

        # DEAD aftermath — same kept slot, proven death.
        dead_aftermath = {
            "experiment_uid": uid,
            "retired_experiment_uids": [uid],
            "messages": create_pair + [
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "blade_destroy",
                        "args": {"uid": uid},
                        "id": "tc-r28-after-kill",
                        "type": "tool_call",
                    }],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"success"}',
                    name="blade_destroy",
                    tool_call_id="tc-r28-after-kill",
                ),
            ],
        }
        assert has_live_fault(dead_aftermath) is False


class TestBuildStatusDataExposedFields:
    """build_status_data is the UI/API gateway. Locking the fields it
    exposes prevents accidental schema regressions when nodes start
    producing new state keys."""

    def test_failure_reason_passes_through(self):
        """PR-A1 — failure_reason must be exposed alongside the merged error
        so the renderer can split it into Cause/Hint without re-parsing."""
        data = build_status_data(
            "t-fr",
            {"failure_reason": "safety_rejected: blacklist | llm_analysis: pick another ns"},
        )
        assert data["failure_reason"].startswith("safety_rejected")
        # merged_error keeps backward compat for older consumers
        assert data["error"].startswith("safety_rejected")

    def test_fault_type_target_and_params_project_from_fault_spec(self):
        data = build_status_data(
            "t-fs",
            {
                "skill_name": "stale-skill",
                "fault_spec": {
                    "namespace": "cms-demo",
                    "scope": "pod",
                    "names": ["pod-a"],
                    "labels": {"app": "demo"},
                    "fault_target": "network",
                    "fault_action": "loss",
                    "params": {"percent": "100"},
                    "params_flags": [],
                    "duration_seconds": 0,
                    "source": "test",
                    "user_description": "",
                },
            },
        )

        assert data["fault_type"] == "pod-network-loss"
        assert data["skill_name"] == "stale-skill"
        assert data["target"] == {
            "namespace": "cms-demo",
            "names": ["pod-a"],
            "labels": {"app": "demo"},
            "resource_type": "pod",
        }
        assert data["params"] == {"percent": "100"}

    def test_failure_reason_empty_when_absent(self):
        data = build_status_data("t-fr2", {})
        assert data["failure_reason"] == ""

    def test_intent_confidence_passes_through(self):
        """PR-A2 — intent_confidence is needed by the intent_confirm panel
        and any future status surface that highlights LLM uncertainty."""
        data = build_status_data("t-ic", {"intent_confidence": 0.45})
        assert data["intent_confidence"] == 0.45

    def test_intent_confidence_defaults_to_zero(self):
        data = build_status_data("t-ic2", {})
        assert data["intent_confidence"] == 0.0

    def test_replan_history_passes_through(self):
        """PR-A3 — replan_history is the data source for the agent
        self-improvement timeline. Lock both replan_count and the list
        so renderer/API consumers can render the convergence story."""
        history = [
            {"attempt": 1, "original_error": "blast radius too large", "action_taken": "shrink scope"},
            {"attempt": 2, "original_error": "blade_create timeout", "action_taken": "switch fault type"},
        ]
        data = build_status_data("t-rh", {"replan_count": 2, "replan_history": history})
        assert data["replan_count"] == 2
        assert len(data["replan_history"]) == 2
        assert data["replan_history"][0]["original_error"] == "blast radius too large"

    def test_replan_history_defaults_to_empty(self):
        """No replan happened → empty list and zero count, never None.
        Renderers gate on truthiness; None would force every caller to
        re-coalesce."""
        data = build_status_data("t-rh2", {})
        assert data["replan_count"] == 0
        assert data["replan_history"] == []

    def test_side_effects_extracted_before_strip(self):
        """PR-A4 — verification.side_effects must reach the UI even though
        ``strip_side_effects`` removes it from the verification subdict.
        The whole point: container_restarts means the fault crashed pods
        for real and operators want to know."""
        verification = {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
            "side_effects": {
                "container_restarts": [
                    {"pod": "web-1", "restart_count": 1, "reason": "OOMKilled"},
                ]
            },
        }
        data = build_status_data("t-se", {"verification": verification})
        # The verification field gets the side_effects stripped (back-compat).
        assert "side_effects" not in data["verification"]
        # But the top-level mirror exposes it for the renderer.
        assert data["side_effects"]["container_restarts"][0]["pod"] == "web-1"

    def test_side_effects_defaults_to_empty_dict(self):
        """Absent verification or absent side_effects → empty dict (not None).
        Lets the renderer use ``data["side_effects"].get("container_restarts")``
        without an extra null check."""
        data = build_status_data("t-se2", {})
        assert data["side_effects"] == {}


class TestRecoverUnverified:
    """Recovery-side three-way verdict: recovered / failed / unverified."""

    def test_recover_unverified_level_returns_unverified(self):
        """recovery_level=unverified (observation unavailable, no
        counter-evidence) is not a recovery failure."""
        state = {
            "operation": "recover",
            "recover_verification": {
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
            "result": {"recovered": False, "recovery_level": "unverified"},
        }
        assert infer_task_state(state) == "unverified"

    def test_recover_unrecovered_stays_failed(self):
        """Counter-evidence (fault still active) keeps the failed verdict."""
        state = {
            "operation": "recover",
            "recover_verification": {
                "level": "unrecovered",
                "layer1": {"status": "passed"},
                "layer2": {"status": "failed"},
            },
            "result": {"recovered": False, "recovery_level": "unrecovered"},
        }
        assert infer_task_state(state) == "failed"

    def test_recover_recovered_unchanged(self):
        state = {
            "operation": "recover",
            "recover_verification": {
                "level": "recovered",
                "layer1": {"status": "passed"},
                "layer2": {"status": "passed"},
            },
            "result": {"recovered": True, "recovery_level": "recovered"},
        }
        assert infer_task_state(state) == "recovered"

    def test_terminal_task_state_passes_unverified_through(self):
        """unverified is a terminal knowledge claim — no injecting fallback."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "unknown"},
            },
        }
        assert terminal_task_state(state) == "unverified"

    def test_infer_inject_status_unverified_is_failed(self):
        """Coarse four-value domain: an ENDED run must not read "pending"."""
        assert infer_inject_status("unverified") == "failed"

    def test_infer_recover_status_unverified_is_failed(self):
        """Recovery-stage mirror: the run has ENDED, "pending" would mislead
        (build_status_data feeds status queries / TUI from this)."""
        assert infer_recover_status("unverified", "recover") == "failed"

    def test_infer_recover_status_recovered_unchanged(self):
        assert infer_recover_status("recovered", "recover") == "success"
        assert infer_recover_status("partial_recovered", "recover") == "success"
