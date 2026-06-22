from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.agent.state_builders import build_inject_initial_state


def test_build_inject_initial_state_from_fault_spec_object():
    spec = FaultSpec(
        namespace="default",
        scope="pod",
        names=("pod-a",),
        labels={"app": "demo"},
        blade_target="cpu",
        blade_action="fullload",
        params={"cpu-percent": "80"},
        source="test",
    )

    state = build_inject_initial_state(
        task_id="task-1",
        tui_session_id="sid-1",
        fault_spec=spec,
        confirmed_intent="inject",
        needs_confirmation=True,
        interaction_mode="tui",
        kubeconfig="/tmp/kubeconfig",
        kube_context="ctx-a",
        kubewiz_cluster_uuid="cluster-a",
        kubewiz_profile="profile-a",
        direct=False,
        dry_run=True,
        created_at="2026-06-18T10:00:00+08:00",
    )

    assert state["task_id"] == "task-1"
    assert state["tui_session_id"] == "sid-1"
    assert state["operation"] == "inject"
    assert state["confirmed_intent"] == "inject"
    assert state["needs_confirmation"] is True
    assert state["interaction_mode"] == "tui"
    assert state["safety_status"] == "pending"
    assert state["created_at"] == "2026-06-18T10:00:00+08:00"
    assert state["dry_run"] is True
    assert state["kubeconfig"] == "/tmp/kubeconfig"
    assert state["kube_context"] == "ctx-a"
    assert state["kubewiz_cluster_uuid"] == "cluster-a"
    assert state["kubewiz_profile"] == "profile-a"
    assert state["fault_spec"]["scope"] == "pod"
    assert state["fault_spec"]["blade_target"] == "cpu"
    assert state["fault_spec"]["params"] == {"cpu-percent": "80"}


def test_build_inject_initial_state_copies_mutable_inputs():
    messages = ["handoff"]
    batch_args = {"faults": [{"scope": "pod"}]}
    fault_spec = {"scope": "pod", "blade_target": "network"}

    state = build_inject_initial_state(
        task_id="task-2",
        fault_spec=fault_spec,
        messages=messages,
        batch_submit_args=batch_args,
    )

    messages.append("later")
    batch_args["new"] = True
    batch_args["faults"][0]["scope"] = "node"
    fault_spec["scope"] = "node"

    assert state["messages"] == ["handoff"]
    assert state["batch_submit_args"] == {"faults": [{"scope": "pod"}]}
    assert state["fault_spec"] == {"scope": "pod", "blade_target": "network"}
