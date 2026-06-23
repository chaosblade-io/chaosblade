from pathlib import Path
from types import SimpleNamespace

import pytest

from chaos_agent.agent.fault_spec import FaultSpec
from chaos_agent.memory.session_finalizer import (
    RESULT_SUMMARY_DATA_ENVELOPE,
    RESULT_SUMMARY_INJECT_ENVELOPE,
    RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    RESULT_SUMMARY_RECOVER_PAYLOAD,
    RESULT_SUMMARY_STATUS_ENVELOPE,
    build_inject_session_summary,
    build_recover_session_summary,
    finalize_inject_session,
    finalize_recover_session,
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


def _recover_values() -> dict:
    return {
        "operation": "recover",
        "result": {"recovered": True, "recovery_level": "recovered"},
        "recover_verification": {
            "level": "recovered",
            "layer1": {"status": "passed"},
            "layer2": {"status": "passed"},
        },
        "verification": {
            "level": "stale-inject",
            "layer1": {"status": "failed"},
            "layer2": {"status": "failed"},
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
        "fault_spec": _inject_values()["fault_spec"],
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
        "fault_spec": _inject_values()["fault_spec"],
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


def test_recover_payload_summary_preserves_server_payload_shape():
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "recovered"},
    }

    summary = build_recover_session_summary(
        {},
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        result_payload=payload,
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert summary is payload


def test_recover_payload_summary_without_payload_stays_empty():
    summary = build_recover_session_summary(
        _recover_values(),
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert summary == ""


def test_recover_cli_summary_uses_recover_verification_not_inject_verification():
    summary = build_recover_session_summary(
        _recover_values(),
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert summary["status"] == "success"
    assert summary["data"]["result"] == "recovered"
    assert summary["data"]["verification"]["level"] == "recovered"
    assert summary["data"]["verification"]["layer1"] == {"status": "passed"}


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
    assert store.finalized["result_summary"]["data"]["fault_spec"] == (
        _inject_values()["fault_spec"]
    )
    assert store.finalized["result_summary"]["data"]["targets"] == [
        {"name": "pod-a", "namespace": "arms-prom"}
    ]


@pytest.mark.asyncio
async def test_finalize_recover_session_uses_payload_status_for_server_mode():
    store = _SessionStore()
    payload = {
        "status": "success",
        "data": {"task_id": "task-recover", "task_state": "failed"},
    }

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["task_id"] == "task-recover"
    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] is payload


@pytest.mark.asyncio
async def test_finalize_recover_session_honors_failed_payload_envelope():
    store = _SessionStore()
    payload = {
        "status": "fail",
        "data": {"task_id": "task-recover"},
    }

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_payload=payload,
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] is payload


@pytest.mark.asyncio
async def test_finalize_recover_failed_fallback_does_not_write_success_summary():
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        default_status="failed",
    )

    assert store.finalized["status"] == "failed"
    assert store.finalized["result_summary"] == ""


@pytest.mark.asyncio
async def test_finalize_recover_session_uses_precomputed_messages():
    store = _SessionStore()
    message = object()

    await finalize_recover_session(
        store,
        recover_graph=None,
        recover_config=None,
        recover_task_id="task-recover",
        inject_task_id="task-inject",
        inject_state_values=_inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_PAYLOAD,
        precomputed_values={**_recover_values(), "messages": [message]},
    )

    assert store.finalized["remaining_messages"] == [message]


@pytest.mark.asyncio
async def test_finalize_recover_session_preserves_cli_completed_status():
    store = _SessionStore()

    await finalize_recover_session(
        store,
        _Graph(_recover_values()),
        {"configurable": {"thread_id": "task-recover"}},
        "task-recover",
        "task-inject",
        _inject_values(),
        result_summary_mode=RESULT_SUMMARY_RECOVER_CLI_ENVELOPE,
    )

    assert store.finalized["status"] == "completed"
    assert store.finalized["result_summary"]["data"]["result"] == "recovered"


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


def test_recover_paths_use_shared_session_finalizer():
    checked_files = [
        PROJECT_ROOT / "src/chaos_agent/cli/runner.py",
        PROJECT_ROOT / "src/chaos_agent/l4/agent.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/recover_stream.py",
        PROJECT_ROOT / "src/chaos_agent/server/routes/turn_event_stream.py",
    ]
    violations = []
    for path in checked_files:
        text = path.read_text(encoding="utf-8")
        if "finalize_recover_session" not in text:
            violations.append(f"{path.name}: missing finalize_recover_session")
        if "def _finalize_recover_session" in text:
            violations.append(f"{path.name}: private _finalize_recover_session")
        if path.name != "runner.py" and "session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct session_store.finalize_session")
        if path.name == "runner.py" and "self._session_store.finalize_session(" in text:
            violations.append(f"{path.name}: direct self._session_store.finalize_session")

    assert violations == []


def test_task_session_direct_finalize_calls_are_intentional():
    """Direct SessionStore finalization should remain limited to terminal/abort paths."""

    allowed = {
        "src/chaos_agent/agent/nodes/memory_nodes.py",
        "src/chaos_agent/memory/session_finalizer.py",
        "src/chaos_agent/server/app.py",
        "src/chaos_agent/server/routes/turn_event_stream.py",
    }
    violations = []
    for path in (PROJECT_ROOT / "src/chaos_agent").rglob("*.py"):
        rel = path.relative_to(PROJECT_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if ".finalize_session(" in text and rel not in allowed:
            violations.append(rel)

    assert violations == []


def test_memory_node_result_summary_uses_session_finalizer_projection():
    text = (
        PROJECT_ROOT / "src/chaos_agent/agent/nodes/memory_nodes.py"
    ).read_text(encoding="utf-8")

    assert "build_inject_session_summary" in text
    assert "build_inject_envelope" not in text
