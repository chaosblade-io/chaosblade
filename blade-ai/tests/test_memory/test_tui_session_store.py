"""Tests for TuiSessionStore persistence layer."""

import json
import os

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


class TestConversationThreadId:
    """The /resume dialogue-continuity binding: ``conversation_thread_id``
    persisted in the session JSON so a server restart can restore the
    LangGraph thread (turn.py reads it on the next turn and backfills
    it when the session predates the field)."""

    def test_create_persists_thread_id(self, store, session_dir):
        store.create(
            "ses-thr-1",
            cluster_name="prod",
            conversation_thread_id="conv-abc123def456",
        )
        disk = json.loads(
            (session_dir / "ses-thr-1.json").read_text(encoding="utf-8")
        )
        assert disk["conversation_thread_id"] == "conv-abc123def456"

    def test_create_defaults_to_empty_thread(self, store, session_dir):
        store.create("ses-thr-2")
        disk = json.loads(
            (session_dir / "ses-thr-2.json").read_text(encoding="utf-8")
        )
        # Empty string = "mint a fresh thread on the next turn" —
        # legacy files share the same semantics.
        assert disk["conversation_thread_id"] == ""

    def test_update_thread_id_rewrites_binding(self, store):
        store.create("ses-thr-3")
        store.update_thread_id("ses-thr-3", "conv-newthread99")
        assert store.read("ses-thr-3")["conversation_thread_id"] == (
            "conv-newthread99"
        )
        # Overwrite semantics — a second update wins.
        store.update_thread_id("ses-thr-3", "conv-second")
        assert store.read("ses-thr-3")["conversation_thread_id"] == (
            "conv-second"
        )

    def test_update_thread_id_missing_session_skips(self, store, session_dir):
        # Defensive skip (same contract as append_dialogue): turn.py
        # only backfills for sessions the turn pipeline already knows,
        # so a stray sid must not sprout a half-initialised file.
        store.update_thread_id("ses-thr-4", "conv-fresh")
        assert not (session_dir / "ses-thr-4.json").exists()

    def test_update_thread_id_does_not_disturb_other_fields(self, store):
        store.create("ses-thr-5", cluster_name="prod", namespace="ns")
        store.add_task("ses-thr-5", "task-1")
        before = store.read("ses-thr-5")
        store.update_thread_id("ses-thr-5", "conv-z")
        after = store.read("ses-thr-5")
        assert after["cluster_name"] == "prod"
        assert after["namespace"] == "ns"
        assert after["task_ids"] == ["task-1"]
        assert after["started_at"] == before["started_at"]


class TestUpdateStatus:
    """``update_status`` — the resume reactivation path. Contract (vs
    ``finalize``): ONLY the status field moves. No ``finished_at``
    stamp, no events-jsonl deletion, no in-memory eviction."""

    def _disk(self, store, sid):
        return json.loads(
            (store.session_dir / f"{sid}.json").read_text(encoding="utf-8")
        )

    def test_flips_status_without_finished_at(self, store):
        store.create("ses-st-1")
        store.update_status("ses-st-1", "completed")
        disk = self._disk(store, "ses-st-1")
        assert disk["status"] == "completed"
        # finalize() would stamp this; update_status must not.
        assert not disk.get("finished_at")

    def test_reactivate_back_to_active(self, store):
        store.create("ses-st-2")
        store.update_status("ses-st-2", "completed")
        store.update_status("ses-st-2", "active")
        assert self._disk(store, "ses-st-2")["status"] == "active"

    def test_preserves_everything_else(self, store):
        store.create("ses-st-3", conversation_thread_id="conv-keep")
        store.add_task("ses-st-3", "task-7")
        store.update_status("ses-st-3", "completed")
        disk = self._disk(store, "ses-st-3")
        assert disk["conversation_thread_id"] == "conv-keep"
        assert disk["task_ids"] == ["task-7"]

    def test_missing_session_skips(self, store):
        # The resume path always resumes an on-disk session, so a
        # stray sid is a bug — skip defensively, no file materialises.
        store.update_status("ses-st-4", "active")
        assert store.read("ses-st-4") is None


def _write_events(events_dir, sid, lines, mtime=None):
    """Write a synthetic events jsonl — the resume source of truth
    lives in ``<memory_dir>/tui/<sid>.events.jsonl`` (sibling of the
    sessions dir, which the store constructor creates as _events_dir)."""
    path = events_dir / f"{sid}.events.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _user_input_event(content: str) -> str:
    """A well-formed user_input event line (json.dumps handles the
    escaping — hand-built JSON breaks the moment content carries a
    newline, splitting one JSONL line into three)."""
    return json.dumps(
        {"event_type": "user_input", "data": {"content": content}}
    )


class TestHasEvents:
    """``has_events`` — the pre-boot guard behind ``blade-ai resume
    -i <sid>``: fail at the CLI instead of booting a whole server for
    a sid that names nothing resumable."""

    def test_true_when_events_jsonl_exists(self, store, tmp_path):
        _write_events(
            tmp_path / "tui", "ses-ev-1", [_user_input_event("hi")]
        )
        assert store.has_events("ses-ev-1") is True

    def test_false_when_missing(self, store):
        assert store.has_events("ses-ev-none") is False


class TestListResumableSessions:
    """``list_resumable_sessions`` — single source for BOTH resume
    pickers (the server's resumable route and the CLI's interactive
    ``blade-ai resume``). Pins row shape and events-only membership."""

    def _events_dir(self, tmp_path):
        return tmp_path / "tui"

    def _rows(self, store):
        return [r["tui_session_id"] for r in store.list_resumable_sessions()]

    def test_empty_when_no_events_files(self, store):
        assert store.list_resumable_sessions() == []

    def test_only_sessions_with_events_are_listed(self, store, tmp_path):
        _write_events(
            self._events_dir(tmp_path), "ses-lr-1", [_user_input_event("hi")]
        )
        # A session JSON without an events file is NOT resumable —
        # the events jsonl is the resume source of truth.
        store.create("ses-lr-jsononly")
        assert self._rows(store) == ["ses-lr-1"]

    def test_sorted_newest_first_by_mtime(self, store, tmp_path):
        d = self._events_dir(tmp_path)
        _write_events(d, "ses-old", [_user_input_event("a")], mtime=1_000_000)
        _write_events(d, "ses-new", [_user_input_event("b")], mtime=2_000_000)
        assert self._rows(store) == ["ses-new", "ses-old"]

    def test_row_shape(self, store, tmp_path, session_dir):
        lines = [
            _user_input_event("inject cpu fault"),
            '{"event_type": "tool_call", "data": {"name": "kubectl"}}',
            '{"event_type": "token", "data": {"content": "ok"}}',
        ]
        path = _write_events(
            self._events_dir(tmp_path), "ses-shape", lines, mtime=1_500_000
        )
        store.create("ses-shape")
        rows = store.list_resumable_sessions()
        assert len(rows) == 1
        r = rows[0]
        assert set(r) == {
            "tui_session_id",
            "size_bytes",
            "event_count",
            "started_at",
            "modified_at",
            "first_input",
        }
        assert r["tui_session_id"] == "ses-shape"
        assert r["event_count"] == 3
        assert r["size_bytes"] == path.stat().st_size
        assert r["modified_at"] == 1_500_000.0
        assert r["first_input"] == "inject cpu fault"
        # started_at comes from the session JSON create() wrote.
        disk = json.loads(
            (session_dir / "ses-shape.json").read_text(encoding="utf-8")
        )
        assert r["started_at"] == disk["started_at"]

    def test_started_at_empty_when_session_json_missing(self, store, tmp_path):
        # Events file only (session JSON deleted) — started_at degrades
        # to "" rather than fabricating a value.
        _write_events(
            self._events_dir(tmp_path), "ses-orphan", [_user_input_event("x")]
        )
        r = store.list_resumable_sessions()[0]
        assert r["tui_session_id"] == "ses-orphan"
        assert r["started_at"] == ""

    def test_limit_trims_to_newest(self, store, tmp_path):
        d = self._events_dir(tmp_path)
        _write_events(d, "ses-a", [_user_input_event("a")], mtime=1_000_000)
        _write_events(d, "ses-b", [_user_input_event("b")], mtime=2_000_000)
        _write_events(d, "ses-c", [_user_input_event("c")], mtime=3_000_000)
        rows = store.list_resumable_sessions(limit=2)
        assert [r["tui_session_id"] for r in rows] == ["ses-c", "ses-b"]

    def test_first_input_collapses_whitespace(self, store, tmp_path):
        _write_events(
            self._events_dir(tmp_path),
            "ses-ws",
            [_user_input_event("  spaced \n\n out  input ")],
        )
        r = store.list_resumable_sessions()[0]
        assert r["first_input"] == "spaced out input"

    def test_first_input_truncated_to_60_chars(self, store, tmp_path):
        _write_events(
            self._events_dir(tmp_path),
            "ses-long",
            [_user_input_event("x" * 80)],
        )
        fi = store.list_resumable_sessions()[0]["first_input"]
        assert len(fi) == 60
        assert fi.endswith("…")

    def test_first_input_empty_without_user_input_event(self, store, tmp_path):
        # A session that crashed before its first turn still lists —
        # the digest degrades to "".
        _write_events(
            self._events_dir(tmp_path),
            "ses-noui",
            ['{"event_type": "session_created", "data": {}}'],
        )
        assert store.list_resumable_sessions()[0]["first_input"] == ""

    def test_corrupt_line_counts_but_is_skipped_for_input(
        self, store, tmp_path
    ):
        # A corrupt line (crash mid-write) still counts toward
        # event_count (byte-level counting), but is skipped over when
        # hunting the first user_input.
        _write_events(
            self._events_dir(tmp_path),
            "ses-corrupt",
            ["{not json", _user_input_event("real input")],
        )
        r = store.list_resumable_sessions()[0]
        assert r["event_count"] == 2
        assert r["first_input"] == "real input"
