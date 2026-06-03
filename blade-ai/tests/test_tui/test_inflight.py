"""Tests for PR-E5 — the in-flight tracker that drives event-coupled spinner verbs.

The tracker is a small state machine fed by ``Renderer.dispatch``.
Five behaviours pinned:

1. ToolStarted increments the counter; ToolCompleted decrements it.
   Sequential pairs balance to zero.
2. The counter never goes negative — orphan tool_end events (rare,
   but possible during a streaming abort) shouldn't push it past zero.
3. TaskResult / TaskError reset the whole tracker so the next turn
   starts clean.
4. ``verb_hint()`` returns the right phrase for each state and ``None``
   when nothing concrete is in flight (so the caller's random verb
   pool can take over).
5. PhaseChanged with "Starting <node>" updates current_phase, but the
   matching "Completed" doesn't — the label survives until the next
   phase begins. Matches the stepper's contract.
"""

from __future__ import annotations

from chaos_agent.tui.events import (
    PhaseChanged,
    TaskError,
    TaskResult,
    ThinkingReceived,
    TokenReceived,
    ToolCompleted,
    ToolStarted,
)
from chaos_agent.tui.inflight import InFlightTracker


class TestCounter:
    def test_start_increments_complete_decrements(self):
        t = InFlightTracker()
        assert t.tool_count == 0
        t.on_event(ToolStarted(tool_name="kubectl"))
        assert t.tool_count == 1
        t.on_event(ToolCompleted(tool_name="kubectl"))
        assert t.tool_count == 0

    def test_two_starts_then_two_completes(self):
        # Models PR-E7's future parallel calls — counter goes to 2,
        # then back to 0 cleanly.
        t = InFlightTracker()
        t.on_event(ToolStarted(tool_name="kubectl"))
        t.on_event(ToolStarted(tool_name="curl"))
        assert t.tool_count == 2
        t.on_event(ToolCompleted(tool_name="kubectl"))
        assert t.tool_count == 1
        t.on_event(ToolCompleted(tool_name="curl"))
        assert t.tool_count == 0

    def test_orphan_complete_clamps_at_zero(self):
        # A tool_end without a preceding tool_start (e.g. recorder
        # replay starting mid-stream) must not push the counter
        # negative — that'd corrupt every later increment.
        t = InFlightTracker()
        t.on_event(ToolCompleted(tool_name="kubectl"))
        assert t.tool_count == 0


class TestStreamingThinkingFlags:
    def test_token_marks_streaming_clears_thinking(self):
        t = InFlightTracker()
        t.on_event(ThinkingReceived(content="weighing"))
        assert t.is_thinking
        t.on_event(TokenReceived(content="answer"))
        # Streaming and thinking are mutually exclusive — once tokens
        # land, the model has stopped reasoning.
        assert t.is_streaming
        assert not t.is_thinking

    def test_tool_start_clears_streaming_and_thinking(self):
        # When a tool fires, the LLM yielded — neither stream nor
        # think state is true any more.
        t = InFlightTracker()
        t.on_event(TokenReceived(content="x"))
        t.on_event(ToolStarted(tool_name="kubectl"))
        assert not t.is_streaming
        assert not t.is_thinking


class TestPhaseChange:
    def test_starting_message_sets_current_phase(self):
        t = InFlightTracker()
        t.on_event(PhaseChanged(source="baseline_capture", message="Starting baseline_capture"))
        assert t.current_phase == "baseline_capture"

    def test_completed_message_does_not_clear_phase(self):
        # The phase label should persist until the NEXT phase starts —
        # matches the stepper. A bare "Completed" event is not a phase
        # boundary in itself.
        t = InFlightTracker()
        t.on_event(PhaseChanged(source="baseline_capture", message="Starting baseline_capture"))
        t.on_event(PhaseChanged(source="baseline_capture", message="Completed baseline_capture"))
        assert t.current_phase == "baseline_capture"
        t.on_event(PhaseChanged(source="agent_loop", message="Starting agent_loop"))
        assert t.current_phase == "agent_loop"


class TestReset:
    def test_task_result_resets_tracker(self):
        t = InFlightTracker()
        t.on_event(ToolStarted(tool_name="kubectl"))
        t.on_event(TokenReceived(content="x"))
        t.on_event(TaskResult(data={}, task_id="T-1"))
        assert t.tool_count == 0
        assert not t.is_streaming
        assert not t.is_thinking
        assert t.current_tool == ""
        assert t.current_phase == ""

    def test_task_error_resets_tracker(self):
        t = InFlightTracker()
        t.on_event(ToolStarted(tool_name="kubectl"))
        t.on_event(TaskError(message="boom", task_id="T-2"))
        assert t.tool_count == 0


class TestVerbHint:
    def test_no_state_returns_none(self):
        # Idle tracker → fall back to the random verb pool.
        assert InFlightTracker().verb_hint() is None

    def test_single_tool_uses_tool_name(self):
        t = InFlightTracker()
        t.on_event(ToolStarted(tool_name="kubectl"))
        hint = t.verb_hint()
        assert hint is not None
        assert "kubectl" in hint
        # Localised verb prefix is present.
        assert "\u8c03\u7528" in hint  # 调用

    def test_multiple_tools_uses_count_phrase(self):
        t = InFlightTracker()
        t.on_event(ToolStarted(tool_name="kubectl"))
        t.on_event(ToolStarted(tool_name="curl"))
        hint = t.verb_hint()
        assert hint is not None
        # Includes the number of tools, not their individual names.
        assert "2" in hint

    def test_streaming_returns_streaming_hint(self):
        t = InFlightTracker()
        t.on_event(TokenReceived(content="x"))
        hint = t.verb_hint()
        assert hint is not None
        # 生成回复
        assert "\u751f\u6210" in hint

    def test_thinking_alone_returns_none(self):
        # Thinking is the *default* spinner state — return None so the
        # caller's random verb pool gives flavour, instead of pinning a
        # boring "思考" forever.
        t = InFlightTracker()
        t.on_event(ThinkingReceived(content="weighing"))
        assert t.verb_hint() is None
