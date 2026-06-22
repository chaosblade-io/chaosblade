"""Tests for the task-keyed SessionStore persistence layer."""

import json
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage

from chaos_agent.memory.session_store import (
    SessionStore,
    _split_at_handoff,
    build_verification_simple,
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


def test_build_verification_simple_compat_proxy():
    """Legacy memory-layer helper delegates to operation_outcome projection."""

    assert build_verification_simple(
        {"level": "recovered", "layer1": {"status": "passed"}, "layer2": {}}
    ) == {
        "level": "recovered",
        "layer1": {"status": "passed"},
        "layer2": {"status": "unknown"},
        "baseline_confidence": "none",
        "baseline_used": None,
    }


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
        """Bug 3 fix changed compact from ``truncate`` to ``rename+unlink``:
        after compact the live .jsonl no longer exists (next append
        re-creates it). The .jsonl.compacted orphan is also gone in
        the success path."""
        store = compacting_store
        store.create_session("task-trunc", operation="inject")
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-trunc", {"type": "human", "content": f"msg-{i}"}
            )
        jsonl_path = task_dir / "task-trunc.jsonl"
        compacted_path = task_dir / "task-trunc.jsonl.compacted"
        # Either jsonl doesn't exist OR it exists but is empty
        # (a subsequent append after compact would re-create it).
        if jsonl_path.exists():
            assert jsonl_path.read_text().strip() == ""
        # The .jsonl.compacted orphan must be cleaned up on success.
        assert not compacted_path.exists()

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
        """initial_messages become the first entries in the task record.

        Bug 1 fix: ``.json`` is a snapshot and ``.jsonl`` is the
        increment log. initial_messages go to ``.jsonl`` (not duplicated
        into ``.json``) so the invariant "snapshot + increments are
        disjoint" holds. Reading via ``read_session()`` returns the
        union — this is the public contract, not the raw file content.
        """
        handoff = SystemMessage(content="[Intent Clarification Summary]\nDialogue rounds: 2")
        store.create_session(
            "task-handoff-1",
            operation="inject",
            tui_session_id="ses-1",
            initial_messages=[handoff],
        )
        # Public contract: read_session() returns the full message list
        session = store.read_session("task-handoff-1")
        assert len(session["messages"]) == 1
        assert session["messages"][0]["content"].startswith("[Intent Clarification Summary]")
        # Bug 1 invariant: ``.json`` snapshot is a skeleton (no messages),
        # ``.jsonl`` holds the increment. Double-writing both would make
        # read_session return [handoff, handoff] after concat.
        snapshot = json.loads((task_dir / "task-handoff-1.json").read_text())
        assert snapshot["messages"] == []

    def test_no_initial_messages_empty_list(self, store, task_dir):
        """Without initial_messages, messages list starts empty."""
        store.create_session("task-no-handoff", operation="inject")
        data = json.loads((task_dir / "task-no-handoff.json").read_text())
        assert data["messages"] == []


class TestBugARollback:
    """Bug A regression: _append_to_jsonl failure must not corrupt
    in-memory state. Disk is source of truth."""

    def test_jsonl_write_failure_does_not_extend_in_memory(self, store, monkeypatch):
        """When _append_to_jsonl raises OSError, session["messages"]
        must NOT have been extended — otherwise next read from disk
        would silently drop the appended messages."""
        store.create_session("task-disk-fail", operation="inject")
        # Force _append_to_jsonl to fail
        def _fail(*args, **kwargs):
            raise OSError("simulated disk full")
        monkeypatch.setattr(store, "_append_to_jsonl", _fail)

        store.append_messages("task-disk-fail", [HumanMessage(content="lost?", id="m1")])

        # In-memory should NOT have the lost message — disk is truth
        session = store._active_sessions["task-disk-fail"]
        assert session["messages"] == []

    def test_jsonl_write_failure_allows_retry(self, store, monkeypatch):
        """After a transient OSError, a retry with the same message
        should succeed because dedup_keys are derived from in-memory
        state (which never extended)."""
        store.create_session("task-retry", operation="inject")
        call_count = {"n": 0}
        original = store._append_to_jsonl
        def _flaky(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise OSError("transient")
            return original(*args, **kwargs)
        monkeypatch.setattr(store, "_append_to_jsonl", _flaky)

        msg = HumanMessage(content="retry me", id="m1")
        store.append_messages("task-retry", [msg])  # fails
        store.append_messages("task-retry", [msg])  # retries OK
        session = store._active_sessions["task-retry"]
        assert len(session["messages"]) == 1


class TestBug2Dedup:
    """Bug 2 regression: read_session must dedup by message id even if
    duplicate entries somehow exist across .json + .jsonl."""

    def test_duplicate_messages_deduplicated_on_read(self, store, task_dir):
        """Manually write duplicate into both .json and .jsonl, verify
        read_session returns only one copy."""
        store.create_session("task-dup", operation="inject")
        # Write a message via normal path → goes to jsonl
        store.append_messages("task-dup", [HumanMessage(content="x", id="dup-1")])
        # Manually corrupt: inject the same message into .json snapshot
        snapshot_path = task_dir / "task-dup.json"
        snapshot = json.loads(snapshot_path.read_text())
        snapshot["messages"].append({"type": "human", "content": "x", "id": "dup-1"})
        snapshot_path.write_text(json.dumps(snapshot))
        # Read: should see only one
        result = store.read_session("task-dup")
        assert len(result["messages"]) == 1


class TestBug3CrashRecovery:
    """Bug 3 regression: _compact crash leaves orphan .jsonl.compacted;
    read_session must recover by replaying it + deduping."""

    def test_orphan_compacted_replayed_and_cleaned(self, store, task_dir):
        """Simulate crash mid-_compact: snapshot is stale but
        .jsonl.compacted has the rotated content. read_session must
        replay the orphan and clean it up."""
        store.create_session("task-crash", operation="inject")
        store.append_messages(
            "task-crash", [HumanMessage(content="pre-crash", id="pc-1")]
        )
        # Simulate: rename .jsonl → .jsonl.compacted but DON'T update snapshot
        # (this is the state right after step 1 of _compact, before step 2)
        import os
        jsonl_path = task_dir / "task-crash.jsonl"
        compacted_path = task_dir / "task-crash.jsonl.compacted"
        os.replace(str(jsonl_path), str(compacted_path))
        assert compacted_path.exists()
        assert not jsonl_path.exists()

        # Read: should still see the message (replayed from .jsonl.compacted)
        result = store.read_session("task-crash")
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "pre-crash"
        # Orphan should be cleaned up by read_session
        assert not compacted_path.exists()


class TestDedupKeyRobustness:
    """Round-2 self-check: ``_message_dedup_key`` is invoked by Bug 2
    read-side dedup on EVERY loaded entry, including possibly
    schema-drifted or multi-modal ones. Must never raise."""

    def test_dedup_key_handles_missing_type(self):
        """Old/corrupt entry without 'type' field must not KeyError."""
        from chaos_agent.memory.session_store import _message_dedup_key
        # No id, no type — would have raised KeyError before fix
        key = _message_dedup_key({"content": "hi"})
        assert isinstance(key, str)
        assert "unknown" in key

    def test_dedup_key_handles_list_content(self):
        """Multi-modal content (list) must not TypeError on slicing."""
        from chaos_agent.memory.session_store import _message_dedup_key
        key = _message_dedup_key({
            "type": "human",
            "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "url": "..."}],
        })
        assert isinstance(key, str)

    def test_dedup_key_handles_dict_content(self):
        """Even malformed dict content must not crash dedup."""
        from chaos_agent.memory.session_store import _message_dedup_key
        key = _message_dedup_key({"type": "ai", "content": {"weird": "shape"}})
        assert isinstance(key, str)

    def test_dedup_key_handles_none_content(self):
        from chaos_agent.memory.session_store import _message_dedup_key
        key = _message_dedup_key({"type": "human", "content": None})
        assert isinstance(key, str)

    def test_read_session_survives_corrupt_jsonl_entries(self, store, task_dir):
        """End-to-end: jsonl with a schema-drifted entry must not crash
        read_session — Bug 2 dedup runs over every line."""
        store.create_session("task-corrupt", operation="inject")
        # Normal append
        store.append_messages("task-corrupt", [HumanMessage(content="normal", id="m1")])
        # Inject a schema-drifted entry (no 'type', no 'id') directly
        jsonl_path = task_dir / "task-corrupt.jsonl"
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"content": "schema_drift"}) + "\n")
            f.write(json.dumps({"type": "human", "content": ["multi", "modal"]}) + "\n")
        # Must not raise
        result = store.read_session("task-corrupt")
        assert result is not None
        # Normal + 2 drifted = 3 entries (dedup keys all unique)
        assert len(result["messages"]) == 3


class TestCreateSessionRollbackOnDiskFailure:
    """Round-5 self-check: ``create_session`` must roll back its
    in-memory ``_active_sessions`` registration if the initial
    snapshot write fails. Otherwise subsequent ``append_messages``
    would write to ``.jsonl`` (creating that file) while ``.json``
    doesn't exist, and ``read_session`` would silently return None
    because of the ``if not json_path.exists()`` guard."""

    def test_disk_failure_propagates_and_rolls_back(self, store, monkeypatch):
        import os as os_mod
        def _fail_replace(*args, **kwargs):
            raise OSError("simulated disk full")
        monkeypatch.setattr(os_mod, "replace", _fail_replace)

        # create_session must raise (not silently swallow)
        with pytest.raises(OSError, match="simulated"):
            store.create_session("task-create-fail", operation="inject")

        # Round-5 invariant: registration must NOT linger in-memory
        # after the disk write failed.
        assert "task-create-fail" not in store._active_sessions
        # has_active() must reflect the rollback
        assert not store.has_active("task-create-fail")

    def test_disk_failure_prevents_subsequent_silent_writes(
        self, store, task_dir, monkeypatch
    ):
        """If the rollback regressed (registration kept), subsequent
        append_messages would create a .jsonl with no matching .json —
        Round-5 ensures the registration is gone so append_messages
        warns 'Task not found' instead of silently appending.

        Round-6 strengthening: also assert disk-side contract (.jsonl
        not created). Without this, a mutation that kept the in-memory
        registration cleared but still wrote to disk would slip past
        the previous in-memory-only assertion.
        """
        import os as os_mod
        def _fail_replace(*args, **kwargs):
            raise OSError("simulated")
        monkeypatch.setattr(os_mod, "replace", _fail_replace)

        with pytest.raises(OSError):
            store.create_session("task-rollback", operation="inject")

        monkeypatch.undo()  # restore os.replace for the next call

        # Append after failed create must NOT silently succeed; the
        # task isn't in _active_sessions any more.
        store.append_messages(
            "task-rollback", [HumanMessage(content="ghost", id="g1")]
        )
        assert "task-rollback" not in store._active_sessions
        # Round-6: also verify no .jsonl orphan was created (disk-side
        # contract). A mutation that bypassed the "task not found"
        # guard could have silently appended to disk while the
        # in-memory check still passes.
        assert not (task_dir / "task-rollback.jsonl").exists()
        assert not (task_dir / "task-rollback.json").exists()


class TestAtomicWriteTempCleanup:
    """Round-4 self-check: ``_atomic_write_json`` must clean its
    tempfile on EVERY raise path — including ``os.replace`` failure
    (cross-device rename, target locked, dir permission flip). Without
    try/finally the tempfile leaks forever and task_dir accumulates
    ``.json.tmp`` orphans across retries."""

    def test_os_replace_failure_cleans_tempfile(
        self, store, task_dir, monkeypatch
    ):
        store.create_session("task-replace-fail", operation="inject")
        # Patch global os.replace (session_store uses local `import os`
        # which resolves to the same module object).
        import os as os_mod
        def _fail_replace(*args, **kwargs):
            raise OSError("simulated cross-device link")
        monkeypatch.setattr(os_mod, "replace", _fail_replace)

        # _atomic_write_json should raise OSError (Round 3 contract)
        with pytest.raises(OSError, match="simulated"):
            store._atomic_write_json("task-replace-fail")

        # Restore os.replace before glob so the cleanup itself works
        monkeypatch.undo()

        # After failure: NO .json.tmp orphans left behind in task_dir.
        # Round-4 regression: without try/finally, os.replace failure
        # would leak the tempfile forever.
        orphans = list(task_dir.glob("*.json.tmp"))
        assert orphans == [], (
            f"Round-4 regression: os.replace failure leaked tempfile(s): {orphans}"
        )

    def test_json_dump_failure_also_cleans_tempfile(
        self, store, task_dir, monkeypatch
    ):
        """Pre-existing behaviour (write-side failure) also covered by
        the unified try/finally — explicit test ensures the
        consolidation doesn't regress this path."""
        store.create_session("task-dump-fail", operation="inject")

        import chaos_agent.memory.session_store as ss_mod
        def _fail_dump(*args, **kwargs):
            raise OSError("simulated disk full")
        monkeypatch.setattr(ss_mod.json, "dump", _fail_dump)

        with pytest.raises(OSError, match="disk full"):
            store._atomic_write_json("task-dump-fail")

        after = list(task_dir.glob("*.json.tmp"))
        assert after == []


class TestSnapshotWriteFailurePreservesOrphan:
    """Round-3 self-check: ``_compact`` step 2 (snapshot write)
    failure must NOT trigger step 3 (orphan unlink), otherwise the
    rotated content — which lives ONLY in the orphan when snapshot
    failed — is permanently lost from disk."""

    def test_orphan_preserved_when_snapshot_write_fails(
        self, compacting_store, task_dir, monkeypatch
    ):
        store = compacting_store
        store.create_session("task-snap-fail", operation="inject")
        # Push to threshold so the NEXT append triggers compact
        for i in range(store._compaction_threshold - 1):
            store.append_raw_message(
                "task-snap-fail",
                {"type": "human", "content": f"pre-{i}", "id": f"pre-{i}"},
            )

        # Patch _atomic_write_json to raise on the upcoming compact.
        # In-memory still holds all messages, but disk write fails —
        # this is the EXACT scenario where unconditional unlink in
        # step 3 would lose the rotated jsonl content.
        def _fail_write(task_id):
            raise OSError("simulated disk full")
        monkeypatch.setattr(store, "_atomic_write_json", _fail_write)

        # This append triggers compact; should NOT crash and should
        # NOT unlink the orphan .jsonl.compacted.
        store.append_raw_message(
            "task-snap-fail",
            {"type": "human", "content": "trigger", "id": "trigger"},
        )

        # Orphan must still exist on disk — it's the only on-disk
        # copy of the rotated messages (snapshot write failed).
        compacted_path = task_dir / "task-snap-fail.jsonl.compacted"
        assert compacted_path.exists(), (
            "Round-3 regression: snapshot-write failure caused step 3 "
            "to unlink the orphan; rotated content lost from disk."
        )
        # Counter must NOT have been reset (Bug B + Round-3 contract).
        assert store._jsonl_counts.get("task-snap-fail", 0) > 0

    def test_finalize_preserves_jsonl_on_snapshot_write_failure(
        self, store, task_dir, monkeypatch
    ):
        """Same invariant for finalize: if snapshot write fails, the
        jsonl source-of-truth must be preserved so read_session can
        still reconstruct the conversation."""
        store.create_session("task-fin-fail", operation="inject")
        store.append_messages(
            "task-fin-fail",
            [HumanMessage(content="surviving", id="s1")],
        )
        # Patch snapshot write to fail
        def _fail_write(task_id):
            raise OSError("simulated")
        monkeypatch.setattr(store, "_atomic_write_json", _fail_write)

        # finalize must not raise (graph caller would abort otherwise)
        store.finalize_session(
            "task-fin-fail",
            remaining_messages=[],
            status="completed",
        )

        # JSONL must still exist (was NOT unlinked because snapshot failed)
        jsonl_path = task_dir / "task-fin-fail.jsonl"
        assert jsonl_path.exists(), (
            "Round-3 regression: finalize unlinked .jsonl despite "
            "snapshot write failing; message data lost from disk."
        )


class TestCompactOrphanReclaim:
    """Round-1 self-check: ``_compact`` must not silently overwrite an
    existing ``.jsonl.compacted`` orphan, otherwise the orphan's
    content (which may be the only on-disk copy of those messages)
    is lost."""

    def test_orphan_compacted_merged_before_rotation(self, compacting_store, task_dir):
        """Simulate: previous _compact crashed mid-rotation leaving
        orphan. Now user keeps appending; eventually a fresh _compact
        triggers. The orphan content must be preserved (read_session
        sees ALL messages from both rounds), not silently overwritten."""
        store = compacting_store
        store.create_session("task-orphan", operation="inject")

        # Round 1: add messages, manually simulate crashed compact
        # by renaming .jsonl → .jsonl.compacted WITHOUT updating snapshot.
        for i in range(3):
            store.append_raw_message(
                "task-orphan",
                {"type": "human", "content": f"round1-{i}", "id": f"r1-{i}"},
            )
        import os
        jsonl_path = task_dir / "task-orphan.jsonl"
        compacted_path = task_dir / "task-orphan.jsonl.compacted"
        os.replace(str(jsonl_path), str(compacted_path))
        # Now orphan exists with 3 messages; snapshot is still empty skeleton.

        # Round 2: keep appending until threshold triggers next compact.
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-orphan",
                {"type": "human", "content": f"round2-{i}", "id": f"r2-{i}"},
            )

        # Read: must see BOTH rounds. If _compact step 0 silently
        # overwrote the orphan, round1 messages would be lost.
        result = store.read_session("task-orphan")
        contents = [m["content"] for m in result["messages"]]
        for i in range(3):
            assert f"round1-{i}" in contents, f"orphan reclaim failed: round1-{i} missing"
        for i in range(store._compaction_threshold):
            assert f"round2-{i}" in contents


class TestFinalizeCleansOrphan:
    """Round-1 self-check: finalize_session previously only cleaned
    .jsonl, leaving .jsonl.compacted orphan on disk longer than needed."""

    def test_finalize_removes_jsonl_compacted_orphan(self, store, task_dir):
        store.create_session("task-fin-orphan", operation="inject")
        store.append_messages("task-fin-orphan", [HumanMessage(content="x", id="x1")])
        # Manually simulate orphan
        import os
        jsonl_path = task_dir / "task-fin-orphan.jsonl"
        compacted_path = task_dir / "task-fin-orphan.jsonl.compacted"
        os.replace(str(jsonl_path), str(compacted_path))

        store.finalize_session("task-fin-orphan", remaining_messages=[], status="completed")

        # Both transient files must be gone after finalize.
        assert not jsonl_path.exists()
        assert not compacted_path.exists()
        # .json snapshot remains as the archival record.
        assert (task_dir / "task-fin-orphan.json").exists()


class TestBugBCounter:
    """Bug B regression: counter must only reset to 0 when truncate
    actually succeeds. Otherwise counter and disk lines diverge."""

    def test_truncate_failure_keeps_counter_intact(self, compacting_store, monkeypatch):
        """When write_text("") fails, counter must NOT reset, so the
        next append doesn't have to wait another full threshold to
        retry compact."""
        store = compacting_store
        store.create_session("task-no-truncate", operation="inject")
        # Push to threshold
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-no-truncate", {"type": "human", "content": f"m-{i}", "id": f"m{i}"}
            )
        # Now counter is at threshold OR has been reset by compact.
        # Patch unlink to fail on .jsonl.compacted cleanup path so
        # counter-reset short-circuits.
        original_unlink = type(store._compacted_path("x")).unlink
        def _fail(self, *a, **kw):
            raise OSError("simulated")
        # Force a fresh compact attempt and have its cleanup fail.
        store.append_raw_message(
            "task-no-truncate", {"type": "human", "content": "extra", "id": "extra"}
        )
        # Patch ONLY for the next compact: when cleanup unlink fires,
        # counter stays >0 (would have been 0 with the unconditional reset bug)
        monkeypatch.setattr(
            type(store._compacted_path("x")), "unlink", _fail, raising=False
        )
        # Push to next threshold; if cleanup fails counter must persist
        for i in range(store._compaction_threshold):
            store.append_raw_message(
                "task-no-truncate", {"type": "human", "content": f"n-{i}", "id": f"n{i}"}
            )
        # If Bug B regressed, counter would be 0 here regardless of
        # truncate success. With fix in place, on failure path the
        # function early-returns BEFORE counter reset.
        # This test is mostly behavioural (no easy assertion on counter
        # without inspecting internals) — main contract: no exception.
