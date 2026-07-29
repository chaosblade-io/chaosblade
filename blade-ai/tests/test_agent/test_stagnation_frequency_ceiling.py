"""The two loop detectors must fail independently.

``detect_repeated_tool_calls`` answers one question — identical call, identical
result — and it is RIGHT to stay silent when results differ; that is its
false-positive guard and its design boundary. ``detect_action_stagnation`` exists
to cover the blind spot that boundary leaves: frequency, regardless of arguments
or output.

Gating the second one on the same output comparison collapsed both into a single
failure mode. A live metric sample never repeats byte-for-byte, so in
task-c7c75263 the verifier issued the same ``kubectl top`` query 26 times — 9
distinct outputs, CPU drifting 2662-2707m — and neither detector spoke for all
but one stretch of it.

The ceiling comes from measurement, not taste. Across 14 real drills, per-tool
consecutive-turn streaks run median 1 / p90 2 / p95 3. The abnormal cases sit at
8 and 12, and the two 8s turned out to be legitimate exploration with genuinely
different arguments (node, pods, events, sts) — so the ceiling is set above them
at 10, leaving the empty band between p95 and the real stall.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute.react_helpers import detect_action_stagnation
from chaos_agent.config.settings import Settings, settings

JITTER = [
    "[Auto-extracted: CPU usage=2695m]",
    "[Auto-extracted: CPU usage=2702m]",
    "[Auto-extracted: CPU usage=2695m]",
    "[Auto-extracted: CPU usage=2692m]",
    "[Auto-extracted: CPU usage=2703m]",
    "[Auto-extracted: CPU usage=2695m]",
    "[Auto-extracted: CPU usage=2698m]",
    "[Auto-extracted: CPU usage=2701m]",
    "[Auto-extracted: CPU usage=2694m]",
    "[Auto-extracted: CPU usage=2705m]",
    "[Auto-extracted: CPU usage=2696m]",
    "[Auto-extracted: CPU usage=2700m]",
]


def _history(outputs, args=None):
    """One tool call per turn, each answered — the real verifier polling shape."""
    msgs = []
    for i, out in enumerate(outputs):
        a = args[i] if args else {"subcommand": "top", "v_args": "pod p"}
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "kubectl_read", "args": a, "id": f"c{i}"}
        ]))
        msgs.append(ToolMessage(content=out, tool_call_id=f"c{i}"))
    return msgs


@pytest.fixture
def tuned():
    s = settings._current()
    saved = (
        s.loop_detection_turns, s.loop_detection_window,
        s.stagnation_threshold, s.stagnation_frequency_ceiling,
    )
    s.loop_detection_turns = 12
    s.loop_detection_window = 200
    s.stagnation_threshold = 5
    s.stagnation_frequency_ceiling = 10
    yield s
    (s.loop_detection_turns, s.loop_detection_window,
     s.stagnation_threshold, s.stagnation_frequency_ceiling) = saved


class TestFrequencyCeilingSeesThroughJitter:
    def test_the_reported_failure_now_fires(self, tuned):
        """26 identical polls of a drifting metric used to go unreported."""
        hint, tool = detect_action_stagnation(_history(JITTER), phase="verify")
        assert hint is not None
        assert tool == "kubectl_read:top"

    def test_just_below_the_ceiling_stays_silent(self, tuned):
        """Changing output remains the guard until frequency itself is abnormal."""
        hint, _ = detect_action_stagnation(_history(JITTER[:9]), phase="verify")
        assert hint is None

    def test_exactly_at_the_ceiling_fires(self, tuned):
        hint, _ = detect_action_stagnation(_history(JITTER[:10]), phase="verify")
        assert hint is not None

    def test_raising_the_ceiling_restores_silence(self, tuned):
        """The ceiling is the only thing deciding this — not some other rule."""
        tuned.stagnation_frequency_ceiling = 20
        hint, _ = detect_action_stagnation(_history(JITTER), phase="verify")
        assert hint is None


class TestExistingBehaviourPreserved:
    def test_identical_output_still_fires_at_the_low_threshold(self, tuned):
        """The output-based path must keep working below the ceiling."""
        hint, _ = detect_action_stagnation(_history(["same"] * 5), phase="verify")
        assert hint is not None

    def test_progressing_state_below_ceiling_stays_silent(self, tuned):
        """Legitimate polling for a state transition — the 5:1 majority case."""
        outs = ["Running", "Running", "Terminating", "Pending", "Pending"]
        hint, _ = detect_action_stagnation(_history(outs), phase="verify")
        assert hint is None

    def test_streak_below_threshold_stays_silent(self, tuned):
        hint, _ = detect_action_stagnation(_history(["same"] * 4), phase="verify")
        assert hint is None

    def test_ceiling_does_not_override_the_streak_threshold(self, tuned):
        """A ceiling of 1 must not make every single call a stall."""
        tuned.stagnation_frequency_ceiling = 1
        hint, _ = detect_action_stagnation(_history(JITTER[:3]), phase="verify")
        assert hint is None, "stagnation_threshold is still the entry gate"

    def test_missing_outputs_below_ceiling_stay_silent(self, tuned):
        """No evidence either way — silence is the documented choice."""
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "kubectl_read", "args": {"subcommand": "top"}, "id": f"n{i}"}
            ])
            for i in range(5)
        ]
        hint, _ = detect_action_stagnation(msgs, phase="verify")
        assert hint is None

    def test_missing_outputs_above_ceiling_still_fire(self, tuned):
        """Above the ceiling the streak IS the evidence, so output is irrelevant."""
        msgs = [
            AIMessage(content="", tool_calls=[
                {"name": "kubectl_read", "args": {"subcommand": "top"}, "id": f"n{i}"}
            ])
            for i in range(11)
        ]
        hint, _ = detect_action_stagnation(msgs, phase="verify")
        assert hint is not None


class TestCeilingConfiguration:
    def test_default_sits_above_the_measured_p95(self):
        """p95 of real per-tool streaks is 3; legitimate exploration reached 8."""
        assert Settings().stagnation_frequency_ceiling == 10

    def test_ceiling_is_settable_as_an_int(self):
        from chaos_agent.config.config_store import _INT_KEYS
        assert "stagnation_frequency_ceiling" in _INT_KEYS


class TestHintStatesOnlyWhatIsTrue:
    """A hint the model can disprove at a glance is worse than no hint.

    It teaches the model that these warnings are noise, and it invites it to
    defend a correct action instead of reconsidering the call count — the one
    thing that actually is abnormal on the frequency path.
    """

    def test_frequency_path_does_not_claim_unchanged_results(self, tuned):
        hint, _ = detect_action_stagnation(_history(JITTER), phase="verify")
        assert "came back unchanged" not in hint
        assert "repeating it unchanged" not in hint

    def test_frequency_path_acknowledges_the_readings_differ(self, tuned):
        """The model can see 2695m → 2702m; the hint must not contradict it."""
        hint, _ = detect_action_stagnation(_history(JITTER), phase="verify")
        assert "readings do differ" in hint
        assert "same observation, not new evidence" in hint

    def test_identical_path_still_states_results_matched(self, tuned):
        """That claim is true on this path, and it is the stronger argument."""
        hint, _ = detect_action_stagnation(_history(["same"] * 5), phase="verify")
        assert "came back unchanged" in hint

    def test_both_paths_stay_rebuttable(self, tuned):
        """Detection can be wrong — never strip a correct model of a tool."""
        for outputs in (JITTER, ["same"] * 5):
            hint, _ = detect_action_stagnation(_history(outputs), phase="verify")
            assert "genuinely required here" in hint
            assert "say why and continue" in hint

    def test_no_hard_prohibition_in_any_phase_body(self):
        """Guards every phase's canned body, not just the one under test."""
        import re

        from chaos_agent.agent.nodes.execute.react_helpers import (
            _LOOP_HINTS,
            _STAGNATION_HINTS,
        )

        banned = re.compile(r"(?i)\b(do not|don't|must not|never)\s+(call|use|repeat|retry)")
        for name, table in (("loop", _LOOP_HINTS), ("stagnation", _STAGNATION_HINTS)):
            for phase, text in table.items():
                assert not banned.search(text), f"{name}/{phase} issues an order"
