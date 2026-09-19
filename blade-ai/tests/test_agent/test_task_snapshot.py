import logging

import pytest
from pathlib import Path

from chaos_agent.agent.result.task_snapshot import (
    TaskSnapshot,
    _rebuild_inject_verification_summary,
    build_recover_initial_from_task_snapshot,
    resolve_recover_initial_state,
)
from chaos_agent.persistence.task_store_backend import _TASK_COLUMNS


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _target(name: str) -> dict:
    return {
        "namespace": "default",
        "names": [name],
        "labels": {},
        "resource_type": "pod",
    }


def test_task_snapshot_prefers_task_store_when_no_increment_log():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("store-pod"),
            "params": {"cpu-percent": "80"},
            "inject_context": "store context",
            "verification": {"layer2": {"status": "passed", "details": "store"}},
        },
        session={
            "result_summary": {
                "data": {
                    "experiment_uid": "uid-from-session",
                    "fault_type": "pod-network-loss",
                    "target": _target("session-pod"),
                    "params": {"percent": "100"},
                    "verification": {
                        "layer2": {"status": "passed", "details": "session"}
                    },
                }
            },
            "messages": [],
        },
        has_increment_log=False,
    )

    assert snapshot is not None
    assert snapshot.experiment_uid == "uid-from-store"
    assert snapshot.skill_name == "pod-cpu-fullload"
    assert snapshot.fault_type == "pod-cpu-fullload"
    assert snapshot.target["names"] == ["store-pod"]
    assert snapshot.params == {"cpu-percent": "80"}
    assert snapshot.inject_context == "store context"
    assert snapshot.verification["layer2"]["details"] == "store"


def test_task_snapshot_prefers_session_when_increment_log_exists():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("store-pod"),
            "params": {"cpu-percent": "80"},
            "inject_context": "store context",
            "verification": {"layer2": {"status": "passed", "details": "store"}},
        },
        session={
            "result_summary": {
                "data": {
                    "experiment_uid": "uid-from-session",
                    "fault_type": "pod-network-loss",
                    "target": _target("session-pod"),
                    "params": {"percent": "100"},
                    "verification": {
                        "layer2": {"status": "passed", "details": "session"}
                    },
                }
            },
            "messages": [],
            "tui_session_id": "sid-from-session",
        },
        has_increment_log=True,
    )

    assert snapshot is not None
    assert snapshot.experiment_uid == "uid-from-session"
    assert snapshot.skill_name == "pod-cpu-fullload"
    assert snapshot.fault_type == "pod-network-loss"
    assert snapshot.target["names"] == ["session-pod"]
    assert snapshot.params == {"percent": "100"}
    assert snapshot.inject_context == "store context"
    assert snapshot.verification["layer2"]["details"] == "session"
    assert snapshot.tui_session_id == "sid-from-session"


def test_task_snapshot_reads_jsonl_even_when_json_snapshot_missing(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.agent.result.task_snapshot import _read_task_session
    from chaos_agent.memory.session_store import SessionStore, set_global_session_store

    session_store = SessionStore(tmp_path / "tasks")
    set_global_session_store(session_store)
    try:
        session_store.create_session("task-jsonl-only", operation="inject")
        session_store.append_messages(
            "task-jsonl-only",
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "blade_create",
                            "args": {},
                            "id": "tc-create",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"a11b2c3d4e5f6071"}',
                    name="blade_create",
                    tool_call_id="tc-create",
                ),
            ],
        )
        (tmp_path / "tasks" / "task-jsonl-only.json").unlink()

        session, has_increment_log = _read_task_session("task-jsonl-only")
        snapshot = TaskSnapshot.from_sources(
            task_id="task-jsonl-only",
            record={
                "skill_name": "pod-cpu-fullload",
                "target": _target("demo"),
                "params": {"cpu-percent": "80"},
            },
            session=session,
            has_increment_log=has_increment_log,
        )
    finally:
        set_global_session_store(None)  # type: ignore[arg-type]

    assert has_increment_log is True
    assert session is not None
    assert len(session["messages"]) == 2
    assert snapshot is not None
    assert snapshot.experiment_uid == "a11b2c3d4e5f6071"
    assert "blade_create" in snapshot.inject_context


def test_task_snapshot_builds_fault_spec_from_merged_context():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
        },
        session=None,
        has_increment_log=False,
    )

    assert snapshot is not None
    assert snapshot.has_recover_context is True
    assert snapshot.fault_spec() == {
        "namespace": "default",
        "scope": "pod",
        "names": ["demo"],
        "labels": {},
        "fault_target": "network",
        "fault_action": "loss",
        "params": {"percent": "100"},
        "params_flags": [],
        "duration_seconds": 0,
        "source": "task_snapshot_rebuild",
        "user_description": "",
        "case_resource_path": "",
        "revision": 0,
        "objective": "",
        "boundaries": [],
        "constraints": [],
        "assumptions": [],
    }


def test_task_snapshot_prefers_record_fault_spec_over_stale_legacy_fields():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "active-chaos-skill",
            "target": _target("stale-pod"),
            "params": {"cpu-percent": "80"},
            "fault_spec": {
                "namespace": "prod",
                "scope": "pod",
                "names": ["fresh-pod"],
                "labels": {"app": "demo"},
                "fault_target": "network",
                "fault_action": "loss",
                "params": {"percent": "100"},
                "params_flags": [],
                "duration_seconds": 0,
                "source": "task_store",
                "user_description": "",
            },
        },
        session=None,
        has_increment_log=False,
    )

    assert snapshot is not None
    assert snapshot.skill_name == "active-chaos-skill"
    assert snapshot.fault_type == "pod-network-loss"
    assert snapshot.target == {
        "namespace": "prod",
        "names": ["fresh-pod"],
        "labels": {"app": "demo"},
        "resource_type": "pod",
    }
    assert snapshot.params == {"percent": "100"}
    assert snapshot.fault_spec()["fault_target"] == "network"
    assert snapshot.fault_spec()["names"] == ["fresh-pod"]


def test_task_snapshot_increment_log_can_override_record_fault_spec():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "active-chaos-skill",
            "fault_spec": {
                "namespace": "prod",
                "scope": "pod",
                "names": ["old-pod"],
                "labels": {},
                "fault_target": "network",
                "fault_action": "loss",
                "params": {"percent": "100"},
                "params_flags": [],
                "duration_seconds": 0,
                "source": "task_store",
                "user_description": "",
            },
        },
        session={
            "result_summary": {
                "data": {
                    "fault_type": "pod-cpu-fullload",
                    "target": _target("session-pod"),
                    "params": {"cpu-percent": "80"},
                }
            },
            "messages": [],
        },
        has_increment_log=True,
    )

    assert snapshot is not None
    assert snapshot.skill_name == "active-chaos-skill"
    assert snapshot.fault_type == "pod-cpu-fullload"
    assert snapshot.target["names"] == ["session-pod"]
    assert snapshot.params == {"cpu-percent": "80"}
    assert snapshot.fault_spec()["fault_target"] == "cpu"
    assert snapshot.fault_spec()["names"] == ["session-pod"]


def test_task_snapshot_incomplete_fault_spec_does_not_mask_legacy_target():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-cpu-fullload",
            "target": _target("legacy-pod"),
            "params": {"cpu-percent": "80"},
            "fault_spec": {"params": {"cpu-percent": "90"}},
        },
        session=None,
        has_increment_log=False,
    )

    assert snapshot is not None
    assert snapshot.target["names"] == ["legacy-pod"]
    assert snapshot.fault_spec()["duration_seconds"] == 0


@pytest.mark.asyncio
async def test_recover_initial_from_task_snapshot_uses_snapshot_fields():
    class _Registry:
        def activate(self, skill_name):
            assert skill_name == "pod-cpu-fullload"
            return "skill case text"

    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("demo"),
            "params": {"cpu-percent": "80"},
            "kubeconfig": "/old/kubeconfig",
            "kube_context": "ctx-a",
            "injection_method": "kubectl_exec",
            "execution_artifacts": [{"artifact_id": "uid-debug", "type": "debug_pod"}],
            "kubectl_exec_pod_name": "tool-pod-a",
            "gmt_create": "2026-06-18T10:00:00+08:00",
            "verification": {
                "layer2": {"status": "passed", "details": "verified"}
            },
        },
        session={"messages": []},
        has_increment_log=False,
        tui_session_id="sid-1",
    )

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        agents={"skill_registry": _Registry()},
        kubeconfig_override="/new/kubeconfig",
    )

    assert initial["task_id"] == "task-recover"
    assert initial["parent_task_id"] == "task-inject"
    assert initial["tui_session_id"] == "sid-1"
    assert initial["experiment_uid"] == "uid-from-store"
    assert initial["skill_name"] == "pod-cpu-fullload"
    assert initial["fault_type"] == "pod-cpu-fullload"
    assert initial["skill_case_content"] == "skill case text"
    assert initial["inject_verification_summary"] == (
        "Layer2=passed, Details=verified"
    )
    assert initial["kubeconfig"] == "/new/kubeconfig"
    assert initial["kube_context"] == "ctx-a"
    assert initial["injection_method"] == "kubectl_exec"
    assert initial["execution_artifacts"] == [
        {"artifact_id": "uid-debug", "type": "debug_pod"}
    ]
    assert initial["kubectl_exec_pod_name"] == "tool-pod-a"


def test_runtime_recover_entrypoints_use_task_snapshot_resolver():
    """Recover entrypoints should not bypass TaskSnapshot merge policy."""

    required_resolver_paths = {
        "src/chaos_agent/cli/runner.py",
        "src/chaos_agent/server/routes/recover_common.py",
        "src/chaos_agent/server/routes/turn_event_stream.py",
        "src/chaos_agent/server/routes/turn_result.py",
        # L4 SDK recover entrypoints: agent.py was split into mixins in the
        # baseline refactor (b78c82c); the recover path now lives in
        # recovery.py (_L4RecoveryMixin) and execution.py (_L4ExecutionMixin).
        "src/chaos_agent/l4/recovery.py",
        "src/chaos_agent/l4/execution.py",
    }
    allowed_checkpoint_builder_paths = {
        "src/chaos_agent/agent/state_mgmt/recovery_state.py",
        "src/chaos_agent/agent/result/task_snapshot.py",
        # Compatibility helper used by adapter unit tests and older SDK callers;
        # runtime L4 recover paths are guarded above through l4/agent.py.
        "src/chaos_agent/l4/adapter.py",
    }

    violations = []
    for rel in required_resolver_paths:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "resolve_recover_initial_state" not in text:
            violations.append(f"{rel}: missing resolve_recover_initial_state")

    for path in (PROJECT_ROOT / "src/chaos_agent").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if (
            "build_recover_initial_from_checkpoint" in text
            and rel not in allowed_checkpoint_builder_paths
        ):
            violations.append(f"{rel}: direct build_recover_initial_from_checkpoint")

    assert violations == []


def test_l4_recover_entry_assembles_connection_override_from_payload():
    """The platform resolves the session-bound environment into task.payload
    keys before dispatching a recover task; the L4 entry must assemble them
    into connection_override (incident 2026-09-15: frozen injector profile
    '526255' won over the recovering user's identity).  Static guard in the
    spirit of test_runtime_recover_entrypoints_use_task_snapshot_resolver.
    """
    text = (
        PROJECT_ROOT / "src/chaos_agent/l4/recovery.py"
    ).read_text(encoding="utf-8")

    assert "connection_override=connection_override" in text
    for key in (
        "kubeconfig",
        "kube_context",
        "kube_connection_mode",
        "kubewiz_cluster_uuid",
        "kubewiz_profile",
    ):
        assert f'"{key}"' in text, f"payload key {key} missing from L4 recover entry"


@pytest.mark.asyncio
async def test_resolver_source_values_preserve_snapshot_verification(monkeypatch):
    """Recover graph stays clean while result/reporting source values keep inject facts."""

    from chaos_agent.agent.result import task_snapshot

    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("demo"),
            "params": {"cpu-percent": "80"},
            "kubeconfig": "/snapshot/kubeconfig",
            "verification": {
                "level": "verified",
                "layer2": {"status": "passed", "details": "snapshot verification"},
            },
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    async def fake_load_task_snapshot(task_id, *, tui_session_id=""):
        assert task_id == "task-inject"
        return snapshot

    monkeypatch.setattr(task_snapshot, "load_task_snapshot", fake_load_task_snapshot)

    resolution = await resolve_recover_initial_state(
        "task-inject",
        record_task_id="task-recover",
        checkpoint_values={
            "verification": {"level": "stale-checkpoint"},
            "messages": ["baseline-message"],
        },
    )

    assert resolution is not None
    assert resolution.initial_state["verification"] is None
    assert resolution.source_values["verification"] == snapshot.verification
    assert resolution.source_values["inject_verification_summary"] == (
        "Layer2=passed, Details=snapshot verification"
    )
    assert resolution.source_values["kubeconfig"] == "/snapshot/kubeconfig"
    assert resolution.source_values["messages"] == ["baseline-message"]


@pytest.mark.asyncio
async def test_resolver_connection_override_outranks_snapshot_and_checkpoint(monkeypatch):
    """Snapshot path: the caller-carried connection must survive the
    snapshot→seed→builder funnel — record-persisted and checkpoint-frozen
    credentials both lose to the recovering caller's (incident 2026-09-15)."""

    from chaos_agent.agent.result import task_snapshot

    snap = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("demo"),
            "params": {"cpu-percent": "80"},
            "kubeconfig": "/snapshot/kubeconfig",
            "kube_context": "ctx-snapshot",
        },
        session={"messages": []},
        has_increment_log=False,
    )

    async def fake_load_task_snapshot(task_id, *, tui_session_id=""):
        assert task_id == "task-inject"
        return snap

    monkeypatch.setattr(task_snapshot, "load_task_snapshot", fake_load_task_snapshot)

    resolution = await resolve_recover_initial_state(
        "task-inject",
        record_task_id="task-recover",
        checkpoint_values={
            "experiment_uid": "uid-from-store",
            "kubeconfig": "/frozen/kubeconfig",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "526255",
        },
        connection_override={
            "kubeconfig": "/recoverer/kubeconfig",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "888888",
            "kube_connection_mode": "kubewiz_k8s",
        },
    )

    assert resolution is not None
    initial = resolution.initial_state
    assert initial["kubewiz_profile"] == "888888"  # beats frozen '526255'
    assert initial["kubeconfig"] == "/recoverer/kubeconfig"  # beats record+checkpoint
    assert initial["kubewiz_cluster_uuid"] == "cluster-injector"
    assert initial["kube_context"] == "ctx-snapshot"  # un-carried axis: record fallback
    assert initial["kube_connection_mode"] == "kubewiz_k8s"


@pytest.mark.asyncio
async def test_resolver_connection_override_survives_checkpoint_fallback_path(monkeypatch):
    """Checkpoint-only path (no snapshot available): the override must
    reach build_recover_initial_from_checkpoint exactly as on the snapshot
    path — both resolver branches are the same contract."""

    from chaos_agent.agent.result import task_snapshot

    async def fake_load_task_snapshot(task_id, *, tui_session_id=""):
        return None

    monkeypatch.setattr(task_snapshot, "load_task_snapshot", fake_load_task_snapshot)

    resolution = await resolve_recover_initial_state(
        "task-inject",
        record_task_id="task-recover",
        checkpoint_values={
            "experiment_uid": "uid-123",
            "skill_name": "pod-cpu-fullload",
            "kubewiz_cluster_uuid": "cluster-injector",
            "kubewiz_profile": "526255",
        },
        connection_override={"kubewiz_profile": "888888"},
    )

    assert resolution is not None
    assert resolution.source == "checkpoint"
    assert resolution.initial_state["kubewiz_profile"] == "888888"
    assert resolution.initial_state["kubewiz_cluster_uuid"] == "cluster-injector"


@pytest.mark.asyncio
async def test_resolver_cross_channel_guard_fires_via_snapshot_path(monkeypatch, caplog):
    """Snapshot path wiring: the seed must carry the injection-time channel
    (from checkpoint values) so the builder's cross-channel guard has data
    to compare on the primary platform path."""

    from chaos_agent.agent.result import task_snapshot

    snap = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "experiment_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("demo"),
        },
        session={"messages": []},
        has_increment_log=False,
    )

    async def fake_load_task_snapshot(task_id, *, tui_session_id=""):
        assert task_id == "task-inject"
        return snap

    monkeypatch.setattr(task_snapshot, "load_task_snapshot", fake_load_task_snapshot)

    with caplog.at_level(
        logging.WARNING,
        logger="chaos_agent.agent.state_mgmt.recovery_state",
    ):
        resolution = await resolve_recover_initial_state(
            "task-inject",
            record_task_id="task-recover",
            checkpoint_values={
                "experiment_uid": "uid-from-store",
                "kube_connection_mode": "kubewiz_k8s",
            },
            connection_override={"kube_connection_mode": "kubewiz_host"},
        )

    assert resolution is not None
    assert resolution.initial_state["kube_connection_mode"] == "kubewiz_host"
    assert "kubewiz_k8s" in caplog.text
    assert "kubewiz_host" in caplog.text


def test_rebuild_inject_verification_summary_includes_warnings():
    """Side-effect warnings are structural facts and must survive rebuild."""
    summary = _rebuild_inject_verification_summary({
        "layer2": {"status": "passed", "details": "fault active as planned"},
        "warnings": ["endpoint removed from svc", "readiness probe failing"],
    })
    assert "Layer2=passed" in summary
    assert "fault active as planned" in summary
    assert "Recorded side-effect warnings at injection" in summary
    assert "(1) endpoint removed from svc" in summary
    assert "(2) readiness probe failing" in summary


def test_rebuild_inject_verification_summary_no_warnings_unchanged():
    summary = _rebuild_inject_verification_summary({
        "layer2": {"status": "passed", "details": "ok"},
        "warnings": [],
    })
    assert summary == "Layer2=passed, Details=ok"


def test_task_snapshot_prefers_persisted_inject_context():
    """Durable-first: finalize-persisted inject_context beats record/message scan."""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={"inject_context": "record context", "target": _target("p")},
        session={
            "result_summary": {"data": {"inject_context": "persisted context"}},
            "messages": [],
        },
        has_increment_log=True,
    )
    assert snapshot is not None
    assert snapshot.inject_context == "persisted context"


def test_task_snapshot_extracts_blast_radius_and_side_effects():
    session_data = {
        "blast_radius_detail": "session blast radius",
        "side_effects": {"endpoint_removals": ["svc-a"]},
    }
    record = {
        "target": _target("p"),
        "blast_radius_detail": "record blast radius",
        "side_effects": {"probe_failures": ["pod-a"]},
    }

    # increment-log branch: session (finalize-persisted) values win.
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record=record,
        session={"result_summary": {"data": session_data}, "messages": []},
        has_increment_log=True,
    )
    assert snapshot is not None
    assert snapshot.blast_radius_detail == "session blast radius"
    assert snapshot.side_effects == {"endpoint_removals": ["svc-a"]}

    # store-preferred branch: record values win.
    snapshot2 = TaskSnapshot.from_sources(
        task_id="task-inject",
        record=record,
        session={"result_summary": {"data": session_data}, "messages": []},
        has_increment_log=False,
    )
    assert snapshot2 is not None
    assert snapshot2.blast_radius_detail == "record blast radius"
    assert snapshot2.side_effects == {"probe_failures": ["pod-a"]}


@pytest.mark.asyncio
async def test_build_recover_initial_from_task_snapshot_carries_side_effects():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
        },
        session={
            "result_summary": {
                "data": {
                    "blast_radius_detail": "2 replicas impacted",
                    "side_effects": {"endpoint_removals": ["svc-a"]},
                }
            },
            "messages": [],
        },
        has_increment_log=True,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot, record_task_id="task-recover"
    )
    assert initial is not None
    assert initial["blast_radius_detail"] == "2 replicas impacted"
    assert initial["side_effects"] == {"endpoint_removals": ["svc-a"]}

    # Live checkpoint fills fields an older record lacks.
    initial2 = await build_recover_initial_from_task_snapshot(
        TaskSnapshot.from_sources(
            task_id="task-inject",
            record={
                "skill_name": "pod-network-loss",
                "target": _target("demo"),
                "params": {"percent": "100"},
                "inject_context": "ctx",
            },
            session={"messages": []},
            has_increment_log=False,
        ),
        record_task_id="task-recover",
        checkpoint_values={"blast_radius_detail": "checkpoint blast radius"},
    )
    assert initial2 is not None
    assert initial2["blast_radius_detail"] == "checkpoint blast radius"


async def test_build_recover_initial_from_task_snapshot_seeds_liability_records():
    """B76 review G: the birth/death registries must reach the recover graph
    — the live checkpoint is the only durable carrier (record first when it
    gains a column, checkpoint fills everything older), or the recover-side
    liability sweep silently runs empty."""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={
            "owned_experiment_uids": ["uid-old", "uid-main"],
            "retired_experiment_uids": ["uid-old"],
        },
    )
    assert initial is not None
    assert initial["owned_experiment_uids"] == ["uid-old", "uid-main"]
    assert initial["retired_experiment_uids"] == ["uid-old"]


async def test_recover_seed_fills_wings_and_marker_from_record():
    """Round-32b：DB-only 恢复 —— record 填充双翼（round-32 已是列），
    且 combo marker 以真 bool 到位（原始 JSON 字串 "false" 对每个
    bool() 消费方都是毒药）。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
            "owned_experiment_uids": '["uid-a", "uid-b"]',
            "retired_experiment_uids": '["uid-a"]',
            "combo_native_issued": "false",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={},
    )
    assert initial is not None
    assert initial["owned_experiment_uids"] == ["uid-a", "uid-b"]
    assert initial["retired_experiment_uids"] == ["uid-a"]
    # TYPED bool — never the raw "false" string (truthy poison)
    assert initial["combo_native_issued"] is False


async def test_recover_seed_checkpoint_marker_outranks_record():
    """活 checkpoint（最新 marker）胜出；record 只在缺席时填位。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
            "combo_native_issued": "false",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={"combo_native_issued": True},
    )
    assert initial is not None
    assert initial["combo_native_issued"] is True


async def test_recover_seed_marker_true_sticks_across_carriers():
    """Round-32b F-2 —— 水合镜像 DB 闩锁的单调语义：任一载体 True 即
    True。窄窗场景：combo 升级后 store sync 已落库 True，但下一次
    superstep checkpoint 保存前进程崩溃 → resume 时 checkpoint 还是
    升级前的旧 False、record 是 True。旧语义（checkpoint 硬优先）会取
    False，让本轮 recover 走 deterministic-only 路由、泄漏 native 变异
    —— 正是 marker 存在意义要防的事故类；DB 行有闩锁保 True 仍可恢复，
    错的路由只发生在这一轮。sticky 语义把水合面与落库面对齐。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
            "combo_native_issued": "true",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={"combo_native_issued": False},
    )
    assert initial is not None
    # Sticky: the record's True outranks the checkpoint's stale False.
    assert initial["combo_native_issued"] is True


async def test_recover_seed_fills_identity_from_record():
    """Round-32b F-6 —— DB-only 恢复的身份补腿：checkpoint 缺席（进程
    崩溃后 checkpoint 丢失、DB 行还活着）时，tenant/workspace 从 record
    落回 seed。单载体读会让 recover 任务自己的行归属退化为 unfiltered
    —— 翼字段同型优先序（checkpoint 优先，record 填空）。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
            "tenant_id": "t-org",
            "workspace_id": "ws-zhangsan",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={},
    )
    assert initial is not None
    assert initial["tenant_id"] == "t-org"
    assert initial["workspace_id"] == "ws-zhangsan"


async def test_recover_seed_checkpoint_identity_outranks_record():
    """checkpoint 是 fresher 载体：携带身份时胜出（record 只在缺席时
    填位）——与 owned/retired 翼的优先序逐字同型。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
            "tenant_id": "t-record",
            "workspace_id": "ws-record",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={
            "tenant_id": "t-checkpoint",
            "workspace_id": "ws-checkpoint",
        },
    )
    assert initial is not None
    assert initial["tenant_id"] == "t-checkpoint"
    assert initial["workspace_id"] == "ws-checkpoint"


async def test_recover_seed_identity_absent_defaults_empty():
    """双缺席 → 空串。空 = unfiltered（本地 CLI / 裸 SDK 入口契约），
    不是错误：与 tenant_id 在三后端 select_active_tasks 的语义一致。"""
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "target": _target("demo"),
            "params": {"percent": "100"},
            "inject_context": "ctx",
        },
        session={"messages": []},
        has_increment_log=False,
    )
    assert snapshot is not None

    initial = await build_recover_initial_from_task_snapshot(
        snapshot,
        record_task_id="task-recover",
        checkpoint_values={},
    )
    assert initial is not None
    assert initial["tenant_id"] == ""
    assert initial["workspace_id"] == ""


# ---------------------------------------------------------------------------
# R4: attribution facts (injection_method / fault_handle) survive finalize
# persistence and feed recover hydration
# ---------------------------------------------------------------------------

class TestR4AttributionHydration:
    def test_session_attribution_wins_with_increment_log(self):
        native_handle = {"kind": "native", "method": "kubectl_native"}
        snapshot = TaskSnapshot.from_sources(
            task_id="task-inject",
            record={
                "injection_method": "kubectl_exec",
                "fault_handle": {"kind": "blade_uid", "value": "u", "method": "kubectl_exec"},
                "target": _target("p"),
            },
            session={
                "result_summary": {
                    "data": {
                        "injection_method": "kubectl_native",
                        "fault_handle": native_handle,
                    }
                },
                "messages": [],
            },
            has_increment_log=True,
        )
        assert snapshot is not None
        assert snapshot.injection_method == "kubectl_native"
        assert snapshot.fault_handle == native_handle

    def test_record_attribution_wins_without_increment_log(self):
        record_handle = {"kind": "blade_uid", "value": "uid-r", "method": "host_blade"}
        snapshot = TaskSnapshot.from_sources(
            task_id="task-inject",
            record={
                "injection_method": "host_blade",
                "fault_handle": record_handle,
                "target": _target("p"),
            },
            session={
                "result_summary": {
                    "data": {
                        "injection_method": "kubectl_exec",
                        "fault_handle": {"kind": "blade_uid", "value": "uid-s"},
                    }
                },
                "messages": [],
            },
            has_increment_log=False,
        )
        assert snapshot is not None
        assert snapshot.injection_method == "host_blade"
        assert snapshot.fault_handle == record_handle

    @pytest.mark.asyncio
    async def test_native_fault_handle_hydrates_recover_initial(self):
        """Old store row (no injection_method column) + finalize-persisted
        native attribution: the recover initial state still carries the
        handle — a UID-less fault is recoverable across a restart."""
        native_handle = {"kind": "native", "method": "kubectl_native"}
        snapshot = TaskSnapshot.from_sources(
            task_id="task-inject",
            record={
                "skill_name": "pod-replicas-scale",
                "target": _target("demo"),
                "params": {"replicas": "0"},
            },
            session={
                "result_summary": {
                    "data": {
                        "injection_method": "kubectl_native",
                        "fault_handle": native_handle,
                    }
                },
                "messages": [],
            },
            has_increment_log=True,
        )
        assert snapshot is not None
        assert snapshot.experiment_uid == ""

        initial = await build_recover_initial_from_task_snapshot(
            snapshot, record_task_id="task-recover"
        )
        assert initial is not None
        assert initial["injection_method"] == "kubectl_native"
        assert initial["fault_handle"] == native_handle
        assert initial["experiment_uid"] == ""

    def test_build_inject_data_persists_attribution_facts(self):
        """Supply side: finalize projection freezes the attribution facts into
        the persisted result-card data."""
        from chaos_agent.agent.result.operation_result import (
            build_inject_data_from_state,
        )

        data = build_inject_data_from_state(
            {"injection_method": "kubectl_native"}, "task-inject"
        )
        assert data["injection_method"] == "kubectl_native"
        assert data["fault_handle"] == {
            "kind": "native", "method": "kubectl_native"
        }
        assert data["experiment_uid"] == ""
        assert "blade_uid" not in data


# ---------------------------------------------------------------------------
# L2: carrier-reconciliation completeness guard (round-32b)
# ---------------------------------------------------------------------------

class TestCarrierReconciliationCompleteness:
    """泛型写 / 枚举读不对称的读侧护栏。

    写侧（sync_to_store）对整个 AgentState 泛型投影——列在
    _TASK_COLUMNS 里即落库，新 durable 列零专门代码自动闭合。读侧
    （build_recover_initial_from_task_snapshot 的 seed）是逐字段手写
    枚举——新列落库了、checkpoint 自动携带了，唯独「从 DB 行读回」这
    条腿需要作者手工承认。次生 bug #4、F-6、workspace 抄 tenant 三例
    同型缺口全部产自这条缝。本对账把「新列落地必须当场归类（水合
    还是豁免）」变成测试红灯，决策点被强制移到正确的时刻。

    诚实边界：拦得住「完全缺席」与「单载体被当双载体」（出现频率
    最高的两个失效形状）；拦不住「双载体都在但优先序选错」（形态
    正确性需要语义类声明——L3 立法，明确记录不实施）。"""

    # 逐列豁免，每项一行理由。新列落地时两边都不在 → 层一断言红
    # → 作者被迫当场归类。
    _EXEMPT = {
        "id": "surrogate key — DB-only, never an AgentState field",
        "task_id": (
            "recover identity is rebuilt (record_task_id / parent_task_id "
            "/ recover_task_id), never hydrated verbatim"
        ),
        "task_state": (
            "lifecycle word — state.py legislation owns the vocabulary; "
            "the recover graph resets its own"
        ),
        "stage": "derived display column (upsert infers), no seed consumer",
        "phase": "derived display column (upsert infers), no seed consumer",
        "operation": (
            "recover graph fixes operation='recover' at the builder; the "
            "inject-time value is deliberately not hydrated"
        ),
        "namespace": (
            "hydrated via target→fault_spec projection "
            "(_merge_snapshot_checkpoint_fault_spec), not a top-level seed key"
        ),
        "target_name": (
            "derived index column (_extract_index_fields draws it from "
            "target.names), not an independent fact"
        ),
        "liability_live": (
            "materialised write-side verdict (may_carry_live_fault); the "
            "read side consumes it via the SQL filter, never via seed"
        ),
        "error": "outcome field — reset on recover entry (lifecycle policy)",
        "finished_at": "terminal timestamp — reset on recover entry",
        "duration_ms": "observability aggregate (tracer), no recover consumer",
        "gmt_modified": "pure DB timestamp, write-side only",
    }

    # 出生恒定事实（checkpoint 优先，record 填空）：双载体纪律白名单。
    # 单载体抄写正是 F-6 的失效形状（workspace 抄了 tenant 的旧先例）。
    # 含 detail 侧的翼 + combo marker——同一 seam 上的同一家族。
    _DUAL_CARRIER = {
        "tenant_id",
        "workspace_id",
        "owned_experiment_uids",
        "retired_experiment_uids",
        "combo_native_issued",
    }

    def _seed_body(self) -> str:
        """Source text of the seed-assembly function (static guard, in the
        spirit of test_runtime_recover_entrypoints_use_task_snapshot_resolver)."""
        src = (
            PROJECT_ROOT / "src/chaos_agent/agent/result/task_snapshot.py"
        ).read_text(encoding="utf-8")
        start = src.index("async def build_recover_initial_from_task_snapshot")
        end = src.index("async def resolve_recover_initial_state")
        return src[start:end]

    def _assignment_fragment(self, body: str, column: str) -> str:
        """Text from the seed key through the expression's own closing comma.

        Bracket-depth scan, NOT a next-key cut: a comment block between two
        seed keys carries no bracket, so cutting at the next 8-space quote
        line swallows it — and ``snapshot.record`` wording a future comment
        happens to mention would then satisfy the dual-carrier assertion
        from OUTSIDE the expression (round-32c F-9, mutation C — the mirror
        of the "negative anchor fired by documentation prose" trap). The
        fragment ends where the expression does: bracket depth back to
        zero, then the top-level comma."""
        _head, sep, rest = body.partition(f'"{column}":')
        assert sep, f"column {column!r} has no seed assignment at all"
        depth = 0
        in_string = False
        quote = ""
        i = 0
        while i < len(rest):
            char = rest[i]
            if in_string:
                if char == "\\":
                    i += 2
                    continue
                if char == quote:
                    in_string = False
                i += 1
                continue
            if char in "\"'":
                in_string = True
                quote = char
            elif char == "#":
                # A comment runs to end of line — skip it wholesale so its
                # prose (and any brackets inside it) cannot leak in either
                # direction.
                newline = rest.find("\n", i)
                i = len(rest) if newline == -1 else newline + 1
                continue
            elif char in "([{":
                depth += 1
            elif char in ")]}":
                depth -= 1
            elif char == "," and depth == 0:
                return rest[:i]
            i += 1
        return rest

    def test_exempt_columns_exist_in_schema(self):
        """豁免清单防漂移：每项必须是真实的 tasks 列（拼写错 → 红）。"""
        unknown = set(self._EXEMPT) - set(_TASK_COLUMNS)
        assert unknown == set(), f"exempt entries that are not real columns: {unknown}"

    def test_every_task_column_is_hydrated_or_exempt(self):
        """层一（完备性）：每个非豁免列必须出现在水合 seed 构造里。

        新 durable 列落进 _TASK_COLUMNS 后若无人归类，这里先红——
        正是次生 bug #4 / F-6 的机器在「读侧忘了承认新列」时暴露的
        那一步。"""
        body = self._seed_body()
        missing = [
            col
            for col in _TASK_COLUMNS
            if col not in self._EXEMPT and f'"{col}"' not in body
        ]
        assert missing == [], (
            f"tasks columns with no hydration policy (classify each into "
            f"seed or _EXEMPT with a reason): {missing}"
        )

    def test_birth_constant_facts_are_dual_carrier(self):
        """层二（双载体纪律）：出生恒定事实的 seed 表达式必须同时引用
        checkpoint 载体与 record 载体——只写一边就是 F-6 的单载体形状。"""
        body = self._seed_body()
        for column in sorted(self._DUAL_CARRIER):
            fragment = self._assignment_fragment(body, column)
            assert "checkpoint_values" in fragment, (
                f"{column}: hydration expression lost the checkpoint carrier"
            )
            assert "snapshot.record" in fragment, (
                f"{column}: hydration expression lost the record carrier "
                f"(single-carrier copy is the F-6 shape)"
            )
