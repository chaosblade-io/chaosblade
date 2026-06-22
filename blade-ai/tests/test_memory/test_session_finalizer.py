from pathlib import Path
from types import SimpleNamespace

import pytest

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_DATA_ENVELOPE,
    RESULT_SUMMARY_INJECT_ENVELOPE,
    RESULT_SUMMARY_STATUS_ENVELOPE,
    build_inject_session_summary,
    finalize_inject_session,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _inject_values() -> dict:
    spec = FaultSpec(
        namespace="arms-prom",
        scope="pod",
        names=("pod-a",),
        blade_target="cpu",
        blade_action="fullload",
        params={"cpu-percent": "80"},
    )
    return {
        "fault_spec": spec.to_dict(),
        "blade_uid": "uid-1",
        "result": {"success": True},
        "verification": {
            "level": "verified",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
    }


class _Graph:
    def __init__(self, values: dict):
        self.values = values

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


class _SessionStore:
    def __init__(self):
        self.finalized = None
        self.appended = None

    def finalize_session(self, task_id, **kwargs):
        self.finalized = {"task_id": task_id, **kwargs}

    def append_messages(self, task_id, messages):
        self.appended = {"task_id": task_id, "messages": messages}


class _TuiStore:
    def __init__(self):
        self.dialogue = None

    def append_dialogue(self, session_id, messages):
        self.dialogue = {"session_id": session_id, "messages": messages}


def test_status_summary_preserves_legacy_server_inject_shape():
    data = {
        "task_id": "task-1",
        "task_state": "injected",
        "fault_type": "pod-cpu-fullload",
        "blade_uid": "uid-1",
        "target": {"namespace": "arms-prom", "names": ["pod-a"]},
        "verification": {"level": "verified"},
        "error": "",
    }

    summary = build_inject_session_summary(
        data,
        mode=RESULT_SUMMARY_STATUS_ENVELOPE,
    )

    assert summary["status"] == "success"
    assert summary["data"] == {
        "task_id": "task-1",
        "result": "injected",
        "fault_type": "pod-cpu-fullload",
        "blade_uid": "uid-1",
        "targets": [{"name": "pod-a", "namespace": "arms-prom"}],
        "verification": {"level": "verified"},
        "error": "",
    }


def test_data_summary_preserves_stream_session_shape():
    data = {"task_id": "task-1", "task_state": "injected"}

    summary = build_inject_session_summary(data, mode=RESULT_SUMMARY_DATA_ENVELOPE)

    assert summary["status"] == "success"
    assert summary["data"] == data


def test_inject_summary_uses_failure_envelope_semantics():
    data = {"task_id": "task-1", "task_state": "failed", "error": "boom"}

    summary = build_inject_session_summary(data, mode=RESULT_SUMMARY_INJECT_ENVELOPE)

    assert summary["status"] == "fail"
    assert summary["data"] == data
    assert summary["message"] == "boom"


@pytest.mark.asyncio
async def test_finalize_inject_session_reads_graph_and_flushes_summary():
    store = _SessionStore()

    await finalize_inject_session(
        store,
        _Graph(_inject_values()),
        {"configurable": {"thread_id": "task-1"}},
        "task-1",
        result_summary_mode=RESULT_SUMMARY_STATUS_ENVELOPE,
    )

    assert store.finalized["task_id"] == "task-1"
    assert store.finalized["status"] == "completed"
    assert store.finalized["remaining_messages"] == []
    assert store.finalized["result_summary"]["data"]["result"] == "injected"
    assert store.finalized["result_summary"]["data"]["targets"] == [
        {"name": "pod-a", "namespace": "arms-prom"}
    ]


@pytest.mark.asyncio
async def test_finalize_open_conversation_routes_dialogue_without_finalizing():
    store = _SessionStore()
    tui_store = _TuiStore()
    message = object()

    await finalize_inject_session(
        store,
        _Graph({}),
        {"configurable": {"thread_id": "task-1"}},
        "task-1",
        is_open_conversation=True,
        precomputed_values={"tui_session_id": "session-1", "messages": [message]},
        tui_session_store=tui_store,
    )

    assert store.finalized is None
    assert store.appended is None
    assert tui_store.dialogue == {"session_id": "session-1", "messages": [message]}


def test_server_inject_routes_use_shared_session_finalizer():
    checked_files = [
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/inject_stream.py",
    ]
    violations = []
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if "finalize_inject_session" not in text:
            violations.append(f"{path.name}: missing finalize_inject_session")
        if "session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct session_store.finalize_session")

    assert violations == []
