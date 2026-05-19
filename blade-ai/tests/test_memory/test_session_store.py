"""Tests for the task-keyed SessionStore persistence layer."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from chaos_agent.memory.session_store import (
    SessionStore,
    _split_at_handoff,
)


@pytest.fixture
def task_dir(tmp_path):
    """Temporary `tasks/` directory for a fresh SessionStore."""
    return tmp_path / "tasks"


@pytest.fixture
def store(task_dir):
    """SessionStore under a fresh task directory."""
    return SessionStore(task_dir)


@pytest.fixture
def compacting_store(task_dir):
    """SessionStore with a very low compaction threshold for testing."""
    return SessionStore(task_dir, compaction_threshold=5)


class TestCreateSession:
    """File creation, schema, and tui_session_id / parent_task_id wiring."""

    def test_creates_file_named_by_task_id(self, store, task_dir):
        store.create_session("task-001", operation="inject")
        assert (task_dir / "task-001.json").exists()

    def test_writes_task_schema_fields(self, store, task_dir):
        store.create_session(
            "task-002",
            operation="inject",
            tui_session_id="ses-tui-1",
        )
        data = json.loads((task_dir / "task-002.json").read_text())
        assert data["taskId"] == "task-002"
        assert data["tui_session_id"] == "ses-tui-1"
        assert data["parent_task_id"] == ""
        assert data["operation"] == "inject"
        assert data["status"] == "active"
        assert data["messages"] == []

    def test_recover_records_parent_task_id(self, store, task_dir):
        store.create_session(
            "task-recover",
            operation="recover",
            tui_session_id="ses-tui-1",
            parent_task_id="task-inject",
        )
        data = json.loads((task_dir / "task-recover.json").read_text())
        assert data["operation"] == "recover"
        assert data["parent_task_id"] == "task-inject"
        assert data["tui_session_id"] == "ses-tui-1"


class TestAppendRawMessage:
    """append_raw_message keyed by task_id."""

    def test_append_raw_message(self, store):
        store.create_session("task-raw", operation="inject")
        store.append_raw_message("task-raw", {
            "type": "tool_execution",
            "content": "[shell] kubectl get pods",
            "detail": {
                "command": "kubectl get pods",
                "exit_code": 0,
                "duration_ms": 1523.4,
                "stdout_preview": '{"items":[]}',
                "source": "kubectl",
            },
        })

        session = store.read_session("task-raw")
        assert session is not None
        msgs = session["messages"]
        assert len(msgs) == 1
        assert msgs[0]["type"] == "tool_execution"
        assert msgs[0]["detail"]["exit_code"] == 0
        assert msgs[0]["detail"]["duration_ms"] == 1523.4


class TestFinalize:
    """finalize_session clears in-memory state and writes status."""

    def test_finalize_writes_status_and_removes_from_active(self, store, task_dir):
        store.create_session("task-fin", operation="inject")
        store.finalize_session("task-fin", status="completed")
        data = json.loads((task_dir / "task-fin.json").read_text())
        assert data["status"] == "completed"
        assert data["finished_at"]
        # After finalize, the in-memory active session is gone; read_session
        # should still serve the persisted file though.
        assert store.read_session("task-fin") is not None


class TestHasActive:
    """``has_active`` is the public read of the in-memory active set.

    It exists so callers outside this module (intent_clarification's
    bootstrap, turn.py's defensive finalize) don't reach into the
    leading-underscore ``_active_sessions`` dict directly. The
    contract is covered by an explicit test so a future refactor of
    the in-memory representation can't silently break the boundary.
    """

    def test_unknown_task_returns_false(self, store):
        assert store.has_active("task-never-created") is False

    def test_returns_true_after_create_session(self, store):
        store.create_session("task-active", operation="inject")
        assert store.has_active("task-active") is True

    def test_returns_false_after_finalize(self, store):
        store.create_session("task-flow", operation="inject")
        assert store.has_active("task-flow") is True
        store.finalize_session("task-flow", status="completed")
        # Finalize deletes the in-memory entry but the on-disk file
        # remains (read_session still returns it). ``has_active``
        # tracks the in-memory state, not disk presence.
        assert store.has_active("task-flow") is False
        assert store.read_session("task-flow") is not None

    def test_handles_invalid_inputs_safely(self, store):
        # Empty / None / non-string must not raise.
        assert store.has_active("") is False
        assert store.has_active(None) is False  # type: ignore[arg-type]
        assert store.has_active(123) is False  # type: ignore[arg-type]


class TestSyncFlushBeforeFinalize:
    """Pre-finalize flush correctness — the "Esc must not lose messages"
    contract.

    The PreReasoningHook writes messages to the session via
    ``asyncio.create_task`` (fire-and-forget). On Esc / SSE cancel /
    unhandled exception, those tasks may not have completed. Plus
    LangGraph's ``ToolNode`` produces ToolMessages outside any hook
    — they normally only reach disk via the *next* iteration's hook,
    so an Esc between a tool call and the next agent_loop iteration
    drops them entirely.

    The fix ``turn.py`` finally + ``save_memory``: synchronously call
    ``append_messages(task_id, state.messages)`` BEFORE finalize.
    Dedup makes re-passing safe; the missing-tail problem disappears
    because we always write the authoritative graph-state list.

    These tests pin the dedup contract so the fix can't silently
    regress: appending the same list twice is a no-op, and partial
    interleaving (some early appends + a final full flush) ends up
    with the full list on disk exactly once.
    """

    def test_repeated_full_append_dedups_to_one_copy(self, store, task_dir):
        # Simulates the real fix path: the hook fired one or more
        # times (each writing a prefix of state.messages), then turn.py's
        # finally fires a synchronous append of the FULL list.
        store.create_session("task-flush", operation="inject")
        h1 = HumanMessage(content="step 1", id="h1")
        a1 = AIMessage(content="reply 1", id="a1")
        h2 = HumanMessage(content="step 2", id="h2")
        a2 = AIMessage(content="reply 2", id="a2")

        # Hook iteration 1 wrote partial.
        store.append_messages("task-flush", [h1, a1])
        # Hook iteration 2 wrote a longer prefix.
        store.append_messages("task-flush", [h1, a1, h2])
        # turn.py finally flushes everything just before finalize.
        store.append_messages("task-flush", [h1, a1, h2, a2])

        store.finalize_session(
            "task-flush",
            remaining_messages=[],  # finalize with nothing extra.
            status="cancelled",
        )

        data = json.loads((task_dir / "task-flush.json").read_text())
        # All four messages present, exactly once.
        assert len(data["messages"]) == 4
        contents = [m["content"] for m in data["messages"]]
        assert contents == ["step 1", "reply 1", "step 2", "reply 2"]

    def test_full_flush_recovers_messages_hook_never_wrote(self, store, task_dir):
        # Simulates the Esc-loses-tool-messages scenario: the hook
        # fired ONLY for the first agent_loop iteration (writing the
        # first 2 messages), then a tool ran (added a ToolMessage to
        # state, no hook fires for ToolNode), then user pressed Esc.
        # The fix flushes the full state.messages (4 entries) before
        # finalize — the ToolMessage that the next-iteration hook
        # would have caught is recovered.
        from langchain_core.messages import ToolMessage as TM
        store.create_session("task-tool-recovery", operation="inject")
        sys_summary = SystemMessage(
            content="[Intent Clarification Summary]\nfault: x", id="sys",
        )
        a1 = AIMessage(
            content="",
            tool_calls=[{"id": "tc1", "name": "kubectl", "args": {}}],
            id="a1",
        )
        # Hook iteration 1 wrote up to the AI tool_call.
        store.append_messages(
            "task-tool-recovery", [sys_summary, a1],
        )
        # ToolNode runs — produces a ToolMessage. No hook fires here.
        tool_result = TM(content="ok", name="kubectl", tool_call_id="tc1", id="t1")
        # User Esc's. State.messages is [sys, a1, t1]. The fix's
        # synchronous flush carries the full list.
        store.append_messages(
            "task-tool-recovery", [sys_summary, a1, tool_result],
        )
        store.finalize_session(
            "task-tool-recovery",
            remaining_messages=[],
            status="cancelled",
        )
        data = json.loads(
            (task_dir / "task-tool-recovery.json").read_text(),
        )
        # The ToolMessage that would have been lost on Esc is now on
        # disk.
        types = [m["type"] for m in data["messages"]]
        assert "tool" in types, (
            "Tool message must survive Esc — turn.py finally's "
            "synchronous flush is the load-bearing path."
        )
        # And no duplicates of the system / ai messages.
        assert types.count("system") == 1
        assert types.count("ai") == 1


class TestListTasks:
    """list_tasks returns task_ids derived from filenames."""

    def test_list_tasks_strips_extension(self, store):
        store.create_session("task-a", operation="inject")
        store.create_session("task-b", operation="recover", parent_task_id="task-a")
        task_ids = set(store.list_tasks())
        assert task_ids == {"task-a", "task-b"}


# ---------------------------------------------------------------------------
# JSONL append-only & compaction tests
# ---------------------------------------------------------------------------


class TestAppendOnly:
    """Append operations write to JSONL instead of rewriting the full JSON."""

    def test_append_creates_jsonl(self, store, task_dir):
        store.create_session("task-jl", operation="inject")
        store.append_raw_message("task-jl", {"type": "human", "content": "hi"})
        assert (task_dir / "task-jl.jsonl").exists()

    def test_jsonl_line_count_matches_appends(self, store, task_dir):
        store.create_session("task-lines", operation="inject")
        for i in range(3):
            store.append_raw_message(
                "task-lines", {"type": "human", "content": f"msg-{i}"}
            )
        jsonl_text = (task_dir / "task-lines.jsonl").read_text()
        lines = [l for l in jsonl_text.strip().split("\n") if l.strip()]
        assert len(lines) == 3

    def test_snapshot_unchanged_on_append(self, store, task_dir):
        store.create_session("task-snap", operation="inject")
        store.append_raw_message("task-snap", {"type": "human", "content": "hello"})
        snapshot = json.loads((task_dir / "task-snap.json").read_text())
        assert snapshot["messages"] == []  # skeleton still has empty messages

    def test_read_session_reconstructs_from_snapshot_and_jsonl(self, store):
        store.create_session("task-recon", operation="inject")
        store.append_raw_message("task-recon", {"type": "human", "content": "a"})
        store.append_raw_message("task-recon", {"type": "ai", "content": "b"})
        session = store.read_session("task-recon")
        assert session is not None
        assert len(session["messages"]) == 2
        assert session["messages"][0]["content"] == "a"
        assert session["messages"][1]["content"] == "b"


class TestCompaction:
    """Compaction triggers when JSONL line count exceeds the threshold."""

    def test_compaction_writes_snapshot(self, compacting_store, task_dir):
        store = compacting_store
        store.create_session("task-cmp", operation="inject")
        # Append exactly threshold messages — triggers compaction on the last one
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-cmp", {"type": "human", "content": f"msg-{i}"}
            )
        # After compaction, the snapshot .json should contain all messages
        snapshot = json.loads((task_dir / "task-cmp.json").read_text())
        assert len(snapshot["messages"]) == store._compaction_threshold

    def test_jsonl_truncated_after_compaction(self, compacting_store, task_dir):
        store = compacting_store
        store.create_session("task-trunc", operation="inject")
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-trunc", {"type": "human", "content": f"msg-{i}"}
            )
        # JSONL should be empty (truncated) after compaction
        jsonl_path = task_dir / "task-trunc.jsonl"
        assert jsonl_path.exists()
        assert jsonl_path.read_text().strip() == ""

    def test_read_session_after_compaction(self, compacting_store):
        store = compacting_store
        store.create_session("task-read", operation="inject")
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-read", {"type": "human", "content": f"msg-{i}"}
            )
        session = store.read_session("task-read")
        assert session is not None
        assert len(session["messages"]) == store._compaction_threshold

    def test_append_after_compaction(self, compacting_store):
        store = compacting_store
        store.create_session("task-post", operation="inject")
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-post", {"type": "human", "content": f"msg-{i}"}
            )
        # Append a few more after compaction
        store.append_raw_message("task-post", {"type": "ai", "content": "extra-1"})
        store.append_raw_message("task-post", {"type": "ai", "content": "extra-2"})
        session = store.read_session("task-post")
        assert session is not None
        assert len(session["messages"]) == store._compaction_threshold + 2


class TestFinalizeCleanup:
    """finalize_session deletes the JSONL file and writes a complete JSON."""

    def test_finalize_deletes_jsonl(self, store, task_dir):
        store.create_session("task-fcl", operation="inject")
        store.append_raw_message("task-fcl", {"type": "human", "content": "hi"})
        assert (task_dir / "task-fcl.jsonl").exists()
        store.finalize_session("task-fcl", status="completed")
        assert not (task_dir / "task-fcl.jsonl").exists()

    def test_finalize_json_contains_all_messages(self, store, task_dir):
        store.create_session("task-fmsg", operation="inject")
        for i in range(5):
            store.append_raw_message(
                "task-fmsg", {"type": "human", "content": f"msg-{i}"}
            )
        store.finalize_session("task-fmsg", status="completed")
        data = json.loads((task_dir / "task-fmsg.json").read_text())
        assert len(data["messages"]) == 5
        assert data["status"] == "completed"

    def test_finalize_with_remaining_messages(self, compacting_store, task_dir):
        store = compacting_store
        store.create_session("task-frm", operation="inject")
        store.append_raw_message("task-frm", {"type": "human", "content": "existing"})
        store.finalize_session(
            "task-frm",
            status="completed",
            result_summary="ok",
        )
        data = json.loads((task_dir / "task-frm.json").read_text())
        assert data["result_summary"] == "ok"
        assert not (task_dir / "task-frm.jsonl").exists()


class TestReadSessionLegacy:
    """read_session handles legacy .json-only files (no .jsonl)."""

    def test_legacy_json_only(self, store, task_dir):
        # Manually write a legacy-format .json file (no .jsonl)
        legacy_data = {
            "taskId": "task-legacy",
            "operation": "inject",
            "status": "completed",
            "messages": [{"type": "human", "content": "old message"}],
        }
        (task_dir / "task-legacy.json").write_text(
            json.dumps(legacy_data, ensure_ascii=False)
        )
        result = store.read_session("task-legacy")
        assert result is not None
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "old message"


class TestCrashRecovery:
    """read_session gracefully handles corrupt JSONL lines."""

    def test_corrupt_jsonl_line_skipped(self, store, task_dir):
        store.create_session("task-crash", operation="inject")
        store.append_raw_message("task-crash", {"type": "human", "content": "good"})
        # Append a corrupt line directly to the JSONL
        jsonl_path = task_dir / "task-crash.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write('{"broken json without closing brace\n')
        store.append_raw_message("task-crash", {"type": "human", "content": "after"})
        session = store.read_session("task-crash")
        assert session is not None
        # Should have the good messages; corrupt line is skipped
        contents = [m["content"] for m in session["messages"]]
        assert "good" in contents
        assert "after" in contents

    def test_empty_jsonl_after_compaction(self, compacting_store):
        store = compacting_store
        store.create_session("task-empty-jl", operation="inject")
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-empty-jl", {"type": "human", "content": f"msg-{i}"}
            )
        session = store.read_session("task-empty-jl")
        assert session is not None
        assert len(session["messages"]) == store._compaction_threshold


class TestPerformance:
    """Verify append latency stays under the 10ms target for 1000 messages."""

    def test_single_append_under_10ms_with_1000_messages(self, task_dir):
        store = SessionStore(task_dir, compaction_threshold=10_000)
        store.create_session("task-perf", operation="inject")
        # Pre-fill 1000 messages via direct in-memory manipulation + JSONL
        for i in range(1000):
            store.append_raw_message(
                "task-perf", {"type": "human", "content": f"msg-{i}"}
            )
        # Measure a single append
        start = time.perf_counter()
        store.append_raw_message(
            "task-perf", {"type": "human", "content": "final-msg"}
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert elapsed_ms < 10, f"Single append took {elapsed_ms:.2f}ms, expected < 10ms"


class TestSplitAtHandoff:
    """Tests for _split_at_handoff — P0-7-5 dialogue/execution split."""

    def test_no_summary_returns_all_as_execution(self):
        """Without IntentClarificationSummary, all messages are execution."""
        msgs = [
            HumanMessage(content="hello"),
            AIMessage(content="hi"),
        ]
        dialogue, execution = _split_at_handoff(msgs)
        assert dialogue == []
        assert execution == msgs

    def test_summary_splits_correctly(self):
        """Summary divides dialogue (before) and execution (after+summary)."""
        dialogue_msgs = [
            HumanMessage(content="我想注入cpu"),
            AIMessage(content="需要节点名"),
        ]
        summary = SystemMessage(content="[Intent Clarification Summary]\nDialogue rounds: 3\nConfirmed intent: inject")
        execution_msgs = [
            AIMessage(content="正在执行..."),
            HumanMessage(content="ok"),
        ]
        all_msgs = dialogue_msgs + [summary] + execution_msgs
        dialogue, execution = _split_at_handoff(all_msgs)
        assert len(dialogue) == 2
        assert dialogue[0].content == "我想注入cpu"
        assert len(execution) == 3  # summary + 2 execution msgs
        assert execution[0] == summary

    def test_remove_messages_are_excluded_from_dialogue(self):
        """RemoveMessage entries in the dialogue portion are filtered out."""
        msgs = [
            HumanMessage(content="hi", id="m1"),
            RemoveMessage(id="m1"),
            SystemMessage(content="[Intent Clarification Summary]\n..."),
            AIMessage(content="exec"),
        ]
        dialogue, execution = _split_at_handoff(msgs)
        assert len(dialogue) == 1  # RemoveMessage filtered
        assert dialogue[0].content == "hi"
        assert len(execution) == 2  # summary + AIMessage

    def test_empty_messages_list(self):
        dialogue, execution = _split_at_handoff([])
        assert dialogue == []
        assert execution == []


class TestCreateSessionWithInitialMessages:
    """Tests for P0-7-6: create_session with initial_messages parameter."""

    def test_initial_messages_written_to_task_file(self, store, task_dir):
        """initial_messages become the first entries in the task file."""
        handoff = SystemMessage(content="[Intent Clarification Summary]\nDialogue rounds: 2")
        store.create_session(
            "task-handoff-1",
            operation="inject",
            tui_session_id="ses-1",
            initial_messages=[handoff],
        )
        data = json.loads((task_dir / "task-handoff-1.json").read_text())
        assert len(data["messages"]) == 1
        assert data["messages"][0]["content"].startswith("[Intent Clarification Summary]")

    def test_no_initial_messages_empty_list(self, store, task_dir):
        """Without initial_messages, messages list starts empty."""
        store.create_session("task-no-handoff", operation="inject")
        data = json.loads((task_dir / "task-no-handoff.json").read_text())
        assert data["messages"] == []
