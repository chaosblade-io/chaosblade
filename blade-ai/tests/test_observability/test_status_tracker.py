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
        remove_tracker("test-global")

    def test_get_tracker_creates_new(self):
        tracker = get_tracker("test-global")
        assert isinstance(tracker, StatusTracker)
        assert tracker.task_id == "test-global"

    def test_get_tracker_returns_same(self):
        t1 = get_tracker("test-global")
        t2 = get_tracker("test-global")
        assert t1 is t2

    def test_remove_tracker(self):
        get_tracker("test-global")
        remove_tracker("test-global")
        # After removal, a new tracker should be created
        new_tracker = get_tracker("test-global")
        assert new_tracker is not None

    def test_subscribe_convenience(self):
        q = subscribe("test-global")
        assert isinstance(q, asyncio.Queue)
        unsubscribe("test-global", q)

    def test_unsubscribe_convenience(self):
        q = subscribe("test-global")
        unsubscribe("test-global", q)
        tracker = get_tracker("test-global")
        assert q not in tracker._subscribers


class TestTrackStatusContextManager:
    """Test the track_status async context manager."""

    @pytest.mark.asyncio
    async def test_emits_start_and_complete(self):
        remove_tracker("ctx-test")
        q = subscribe("ctx-test")

        async with track_status("ctx-test", "test_node", "Working...") as tracker:
            pass

        start_event = q.get_nowait()
        assert start_event.phase == StatusPhase.STARTED
        assert start_event.source == "test_node"

        complete_event = q.get_nowait()
        assert complete_event.phase == StatusPhase.COMPLETED

        unsubscribe("ctx-test", q)
        remove_tracker("ctx-test")

    @pytest.mark.asyncio
    async def test_emits_failed_on_exception(self):
        remove_tracker("ctx-test-fail")
        q = subscribe("ctx-test-fail")

        with pytest.raises(ValueError, match="boom"):
            async with track_status("ctx-test-fail", "failing_node", "Will fail"):
                raise ValueError("boom")

        q.get_nowait()  # skip started
        fail_event = q.get_nowait()
        assert fail_event.phase == StatusPhase.FAILED
        assert "boom" in fail_event.message

        unsubscribe("ctx-test-fail", q)
        remove_tracker("ctx-test-fail")

    @pytest.mark.asyncio
    async def test_update_within_context(self):
        remove_tracker("ctx-test-update")
        q = subscribe("ctx-test-update")

        async with track_status("ctx-test-update", "node", "Starting") as tracker:
            tracker.update("Midway update")

        q.get_nowait()  # skip started
        running_event = q.get_nowait()
        assert running_event.phase == StatusPhase.RUNNING
        assert "Midway" in running_event.message

        q.get_nowait()  # complete event

        unsubscribe("ctx-test-update", q)
        remove_tracker("ctx-test-update")


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
