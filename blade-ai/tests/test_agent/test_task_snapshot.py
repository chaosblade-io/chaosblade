import pytest

from chaos_agent.agent.task_snapshot import (
    TaskSnapshot,
    build_recover_initial_from_task_snapshot,
)


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
