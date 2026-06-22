"""Tests for AgentState definition."""

from chaos_agent.agent.state import (
    AgentState,
    build_status_data,
    infer_phase,
    infer_task_state,
)
from chaos_agent.agent.state_lifecycle import (
    STATE_DURABLE_FACT_FIELDS,
    STATE_FIELD_GROUPS,
    STATE_FIELD_POLICIES,
    ensure_recover_runtime_defaults,
    iter_state_fields,
    per_fault_reset_state,
    recover_reset_state,
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
        assert state.get("direct", False) is False

    def test_declares_runtime_checkpoint_fields(self):
        annotations = AgentState.__annotations__
        for field in (
            "plan_summary",
            "_planning_alternatives",
            "_catalogue_rejection_nudged",
            "_execute_text_nudged",
            "_kubectl_step_nudged",
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
        assert state_field_group("blade_uid") == "execution"
        assert state_field_group("recover_verification") == "verification"

    def test_durable_facts_are_not_cleared_by_per_fault_reset(self):
        reset_fields = set(per_fault_reset_state())
        durable_fields = set(STATE_DURABLE_FACT_FIELDS)
        classified = set(iter_state_fields())

        assert reset_fields - classified == set()
        assert "blade_uid" in reset_fields
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

        blade_policy = state_field_policy("blade_uid")
        assert blade_policy is not None
        assert blade_policy.group == "execution"
        assert blade_policy.durable is True
        assert blade_policy.reset_on_batch_fault is True
        assert blade_policy.reset_on_recover is False

        recover_policy = state_field_policy("recover_verification")
        assert recover_policy is not None
        assert recover_policy.group == "verification"
        assert recover_policy.reset_on_batch_fault is True
        assert recover_policy.reset_on_recover is True

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

    def test_l1_skipped_l2_unknown_returns_failed(self):
        """Non-ChaosBlade: L1=skipped + L2=unknown → failed (core bug fix)."""
        state = {
            "operation": "inject",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_task_state(state) == "failed"

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

    def test_replan_exhausted_no_blade_uid_returns_failed(self):
        """Replan was attempted but graph completed without blade_uid or verification → failed."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "replan_count": 2,
            "replan_context": {"error_summary": "blade_create failed"},
        }
        assert infer_task_state(state) == "failed"

    def test_replan_exhausted_with_blade_uid_returns_injecting(self):
        """Replan was attempted but blade_uid exists (partial success) → injecting."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "blade_uid": "abc123",
            "replan_count": 2,
            "replan_context": {"error_summary": "partial failure"},
        }
        assert infer_task_state(state) == "injecting"

    def test_replan_exhausted_with_verification_returns_injected(self):
        """Replan was attempted and eventually succeeded (verification present) → injected."""
        state = {
            "operation": "inject",
            "skill_name": "k8s-chaos-skills",
            "blade_uid": "abc123",
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


class TestInferPhase:
    """Test infer_phase() logic for ChaosBlade vs non-ChaosBlade verification.

    Note: infer_phase() has an early exit `if not blade_uid: return "planning"`
    that prevents non-ChaosBlade faults from reaching the verification result
    section. This is a pre-existing design limitation. The core bug fix is in
    infer_task_state(), which correctly handles the non-ChaosBlade case.
    """

    def test_l1_passed_l2_unknown_returns_verification_passed(self):
        """ChaosBlade: L1=passed + L2=unknown → verification_passed."""
        state = {
            "operation": "inject",
            "blade_uid": "abc123",
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
            "blade_uid": "abc123",
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
            "blade_uid": "abc123",
            "skill_name": "cpu-stress",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "passed"},
                "layer2": {"status": "failed"},
            },
        }
        assert infer_phase(state) == "verification_failed"

    def test_non_chaosblade_no_blade_uid_returns_planning(self):
        """Non-ChaosBlade: no blade_uid → infer_phase returns 'planning' (known limitation).

        infer_phase() has early exits based on blade_uid, so non-ChaosBlade faults
        never reach the verification result section. The correct state inference is
        handled by infer_task_state() instead.
        """
        state = {
            "operation": "inject",
            "skill_name": "pvc-pending",
            "blade_uid": "",
            "verification": {
                "level": "unverified",
                "layer1": {"status": "skipped"},
                "layer2": {"status": "unknown"},
            },
        }
        assert infer_phase(state) == "planning"


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
                    "blade_target": "network",
                    "blade_action": "loss",
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
