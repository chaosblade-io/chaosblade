import pytest
from pathlib import Path

from chaos_agent.agent.task_snapshot import (
    TaskSnapshot,
    build_recover_initial_from_task_snapshot,
    resolve_recover_initial_state,
)


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
            "blade_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("store-pod"),
            "params": {"cpu-percent": "80"},
            "inject_context": "store context",
            "verification": {"layer2": {"status": "passed", "details": "store"}},
        },
        session={
            "result_summary": {
                "data": {
                    "blade_uid": "uid-from-session",
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
    assert snapshot.blade_uid == "uid-from-store"
    assert snapshot.skill_name == "pod-cpu-fullload"
    assert snapshot.target["names"] == ["store-pod"]
    assert snapshot.params == {"cpu-percent": "80"}
    assert snapshot.inject_context == "store context"
    assert snapshot.verification["layer2"]["details"] == "store"


def test_task_snapshot_prefers_session_when_increment_log_exists():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "blade_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("store-pod"),
            "params": {"cpu-percent": "80"},
            "inject_context": "store context",
            "verification": {"layer2": {"status": "passed", "details": "store"}},
        },
        session={
            "result_summary": {
                "data": {
                    "blade_uid": "uid-from-session",
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
    assert snapshot.blade_uid == "uid-from-session"
    assert snapshot.skill_name == "pod-network-loss"
    assert snapshot.target["names"] == ["session-pod"]
    assert snapshot.params == {"percent": "100"}
    assert snapshot.inject_context == "store context"
    assert snapshot.verification["layer2"]["details"] == "session"
    assert snapshot.tui_session_id == "sid-from-session"


def test_task_snapshot_reads_jsonl_even_when_json_snapshot_missing(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.agent.task_snapshot import _read_task_session
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
                    content='{"code":200,"success":true,"result":"uid-jsonl-only"}',
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
    assert snapshot.blade_uid == "uid-jsonl-only"
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
        "blade_target": "network",
        "blade_action": "loss",
        "params": {"percent": "100"},
        "params_flags": [],
        "duration_seconds": 0,
        "source": "task_snapshot_rebuild",
        "user_description": "",
    }


def test_task_snapshot_prefers_record_fault_spec_over_stale_legacy_fields():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-cpu-fullload",
            "target": _target("stale-pod"),
            "params": {"cpu-percent": "80"},
            "fault_spec": {
                "namespace": "prod",
                "scope": "pod",
                "names": ["fresh-pod"],
                "labels": {"app": "demo"},
                "blade_target": "network",
                "blade_action": "loss",
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
    assert snapshot.skill_name == "pod-network-loss"
    assert snapshot.target == {
        "namespace": "prod",
        "names": ["fresh-pod"],
        "labels": {"app": "demo"},
        "resource_type": "pod",
    }
    assert snapshot.params == {"percent": "100"}
    assert snapshot.fault_spec()["blade_target"] == "network"
    assert snapshot.fault_spec()["names"] == ["fresh-pod"]


def test_task_snapshot_increment_log_can_override_record_fault_spec():
    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "skill_name": "pod-network-loss",
            "fault_spec": {
                "namespace": "prod",
                "scope": "pod",
                "names": ["old-pod"],
                "labels": {},
                "blade_target": "network",
                "blade_action": "loss",
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
    assert snapshot.skill_name == "pod-cpu-fullload"
    assert snapshot.target["names"] == ["session-pod"]
    assert snapshot.params == {"cpu-percent": "80"}
    assert snapshot.fault_spec()["blade_target"] == "cpu"
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
            "blade_uid": "uid-from-store",
            "skill_name": "pod-cpu-fullload",
            "target": _target("demo"),
            "params": {"cpu-percent": "80"},
            "kubeconfig": "/old/kubeconfig",
            "kube_context": "ctx-a",
            "injection_method": "kubectl_exec",
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
    assert initial["blade_uid"] == "uid-from-store"
    assert initial["skill_name"] == "pod-cpu-fullload"
    assert initial["skill_case_content"] == "skill case text"
    assert initial["inject_verification_summary"] == (
        "Layer2=passed, Details=verified"
    )
    assert initial["kubeconfig"] == "/new/kubeconfig"
    assert initial["kube_context"] == "ctx-a"
    assert initial["injection_method"] == "kubectl_exec"
    assert initial["kubectl_exec_pod_name"] == "tool-pod-a"


def test_runtime_recover_entrypoints_use_task_snapshot_resolver():
    """Recover entrypoints should not bypass TaskSnapshot merge policy."""

    required_resolver_paths = {
        "src/chaos_agent/cli/runner.py",
        "src/chaos_agent/server/routes/recover_common.py",
        "src/chaos_agent/server/routes/turn_event_stream.py",
        "src/chaos_agent/server/routes/turn_result.py",
        "src/chaos_agent/l4/agent.py",
    }
    allowed_checkpoint_builder_paths = {
        "src/chaos_agent/agent/recovery_state.py",
        "src/chaos_agent/agent/task_snapshot.py",
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


@pytest.mark.asyncio
async def test_resolver_source_values_preserve_snapshot_verification(monkeypatch):
    """Recover graph stays clean while result/reporting source values keep inject facts."""

    from chaos_agent.agent import task_snapshot

    snapshot = TaskSnapshot.from_sources(
        task_id="task-inject",
        record={
            "blade_uid": "uid-from-store",
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
