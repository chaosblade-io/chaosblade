"""Vehicle-name defenses (task-29848471).

A k3-class replan once quoted the ``kubectl debug`` pod name as the fault
target and the verifier validated against the transient vehicle. These
tests pin the shared detector and both defense layers:

  * ``is_vehicle_name`` — data sources first, prefix heuristic last
  * write-side: spec writers refuse to persist vehicle names
  * present-side: Layer 2 Fault Context warns and points at the anchor
"""

import json

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from chaos_agent.agent.execution_artifacts import is_vehicle_name
from chaos_agent.agent.nodes.execute.agent_loop import _drop_vehicle_names
from chaos_agent.agent.nodes.planning.plan_builder import _build_spec_from_submit
from chaos_agent.agent.spec.fault_spec import FaultSpec

VEHICLE = "node-debugger-node-a-xp8nc"


# ---------------------------------------------------------------------------
# is_vehicle_name — the shared detector
# ---------------------------------------------------------------------------


class TestIsVehicleName:
    def test_artifact_registered_debug_pod(self):
        state = {
            "execution_artifacts": [
                {"type": "debug_pod", "name": VEHICLE, "status": "active"},
            ],
        }
        assert is_vehicle_name(VEHICLE, state)

    def test_cleaned_artifact_is_still_a_vehicle(self):
        """A deleted vehicle stays a vehicle — history must not re-target it."""
        state = {
            "execution_artifacts": [
                {"type": "debug_pod", "name": VEHICLE, "status": "cleaned"},
            ],
        }
        assert is_vehicle_name(VEHICLE, state)

    def test_non_debug_artifact_does_not_match(self):
        state = {
            "execution_artifacts": [
                {"type": "chaos_experiment", "name": "mysql-79794985d4-7zl5p"},
            ],
        }
        assert not is_vehicle_name("mysql-79794985d4-7zl5p", state)

    def test_kubectl_exec_pod_name(self):
        state = {"kubectl_exec_pod_name": "otel-c-tool-abc"}
        assert is_vehicle_name("otel-c-tool-abc", state)

    def test_debug_pod_meta_tag_in_messages(self):
        """Covers artifacts not yet collected this iteration."""
        meta = json.dumps({"name": VEHICLE, "namespace": "default"})
        state = {
            "messages": [
                ToolMessage(
                    content=f"ok\n\n[debug-pod-meta: {meta}]",
                    tool_call_id="tc1",
                ),
            ],
        }
        assert is_vehicle_name(VEHICLE, state)

    def test_prefix_heuristic_without_state(self):
        assert is_vehicle_name(VEHICLE, None)
        assert is_vehicle_name("node-debugger-node-b-q1", {})

    def test_workload_pod_is_not_a_vehicle(self):
        state = {
            "execution_artifacts": [
                {"type": "debug_pod", "name": VEHICLE, "status": "active"},
            ],
            "kubectl_exec_pod_name": "otel-c-tool-abc",
            "messages": [],
        }
        assert not is_vehicle_name("mysql-79794985d4-7zl5p", state)

    def test_empty_name_is_not_a_vehicle(self):
        assert not is_vehicle_name("", {"messages": []})


# ---------------------------------------------------------------------------
# Write-side defense 1: plan_builder._build_spec_from_submit
# ---------------------------------------------------------------------------


class TestBuildSpecFromSubmitVehicleBlock:
    def test_vehicle_name_rejected_keeps_prior_names(self):
        existing = FaultSpec(
            namespace="cms-demo", scope="pod", names=("mysql-79794985d4-7zl5p",),
        )
        submit = {"faults": [{
            "namespace": "cms-demo", "scope": "pod",
            "names": [VEHICLE], "target": "cpu", "action": "fullload",
        }]}
        spec = _build_spec_from_submit(submit, existing, state={})
        assert spec.names == ("mysql-79794985d4-7zl5p",)

    def test_legit_names_still_written(self):
        existing = FaultSpec(namespace="cms-demo", scope="pod")
        submit = {"faults": [{
            "namespace": "cms-demo", "scope": "pod",
            "names": ["mysql-79794985d4-7zl5p"], "target": "cpu",
            "action": "fullload",
        }]}
        spec = _build_spec_from_submit(submit, existing, state={})
        assert spec.names == ("mysql-79794985d4-7zl5p",)

    def test_artifact_backed_vehicle_rejected(self):
        """Data-source detection (not just the prefix heuristic)."""
        existing = FaultSpec(scope="node", names=("node-a",))
        submit = {"faults": [{
            "scope": "node", "names": ["custom-dbg-pod-x"],
            "target": "cpu", "action": "fullload",
        }]}
        state = {
            "execution_artifacts": [
                {"type": "debug_pod", "name": "custom-dbg-pod-x"},
            ],
        }
        spec = _build_spec_from_submit(submit, existing, state)
        assert spec.names == ("node-a",)


# ---------------------------------------------------------------------------
# Write-side defense 2: agent_loop lazy derivation helper
# ---------------------------------------------------------------------------


class TestDropVehicleNames:
    def test_vehicle_names_dropped_other_fields_kept(self):
        updates = {"namespace": "default", "scope": "pod", "names": (VEHICLE,)}
        _drop_vehicle_names(updates, state={}, v_args=f"get pod {VEHICLE}")
        assert "names" not in updates
        assert updates["namespace"] == "default"
        assert updates["scope"] == "pod"

    def test_legit_names_untouched(self):
        updates = {"names": ("mysql-79794985d4-7zl5p",)}
        _drop_vehicle_names(updates, state={}, v_args="get pod mysql")
        assert updates["names"] == ("mysql-79794985d4-7zl5p",)

    def test_no_names_key_is_noop(self):
        updates = {"namespace": "default"}
        _drop_vehicle_names(updates, state={}, v_args="get pods -n default")
        assert updates == {"namespace": "default"}

    def test_mixed_names_with_one_vehicle_drops_all(self):
        """A names tuple is atomic — partial writes would split the target."""
        updates = {"names": ("mysql-abc", VEHICLE)}
        _drop_vehicle_names(updates, state={}, v_args="get pod")
        assert "names" not in updates


# ---------------------------------------------------------------------------
# Present-side defense: Layer 2 Fault Context anchor warning
# ---------------------------------------------------------------------------


class TestLayer2VehicleWarning:
    def _state_with_vehicle_target(self):
        return {
            "messages": [],
            "fault_spec": FaultSpec(
                namespace="default", scope="node", names=(VEHICLE,),
                blade_target="disk", blade_action="fill",
            ).to_dict(),
            "approved_target": {
                "namespace": "default",
                "names": ["node-a"],
                "labels": {},
                "resolved_names": ["node-a"],
            },
            "blade_parsed_flags": {"path": "/tmp", "size": "10000"},
            "params": {},
            "kubeconfig": "/path/to/kubeconfig",
        }

    def test_warning_emitted_with_anchor(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        layer1 = Layer1Result(
            status="passed", affected_count=1, raw_output="Success",
        )
        msgs = _build_layer2_messages(
            self._state_with_vehicle_target(), layer1, "uid-1", "disk-fill",
            "/path/to/kubeconfig", count=1,
        )
        humans = [m for m in msgs if isinstance(m, HumanMessage)]
        assert humans, "layer2 context must contain a HumanMessage"
        joined = "\n".join(m.content for m in humans)
        assert "VEHICLE WARNING" in joined
        assert VEHICLE in joined
        assert "node-a" in joined  # approved anchor surfaced

    def test_no_warning_for_clean_targets(self):
        from chaos_agent.agent.nodes.verify._verifier_messages import (
            _build_layer2_messages,
        )
        from chaos_agent.agent.result.verdict import Layer1Result

        state = self._state_with_vehicle_target()
        state["fault_spec"] = FaultSpec(
            namespace="default", scope="node", names=("node-a",),
            blade_target="disk", blade_action="fill",
        ).to_dict()
        layer1 = Layer1Result(
            status="passed", affected_count=1, raw_output="Success",
        )
        msgs = _build_layer2_messages(
            state, layer1, "uid-1", "disk-fill",
            "/path/to/kubeconfig", count=1,
        )
        humans = [m for m in msgs if isinstance(m, HumanMessage)]
        joined = "\n".join(m.content for m in humans)
        assert "VEHICLE WARNING" not in joined


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
