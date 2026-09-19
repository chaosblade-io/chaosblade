"""Tests for recover streaming: runner.recover_stream, client.recover_stream.

The streaming twin must preserve the non-streaming contract (same
JSONEnvelope shape on the result event, same TaskStore/session
side effects) while forwarding intermediate graph events verbatim.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chaos_agent.agent.streaming import StreamEvent
from chaos_agent.cli.runner import AgentRunner


# ── runner.recover_stream ────────────────────────────────────────────


def _make_runner(agents, session_store=None):
    runner = AgentRunner()
    runner._initialized = True
    runner._agents = agents
    runner._session_store = session_store
    return runner


def _patch_streaming_common(resolution, recover_data):
    """Shared patch stack: initial-state resolution, ledger builder,
    TaskStore write, timer-state reset, status-tracker plumbing."""
    return (
        patch(
            "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
            new=AsyncMock(return_value=resolution),
        ),
        patch(
            "chaos_agent.agent.result.operation_result.build_recover_cli_data_from_state",
            return_value=recover_data,
        ),
        patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=MagicMock(update_task_state=AsyncMock())),
        ),
        patch("chaos_agent.tools.wait.reset_wait_state"),
        patch("chaos_agent.cli.runner.subscribe", return_value=MagicMock()),
        patch("chaos_agent.cli.runner.unsubscribe"),
        patch("chaos_agent.cli.runner.remove_tracker"),
        patch("chaos_agent.cli.runner._status_printer", new=AsyncMock()),
    )


@pytest.mark.asyncio
async def test_recover_stream_forwards_events_and_ok_envelope():
    intermediate = [
        StreamEvent(type="node_message", content="recovering..."),
        StreamEvent(type="tool_start", tool_name="blade_destroy"),
        StreamEvent(type="tool_end", tool_name="blade_destroy", content="ok"),
    ]

    async def fake_astream(initial_state, config, version="v2"):
        for i in range(3):
            yield {"raw": i}

    recover_graph = MagicMock()
    recover_graph.astream_events = fake_astream
    final_snapshot = MagicMock()
    final_snapshot.values = {"task_state": "recovered"}
    recover_graph.aget_state = AsyncMock(return_value=final_snapshot)

    pipeline = MagicMock()
    pipeline.aget_state = AsyncMock(return_value=MagicMock(values={}))

    runner = _make_runner({"pipeline": pipeline, "recover": recover_graph})

    resolution = MagicMock()
    resolution.initial_state = {"experiment_uid": "uid-1", "tui_session_id": "s1"}
    resolution.source_values = {"messages": []}

    patches = _patch_streaming_common(
        resolution,
        {"result": "recovered", "task_state": "recovered"},
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6], patches[7], patch(
        "chaos_agent.cli.runner.parse_stream_events",
        side_effect=lambda raw: [intermediate[raw["raw"]]],
    ):
        events = [e async for e in runner.recover_stream("inject-x")]

    # Intermediate events forwarded verbatim, tagged with the record id.
    assert [e.type for e in events[:3]] == [
        "node_message", "tool_start", "tool_end",
    ]
    assert all(e.task_id.startswith("recover-") for e in events[:3])

    # Terminal result event carries the ok envelope (recover() shape).
    result_evt = events[-1]
    assert result_evt.type == "result"
    envelope = json.loads(result_evt.content)
    assert envelope["code"] == 0
    assert envelope["data"]["result"] == "recovered"


@pytest.mark.asyncio
async def test_recover_stream_failed_recovery_yields_fail_envelope():
    async def fake_astream(initial_state, config, version="v2"):
        return
        yield  # pragma: no cover

    recover_graph = MagicMock()
    recover_graph.astream_events = fake_astream
    recover_graph.aget_state = AsyncMock(return_value=MagicMock(values={}))

    pipeline = MagicMock()
    pipeline.aget_state = AsyncMock(return_value=MagicMock(values={}))

    runner = _make_runner({"pipeline": pipeline, "recover": recover_graph})

    resolution = MagicMock()
    resolution.initial_state = {"experiment_uid": "uid-1", "tui_session_id": ""}
    resolution.source_values = {"messages": []}

    patches = _patch_streaming_common(
        resolution,
        {"result": "failed", "error": "boom"},
    )
    with patches[0], patches[1], patches[2], patches[3], patches[4], \
            patches[5], patches[6], patches[7]:
        events = [e async for e in runner.recover_stream("inject-x")]

    assert len(events) == 1
    envelope = json.loads(events[0].content)
    assert envelope["status"] == "fail"
    assert envelope["code"] == 4001  # ResponseCode.RECOVERY_FAILED
    assert envelope["message"] == "boom"
    assert envelope["data"]["error"] == "boom"


@pytest.mark.asyncio
async def test_recover_stream_unrecoverable_task_yields_task_not_found():
    pipeline = MagicMock()
    pipeline.aget_state = AsyncMock(return_value=MagicMock(values={}))

    runner = _make_runner({"pipeline": pipeline, "recover": MagicMock()})

    with patch(
        "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
        new=AsyncMock(return_value=None),
    ), patch("chaos_agent.tools.wait.reset_wait_state"), \
            patch("chaos_agent.cli.runner.subscribe", return_value=MagicMock()), \
            patch("chaos_agent.cli.runner.unsubscribe"), \
            patch("chaos_agent.cli.runner.remove_tracker"), \
            patch("chaos_agent.cli.runner._status_printer", new=AsyncMock()):
        events = [e async for e in runner.recover_stream("inject-x")]

    # error event for the console + result event for exit-code parity
    # with the non-streaming TASK_NOT_FOUND return.
    assert [e.type for e in events] == ["error", "result"]
    envelope = json.loads(events[1].content)
    assert envelope["code"] == 2001  # ResponseCode.TASK_NOT_FOUND
    assert "not recoverable" in envelope["message"].lower()


# ── client.recover_stream ────────────────────────────────────────────


class _StreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


class _FakeHttpClient:
    def __init__(self, lines):
        self._lines = lines
        self.stream_args = None
        self.stream_kwargs = None

    def stream(self, *args, **kwargs):
        self.stream_args = args
        self.stream_kwargs = kwargs
        return _StreamResponse(self._lines)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_client_recover_stream_forwards_intermediate_and_envelope():
    from chaos_agent.cli.client import AgentClient

    result_payload = json.dumps({
        "status": "success",
        "data": {
            "task_id": "task-recover",
            "task_state": "recovered",
            "experiment_uid": "uid-1",
            "target": {"namespace": "default", "names": ["pod-a"]},
        },
    })
    # Lines must be built with json.dumps so the embedded JSON string in
    # ``content`` is properly escaped — a hand-spliced f-string line would
    # produce invalid JSON and the client would silently drop the event.
    lines = [
        "data: " + json.dumps({
            "type": "node_message", "content": "layer1 verifying\n",
            "task_id": "task-recover",
        }),
        "data: " + json.dumps({
            "type": "tool_start", "tool_name": "kubectl", "task_id": "task-recover",
        }),
        "data: " + json.dumps({"type": "done", "task_id": "task-recover"}),  # sentinel: dropped
        "data: " + json.dumps({"type": "result", "content": result_payload}),
    ]

    client = AgentClient(base_url="http://localhost:8089")
    fake = _FakeHttpClient(lines)
    with patch("httpx.AsyncClient", return_value=fake):
        events = [e async for e in client.recover_stream(task_id="task-123")]

    assert fake.stream_args[:2] == (
        "POST", "http://localhost:8089/api/v1/recover-stream",
    )
    # Intermediate events forwarded with the original fields.
    assert events[0].type == "node_message"
    assert events[0].content == "layer1 verifying\n"
    assert events[1].type == "tool_start"
    assert events[1].tool_name == "kubectl"
    # done sentinel never surfaces.
    assert not any(e.type == "done" for e in events)
    # result event normalized through the recover() envelope mapping.
    result_evt = events[-1]
    assert result_evt.type == "result"
    envelope = json.loads(result_evt.content)
    assert envelope["code"] == 0
    assert envelope["data"]["task_id"] == "task-123"
    assert envelope["data"]["recover_task_id"] == "task-recover"
    assert envelope["data"]["targets"] == [{"name": "pod-a", "namespace": "default"}]


@pytest.mark.asyncio
async def test_client_recover_stream_error_only_stream_yields_error_event():
    from chaos_agent.cli.client import AgentClient

    lines = [
        'data: {"type":"error","content":"Task not found","task_id":"t"}',
        'data: {"type":"done","task_id":"t"}',
    ]

    client = AgentClient(base_url="http://localhost:8089")
    with patch("httpx.AsyncClient", return_value=_FakeHttpClient(lines)):
        events = [e async for e in client.recover_stream(task_id="task-x")]

    assert [e.type for e in events] == ["error"]
    assert events[0].content == "Task not found"


# ── CLI drain helper ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_recover_stream_returns_result_envelope(capsys):
    from chaos_agent.cli.commands.recover import _drain_recover_stream

    envelope = {"status": "success", "code": 0, "data": {"result": "recovered"}}
    events = [
        StreamEvent(type="node_message", content="step 1\n"),
        StreamEvent(type="tool_start", tool_name="blade_destroy"),
        StreamEvent(type="token", content="recovered ok"),
        StreamEvent(type="result", content=json.dumps(envelope)),
    ]

    async def _agen():
        for e in events:
            yield e

    result = await _drain_recover_stream(_agen())
    assert result == envelope

    captured = capsys.readouterr()
    # Token output lands on stdout (pipeable final answer).
    assert "recovered ok" in captured.out
    # Progress lines land on stderr.
    assert "blade_destroy" in captured.err


@pytest.mark.asyncio
async def test_drain_recover_stream_without_result_returns_fallback(capsys):
    from chaos_agent.cli.commands.recover import _drain_recover_stream

    async def _agen():
        yield StreamEvent(type="error", content="boom")

    result = await _drain_recover_stream(_agen())
    assert result == {"code": 1, "message": "No result received", "data": None}
    captured = capsys.readouterr()
    assert "boom" in captured.err
