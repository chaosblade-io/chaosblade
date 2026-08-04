"""Config invariant: a detection window smaller than its threshold never fires.

This guards against a repeat of the defect these detectors were just fixed for.
The window used to be counted in MESSAGES (10) while repeats sat 5-8 messages
apart, so it held at most 2 occurrences against ``loop_detection_threshold=3``.
Both detectors were dead for the entire life of that code and nobody noticed —
failing to fire produces no error, just a drill that loops 89 times.

Switching to AI turns fixed the default but left the trap one step away:
``loop_detection_turns=6`` against thresholds of 5 and 3, all env-tunable.
"""

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from chaos_agent.agent.nodes.execute.react_helpers import (
    detect_action_stagnation,
    detect_repeated_tool_calls,
)
from chaos_agent.config.settings import Settings, settings


def _stalled_history(turns: int = 30) -> list:
    """A maximally obvious stall: same call, same output, many turns."""
    msgs: list = []
    for i in range(turns):
        msgs.append(AIMessage(content="", tool_calls=[
            {"name": "kubectl", "args": {"subcommand": "get", "v_args": "nodes"}, "id": f"x{i}"},
        ]))
        msgs.append(ToolMessage(content="unchanged", tool_call_id=f"x{i}"))
    return msgs


class TestInvariantRejectsSilentlyBrokenConfigs:
    """Each of these combinations makes a detector permanently silent."""

    def test_defaults_are_valid(self):
        s = Settings()
        assert s.loop_detection_turns >= max(
            s.loop_detection_threshold,
            s.stagnation_threshold,
            s.stagnation_frequency_ceiling,
        )

    def test_ceiling_is_reachable_on_defaults(self):
        """The ceiling is compared against a streak counted INSIDE the window.

        Shipped defaults had ``turns=6`` against ``ceiling=10``, so the streak
        capped at 6 and the frequency path — the one that fires when output
        comparison cannot — was arithmetically dead. Replaying sess_41dc42aa
        (30 consecutive ``blade_help`` turns) it stayed silent for all 30.
        """
        s = Settings()
        assert s.loop_detection_turns >= s.stagnation_frequency_ceiling

    def test_raising_the_ceiling_past_the_window_is_rejected(self):
        with pytest.raises(Exception) as exc:
            Settings(stagnation_frequency_ceiling=20)
        assert "loop_detection_turns" in str(exc.value)

    def test_shrinking_the_window_below_the_ceiling_is_rejected(self):
        """The exact combination that shipped broken."""
        with pytest.raises(Exception) as exc:
            Settings(loop_detection_turns=6)
        assert "loop_detection_turns" in str(exc.value)

    @pytest.mark.parametrize(
        ("overrides", "why"),
        [
            ({"stagnation_threshold": 13}, "stagnation can never reach 13 in a 12-turn window"),
            ({"loop_detection_threshold": 15}, "repeated can never reach 15 in a 12-turn window"),
            ({"loop_detection_turns": 2}, "a 2-turn window is below every threshold"),
        ],
    )
    def test_window_below_threshold_is_rejected(self, overrides, why):
        with pytest.raises(Exception) as exc:
            Settings(**overrides)
        assert "loop_detection_turns" in str(exc.value), why

    def test_error_names_all_three_values_and_the_fix(self):
        """An operator must be able to act on the message without reading code."""
        with pytest.raises(Exception) as exc:
            Settings(loop_detection_turns=2)
        msg = str(exc.value)
        assert "loop_detection_threshold" in msg
        assert "stagnation_threshold" in msg
        assert "permanently silent" in msg

    def test_equal_window_and_threshold_is_allowed(self):
        """The boundary is inclusive: a streak of exactly N fits an N-turn window."""
        s = Settings(loop_detection_turns=10, stagnation_threshold=10)
        assert s.loop_detection_turns == 10

    @pytest.mark.parametrize("turns", [6, 0, -1])
    def test_message_cap_bound_applies_regardless_of_the_turn_setting(self, turns):
        """The message cap is the OUTER bound — it can mask the turn window.

        Regression: this bound was originally only checked when ``turns <= 0``,
        so ``turns=6, window=4`` passed validation while capping the scan to 2
        turns and silencing BOTH detectors — the same silent failure the
        invariant exists to prevent, reached by a different path.
        """
        with pytest.raises(Exception) as exc:
            Settings(loop_detection_turns=turns, loop_detection_window=4)
        assert "loop_detection_window" in str(exc.value)

    def test_cap_must_hold_the_configured_turn_window(self):
        """Sizing the cap against the THRESHOLD alone was not enough.

        Measured with 9-message turns (a 4-call batch plus its results),
        a 6-turn window with ``window=10`` leaves 2 turns visible and both
        detectors go quiet — yet it satisfied the old ``widest × 2`` bound. The
        cap is now sized against the turn window it has to hold.

        ``stagnation_frequency_ceiling`` is lowered alongside ``turns`` here
        because the ceiling must also fit the window; this case is about the
        MESSAGE cap, so the turn bound is kept satisfied deliberately.
        """
        with pytest.raises(Exception):
            Settings(
                loop_detection_turns=6,
                loop_detection_window=10,
                stagnation_frequency_ceiling=6,
            )
        assert Settings(
            loop_detection_turns=6,
            loop_detection_window=18,
            stagnation_frequency_ceiling=6,
        )

    @pytest.mark.parametrize(
        ("turns", "window", "ceiling"),
        [
            (6, 18, 6),    # turns × typical messages per turn
            (12, 36, 10),  # shipped defaults' turn count
            (0, 30, 10),   # turn window disabled → sized against widest threshold
            (-1, 30, 10),
        ],
    )
    def test_message_cap_bound_is_inclusive(self, turns, window, ceiling):
        s = Settings(
            loop_detection_turns=turns,
            loop_detection_window=window,
            stagnation_frequency_ceiling=ceiling,
        )
        assert s.loop_detection_window == window

    def test_message_cap_bound_scales_with_the_configuration(self):
        """A tighter turn window and lower thresholds legitimately allow less."""
        s = Settings(
            loop_detection_turns=3,
            loop_detection_window=9,
            stagnation_threshold=3,
            loop_detection_threshold=3,
            stagnation_frequency_ceiling=3,
        )
        assert s.loop_detection_window == 9


    @pytest.mark.parametrize("window", [10, 18])
    def test_batched_turns_need_the_wider_cap(self, window):
        """The shape that exposed the gap: 9-message turns, not 2-message ones.

        A batch of 4 calls plus their results is 9 messages, so a 10-message cap
        holds barely 2 turns. The invariant now rejects that pairing; here we
        measure that it really is the point where detection dies.
        """
        history: list = []
        for turn in range(10):
            calls = [
                {"name": "kubectl", "args": {"subcommand": "get", "v_args": f"r{i}"}, "id": f"{turn}_{i}"}
                for i in range(4)
            ]
            history.append(AIMessage(content="", tool_calls=calls))
            for i in range(4):
                history.append(ToolMessage(content="unchanged", tool_call_id=f"{turn}_{i}"))

        original = (settings.loop_detection_turns, settings.loop_detection_window)
        try:
            settings.loop_detection_turns = 6
            settings.loop_detection_window = window
            fired = detect_repeated_tool_calls(history, phase="execute") is not None
        finally:
            settings.loop_detection_turns, settings.loop_detection_window = original

        # window=10 → ~2 turns visible → quiet; window=18 → 6 turns → fires.
        assert fired is (window == 18)


class TestInvariantMatchesActualDetectorBehaviour:
    """The invariant must reject exactly the configs that really go silent."""

    def _probe(self, turns: int, stagnation: int, repeated: int) -> tuple[bool, bool]:
        history = _stalled_history()
        original = (
            settings.loop_detection_turns,
            settings.stagnation_threshold,
            settings.loop_detection_threshold,
        )
        try:
            settings.loop_detection_turns = turns
            settings.stagnation_threshold = stagnation
            settings.loop_detection_threshold = repeated
            return (
                detect_repeated_tool_calls(history, phase="execute") is not None,
                detect_action_stagnation(history, phase="execute")[0] is not None,
            )
        finally:
            (
                settings.loop_detection_turns,
                settings.stagnation_threshold,
                settings.loop_detection_threshold,
            ) = original

    def test_valid_config_fires_on_a_real_stall(self):
        assert self._probe(6, 5, 3) == (True, True)

    def test_stagnation_goes_silent_when_threshold_exceeds_window(self):
        _, stagnation = self._probe(6, 7, 3)
        assert stagnation is False

    def test_repeated_goes_silent_when_threshold_exceeds_window(self):
        repeated, _ = self._probe(6, 5, 8)
        assert repeated is False

    def test_both_go_silent_when_window_is_tiny(self):
        assert self._probe(2, 5, 3) == (False, False)

    @pytest.mark.parametrize("turns", [0, -1])
    def test_exempt_escape_hatch_still_detects(self, turns):
        """Justifies the bound: at the allowed cap both detectors stay alive."""
        assert self._probe(turns, 5, 3) == (True, True)

    @pytest.mark.parametrize(
        ("turns", "window", "expected"),
        [
            (0, 10, (True, True)),   # exactly widest*2 → the invariant's boundary
            (0, 8, (True, False)),   # below it → stagnation goes silent
            (0, 4, (False, False)),  # far below → both go silent
            # Identical behaviour with the turn window ENABLED — the message cap
            # is the outer bound, which is why it is validated unconditionally.
            (6, 10, (True, True)),
            (6, 8, (True, False)),
            (6, 4, (False, False)),
        ],
    )
    def test_message_cap_bound_matches_real_behaviour(self, turns, window, expected):
        """The widest*2 bound must sit exactly where behaviour breaks.

        A bound that is merely plausible would either reject workable configs or
        let silent ones through. These are the measured transition points.
        """
        history = _stalled_history()
        original = (
            settings.loop_detection_turns,
            settings.loop_detection_window,
            settings.stagnation_threshold,
            settings.loop_detection_threshold,
        )
        try:
            settings.loop_detection_turns = turns
            settings.loop_detection_window = window
            settings.stagnation_threshold = 5
            settings.loop_detection_threshold = 3
            actual = (
                detect_repeated_tool_calls(history, phase="execute") is not None,
                detect_action_stagnation(history, phase="execute")[0] is not None,
            )
        finally:
            (
                settings.loop_detection_turns,
                settings.loop_detection_window,
                settings.stagnation_threshold,
                settings.loop_detection_threshold,
            ) = original
        assert actual == expected


class TestCeilingFiresOnDefaults:
    """The invariant is only worth having if the path it guards actually runs.

    A config check that passes while the detector stays silent is the same class
    of defect as the one this file exists to prevent, so this asserts the
    end-to-end behaviour on the SHIPPED defaults rather than a tuned fixture.
    """

    @staticmethod
    def _varying_output_streak(turns: int):
        """Consecutive same-tool turns whose output differs every time.

        This is the sess_41dc42aa shape: help text for a different subcommand
        each turn, so ``_outputs_confirm_stall`` is False and only the frequency
        ceiling can fire.
        """
        msgs = []
        for i in range(turns):
            msgs.append(AIMessage(content="", tool_calls=[
                {"name": "blade_help", "args": {"subcommand": f"cmd{i % 5}"}, "id": f"c{i}"}
            ]))
            msgs.append(ToolMessage(content=f"help variant {i % 5}", tool_call_id=f"c{i}"))
        return msgs

    def _with_defaults(self, monkeypatch):
        from chaos_agent.config.settings import settings

        defaults = Settings()
        current = settings._current()
        for key in (
            "loop_detection_turns",
            "loop_detection_window",
            "loop_detection_threshold",
            "stagnation_threshold",
            "stagnation_frequency_ceiling",
        ):
            monkeypatch.setattr(current, key, getattr(defaults, key), raising=False)
        return defaults

    def test_long_varying_streak_fires_on_defaults(self, monkeypatch):
        from chaos_agent.agent.nodes.execute.react_helpers import (
            detect_action_stagnation,
        )

        self._with_defaults(monkeypatch)
        hint, tool = detect_action_stagnation(
            self._varying_output_streak(30), phase="intent",
        )
        assert hint is not None, "the frequency ceiling must be reachable"
        assert tool == "blade_help"

    def test_streak_below_the_ceiling_stays_silent_on_defaults(self, monkeypatch):
        """Changing output is still the guard until frequency is abnormal."""
        from chaos_agent.agent.nodes.execute.react_helpers import (
            detect_action_stagnation,
        )

        defaults = self._with_defaults(monkeypatch)
        hint, _ = detect_action_stagnation(
            self._varying_output_streak(defaults.stagnation_frequency_ceiling - 1),
            phase="intent",
        )
        assert hint is None
