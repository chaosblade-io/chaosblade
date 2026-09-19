"""Teeth for the task-end vehicle teardown fact (inject-dfee9d3d, R13-1).

Root cause closed by this layer: the teardown fact was re-derived at each
terminal construction site (result_builder only — the SSE / turn / task
JSON builders answered the question differently or not at all).
Single-source fix: ``operation_result.pending_vehicle_teardown`` computes
the list once and ``build_inject_data_from_state`` carries it as the
``vehicle_teardown_pending`` field — every terminal surface (CLI hint,
SSE envelope, persisted task JSON) reads the SAME field.
"""

import json

from chaos_agent.agent.result.operation_result import (
    build_inject_data_from_state,
    pending_vehicle_teardown,
)
from chaos_agent.cli.result_builder import (
    _build_inject_result_events,
    _vehicle_teardown_hint,
)


def _carrier_artifact(status: str) -> dict:
    return {
        "type": "recovery_carrier",
        "name": "drill-rc-mntopt",
        "namespace": "default",
        "status": status,
        "rbac_family": [],
    }


class TestPendingVehicleTeardown:
    def test_armed_carrier_is_pending(self):
        """Long-window form: deadline still counting, sweep skipped."""
        assert pending_vehicle_teardown({
            "execution_artifacts": [_carrier_artifact("recovery_armed")],
        }) == ["recovery_carrier:default/drill-rc-mntopt"]

    def test_active_occupant_is_pending(self):
        """Occupant family counts too: task-built targets need collection."""
        assert pending_vehicle_teardown({
            "execution_artifacts": [{
                "type": "occupant_deployment",
                "name": "drill-mntopt-target",
                "namespace": "default",
                "status": "active",
            }],
        }) == ["occupant_deployment:default/drill-mntopt-target"]

    def test_cleaned_carrier_is_not_pending(self):
        """Short-window form: finalize sweep already collected and marked."""
        assert pending_vehicle_teardown({
            "execution_artifacts": [_carrier_artifact("cleaned")],
        }) == []

    def test_non_vehicle_artifact_ignored(self):
        assert pending_vehicle_teardown({
            "execution_artifacts": [{"type": "other", "status": "active", "name": "x"}],
        }) == []

    def test_nameless_artifact_falls_back_to_type(self):
        assert pending_vehicle_teardown({
            "execution_artifacts": [{"type": "occupant_pod", "status": "active"}],
        }) == ["occupant_pod"]

    def test_malformed_entries_ignored(self):
        assert pending_vehicle_teardown({
            "execution_artifacts": [None, "junk", {}],
        }) == []

    def test_missing_key_is_empty(self):
        assert pending_vehicle_teardown({}) == []


class TestTeardownFieldRidesEveryEnvelope:
    def test_build_inject_data_carries_field(self):
        """The single-source field rides the canonical data dict — the SSE
        / turn / persisted-JSON builders all consume build_inject_data_
        from_state, so ONE field addition covers every terminal surface."""
        data = build_inject_data_from_state(
            {"execution_artifacts": [_carrier_artifact("recovery_armed")]},
            "inject-dfee9d3d",
        )
        assert data["vehicle_teardown_pending"] == [
            "recovery_carrier:default/drill-rc-mntopt"
        ]

    def test_build_inject_data_field_empty_when_clean(self):
        data = build_inject_data_from_state(
            {"execution_artifacts": [_carrier_artifact("cleaned")]},
            "t1",
        )
        assert data["vehicle_teardown_pending"] == []

    def test_field_serializes_into_result_envelope_json(self):
        """Envelope round-trip: the field must survive JSON serialization
        (SSE / task-JSON consumers parse the envelope, not the dict)."""
        from chaos_agent.models.schemas import build_inject_envelope

        data = build_inject_data_from_state(
            {"execution_artifacts": [_carrier_artifact("recovery_armed")]},
            "t1",
        )
        payload = json.loads(json.dumps(build_inject_envelope(
            data, data["task_state"], data.get("error", ""),
        )))
        assert payload["data"]["vehicle_teardown_pending"] == [
            "recovery_carrier:default/drill-rc-mntopt"
        ]


class TestResultEventsTeardownHint:
    def test_pending_carrier_emits_hint_after_result(self):
        events, should_return = _build_inject_result_events(
            {"execution_artifacts": [_carrier_artifact("recovery_armed")]},
            "inject-dfee9d3d",
            False,
            "cli",
        )
        assert should_return is False
        assert [e.type for e in events] == ["result", "token"]
        content = events[1].content
        assert "blade-ai recover --task-id inject-dfee9d3d" in content
        assert "recovery_carrier:default/drill-rc-mntopt" in content
        # The hint rides AFTER the result envelope: action guidance comes
        # last, never interleaving with the result JSON itself.
        assert events[1].task_id == "inject-dfee9d3d"

    def test_cleaned_carrier_no_hint(self):
        events, _ = _build_inject_result_events(
            {"execution_artifacts": [_carrier_artifact("cleaned")]},
            "t1",
            False,
            "cli",
        )
        assert [e.type for e in events] == ["result"]

    def test_hint_renderer_reads_field_not_state(self):
        """Renderer contract (R13-1): the hint builder consumes the field
        value, never the graph state — re-derivation here would fork the
        single source this layer exists to enforce."""
        hint = _vehicle_teardown_hint(["recovery_carrier:default/x"], "t9")
        assert "recovery_carrier:default/x" in hint
        assert "blade-ai recover --task-id t9" in hint

    def test_tui_reject_branch_behavior_preserved(self):
        """The TUI early-return branch (reject / never-injected) must keep
        its original event shape — the hint only exists on the result path
        where a task actually ran."""
        events, should_return = _build_inject_result_events(
            {"safety_status": "rejected", "safety_reason": "no"},
            "t1",
            False,
            "tui",
        )
        assert should_return is True
        assert all(e.type != "result" for e in events)
