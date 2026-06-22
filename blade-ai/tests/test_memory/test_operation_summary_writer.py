from pathlib import Path

import pytest

from chaos_agent.memory.operation_summary_writer import write_operation_summary


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RecordingIntentGraph:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.updates: list[dict] = []

    async def aupdate_state(self, config, values, as_node=None):
        if self.fail:
            raise RuntimeError("checkpoint write failed")
        self.updates.append(
            {"config": config, "values": values, "as_node": as_node}
        )


class RecordingTuiStore:
    def __init__(self, *, fail_append: bool = False) -> None:
        self.fail_append = fail_append
        self.dialogue: list[tuple[str, list]] = []
        self.tasks: list[tuple[str, str]] = []

    def append_dialogue(self, sid: str, messages: list) -> None:
        if self.fail_append:
            raise RuntimeError("session append failed")
        self.dialogue.append((sid, messages))

    def add_task(self, sid: str, task_id: str) -> None:
        self.tasks.append((sid, task_id))


class RecordingSessionIndex:
    def __init__(self) -> None:
        self.tasks: list[tuple[str, str]] = []

    def add_task(self, sid: str, task_id: str) -> None:
        self.tasks.append((sid, task_id))


@pytest.mark.asyncio
async def test_write_operation_summary_updates_graph_and_tui_session():
    graph = RecordingIntentGraph()
    tui = RecordingTuiStore()
    session_index = RecordingSessionIndex()

    result = await write_operation_summary(
        "[Task Summary] task_id=task-a",
        intent_graph=graph,
        thread_id="thread-1",
        state_update={"pipeline_task_id": "task-a"},
        tui_session_id="sid-1",
        tui_session_store=tui,
        session_index_store=session_index,
        task_id="task-a",
        recursion_limit=7,
    )

    assert result.graph_written is True
    assert result.tui_dialogue_written is True
    assert result.tui_task_indexed is True
    assert result.session_task_indexed is True
    assert graph.updates[0]["config"]["configurable"]["thread_id"] == "thread-1"
    assert graph.updates[0]["config"]["recursion_limit"] == 7
    assert graph.updates[0]["as_node"] == "save_dialogue"
    assert graph.updates[0]["values"]["pipeline_task_id"] == "task-a"
    assert graph.updates[0]["values"]["messages"][0].content.startswith("[Task Summary]")
    assert tui.dialogue[0][0] == "sid-1"
    assert tui.tasks == [("sid-1", "task-a")]
    assert session_index.tasks == [("sid-1", "task-a")]


@pytest.mark.asyncio
async def test_write_operation_summary_graph_failure_still_appends_tui_then_raises():
    graph = RecordingIntentGraph(fail=True)
    tui = RecordingTuiStore()

    with pytest.raises(RuntimeError, match="checkpoint write failed"):
        await write_operation_summary(
            "[Recover Summary] task_id=task-r",
            intent_graph=graph,
            thread_id="thread-1",
            tui_session_id="sid-1",
            tui_session_store=tui,
            recursion_limit=7,
        )

    assert tui.dialogue[0][0] == "sid-1"
    assert tui.dialogue[0][1][0].content.startswith("[Recover Summary]")


@pytest.mark.asyncio
async def test_write_operation_summary_can_suppress_graph_error_for_stream_routes():
    graph = RecordingIntentGraph(fail=True)
    tui = RecordingTuiStore()

    result = await write_operation_summary(
        "[Recover Summary] task_id=task-r",
        intent_graph=graph,
        thread_id="thread-1",
        tui_session_id="sid-1",
        tui_session_store=tui,
        recursion_limit=7,
        raise_graph_error=False,
    )

    assert result.graph_written is False
    assert result.tui_dialogue_written is True


@pytest.mark.asyncio
async def test_write_operation_summary_noops_for_empty_text():
    graph = RecordingIntentGraph()
    tui = RecordingTuiStore()

    result = await write_operation_summary(
        "",
        intent_graph=graph,
        thread_id="thread-1",
        tui_session_id="sid-1",
        tui_session_store=tui,
    )

    assert result.graph_written is False
    assert result.tui_dialogue_written is False
    assert graph.updates == []
    assert tui.dialogue == []


@pytest.mark.asyncio
async def test_write_operation_summary_rejects_reserved_state_update_keys():
    graph = RecordingIntentGraph()
    tui = RecordingTuiStore()

    with pytest.raises(ValueError, match="messages"):
        await write_operation_summary(
            "[Task Summary] task_id=task-a",
            intent_graph=graph,
            thread_id="thread-1",
            state_update={"messages": []},
            tui_session_id="sid-1",
            tui_session_store=tui,
        )

    assert graph.updates == []
    assert tui.dialogue == []


def test_operation_summary_persistence_paths_use_shared_writer():
    """Server/CLI orchestration must not reimplement summary persistence."""

    checked_files = [
        "src/chaos_agent/server/routes/turn_event_stream.py",
        "src/chaos_agent/server/routes/recover_stream.py",
        "src/chaos_agent/cli/runner.py",
    ]
    forbidden_snippets = [
        "SystemMessage(content=summary",
        "SystemMessage(content=_summary",
        "append_dialogue(",
        "_write_operation_summary",
    ]

    violations = []
    for rel in checked_files:
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8")
        if "write_operation_summary" not in text:
            violations.append(f"{rel}: missing write_operation_summary")
        for snippet in forbidden_snippets:
            if snippet in text:
                violations.append(f"{rel}: {snippet}")

    assert violations == []
