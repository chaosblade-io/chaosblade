from unittest.mock import patch

from chaos_agent.agent.recovery_state import (
    build_recover_initial_from_checkpoint,
    ensure_recover_runtime_defaults,
)


@patch("chaos_agent.utils.inject_context.build_inject_context")
def test_build_recover_initial_from_checkpoint_copies_durable_facts_and_resets_runtime(mock_ctx):
    mock_ctx.return_value = "inject context"
    inject_values = {
        "task_id": "task-inject",
        "tui_session_id": "sid-1",
        "blade_uid": "uid-123",
        "skill_name": "pod-cpu-fullload",
        "skill_case_content": "case text",
        "inject_verification_summary": "verified",
        "fault_spec": {"scope": "pod", "blade_target": "cpu", "blade_action": "fullload"},
        "kubeconfig": "/old/kubeconfig",
        "kube_context": "ctx-a",
        "kubewiz_cluster_uuid": "cluster-a",
        "kubewiz_profile": "profile-a",
        "injection_method": "kubectl_exec",
        "kubectl_exec_pod_name": "tool-pod-a",
        "created_at": "2026-06-18T10:00:00+08:00",
        "verification": {"level": "verified"},
        "recover_verification": {"level": "stale"},
        "messages": ["inject message"],
        "error": "stale error",
    }

    initial = build_recover_initial_from_checkpoint(
        inject_values,
        "task-inject",
        record_task_id="task-recover",
        kubeconfig_override="/new/kubeconfig",
    )

    assert initial["task_id"] == "task-recover"
    assert initial["parent_task_id"] == "task-inject"
    assert initial["operation"] == "recover"
    assert initial["blade_uid"] == "uid-123"
    assert initial["skill_name"] == "pod-cpu-fullload"
    assert initial["inject_context"] == "inject context"
    assert initial["fault_spec"] == inject_values["fault_spec"]
    assert initial["kubeconfig"] == "/new/kubeconfig"
    assert initial["kube_context"] == "ctx-a"
    assert initial["injection_method"] == "kubectl_exec"
    assert initial["kubectl_exec_pod_name"] == "tool-pod-a"

    assert initial["verification"] is None
    assert initial["recover_verification"] is None
    assert initial["messages"] == []
    assert initial["error"] is None
    assert initial["failure_reason"] is None
    assert initial["failure_detail"] is None
    assert initial["recover_phase"] == "layer1_recovery"
    assert initial["layer1_iteration_count"] == 0


def test_build_recover_initial_from_checkpoint_rebuilds_fault_spec_from_legacy_target():
    initial = build_recover_initial_from_checkpoint(
        {
            "skill_name": "node-disk-fill",
            "target": {
                "namespace": "",
                "names": ["node-a"],
                "labels": {},
                "resource_type": "node",
            },
            "params": {"percent": "85"},
        },
        "task-inject",
    )

    assert initial["fault_spec"] == {
        "namespace": "",
        "scope": "node",
        "names": ["node-a"],
        "labels": {},
        "blade_target": "disk",
        "blade_action": "fill",
        "params": {"percent": "85"},
        "params_flags": [],
        "duration_seconds": 0,
        "source": "recover_checkpoint",
        "user_description": "",
    }


def test_ensure_recover_runtime_defaults_keeps_existing_durable_fields():
    initial = ensure_recover_runtime_defaults({
        "task_id": "task-recover",
        "blade_uid": "uid-123",
        "recover_phase": "layer2_verification",
    })

    assert initial["task_id"] == "task-recover"
    assert initial["blade_uid"] == "uid-123"
    assert initial["recover_phase"] == "layer2_verification"
    assert initial["operation"] == "recover"
    assert initial["recover_verification"] is None
    assert initial["messages"] == []
