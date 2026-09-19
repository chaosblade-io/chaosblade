"""Tests for chaos_agent.l4.adapter — TestTask ↔ AgentState conversions."""

from unittest.mock import patch

import pytest

from chaos_agent.l4 import adapter as _adapter_mod
from chaos_agent.l4.schemas import L4TaskResult, L4TestTask

# Use underscore-prefixed aliases to prevent pytest from collecting
# source functions whose names start with 'test_'.
_to_initial_state = _adapter_mod.test_task_to_initial_state
_to_task_result = _adapter_mod.state_to_task_result
_build_recover = _adapter_mod.build_recover_initial_state
_make_traj_id = _adapter_mod.make_trajectory_id


def _valid_payload(**overrides):
    payload = {
        "fault_intent": {
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
            "names": ["app=myapp"],
            "labels": {"app": "myapp"},
            "params": {"cpu-percent": "80"},
            "duration_seconds": 300,
        },
        "kubeconfig": "/home/user/.kube/config",
    }
    payload.update(overrides)
    return payload


class TestTestTaskToInitialState:
    """Test inbound conversion: L4TestTask → inject graph initial_state."""

    def test_fault_intent_conversion(self):
        task = L4TestTask(
            task_id="t-001",
            intent="inject pod cpu fault",
            payload=_valid_payload(),
        )
        state = _to_initial_state(task)

        assert state["task_id"] == "t-001"
        assert state["operation"] == "inject"
        assert state["interaction_mode"] == "l4"
        assert state["kubeconfig"] == "/home/user/.kube/config"

        fs = state["fault_spec"]
        assert fs["namespace"] == "cms-demo"
        assert fs["scope"] == "pod"
        assert fs["fault_target"] == "cpu"
        assert fs["fault_action"] == "fullload"
        assert fs["names"] == ["app=myapp"]
        assert fs["labels"] == {"app": "myapp"}
        assert fs["params"] == {"cpu-percent": "80"}
        assert fs["duration_seconds"] == 300
        assert fs["source"] == "l4_sdk"
        assert fs["user_description"] == "inject pod cpu fault"

    def _payload_with_duration(self, **duration_keys):
        """Build a valid payload whose fault_intent carries only duration_keys."""
        fi = {
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "cms-demo",
        }
        fi.update(duration_keys)
        return {"fault_intent": fi, "kubeconfig": "/home/user/.kube/config"}

    def test_duration_seconds_key_honored(self):
        """The single canonical duration key (to_intent_dict's output)
        reaches the fault spec verbatim — regression for the seam where
        the adapter read `duration` while the contract emitted
        `duration_seconds`, silently dropping the platform's value."""
        task = L4TestTask(
            task_id="t-durs",
            intent="x",
            payload=self._payload_with_duration(duration_seconds=180),
        )
        state = _to_initial_state(task)
        assert state["fault_spec"]["duration_seconds"] == 180

    def test_retired_duration_alias_not_honored(self):
        """The retired `duration` alias has no reader: a payload carrying
        only it falls to the default (l4-contract-faithfulness ruling —
        no compatibility layer for old keys)."""
        task = L4TestTask(
            task_id="t-dura",
            intent="x",
            payload=self._payload_with_duration(duration=240),
        )
        state = _to_initial_state(task)
        assert state["fault_spec"]["duration_seconds"] == 300

    def test_duration_absent_falls_to_default(self):
        task = L4TestTask(
            task_id="t-durx",
            intent="x",
            payload=self._payload_with_duration(),
        )
        state = _to_initial_state(task)
        assert state["fault_spec"]["duration_seconds"] == 300

    def test_transport_fields_forwarded(self):
        """L4 payload transport fields (channel override + kubewiz + ssh/host)
        must reach initial_state — regression for the gap where the schema
        declared them but the adapter dropped everything except kubeconfig."""
        task = L4TestTask(
            task_id="t-transport",
            intent="inject via ssh",
            payload=_valid_payload(
                kube_connection_mode="ssh",
                kubewiz_cluster_uuid="cluster-xyz",
                kubewiz_profile="prof-1",
                host_name="10.0.0.9",
                ssh_host="10.0.0.10",
                ssh_user="root",
                ssh_key_path="/tmp/id_rsa",
                ssh_port=2222,
            ),
        )
        state = _to_initial_state(task)
        assert state["kube_connection_mode"] == "ssh"
        assert state["kubewiz_cluster_uuid"] == "cluster-xyz"
        assert state["kubewiz_profile"] == "prof-1"
        assert state["host_name"] == "10.0.0.9"
        assert state["ssh_host"] == "10.0.0.10"
        assert state["ssh_user"] == "root"
        assert state["ssh_key_path"] == "/tmp/id_rsa"
        assert state["ssh_port"] == 2222

    def test_transport_fields_default_empty(self):
        """Absent transport fields default to empty / port 22."""
        task = L4TestTask(task_id="t-td", intent="x", payload=_valid_payload())
        state = _to_initial_state(task)
        assert state["kube_connection_mode"] == ""
        assert state["ssh_host"] == ""
        assert state["ssh_port"] == 22

    def test_invalid_kube_connection_mode_fails_closed(self):
        """A bad kube_connection_mode must raise a clear ValueError at the
        adapter (fail-closed), not crash deep inside resolve(). Guards the gap
        where the L4 schema enum was declarative-only (never enforced)."""
        task = L4TestTask(
            task_id="t-badmode",
            intent="x",
            payload=_valid_payload(kube_connection_mode="kubewiz"),  # deprecated
        )
        with pytest.raises(ValueError, match="invalid kube_connection_mode"):
            _to_initial_state(task)

    def test_flat_payload_without_fault_intent_fails_closed(self):
        """Flat fields (fault_scope etc) are no longer accepted — must use fault_intent."""
        task = L4TestTask(
            task_id="t-002",
            intent="inject pod cpu fault",
            payload={
                "fault_scope": "pod",
                "fault_target": "cpu",
                "fault_action": "fullload",
                "namespace": "cms-demo",
                "target_names": ["app=myapp"],
            },
        )
        with pytest.raises(ValueError, match="fault_intent"):
            _to_initial_state(task)

    def test_none_payload_fails_closed(self):
        task = L4TestTask(task_id="t-003", intent="test", payload=None)
        with pytest.raises(ValueError, match="fault_intent"):
            _to_initial_state(task)

    def test_empty_payload_fails_closed(self):
        task = L4TestTask(task_id="t-004", intent="test")
        with pytest.raises(ValueError, match="fault_intent"):
            _to_initial_state(task)

    def test_missing_required_field_fails_closed(self):
        payload = _valid_payload(fault_intent={"scope": "pod", "target": "cpu"})
        task = L4TestTask(task_id="t-005", intent="test", payload=payload)
        with pytest.raises(ValueError, match="missing required field"):
            _to_initial_state(task)

    def test_messages_empty(self):
        task = L4TestTask(task_id="t-007", intent="test", payload=_valid_payload())
        state = _to_initial_state(task)
        assert state["messages"] == []

    def test_safety_status_pending(self):
        task = L4TestTask(task_id="t-008", intent="test", payload=_valid_payload())
        state = _to_initial_state(task)
        assert state["safety_status"] == "pending"


class TestStateToTaskResult:
    """Test outbound conversion: graph final state → L4TaskResult."""

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_injected_maps_to_passed(self, mock_infer, mock_build):
        mock_infer.return_value = "injected"
        mock_build.return_value = {"fault_type": "pod-cpu", "phase": "verify"}

        values = {"experiment_uid": "uid-123", "safety_status": "safe"}
        result = _to_task_result(values, "t-001", "traj-001")

        assert isinstance(result, L4TaskResult)
        assert result.status == "passed"
        assert result.task_id == "t-001"
        assert result.trajectory_id == "traj-001"
        assert result.error is None
        assert "pod-cpu" in result.summary

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_failed_maps_to_failed_with_error(self, mock_infer, mock_build):
        mock_infer.return_value = "failed"
        mock_build.return_value = {"fault_type": "pod-network"}

        values = {"error": "connection timed out"}
        result = _to_task_result(values, "t-002")

        assert result.status == "failed"
        assert result.error is not None
        assert result.error.code == "AGENT_TIMEOUT"

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_partial_recovered_maps_to_degraded(self, mock_infer, mock_build):
        mock_infer.return_value = "partial_recovered"
        mock_build.return_value = {"fault_type": "node-disk"}

        result = _to_task_result({}, "t-003")
        assert result.status == "degraded"

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_unverified_maps_to_degraded_without_error(self, mock_infer, mock_build):
        """Honest ignorance → "completed with reservations".

        Verification ran but evidence was unavailable: not passed (no evidence
        of success), not failed (no counter-evidence either — so no
        L4AgentError: there is nothing to report as an error).
        """
        mock_infer.return_value = "unverified"
        mock_build.return_value = {"fault_type": "pod-cpu"}

        result = _to_task_result({}, "t-uv")
        assert result.status == "degraded"
        assert result.error is None
        assert "unverified" in result.summary

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_extras_contain_status_data(self, mock_infer, mock_build):
        mock_infer.return_value = "recovered"
        mock_build.return_value = {
            "fault_type": "pod-cpu",
            "phase": "recovery",
            "duration_ms": 5000,
            "task_id": "t-004",  # should be excluded
            "stage": "done",  # should be excluded
            "status": "ok",  # should be excluded
        }

        values = {"experiment_uid": "abc", "safety_status": "safe"}
        result = _to_task_result(values, "t-004")

        # Explicit fields pass through on the modern key (phase-14 G4
        # retired the legacy-spelling hydration this once exercised).
        assert result.extras["experiment_uid"] == "abc"
        assert "blade_uid" not in result.extras
        assert result.extras["safety"] == "safe"
        # Spread from status_data (excluding task_id, stage, status)
        assert result.extras["fault_type"] == "pod-cpu"
        assert result.extras.get("task_state") == "recovered"


class TestBuildRecoverInitialState:
    """Test recover graph initial state construction."""

    @patch("chaos_agent.utils.inject_context.build_inject_context")
    def test_basic_fields(self, mock_ctx):
        mock_ctx.return_value = "inject context summary"

        inject_values = {
            "tui_session_id": "sess-1",
            "experiment_uid": "uid-abc",
            "skill_name": "pod-cpu-fullload",
            "skill_case_content": "steps...",
            "inject_verification_summary": "verified OK",
            "fault_spec": {"scope": "pod"},
            "kubeconfig": "/path/to/kube",
            "messages": [{"role": "ai", "content": "done"}],
        }
        result = _build_recover(inject_values, "t-001")

        assert result["task_id"] == "recover-t-001"
        assert result["parent_task_id"] == "t-001"
        assert result["operation"] == "recover"
        assert result["experiment_uid"] == "uid-abc"
        assert result["inject_context"] == "inject context summary"
        assert result["fault_spec"] == {"scope": "pod"}
        assert result["messages"] == []  # Fresh messages
        assert result["verification"] is None
        assert result["recover_verification"] is None
        assert result["verifier_loop_count"] == 0

    @patch("chaos_agent.utils.inject_context.build_inject_context")
    def test_missing_fields_use_defaults(self, mock_ctx):
        mock_ctx.return_value = ""
        result = _build_recover({}, "t-002")
        assert result["experiment_uid"] == ""
        assert result["skill_name"] == ""
        assert result["kubeconfig"] == ""


class TestMakeTrajectoryId:
    """Test trajectory ID generation."""

    def test_format(self):
        tid = _make_traj_id("task-001")
        assert tid.startswith("traj-task-001-")
        # UUID hex 8 chars suffix
        suffix = tid.split("-", 3)[-1]
        assert len(suffix) == 8

    def test_uniqueness(self):
        ids = {_make_traj_id("t-001") for _ in range(100)}
        assert len(ids) == 100


class TestVerificationFirstClassFields:
    """D6: verification as a first-class field, extras mirror kept."""

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_verification_field_mirrors_extras(self, mock_infer, mock_build):
        """First-class field and extras mirror carry the same verdict —
        legacy readers (benchmark worker) and new readers agree."""
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "unknown"},
            "warnings": ["metrics query forbidden"],
        }
        mock_infer.return_value = "unverified"
        mock_build.return_value = {"fault_type": "pod-cpu", "verification": verification}

        result = _to_task_result({}, "t-vc")
        assert result.verification == verification
        assert result.extras["verification"] == verification
        assert result.status == "degraded"
        assert result.error is None

    def test_new_fields_default_none(self):
        """dataclass tail defaults: legacy constructors keep working."""
        result = L4TaskResult(task_id="t-legacy", status="passed")
        assert result.verification is None
        assert result.observation_failures is None

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_observation_failures_auth_class(self, mock_infer, mock_build):
        """Forbidden (403) in checklist evidence → auth-class entry."""
        verification = {
            "level": "unverified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "unknown"},
            "checklist": {
                "items": [
                    {"step": 1, "status": "skipped", "evidence": "kubectl top forbidden (403)"},
                    {"step": 2, "status": "passed", "evidence": "CPU at 95%"},
                ],
            },
        }
        mock_infer.return_value = "unverified"
        mock_build.return_value = {"fault_type": "pod-cpu", "verification": verification}

        result = _to_task_result({}, "t-of")
        assert result.observation_failures == [
            {"channel": "step-1", "error_class": "auth", "count": 1},
        ]

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_observation_failures_transient_class(self, mock_infer, mock_build):
        """Timeout in evidence → transient-class entry (marker vocabulary
        shared with the verifier prompt's evidence boundary)."""
        verification = {
            "level": "partial",
            "layer1": {"status": "passed"},
            "layer2": {"status": "partial"},
            "checklist": {
                "items": [
                    {"step": 1, "status": "failed", "evidence": "metrics query timed out"},
                    {"step": 1, "status": "failed", "evidence": "connection reset, retried ok"},
                ],
            },
        }
        mock_infer.return_value = "injected"
        mock_build.return_value = {"fault_type": "pod-cpu", "verification": verification}

        result = _to_task_result({}, "t-tr")
        assert result.observation_failures == [
            {"channel": "step-1", "error_class": "transient", "count": 2},
        ]

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_observation_failures_empty_when_healthy(self, mock_infer, mock_build):
        """No error markers anywhere → None, no placeholder entries."""
        verification = {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
            "checklist": {
                "items": [{"step": 1, "status": "passed", "evidence": "CPU at 95%"}],
            },
        }
        mock_infer.return_value = "injected"
        mock_build.return_value = {"fault_type": "pod-cpu", "verification": verification}

        result = _to_task_result({}, "t-ok")
        assert result.observation_failures is None

    @patch("chaos_agent.agent.state.build_status_data")
    @patch("chaos_agent.agent.state.infer_task_state")
    def test_observation_failures_unknown_class_via_skipped(self, mock_infer, mock_build):
        """A skipped step with marker-free text is still recorded (the
        ``status == "skipped"`` flag IS the structural signal), classifying
        as unknown. Free-form warnings without markers are NOT recorded —
        otherwise every string warning would become noise."""
        verification = {
            "level": "partial",
            "layer1": {"status": "passed"},
            "layer2": {"status": "partial"},
            "checklist": {
                "items": [
                    {"step": 3, "status": "skipped", "evidence": "tool unavailable"},
                ],
            },
            "warnings": ["baseline was stale"],
        }
        mock_infer.return_value = "injected"
        mock_build.return_value = {"fault_type": "pod-cpu", "verification": verification}

        result = _to_task_result({}, "t-wn")
        assert result.observation_failures == [
            {"channel": "step-3", "error_class": "unknown", "count": 1},
        ]

    def test_status_code_matching_requires_word_boundary(self):
        """Plain substring matching would let "24013ms" (transient timing
        noise) contain "401" and misclassify it as auth — steering the
        operator toward credentials instead of the network. Status codes
        must match on word boundaries; real auth errors still classify."""
        from chaos_agent.l4.adapter import _classify_observation_error

        # Digit-noise containing "401"/"403" as substrings → NOT auth
        assert _classify_observation_error("connection reset after 24013ms") == "transient"
        assert _classify_observation_error("read 14033 bytes") == "unknown"
        # Genuine status codes → auth
        assert _classify_observation_error("HTTP 401 Unauthorized") == "auth"
        assert _classify_observation_error("metrics query failed: (403)") == "auth"
        assert _classify_observation_error("Error from server (Forbidden)") == "auth"
