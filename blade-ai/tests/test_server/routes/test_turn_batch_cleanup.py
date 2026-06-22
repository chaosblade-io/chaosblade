"""Regression tests for clearing dispatched inject intent state."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from chaos_agent.agent.operation_summary import (
    build_batch_summary_text,
    build_recover_summary_text,
)
from chaos_agent.server.routes import turn_event_stream as stream_mod


class RecordingIntentGraph:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    async def aupdate_state(self, config, values, as_node=None):
        self.updates.append(
            {"config": config, "values": values, "as_node": as_node}
        )


class EmptyPipelineGraph:
    def astream_events(self, *_args, **_kwargs):
        async def _events():
            if False:
                yield {}

        return _events()

    async def aget_state(self, _config):
        return SimpleNamespace(values={"batch_results": []})


class RecordingStore:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, str]] = []

    def add_task(self, sid: str, task_id: str) -> None:
        self.tasks.append((sid, task_id))


def _ctx() -> SimpleNamespace:
    return SimpleNamespace(
        sid="sid-1",
        turn_id="turn-1",
        thread_id="thread-1",
        intent_graph=RecordingIntentGraph(),
        pipeline_graph=EmptyPipelineGraph(),
        graph_config={
            "configurable": {"thread_id": "thread-1"},
            "recursion_limit": 10,
        },
        tracker_queue=asyncio.Queue(),
        req=SimpleNamespace(),
        store=RecordingStore(),
        dry_run=False,
    )


def _batch_iv() -> dict:
    return {
        "tui_session_id": "sid-1",
        "handoff_summary": "[Intent Clarification Summary]",
        "batch_submit_args": {
            "faults": [
                {
                    "scope": "pod",
                    "target": "pod",
                    "action": "terminate",
                    "namespace": "arms-prom",
                    "names": ["pod-a"],
                },
                {
                    "scope": "pod",
                    "target": "pod",
                    "action": "terminate",
                    "namespace": "arms-prom",
                    "names": ["pod-b"],
                },
            ],
            "execution_order": "serial",
            "interval_seconds": 0,
        },
        "fault_spec": {
            "scope": "pod",
            "target": "pod",
            "action": "terminate",
            "namespace": "arms-prom",
            "names": ["pod-a"],
        },
    }


def _single_iv() -> dict:
    return {
        "task_id": "task-single",
        "tui_session_id": "sid-1",
        "handoff_summary": "[Intent Clarification Summary]",
        "fault_spec": {
            "scope": "pod",
            "target": "cpu",
            "action": "fullload",
            "namespace": "default",
            "names": ["pod-a"],
        },
    }


def _assert_batch_cleared(update: dict) -> None:
    values = update["values"]
    assert values["confirmed_intent"] is None
    assert values["batch_submit_args"] is None
    assert values["fault_spec"] is None
    assert values["handoff_summary"] is None
    assert values["intent_reasoning"] is None
    assert values["intent_confidence"] == 0.0
    assert values["clarification_round"] == 0
    assert update["as_node"] == "save_dialogue"


@pytest.mark.asyncio
async def test_clear_dispatched_inject_intent_state_removes_one_shot_fields():
    ctx = _ctx()

    await stream_mod._clear_dispatched_inject_intent_state(ctx, reason="test")

    assert len(ctx.intent_graph.updates) == 1
    _assert_batch_cleared(ctx.intent_graph.updates[0])


@pytest.mark.asyncio
async def test_batch_pipeline_clears_intent_state_before_cancel(monkeypatch):
    """Esc aborts the SSE stream, so cleanup must happen before streaming."""

    async def fake_drain_merged(*_args, **_kwargs):
        raise asyncio.CancelledError()
        if False:
            yield ""

    monkeypatch.setattr(stream_mod, "_merged_stream", lambda *_a, **_k: object())
    monkeypatch.setattr(stream_mod, "_drain_merged", fake_drain_merged)

    ctx = _ctx()

    with pytest.raises(asyncio.CancelledError):
        async for _ in stream_mod._run_batch_pipeline(
            ctx,
            _batch_iv(),
            batcher=None,
            sidewrite=lambda _evt: None,
            converters={},
        ):
            pass

    assert ctx.intent_graph.updates
    _assert_batch_cleared(ctx.intent_graph.updates[0])


def test_batch_summary_contains_targets_and_freshness_note():
    text = build_batch_summary_text(
        [
            {
                "task_id": "task-a",
                "task_state": "injected",
                "fault_type": "pod-pod-delete",
                "target": {"namespace": "arms-prom", "names": ["pod-a"]},
            },
            {
                "task_id": "task-b",
                "task_state": "failed",
                "fault_type": "pod-pod-delete",
                "target": {"namespace": "arms-prom", "names": ["pod-b"]},
                "failure_reason": "pod not found",
            },
        ],
        "/tmp/batch.md",
    )

    assert text.startswith("[Batch Summary] 2 faults")
    assert "操作: batch_inject" in text
    assert "target=arms-prom/pod-a" in text
    assert "失败原因: pod not found" in text
    assert "批量分析报告: /tmp/batch.md" in text
    assert "本概要及更早历史中的资源名仅作历史上下文" in text
    assert "若要复用这些目标，必须重新 kubectl 验证当前存在性" in text


def test_recover_summary_contains_parent_task_and_verification():
    text = build_recover_summary_text(
        {
            "data": {
                "task_id": "task-recover",
                "task_state": "recovered",
                "fault_type": "pod-pod-delete",
                "blade_uid": "uid-1",
                "target": {"namespace": "arms-prom", "names": ["pod-a"]},
                "verification": {
                    "level": "recovered",
                    "layer1": {"status": "passed"},
                    "layer2": {"status": "passed"},
                },
            },
        },
        "task-inject",
        {},
    )

    assert text.startswith("[Recover Summary] task_id=task-recover")
    assert "parent_task_id: task-inject" in text
    assert "类型: pod-pod-delete | 目标: arms-prom/pod-a" in text
    assert "结果: recovered | blade_uid: uid-1" in text
    assert "恢复验证: recovered (L1=passed, L2=passed)" in text


def test_intent_trim_preserves_batch_and_recover_summaries():
    from chaos_agent.agent.nodes.intent_confirm import _build_trim_remove_list

    batch = SystemMessage(content="[Batch Summary] 1 faults", id="batch")
    recover = SystemMessage(content="[Recover Summary] task_id=task-r", id="recover")
    old = [HumanMessage(content=f"old-{i}", id=f"old-{i}") for i in range(10)]
    messages = [batch, recover, *old]

    remove_ids = {rm.id for rm in _build_trim_remove_list(messages)}

    assert "batch" not in remove_ids
    assert "recover" not in remove_ids
    assert "old-0" in remove_ids


@pytest.mark.asyncio
async def test_single_inject_pipeline_clears_intent_state_before_cancel(monkeypatch):
    """A single inject Esc abort must not leave fault_spec as pending intent."""

    async def fake_drain_merged(*_args, **_kwargs):
        raise asyncio.CancelledError()
        if False:
            yield ""

    from chaos_agent.agent.nodes import intent_clarification

    monkeypatch.setattr(
        intent_clarification,
        "bootstrap_task_session",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(stream_mod, "_merged_stream", lambda *_a, **_k: object())
    monkeypatch.setattr(stream_mod, "_drain_merged", fake_drain_merged)

    ctx = _ctx()

    with pytest.raises(asyncio.CancelledError):
        async for _ in stream_mod._run_inject_pipeline(
            ctx,
            _single_iv(),
            batcher=None,
            sidewrite=lambda _evt: None,
            converters={},
        ):
            pass

    assert ctx.intent_graph.updates
    _assert_batch_cleared(ctx.intent_graph.updates[0])
