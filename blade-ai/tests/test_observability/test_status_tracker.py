"""Tests for real-time agent status tracking."""

import asyncio

import pytest

from chaos_agent.observability.status_tracker import (
    NullTracker,
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
        tracker.subscribe(maxsize=1)
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

        async with track_status("task-ctx-test", "test_node", "Working..."):
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


class TestInterventionChannel:
    """The framework-intervention fact must reach a surface an auditor reads.

    Case #64: the contract pin rewrote the plan's 300s into the approved
    420s and recorded it on the logger and the session ledger. The run was
    launched through ``inject --stream``, which renders tracker events only
    — so the rewrite was invisible, and a CORRECT pin was audited as an
    unexplained drift. ``SYSTEM`` had been legislated as the one category
    that survives non-debug rendering (see ``cli.status_display``) but had
    no producer at all until ``intervention`` existed. These tests pin both
    the channel and the two drop rules that make it necessary.
    """

    @staticmethod
    def _render(event, monkeypatch, *, debug: bool):
        from chaos_agent.cli.status_display import format_status_event
        from chaos_agent.config.settings import settings
        # ``is_debug`` is a derived property (log_level == "DEBUG") with no
        # setter, so the underlying field is what gets patched. The suite runs
        # at DEBUG by default — without this the drop rules under test would
        # never fire and every assertion below would pass vacuously.
        monkeypatch.setattr(settings, "log_level", "DEBUG" if debug else "INFO")
        assert settings.is_debug is debug
        return format_status_event(event)

    def _one(self):
        tracker = StatusTracker(task_id="t1")
        tracker.intervention(
            "pin",
            "[contract] blade_create: blade --timeout 300 -> 420s",
            {"tool": "blade_create", "before": "blade --timeout 300",
             "after": 420, "window": 300},
        )
        assert len(tracker._history) == 1
        return tracker._history[0]

    def test_lands_on_the_system_channel_with_the_facts(self):
        event = self._one()
        assert event.category == StatusCategory.SYSTEM
        assert event.phase == StatusPhase.RUNNING
        assert event.detail["intervention"] == "pin"
        assert event.detail["before"] == "blade --timeout 300"
        assert event.detail["after"] == 420

    def test_survives_non_debug_rendering(self, monkeypatch):
        event = self._one()
        assert self._render(event, monkeypatch, debug=False) != ""

    def test_update_would_have_been_dropped(self, monkeypatch):
        # The counterfactual that makes SYSTEM load-bearing: the identical
        # message sent through ``update`` (category NODE — the route the
        # ``[FCAT P0]`` precedent uses) renders to nothing outside debug
        # mode. Copying that precedent would have reproduced the gap.
        tracker = StatusTracker(task_id="t1")
        tracker.update("[contract] blade_create: 300 -> 420s")
        assert self._render(tracker._history[0], monkeypatch, debug=False) == ""

    def test_debug_marker_would_have_been_dropped(self, monkeypatch):
        # The second drop rule: ``detail["debug"]`` is discarded non-debug
        # regardless of category. An audit fact is not a debug aid, so
        # ``intervention`` must not set the marker.
        event = self._one()
        assert "debug" not in event.detail
        event.detail["debug"] = True
        assert self._render(event, monkeypatch, debug=False) == ""

    def test_is_a_point_event_with_its_own_timestamp(self):
        event = self._one()
        # A rewrite is instantaneous: no span, so the renderer omits a
        # duration. The wall-clock stamp is the fact's own time.
        assert event.duration_ms == 0
        assert event.timestamp > 0

    def test_null_tracker_swallows_it(self):
        # Dialogue turns run the same nodes with no task identity; an
        # intervention there must not fabricate a history entry.
        tracker = NullTracker()
        tracker.intervention("pin", "rewritten", {"after": 420})
        assert len(tracker._history) == 0


class TestSpanSegmentation:
    """``mark`` — sub-span breakdown inside one node span.

    A node emits ONE cumulative number while its message names one phase, so
    ``Iteration 1 LLM response: (71751ms)`` reads as "the model took 71.7s".
    inject-6ebf341c turn 1 was audited exactly that way and the 67s could be
    neither confirmed nor refuted: no layer held a breakdown. These tests pin
    the three properties that make the number falsifiable — labels in call
    order, parts that sum to the whole, and a renderer that says what it is
    showing.
    """

    def _tracker(self):
        tracker = StatusTracker("t1")
        tracker.start(StatusCategory.NODE, "agent_loop", "iteration 1")
        return tracker

    @staticmethod
    def _labels(event):
        return [s["label"] for s in event.detail["segments"]]

    def test_mark_records_labels_in_call_order(self):
        tracker = self._tracker()
        tracker.mark("hook")
        tracker.mark("prompt-build")
        tracker.mark("llm-call")
        tracker.update("Iteration 1 LLM response:")
        assert self._labels(tracker._history[-1]) == [
            "hook", "prompt-build", "llm-call", "other",
        ]

    def test_segments_measure_their_own_intervals(self):
        import time

        tracker = self._tracker()
        time.sleep(0.02)
        tracker.mark("hook")
        tracker.mark("llm-call")
        tracker.update("x")
        segs = {s["label"]: s["ms"] for s in tracker._history[-1].detail["segments"]}
        # The slept interval is attributed to the phase it was slept in, not
        # smeared across the span.
        assert segs["hook"] >= 15
        assert segs["llm-call"] < 15

    def test_parts_sum_to_the_whole(self):
        import time

        tracker = self._tracker()
        time.sleep(0.02)
        tracker.mark("hook")
        tracker.update("x")
        event = tracker._history[-1]
        total = sum(s["ms"] for s in event.detail["segments"])
        # ``other`` exists so an un-instrumented remainder can never be
        # silently dropped from the breakdown — a sum that looks complete but
        # is not would re-create the mis-attribution this closes.
        assert abs(total - event.duration_ms) < 5

    def test_no_segments_until_something_is_marked(self):
        tracker = self._tracker()
        tracker.update("x")
        tracker.complete("done")
        # Un-instrumented producers keep their exact previous payload.
        assert all("segments" not in e.detail for e in tracker._history)

    def test_start_resets_previous_segments(self):
        tracker = self._tracker()
        tracker.mark("hook")
        tracker.start(StatusCategory.NODE, "agent_loop", "iteration 2")
        tracker.mark("llm-call")
        tracker.update("x")
        # A new span must not inherit the previous one's breakdown.
        assert self._labels(tracker._history[-1]) == ["llm-call", "other"]

    def test_complete_logs_the_breakdown(self, caplog):
        import logging

        tracker = self._tracker()
        tracker.mark("hook")
        tracker.mark("llm-call")
        with caplog.at_level(logging.INFO, logger="chaos_agent.observability.status_tracker"):
            tracker.complete("iteration 1 done")
        # Events reach a live subscriber or nothing; the log line is what makes
        # a slow turn in an unwatched run answerable afterwards.
        assert "span breakdown" in caplog.text
        assert "hook=" in caplog.text and "llm-call=" in caplog.text

    def test_save_restore_keeps_the_parents_segments(self):
        tracker = self._tracker()
        tracker.mark("hook")
        saved = tracker.save_state()
        # A sub-operation takes over the tracker and clears its bookkeeping.
        tracker.start(StatusCategory.NODE, "conflict_check", "checking")
        tracker.mark("kubectl")
        tracker.restore_state(saved)
        tracker.complete("iteration 1 done")
        assert self._labels(tracker._history[-1]) == ["hook", "other"]

    def test_renderer_states_what_the_number_measures(self, monkeypatch):
        from chaos_agent.cli.status_display import format_status_event
        from chaos_agent.config.settings import settings

        tracker = self._tracker()
        tracker.mark("hook")
        tracker.mark("llm-call")
        tracker.update("Iteration 1 LLM response:")
        monkeypatch.setattr(settings, "log_level", "DEBUG")
        line = format_status_event(tracker._history[-1])
        # The cumulative figure is labelled as the NODE's, and the phase split
        # sits beside it — the reading "LLM response took <node total>" is no
        # longer the only one available.
        assert "(node " in line
        assert "llm-call=" in line
        assert "segments" not in line.split("→ detail:")[-1]

    def test_null_tracker_mark_is_inert(self):
        tracker = NullTracker()
        tracker.mark("hook")
        tracker.update("x")
        assert tracker._segments == []
        assert len(tracker._history) == 0

