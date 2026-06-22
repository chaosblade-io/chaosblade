"""Tests for shared recover endpoint setup logic."""

from __future__ import annotations

import pytest

from chaos_agent.server.routes import recover_common


class _MissingPipeline:
    async def aget_state(self, config):
        return None


class _CheckpointPipeline:
    async def aget_state(self, config):
        class _State:
            values = {
                "task_id": "task-inject",
                "tui_session_id": "sid-1",
                "blade_uid": "blade-123",
                "skill_name": "pod-cpu-fullload",
                "fault_spec": {
                    "namespace": "default",
                    "scope": "pod",
                    "names": ["demo"],
                    "labels": {},
                    "params": {"cpu-percent": "80"},
                },
                "messages": [],
            }

        return _State()


@pytest.mark.asyncio
async def test_recover_initial_state_uses_checkpoint_when_available():
    initial, state_values = await recover_common.build_recover_initial_state(
        {"pipeline": _CheckpointPipeline()},
        "task-inject",
        "task-recover",
        "req-1",
    )

    assert initial["task_id"] == "task-recover"
    assert initial["parent_task_id"] == "task-inject"
    assert initial["blade_uid"] == "blade-123"
    assert initial["recover_phase"] == "layer1_recovery"
    assert initial["layer1_iteration_count"] == 0
    assert initial["layer2_context_added"] is False
    assert state_values["task_id"] == "task-inject"


@pytest.mark.asyncio
async def test_recover_initial_state_uses_resolver_without_checkpoint(monkeypatch):
    from chaos_agent.agent.task_snapshot import RecoverInitialResolution
    from chaos_agent.agent import task_snapshot

    async def fake_resolve(task_id, *, record_task_id, agents, checkpoint_values, **kwargs):
        assert task_id == "task-inject"
        assert record_task_id == "task-recover"
        assert checkpoint_values == {}
        assert agents["pipeline"].__class__ is _MissingPipeline
        initial = {
            "task_id": record_task_id,
            "parent_task_id": task_id,
            "operation": "recover",
            "blade_uid": "blade-from-store",
            "skill_name": "pod-network-loss",
            "fault_spec": {
                "namespace": "default",
                "scope": "pod",
                "names": ["demo"],
                "labels": {},
                "params": {},
            },
            "messages": [],
            "recover_phase": "layer1_recovery",
            "layer1_iteration_count": 0,
            "layer2_context_added": False,
        }
        return RecoverInitialResolution(initial_state=initial, source_values=initial, source="snapshot")

    monkeypatch.setattr(
        task_snapshot,
        "resolve_recover_initial_state",
        fake_resolve,
    )

    initial, state_values = await recover_common.build_recover_initial_state(
        {"pipeline": _MissingPipeline()},
        "task-inject",
        "task-recover",
        "req-1",
    )

    assert initial["task_id"] == "task-recover"
    assert initial["parent_task_id"] == "task-inject"
    assert initial["blade_uid"] == "blade-from-store"
    assert initial["recover_phase"] == "layer1_recovery"
    assert initial["layer1_iteration_count"] == 0
    assert initial["layer2_context_added"] is False
    assert state_values is initial


@pytest.mark.asyncio
async def test_recover_initial_state_raises_when_resolver_misses(monkeypatch):
    from chaos_agent.agent import task_snapshot

    async def fake_resolve(*args, **kwargs):
        return None

    monkeypatch.setattr(
        task_snapshot,
        "resolve_recover_initial_state",
        fake_resolve,
    )

    with pytest.raises(recover_common.RecoverSetupError):
        await recover_common.build_recover_initial_state(
            {"pipeline": _MissingPipeline()},
            "task-missing",
            "task-recover",
            "req-1",
        )


@pytest.mark.asyncio
async def test_recover_store_rebuild_fills_missing_uid_from_task_jsonl(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.memory.session_store import SessionStore, set_global_session_store
    from chaos_agent.persistence.task_store import get_task_store
    from chaos_agent.server.routes.turn_result import build_recover_initial_from_store

    session_store = SessionStore(tmp_path / "tasks")
    set_global_session_store(session_store)
    try:
        task_store = await get_task_store()
        await task_store.upsert(
            "task-inject",
            operation="inject",
            skill_name="pod-cpu-fullload",
            target={
                "namespace": "default",
                "names": ["demo"],
                "labels": {},
                "resource_type": "pod",
            },
            params={"cpu-percent": "80"},
        )

        session_store.create_session("task-inject", operation="inject")
        session_store.append_messages(
            "task-inject",
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
                    content='{"code":200,"success":true,"result":"uid-from-jsonl"}',
                    name="blade_create",
                    tool_call_id="tc-create",
                ),
            ],
        )

        initial = await build_recover_initial_from_store(
            "task-inject",
            "task-recover",
            "sid-1",
            {},
        )
    finally:
        set_global_session_store(None)  # type: ignore[arg-type]

    assert initial is not None
    assert initial["task_id"] == "task-recover"
    assert initial["parent_task_id"] == "task-inject"
    assert initial["blade_uid"] == "uid-from-jsonl"
    assert initial["skill_name"] == "pod-cpu-fullload"
    assert initial["fault_spec"]["scope"] == "pod"
    assert initial["fault_spec"]["blade_target"] == "cpu"
    assert initial["fault_spec"]["blade_action"] == "fullload"
    assert initial["fault_spec"]["params"] == {"cpu-percent": "80"}
    assert "EXPIRED DATA" in initial["inject_context"]
    assert "blade_create" in initial["inject_context"]


@pytest.mark.asyncio
async def test_recover_store_rebuild_restores_tui_session_id_from_task_file(tmp_path):
    from chaos_agent.memory.session_store import SessionStore, set_global_session_store
    from chaos_agent.persistence.task_store import get_task_store
    from chaos_agent.server.routes.turn_result import build_recover_initial_from_store

    session_store = SessionStore(tmp_path / "tasks")
    set_global_session_store(session_store)
    try:
        task_store = await get_task_store()
        await task_store.upsert(
            "task-inject-tui-session",
            operation="inject",
            skill_name="pod-cpu-fullload",
            blade_uid="uid-from-task-store",
            target={
                "namespace": "default",
                "names": ["demo"],
                "labels": {},
                "resource_type": "pod",
            },
            params={"cpu-percent": "80"},
        )
        session_store.create_session(
            "task-inject-tui-session",
            operation="inject",
            tui_session_id="sid-from-task-file",
        )

        initial = await build_recover_initial_from_store(
            "task-inject-tui-session",
            "task-recover",
            "",
            {},
        )
    finally:
        set_global_session_store(None)  # type: ignore[arg-type]

    assert initial is not None
    assert initial["tui_session_id"] == "sid-from-task-file"


@pytest.mark.asyncio
async def test_recover_store_rebuild_prefers_task_jsonl_when_live(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.memory.session_store import SessionStore, set_global_session_store
    from chaos_agent.persistence.task_store import get_task_store
    from chaos_agent.server.routes.turn_result import build_recover_initial_from_store

    session_store = SessionStore(tmp_path / "tasks")
    set_global_session_store(session_store)
    try:
        task_store = await get_task_store()
        await task_store.upsert(
            "task-inject-live",
            operation="inject",
            skill_name="pod-cpu-fullload",
            blade_uid="uid-from-task-store",
            inject_context="stale task-store context",
            target={
                "namespace": "default",
                "names": ["demo"],
                "labels": {},
                "resource_type": "pod",
            },
            params={"cpu-percent": "80"},
        )

        session_store.create_session("task-inject-live", operation="inject")
        session_store.append_messages(
            "task-inject-live",
            [
                AIMessage(
                    content="latest inject observation",
                    tool_calls=[
                        {
                            "name": "blade_create",
                            "args": {},
                            "id": "tc-create-live",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"uid-from-live-jsonl"}',
                    name="blade_create",
                    tool_call_id="tc-create-live",
                ),
            ],
        )

        initial = await build_recover_initial_from_store(
            "task-inject-live",
            "task-recover",
            "sid-1",
            {},
        )
    finally:
        set_global_session_store(None)  # type: ignore[arg-type]

    assert initial is not None
    assert initial["blade_uid"] == "uid-from-live-jsonl"
    assert "latest inject observation" in initial["inject_context"]
    assert initial["inject_context"] != "stale task-store context"


@pytest.mark.asyncio
async def test_recover_initial_state_prefers_live_jsonl_over_checkpoint(tmp_path):
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.memory.session_store import SessionStore, set_global_session_store
    from chaos_agent.persistence.task_store import get_task_store

    class _StaleCheckpointPipeline:
        async def aget_state(self, config):
            class _State:
                values = {
                    "task_id": "task-inject-checkpoint-live",
                    "tui_session_id": "sid-checkpoint",
                    "blade_uid": "uid-from-checkpoint",
                    "skill_name": "pod-cpu-fullload",
                    "fault_spec": {
                        "namespace": "default",
                        "scope": "pod",
                        "names": ["demo"],
                        "labels": {},
                        "blade_target": "cpu",
                        "blade_action": "fullload",
                        "params": {"cpu-percent": "80"},
                    },
                    "inject_context": "stale checkpoint context",
                    "messages": ["baseline-message"],
                }

            return _State()

    session_store = SessionStore(tmp_path / "tasks")
    set_global_session_store(session_store)
    try:
        task_store = await get_task_store()
        await task_store.upsert(
            "task-inject-checkpoint-live",
            operation="inject",
            skill_name="pod-cpu-fullload",
            blade_uid="uid-from-task-store",
            inject_context="stale task-store context",
            target={
                "namespace": "default",
                "names": ["demo"],
                "labels": {},
                "resource_type": "pod",
            },
            params={"cpu-percent": "80"},
        )

        session_store.create_session("task-inject-checkpoint-live", operation="inject")
        session_store.append_messages(
            "task-inject-checkpoint-live",
            [
                AIMessage(
                    content="fresh jsonl observation",
                    tool_calls=[
                        {
                            "name": "blade_create",
                            "args": {},
                            "id": "tc-create-live-checkpoint",
                        }
                    ],
                ),
                ToolMessage(
                    content='{"code":200,"success":true,"result":"uid-from-jsonl-even-with-checkpoint"}',
                    name="blade_create",
                    tool_call_id="tc-create-live-checkpoint",
                ),
            ],
        )

        initial, state_values = await recover_common.build_recover_initial_state(
            {"pipeline": _StaleCheckpointPipeline()},
            "task-inject-checkpoint-live",
            "task-recover",
            "req-1",
        )
    finally:
        set_global_session_store(None)  # type: ignore[arg-type]

    assert initial["blade_uid"] == "uid-from-jsonl-even-with-checkpoint"
    assert state_values["blade_uid"] == "uid-from-jsonl-even-with-checkpoint"
    assert "fresh jsonl observation" in initial["inject_context"]
    assert initial["inject_context"] != "stale checkpoint context"
    assert state_values["messages"] == ["baseline-message"]
