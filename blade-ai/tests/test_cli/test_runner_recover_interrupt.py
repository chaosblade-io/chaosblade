"""Round-64 F1 behaviour: an interrupted CLI recover run ships ONE word.

The wiring invariants live in test_runner_signal_guard.py; these prove the
wiring does what it promises on the real objects — a Ctrl-C (or the
CancelError a SIGTERM guard raises) during ``AgentRunner.recover()`` must
leave the row word AND the session word on the SAME classified value. The
pre-fix pair was "cancelled" on the row and "completed" on the session:
the boolean ``recover_failed`` could not spell an interrupt, and the
finalizer's default spelled it as a success.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from chaos_agent.cli.runner import AgentRunner
from chaos_agent.memory.session_store import SessionStore

_RECOVER_MIDFLIGHT = {"operation": "recover"}
_RECOVER_DONE = {
    "operation": "recover",
    "result": {"recovered": True, "recovery_level": "recovered"},
    "recover_verification": {
        "level": "recovered",
        "layer1": {"status": "passed"},
        "layer2": {"status": "passed"},
    },
}


class _StubPipeline:
    def __init__(self, values: dict):
        self.values = values

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


class _HangingGraph:
    """A recover graph that hangs inside ``ainvoke``.

    ``aget_state`` reports ``values`` — the real engine's semantics: a
    cancelled run keeps the last checkpoint it wrote, which is what the
    finalizer reads when it closes the session.
    """

    def __init__(self, values: dict):
        self.values = values
        self.started = asyncio.Event()

    async def ainvoke(self, state, config):
        self.started.set()
        await asyncio.sleep(3600)
        return self.values  # pragma: no cover — the cancel always wins

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


class _FinishingGraph:
    """A recover graph that returns its verdict immediately."""

    def __init__(self, values: dict):
        self.values = values

    async def ainvoke(self, state, config):
        return self.values

    async def aget_state(self, config):
        return SimpleNamespace(values=self.values)


class _StubTaskStore:
    async def update_task_state(self, *args, **kwargs):
        return None


def _build_runner(
    tmp_path: Path, recover_graph
) -> tuple[AgentRunner, SessionStore]:
    runner = AgentRunner()
    runner._initialized = True
    runner._agents = {"pipeline": _StubPipeline({}), "recover": recover_graph}
    session_store = SessionStore(tmp_path / "sessions")
    runner._session_store = session_store
    runner._checkpointer_conn = None
    return runner, session_store


def _resolution() -> SimpleNamespace:
    return SimpleNamespace(
        initial_state={"experiment_uid": "uid-1", "tui_session_id": "tui-1"},
        source_values={"experiment_uid": "uid-1"},
    )


@pytest.mark.asyncio
async def test_interrupted_recover_ships_one_word_to_both_surfaces(tmp_path):
    """Ctrl-C mid-run: the row write and the session record carry the
    same classified word — "cancelled", never "completed"."""

    recover_graph = _HangingGraph(_RECOVER_MIDFLIGHT)
    runner, session_store = _build_runner(tmp_path, recover_graph)
    row_writes: list[tuple[str, str]] = []

    async def _record_row_write(task_id: str, word: str) -> None:
        row_writes.append((task_id, word))

    with (
        patch(
            "chaos_agent.cli.runner.new_recover_task_id",
            return_value="recover-test-1",
        ),
        patch(
            "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
            new=AsyncMock(return_value=_resolution()),
        ),
        patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=_StubTaskStore()),
        ),
        patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new=_record_row_write,
        ),
    ):
        task = asyncio.create_task(runner.recover("inject-test-1"))
        await asyncio.wait_for(recover_graph.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert row_writes == [("recover-test-1", "cancelled")], (
        "the interrupt arm must leave the row its classified terminal word"
    )

    session = session_store.read_session("recover-test-1")
    assert session is not None, "the recover session must still be closed"
    assert session["status"] == "cancelled", (
        "the session word must be the SAME classified word as the row's — "
        "the pre-fix expression recorded this run as 'completed'"
    )
    # The envelope keeps describing where the run stopped instead of
    # claiming a recovery: the two faces agree, one is the position
    # ("recovering"), the other the verdict ("cancelled").
    assert session["result_summary"]["data"]["result"] == "recovering"


@pytest.mark.asyncio
async def test_interrupted_recover_verdict_on_record_keeps_its_word(tmp_path):
    """The verdict-yield contract still holds on the interrupt exit: a
    recovery that reached its verdict before the crash keeps it — the
    interrupt classifies mid-flight runs only."""

    recover_graph = _HangingGraph(_RECOVER_DONE)
    runner, session_store = _build_runner(tmp_path, recover_graph)

    with (
        patch(
            "chaos_agent.cli.runner.new_recover_task_id",
            return_value="recover-test-2",
        ),
        patch(
            "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
            new=AsyncMock(return_value=_resolution()),
        ),
        patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=_StubTaskStore()),
        ),
        patch(
            "chaos_agent.server.routes.stream_abort.write_aborted_task_row",
            new=AsyncMock(),
        ),
    ):
        task = asyncio.create_task(runner.recover("inject-test-2"))
        await asyncio.wait_for(recover_graph.started.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    session = session_store.read_session("recover-test-2")
    assert session is not None
    assert session["status"] == "completed"


@pytest.mark.asyncio
async def test_completed_recover_keeps_completed(tmp_path):
    """The normal path is untouched: a recover run whose graph finished
    with a verdict still records "completed" — the interrupt arm and the
    new fail-closed floor must not swallow the success path."""

    recover_graph = _FinishingGraph(_RECOVER_DONE)
    runner, session_store = _build_runner(tmp_path, recover_graph)

    with (
        patch(
            "chaos_agent.cli.runner.new_recover_task_id",
            return_value="recover-test-3",
        ),
        patch(
            "chaos_agent.agent.result.task_snapshot.resolve_recover_initial_state",
            new=AsyncMock(return_value=_resolution()),
        ),
        patch(
            "chaos_agent.persistence.task_store.get_task_store",
            new=AsyncMock(return_value=_StubTaskStore()),
        ),
    ):
        envelope = await runner.recover("inject-test-3")

    assert envelope["status"] == "success", envelope
    session = session_store.read_session("recover-test-3")
    assert session is not None
    assert session["status"] == "completed"
    assert session["result_summary"]["data"]["result"] == "recovered"
