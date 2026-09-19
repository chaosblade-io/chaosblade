import logging
from unittest.mock import patch

from chaos_agent.agent.state_mgmt.recovery_state import (
    build_recover_initial_from_checkpoint,
    ensure_recover_runtime_defaults,
)


@patch("chaos_agent.utils.inject_context.build_inject_context")
def test_build_recover_initial_from_checkpoint_copies_durable_facts_and_resets_runtime(mock_ctx):
    mock_ctx.return_value = "inject context"
    inject_values = {
        "task_id": "task-inject",
        "tui_session_id": "sid-1",
        "experiment_uid": "uid-123",
        "skill_name": "pod-cpu-fullload",
        "skill_case_content": "case text",
        "inject_verification_summary": "verified",
        "fault_spec": {"scope": "pod", "fault_target": "cpu", "fault_action": "fullload"},
        "kubeconfig": "/old/kubeconfig",
        "kube_context": "ctx-a",
        "kubewiz_cluster_uuid": "cluster-a",
        "kubewiz_profile": "profile-a",
        "injection_method": "kubectl_exec",
        "execution_artifacts": [{"artifact_id": "uid-debug", "type": "debug_pod"}],
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
    assert initial["recover_task_id"] == "task-inject"
    assert initial["operation"] == "recover"
    assert initial["experiment_uid"] == "uid-123"
    assert initial["skill_name"] == "pod-cpu-fullload"
    assert initial["inject_context"] == "inject context"
    # The fault_spec passes through verbatim on the modern key face
    # (phase-14 G4: the legacy-spelling hydration is retired).
    assert initial["fault_spec"] == {
        "scope": "pod", "fault_target": "cpu", "fault_action": "fullload",
    }
    assert "blade_uid" not in initial
    assert "blade_target" not in initial["fault_spec"]
    assert initial["kubeconfig"] == "/new/kubeconfig"
    assert initial["kube_context"] == "ctx-a"
    assert initial["injection_method"] == "kubectl_exec"
    assert initial["execution_artifacts"] == inject_values["execution_artifacts"]
    assert initial["kubectl_exec_pod_name"] == "tool-pod-a"
    # No side effects recorded -> empty dict, never missing key.
    assert initial["side_effects"] == {}

    assert initial["verification"] is None
    assert initial["recover_verification"] is None
    assert initial["messages"] == []
    assert initial["error"] is None
    assert initial["failure_reason"] is None
    assert initial["failure_detail"] is None
    assert initial["recover_phase"] == "layer1_recovery"
    assert initial["layer1_iteration_count"] == 0


def test_build_recover_initial_from_checkpoint_carries_side_effects():
    """Inject-time side effects must reach recover Layer 1/2 (problem ③)."""
    initial = build_recover_initial_from_checkpoint(
        {
            "skill_name": "pod-network-loss",
            "target": {
                "namespace": "default",
                "names": ["pod-a"],
                "labels": {},
                "resource_type": "pod",
            },
            "params": {"percent": "100"},
            "blast_radius_detail": "2 replicas impacted",
            "side_effects": {"endpoint_removals": ["svc-a"]},
        },
        "task-inject",
        inject_context="ctx",
    )

    assert initial["side_effects"] == {"endpoint_removals": ["svc-a"]}
    assert initial["blast_radius_detail"] == "2 replicas impacted"


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
        # Phase-10: to_dict() projects the modern fault_target/fault_action
        # only — the legacy blade_* dual-write mirrors are retired, old
        # records hydrate on read instead.
        "fault_target": "disk",
        "fault_action": "fill",
        "params": {"percent": "85"},
        "params_flags": [],
        "duration_seconds": 0,
        "source": "recover_checkpoint",
        "user_description": "",
        "case_resource_path": "",
        "revision": 0,
        "objective": "",
        "boundaries": [],
        "constraints": [],
        "assumptions": [],
    }


def test_ensure_recover_runtime_defaults_keeps_existing_durable_fields():
    initial = ensure_recover_runtime_defaults({
        "task_id": "task-recover",
        "experiment_uid": "uid-123",
        "recover_phase": "layer2_verification",
    })

    assert initial["task_id"] == "task-recover"
    assert initial["experiment_uid"] == "uid-123"
    assert initial["recover_phase"] == "layer2_verification"
    assert initial["operation"] == "recover"
    assert initial["recover_verification"] is None
    assert initial["messages"] == []


def test_build_recover_initial_from_checkpoint_carries_liability_records():
    """B76 review G: the recover finale's liability sweep reads the birth /
    death registries — this bridge is an explicit whitelist copier, so the
    two keys must be named here or the recover-side live-liability view
    silently runs empty (the dual-graph field-passing gap: StateGraph schema
    alone does not carry values across the checkpoint boundary)."""
    initial = build_recover_initial_from_checkpoint(
        {
            "experiment_uid": "uid-main",
            "owned_experiment_uids": ["uid-old", "uid-main"],
            "retired_experiment_uids": ["uid-old"],
        },
        "task-inject",
    )
    assert initial["owned_experiment_uids"] == ["uid-old", "uid-main"]
    assert initial["retired_experiment_uids"] == ["uid-old"]

    # Legacy inject states predate both keys: empty defaults (never missing),
    # so the sweep is a no-op there and hydration owns the fallback.
    legacy = build_recover_initial_from_checkpoint({}, "task-inject")
    assert legacy["owned_experiment_uids"] == []
    assert legacy["retired_experiment_uids"] == []


# ---------------------------------------------------------------------------
# connection_override — connection credentials are RUNTIME CONTEXT, not
# durable inject facts (incident 2026-09-15: a cross-user recover inherited
# the injector's wiz profile '526255' from the frozen checkpoint and hit the
# auth wall while the recovering user's own login sat unused).
# ---------------------------------------------------------------------------


def test_connection_override_outranks_frozen_checkpoint_on_every_field():
    """The caller-carried connection (session-bound environment snapshot)
    replaces the injector's frozen credentials on all four axes, and a
    carried channel mode travels with it so TransportTarget.from_state
    resolves the matching transport."""
    initial = build_recover_initial_from_checkpoint(
        {
            "experiment_uid": "uid-123",
            "kubeconfig": "/injector/kubeconfig",
            "kube_context": "ctx-injector",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "526255",
        },
        "task-inject",
        connection_override={
            "kubeconfig": "/recoverer/kubeconfig",
            "kube_context": "ctx-recoverer",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "888888",
            "kube_connection_mode": "kubewiz_k8s",
        },
    )

    assert initial["kubeconfig"] == "/recoverer/kubeconfig"
    assert initial["kube_context"] == "ctx-recoverer"
    assert initial["kubewiz_cluster_uuid"] == "cluster-injector"
    assert initial["kubewiz_profile"] == "888888"
    assert initial["kube_connection_mode"] == "kubewiz_k8s"


def test_connection_override_none_keeps_frozen_values():
    """Bare entries (CLI / HTTP / TUI / auto-recover) carry no connection:
    the frozen values remain the fallback — legacy behavior, zero regression.

    The channel key must NOT be written so resolution stays on the caller's
    current settings (``_NO_RESET`` recover default for kube_connection_mode).
    """
    initial = build_recover_initial_from_checkpoint(
        {
            "experiment_uid": "uid-123",
            "kubeconfig": "/injector/kubeconfig",
            "kube_context": "ctx-injector",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "526255",
        },
        "task-inject",
    )

    assert initial["kubeconfig"] == "/injector/kubeconfig"
    assert initial["kube_context"] == "ctx-injector"
    assert initial["kubewiz_cluster_uuid"] == "cluster-injector"
    assert initial["kubewiz_profile"] == "526255"
    assert "kube_connection_mode" not in initial


def test_connection_override_partial_fields_fall_back_per_field():
    """Per-field override: a carried profile alone swaps the identity while
    the cluster / context / kubeconfig axes keep their frozen values."""
    initial = build_recover_initial_from_checkpoint(
        {
            "experiment_uid": "uid-123",
            "kubeconfig": "/injector/kubeconfig",
            "kube_context": "ctx-injector",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "526255",
        },
        "task-inject",
        connection_override={"kubewiz_profile": "888888"},
    )

    assert initial["kubewiz_profile"] == "888888"
    assert initial["kubeconfig"] == "/injector/kubeconfig"
    assert initial["kube_context"] == "ctx-injector"
    assert initial["kubewiz_cluster_uuid"] == "cluster-injector"


def test_kubeconfig_precedence_chain_override_beats_legacy_override():
    """kubeconfig: conn.kubeconfig > kubeconfig_override > checkpoint —
    the new axis sits on top of, not instead of, the legacy override."""
    values = {"kubeconfig": "/frozen"}

    legacy = build_recover_initial_from_checkpoint(
        values, "task-inject", kubeconfig_override="/legacy-override"
    )
    assert legacy["kubeconfig"] == "/legacy-override"

    top = build_recover_initial_from_checkpoint(
        values,
        "task-inject",
        kubeconfig_override="/legacy-override",
        connection_override={"kubeconfig": "/conn"},
    )
    assert top["kubeconfig"] == "/conn"

    # Empty-string carried kubeconfig does not mask the legacy override.
    fallback = build_recover_initial_from_checkpoint(
        values,
        "task-inject",
        kubeconfig_override="/legacy-override",
        connection_override={"kubeconfig": ""},
    )
    assert fallback["kubeconfig"] == "/legacy-override"


def test_cross_cluster_connection_override_warns_but_proceeds(caplog):
    """A carried cluster different from the injection cluster is a
    mis-binding smell: warn loudly (visibility), never block (kubeconfig
    mode has always switched clusters silently — the guard only restores
    visibility on the kubewiz axis)."""
    with caplog.at_level(
        logging.WARNING,
        logger="chaos_agent.agent.state_mgmt.recovery_state",
    ):
        initial = build_recover_initial_from_checkpoint(
            {
                "experiment_uid": "uid-123",
                "kubewiz_cluster_uuid": "cluster-injector",
                "kubewiz_profile": "526255",
            },
            "task-inject",
            connection_override={
                "kubewiz_cluster_uuid": "cluster-other",
                "kubewiz_profile": "888888",
            },
        )

    assert initial["kubewiz_cluster_uuid"] == "cluster-other"  # non-blocking
    assert initial["kubewiz_profile"] == "888888"
    assert "cluster-other" in caplog.text
    assert "cluster-injector" in caplog.text
    assert "task-inject" in caplog.text


def test_same_cluster_or_missing_cluster_override_does_not_warn(caplog):
    """Guard fires only when both axes are known AND differ: same cluster,
    or a checkpoint / override missing its cluster value, stays silent."""
    with caplog.at_level(
        logging.WARNING,
        logger="chaos_agent.agent.state_mgmt.recovery_state",
    ):
        same = build_recover_initial_from_checkpoint(
            {
                "experiment_uid": "uid-123",
                "kubewiz_cluster_uuid": "cluster-injector",
            },
            "task-inject",
            connection_override={"kubewiz_cluster_uuid": "cluster-injector"},
        )
        no_checkpoint_cluster = build_recover_initial_from_checkpoint(
            {"experiment_uid": "uid-123"},
            "task-inject",
            connection_override={"kubewiz_cluster_uuid": "cluster-other"},
        )
        no_override_cluster = build_recover_initial_from_checkpoint(
            {
                "experiment_uid": "uid-123",
                "kubewiz_cluster_uuid": "cluster-injector",
            },
            "task-inject",
            connection_override={"kubewiz_profile": "888888"},
        )

    assert same["kubewiz_cluster_uuid"] == "cluster-injector"
    assert no_checkpoint_cluster["kubewiz_cluster_uuid"] == "cluster-other"
    assert no_override_cluster["kubewiz_cluster_uuid"] == "cluster-injector"
    assert caplog.text == ""


def test_cross_channel_connection_override_warns_but_proceeds(caplog):
    """Channel-axis companion of the cluster guard: a carried channel
    different from the injection channel — a mis-bound kubewiz_host
    environment for a kubewiz_k8s fault, or a kubeconfig environment whose
    cluster affinity cannot be checked on the uuid axis — warns loudly but
    never blocks."""
    with caplog.at_level(
        logging.WARNING,
        logger="chaos_agent.agent.state_mgmt.recovery_state",
    ):
        initial = build_recover_initial_from_checkpoint(
            {
                "experiment_uid": "uid-123",
                "kube_connection_mode": "kubewiz_k8s",
                "kubewiz_cluster_uuid": "cluster-injector",
                "kubewiz_profile": "526255",
            },
            "task-inject",
            connection_override={
                "kube_connection_mode": "kubewiz_host",
                "kubewiz_profile": "888888",
            },
        )

    assert initial["kube_connection_mode"] == "kubewiz_host"  # non-blocking
    assert "kubewiz_k8s" in caplog.text
    assert "kubewiz_host" in caplog.text
    assert "task-inject" in caplog.text


def test_same_channel_connection_override_does_not_warn(caplog):
    """Equal channels stay silent — the uuid-axis guard owns the cluster
    comparison, this guard only owns the channel axis."""
    with caplog.at_level(
        logging.WARNING,
        logger="chaos_agent.agent.state_mgmt.recovery_state",
    ):
        initial = build_recover_initial_from_checkpoint(
            {
                "experiment_uid": "uid-123",
                "kube_connection_mode": "kubewiz_k8s",
                "kubewiz_cluster_uuid": "cluster-injector",
            },
            "task-inject",
            connection_override={
                "kube_connection_mode": "kubewiz_k8s",
                "kubewiz_cluster_uuid": "cluster-injector",
                "kubewiz_profile": "888888",
            },
        )

    assert initial["kube_connection_mode"] == "kubewiz_k8s"
    assert caplog.text == ""


# ---------------------------------------------------------------------------
# Workspace axis（方案 A）—— ownership fact 与凭据（runtime context）语义相反
# ---------------------------------------------------------------------------


def test_workspace_id_is_durable_ownership_not_runtime_context():
    """凭据跟恢复者走（connection_override），归属跟注入者走：
    李四恢复张三空间的任务，recover state 的 workspace 仍是张三的
    ws-zhangsan —— recover 侧查询仍锁定任务归属空间，不随恢复者漂移。
    与凭据链恰好相反的两套语义在同一 builder 里共存。"""
    initial = build_recover_initial_from_checkpoint(
        {
            "experiment_uid": "uid-123",
            "kubeconfig": "/injector/kubeconfig",
            "kubewiz_profile": "526255",
            "tenant_id": "t-org",
            "workspace_id": "ws-zhangsan",
        },
        "task-inject",
        connection_override={
            "kubeconfig": "/recoverer/kubeconfig",
            "kubewiz_profile": "888888",
            "kube_connection_mode": "kubewiz_k8s",
        },
    )

    # Credentials: the RECOVERER's (runtime context — override chain).
    assert initial["kubeconfig"] == "/recoverer/kubeconfig"
    assert initial["kubewiz_profile"] == "888888"
    # Ownership: the INJECTOR's (durable fact — checkpoint carrier, never
    # part of the connection override).
    assert initial["tenant_id"] == "t-org"
    assert initial["workspace_id"] == "ws-zhangsan"


def test_workspace_id_absent_defaults_to_empty_string():
    """旧 checkpoint（无 workspace 键）：空串兑底 = 不过滤，
    与本地 CLI 契约同构，零回归。"""
    initial = build_recover_initial_from_checkpoint(
        {"experiment_uid": "uid-123"},
        "task-inject",
    )
    assert initial["workspace_id"] == ""
    assert initial["tenant_id"] == ""
