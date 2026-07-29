"""Tests for real-time agent status tracking."""

import asyncio

import pytest

from chaos_agent.observability.status_tracker import (
    StatusCategory,
    StatusEvent,
    StatusPhase,
    StatusTracker,
    get_tracker,
    remove_tracker,
    subscribe,
    unsubscribe,
    track_status,
)


class TestStatusEvent:
    """Test StatusEvent dataclass."""

    def test_auto_timestamp(self):
        event = StatusEvent(
            task_id="t1",
            phase=StatusPhase.STARTED,
            category=StatusCategory.NODE,
            source="agent_loop",
            message="Starting...",
        )
        assert event.timestamp > 0

    def test_to_dict(self):
        event = StatusEvent(
            task_id="t1",
            phase=StatusPhase.COMPLETED,
            category=StatusCategory.TOOL,
            source="blade_create",
            message="Done",
            timestamp=1000.0,
            duration_ms=500.0,
            detail={"exit_code": 0},
        )
        d = event.to_dict()
        assert d["task_id"] == "t1"
        assert d["phase"] == StatusPhase.COMPLETED
        assert d["category"] == StatusCategory.TOOL
        assert d["source"] == "blade_create"
        assert d["duration_ms"] == 500.0
        assert d["detail"]["exit_code"] == 0


class TestStatusTracker:
    """Test StatusTracker core functionality."""

    def test_subscribe_returns_queue(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()
        assert isinstance(q, asyncio.Queue)

    def test_emit_delivers_to_subscriber(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()
        event = StatusEvent(
            task_id="t1",
            phase=StatusPhase.STARTED,
            category=StatusCategory.NODE,
            source="test",
            message="hello",
        )
        tracker.emit(event)
        received = q.get_nowait()
        assert received.task_id == "t1"
        assert received.message == "hello"

    def test_emit_to_multiple_subscribers(self):
        tracker = StatusTracker("t1")
        q1 = tracker.subscribe()
        q2 = tracker.subscribe()
        event = StatusEvent(
            task_id="t1",
            phase=StatusPhase.STARTED,
            category=StatusCategory.NODE,
            source="test",
            message="fan-out",
        )
        tracker.emit(event)
        assert q1.get_nowait().message == "fan-out"
        assert q2.get_nowait().message == "fan-out"

    def test_unsubscribe_removes_queue(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()
        tracker.unsubscribe(q)
        assert q not in tracker._subscribers

    def test_emit_drops_on_full_queue(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe(maxsize=1)
        event = StatusEvent(
            task_id="t1",
            phase=StatusPhase.STARTED,
            category=StatusCategory.NODE,
            source="test",
            message="first",
        )
        tracker.emit(event)  # fills queue
        # Second emit should not raise, just log warning
        tracker.emit(StatusEvent(
            task_id="t1",
            phase=StatusPhase.RUNNING,
            category=StatusCategory.NODE,
            source="test",
            message="dropped",
        ))

    def test_start_complete_lifecycle(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()

        tracker.start(StatusCategory.NODE, "agent_loop", "Planning...")
        start_event = q.get_nowait()
        assert start_event.phase == StatusPhase.STARTED
        assert start_event.source == "agent_loop"

        tracker.complete("Done planning")
        complete_event = q.get_nowait()
        assert complete_event.phase == StatusPhase.COMPLETED
        assert complete_event.duration_ms >= 0

    def test_start_fail_lifecycle(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()

        tracker.start(StatusCategory.NODE, "safety_check", "Checking...")
        tracker.fail("Namespace blacklisted")
        fail_event = q.get_nowait()  # skip started
        fail_event = q.get_nowait()
        assert fail_event.phase == StatusPhase.FAILED
        assert "blacklisted" in fail_event.message

    def test_update_emits_running_event(self):
        tracker = StatusTracker("t1")
        q = tracker.subscribe()

        tracker.start(StatusCategory.NODE, "agent_loop", "Thinking...")
        q.get_nowait()  # consume started

        tracker.update("Still thinking...")
        running_event = q.get_nowait()
        assert running_event.phase == StatusPhase.RUNNING
        assert "Still thinking" in running_event.message

    def test_get_history(self):
        tracker = StatusTracker("t1")
        tracker.start(StatusCategory.NODE, "n1", "start")
        tracker.complete("done")
        history = tracker.get_history()
        assert len(history) == 2
        assert history[0]["phase"] == StatusPhase.STARTED
        assert history[1]["phase"] == StatusPhase.COMPLETED


class TestGlobalRegistry:
    """Test global tracker registry functions."""

    def setup_method(self):
        # Clean up any leftover trackers
        remove_tracker("task-test-global")

    def test_get_tracker_creates_new(self):
        tracker = get_tracker("task-test-global")
        assert isinstance(tracker, StatusTracker)
        assert tracker.task_id == "task-test-global"

    def test_get_tracker_returns_same(self):
        t1 = get_tracker("task-test-global")
        t2 = get_tracker("task-test-global")
        assert t1 is t2

    def test_remove_tracker(self):
        get_tracker("task-test-global")
        remove_tracker("task-test-global")
        # After removal, a new tracker should be created
        new_tracker = get_tracker("task-test-global")
        assert new_tracker is not None

    def test_subscribe_convenience(self):
        q = subscribe("task-test-global")
        assert isinstance(q, asyncio.Queue)
        unsubscribe("task-test-global", q)

    def test_unsubscribe_convenience(self):
        q = subscribe("task-test-global")
        unsubscribe("task-test-global", q)
        tracker = get_tracker("task-test-global")
        assert q not in tracker._subscribers


class TestTrackerEligibilityContract:
    """Pin the two-concept split enforced by ``is_event_channel_id``.

    ``task-*`` is a persistable task identity; ``tui-*`` is a live UI event
    channel that must ALSO get a working tracker (the TS TUI's /turn stream
    subscribes to it and PreReasoningHook fans compaction events out to it).
    Everything else gets the NullTracker so dialogue turns can call
    ``tracker.start(...)`` without fabricating task state. Regression guard:
    tightening the gate to ``is_real_task_id`` alone silently killed the TUI
    fan-out while every assertion below still looked plausible.
    """

    def test_task_prefix_gets_real_tracker(self):
        tracker = get_tracker("task-eligible")
        tracker.start(StatusCategory.NODE, "n", "m")
        assert tracker.task_id == "task-eligible"
        assert len(tracker.get_history()) == 1
        remove_tracker("task-eligible")

    @pytest.mark.parametrize("channel_id", ["tui-sess-abc", "compact-deadbeef1234"])
    def test_ephemeral_channel_prefixes_get_real_tracker(self, channel_id):
        """``tui-``/``compact-`` are event channels, not tasks — still real.

        ``tui-<sid>`` backs the TS TUI's /turn stream; ``compact-<uuid>``
        backs the /compact progress stream (the route mints the id and
        overrides ``state.task_id`` with it). A NullTracker here means the
        consumer's SSE receives nothing but keepalives.
        """
        tracker = get_tracker(channel_id)
        tracker.start(StatusCategory.NODE, "n", "m")
        assert tracker.task_id == channel_id
        assert len(tracker.get_history()) == 1
        remove_tracker(channel_id)

    @pytest.mark.parametrize("bad_id", ["", "unknown", "turn-abc", "chaos-thread", None])
    def test_other_ids_get_null_tracker(self, bad_id):
        tracker = get_tracker(bad_id)
        tracker.start(StatusCategory.NODE, "n", "m")
        tracker.complete("done")
        assert tracker.get_history() == [], "NullTracker must record nothing"
        assert tracker.task_id == ""

    def test_history_is_bounded(self):
        """History is capped so never-removed ``tui-*`` trackers can't leak."""
        from chaos_agent.observability.status_tracker import _HISTORY_MAXLEN

        tracker = get_tracker("task-bounded")
        for i in range(_HISTORY_MAXLEN + 50):
            tracker.update(f"tick {i}")
        assert len(tracker.get_history()) == _HISTORY_MAXLEN
        remove_tracker("task-bounded")


class TestTrackStatusContextManager:
    """Test the track_status async context manager."""

    @pytest.mark.asyncio
    async def test_emits_start_and_complete(self):
        remove_tracker("task-ctx-test")
        q = subscribe("task-ctx-test")

        async with track_status("task-ctx-test", "test_node", "Working...") as tracker:
            pass

        start_event = q.get_nowait()
        assert start_event.phase == StatusPhase.STARTED
        assert start_event.source == "test_node"

        complete_event = q.get_nowait()
        assert complete_event.phase == StatusPhase.COMPLETED

        unsubscribe("task-ctx-test", q)
        remove_tracker("task-ctx-test")

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception(self):
        remove_tracker("task-ctx-test-fail")
        q = subscribe("task-ctx-test-fail")

        with pytest.raises(ValueError, match="boom"):
            async with track_status("task-ctx-test-fail", "failing_node", "Will fail"):
                raise ValueError("boom")

        q.get_nowait()  # skip started
        fail_event = q.get_nowait()
        assert fail_event.phase == StatusPhase.FAILED
        assert "boom" in fail_event.message

        unsubscribe("task-ctx-test-fail", q)
        remove_tracker("task-ctx-test-fail")

    @pytest.mark.asyncio
    async def test_update_within_context(self):
        remove_tracker("task-ctx-test-update")
        q = subscribe("task-ctx-test-update")

        async with track_status("task-ctx-test-update", "node", "Starting") as tracker:
            tracker.update("Midway update")

        q.get_nowait()  # skip started
        running_event = q.get_nowait()
        assert running_event.phase == StatusPhase.RUNNING
        assert "Midway" in running_event.message

        q.get_nowait()  # complete event

        unsubscribe("task-ctx-test-update", q)
        remove_tracker("task-ctx-test-update")


class TestStatusCategories:
    """Test that status events correctly categorize sources."""

    def test_node_category(self):
        event = StatusEvent(
            task_id="t1", phase=StatusPhase.STARTED,
            category=StatusCategory.NODE, source="agent_loop", message="test",
        )
        assert event.category == StatusCategory.NODE

    def test_tool_category(self):
        event = StatusEvent(
            task_id="t1", phase=StatusPhase.STARTED,
            category=StatusCategory.TOOL, source="blade_create", message="test",
        )
        assert event.category == StatusCategory.TOOL

    def test_llm_category(self):
        event = StatusEvent(
            task_id="t1", phase=StatusPhase.STARTED,
            category=StatusCategory.LLM, source="chat_model", message="test",
        )
        assert event.category == StatusCategory.LLM

    def test_system_category(self):
        event = StatusEvent(
            task_id="t1", phase=StatusPhase.STARTED,
            category=StatusCategory.SYSTEM, source="init", message="test",
        )
        assert event.category == StatusCategory.SYSTEM
