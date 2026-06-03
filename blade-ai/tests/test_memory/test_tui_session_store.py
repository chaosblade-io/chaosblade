"""Tests for TuiSessionStore persistence layer."""

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from chaos_agent.memory.tui_session_store import TuiSessionStore


@pytest.fixture
def session_dir(tmp_path):
    return tmp_path / "sessions"


@pytest.fixture
def store(session_dir):
    return TuiSessionStore(session_dir)


class TestCreate:
    def test_creates_file_named_by_tui_session_id(self, store, session_dir):
        store.create("ses-tui-1", cluster_name="staging", namespace="ns-a")
        assert (session_dir / "ses-tui-1.json").exists()

    def test_writes_initial_schema(self, store, session_dir):
        store.create("ses-tui-2", cluster_name="prod", namespace="ns-b")
        data = json.loads((session_dir / "ses-tui-2.json").read_text())
        assert data["tui_session_id"] == "ses-tui-2"
        assert data["status"] == "active"
        assert data["cluster_name"] == "prod"
        assert data["namespace"] == "ns-b"
        assert data["finished_at"] is None
        assert data["task_ids"] == []
        assert data["stats"]["injection_count"] == 0
        # New field: messages should start empty
        assert data["messages"] == []


class TestAddTask:
    def test_appends_task_id(self, store, session_dir):
        store.create("ses-tui-3")
        store.add_task("ses-tui-3", "task-a")
        store.add_task("ses-tui-3", "task-b")
        data = json.loads((session_dir / "ses-tui-3.json").read_text())
        assert data["task_ids"] == ["task-a", "task-b"]

    def test_no_duplicates(self, store, session_dir):
        store.create("ses-tui-4")
        store.add_task("ses-tui-4", "task-a")
        store.add_task("ses-tui-4", "task-a")
        data = json.loads((session_dir / "ses-tui-4.json").read_text())
        assert data["task_ids"] == ["task-a"]

    def test_create_fresh_if_session_missing(self, store, session_dir):
        # add_task should not raise when the session file doesn't exist yet
        store.add_task("ses-tui-missing", "task-x")
        data = json.loads((session_dir / "ses-tui-missing.json.json").read_text()) if (session_dir / "ses-tui-missing.json.json").exists() else None
        # The store auto-creates with create(), so just verify it works
        data = json.loads((session_dir / "ses-tui-missing.json").read_text())
        assert data["task_ids"] == ["task-x"]


class TestUpdateStats:
    def test_merges_into_stats(self, store, session_dir):
        store.create("ses-tui-5")
        store.update_stats("ses-tui-5", {
            "message_count": 4,
            "injection_count": 2,
            "injection_success": 1,
            "injection_fail": 1,
            "recovery_count": 1,
        })
        data = json.loads((session_dir / "ses-tui-5.json").read_text())
        assert data["stats"]["message_count"] == 4
        assert data["stats"]["injection_success"] == 1


class TestFinalize:
    def test_marks_completed_with_timestamp(self, store, session_dir):
        store.create("ses-tui-6")
        store.finalize("ses-tui-6")
        data = json.loads((session_dir / "ses-tui-6.json").read_text())
        assert data["status"] == "completed"
        assert data["finished_at"]


class TestRead:
    def test_read_missing_returns_none(self, store):
        assert store.read("ses-tui-nope") is None

    def test_read_returns_dict(self, store):
        store.create("ses-tui-7")
        data = store.read("ses-tui-7")
        assert data is not None
        assert data["tui_session_id"] == "ses-tui-7"


class TestAppendDialogue:
    """Tests for append_dialogue — intent clarification message storage
    with dedup logic that prevents double-writing from hook + node."""

    def test_append_adds_messages(self, store, session_dir):
        store.create("ses-dlg-1")
        msgs = [
            HumanMessage(content="我想注入CPU故障"),
            AIMessage(content="好的，需要知道节点名称"),
        ]
        store.append_dialogue("ses-dlg-1", msgs)
        # read() merges JSON snapshot + JSONL increments
        data = store.read("ses-dlg-1")
        assert len(data["messages"]) == 2
        assert data["stats"]["message_count"] == 2
        assert data["messages"][0]["type"] == "human"
        assert data["messages"][1]["type"] == "ai"

    def test_dedup_skips_identical_messages(self, store, session_dir):
        """Appending the same messages twice should not duplicate them."""
        store.create("ses-dlg-2")
        msgs = [AIMessage(content="需要节点名", id="msg-1")]
        store.append_dialogue("ses-dlg-2", msgs)
        store.append_dialogue("ses-dlg-2", msgs)  # second write
        data = store.read("ses-dlg-2")
        assert len(data["messages"]) == 1
        assert data["stats"]["message_count"] == 1

    def test_dedup_different_messages_are_added(self, store, session_dir):
        """Different content passes through dedup."""
        store.create("ses-dlg-3")
        msg1 = AIMessage(content="第一轮回复", id="m1")
        msg2 = AIMessage(content="第二轮回复", id="m2")
        store.append_dialogue("ses-dlg-3", [msg1])
        store.append_dialogue("ses-dlg-3", [msg2])
        data = store.read("ses-dlg-3")
        assert len(data["messages"]) == 2

    def test_dedup_no_write_when_all_duplicates(self, store, session_dir):
        """If every message is already present, no JSONL write occurs."""
        store.create("ses-dlg-4")
        msg = AIMessage(content="hello", id="m1")
        store.append_dialogue("ses-dlg-4", [msg])
        # Get JSONL mtime after first write
        jsonl_path = session_dir / "ses-dlg-4.jsonl"
        if jsonl_path.exists():
            first_mtime = jsonl_path.stat().st_mtime_ns
        else:
            first_mtime = 0
        store.append_dialogue("ses-dlg-4", [msg])  # all dup — no JSONL write
        if jsonl_path.exists():
            second_mtime = jsonl_path.stat().st_mtime_ns
        else:
            second_mtime = first_mtime
        # JSONL should not have been modified (mtime unchanged)
        assert second_mtime == first_mtime

    def test_read_dialogue_returns_messages(self, store, session_dir):
        store.create("ses-dlg-5")
        msgs = [HumanMessage(content="你好"), AIMessage(content="你好！")]
        store.append_dialogue("ses-dlg-5", msgs)
        dialogue = store.read_dialogue("ses-dlg-5")
        assert len(dialogue) == 2
        assert dialogue[0]["type"] == "human"
        assert dialogue[1]["type"] == "ai"

    def test_read_dialogue_missing_session(self, store):
        assert store.read_dialogue("ses-nope") == []

    def test_append_dialogue_missing_session_skips(self, store):
        """No error when session doesn't exist."""
        store.append_dialogue("ses-missing", [AIMessage(content="x")])
        # Should not create a file (read returns None)
        assert store.read("ses-missing") is None
