"""Tests for PR-E9 — the JIT hint engine.

Behaviour pinned:

1. Each hint fires AT MOST once per session — banner blindness is the
   failure mode we're avoiding, so a second occurrence of the same
   trigger must return None.
2. Calm mode silences every hint (the user opted out of decorative
   output via /mode calm or Ctrl-G). The streak / fired-bookkeeping
   still advances under the hood, but nothing surfaces.
3. The chat-streak hint fires only after CHAT_STREAK_THRESHOLD
   consecutive non-injection turns. An injection in the middle resets
   the streak so a later chat-only spell can still trip the hint.
4. The first-locator hint embeds the locator label so the user can
   copy /show E1 verbatim instead of guessing.
5. on_injection_turn never returns a hint — it's purely streak
   bookkeeping. The hint surface is on_chat_turn.
"""

from __future__ import annotations

from chaos_agent.tui.hints import JITHintEngine
from chaos_agent.tui.state import DisplayMode, SessionState


def _engine(mode: DisplayMode = DisplayMode.WORKING) -> tuple[JITHintEngine, SessionState]:
    state = SessionState()
    state.display_mode = mode
    return JITHintEngine(state), state


class TestOneShot:
    def test_first_error_fires_once(self):
        engine, _ = _engine()
        assert engine.on_first_error() is not None
        # Second call returns None — the user has already seen the tip.
        assert engine.on_first_error() is None

    def test_first_locator_fires_once_regardless_of_label(self):
        engine, _ = _engine()
        assert engine.on_first_locator("E1") is not None
        # Even with a different label, a second invocation must not fire —
        # the lesson ("/show works") is taught once.
        assert engine.on_first_locator("E2") is None

    def test_display_mode_cycle_fires_once(self):
        engine, _ = _engine()
        assert engine.on_display_mode_cycled() is not None
        assert engine.on_display_mode_cycled() is None

    def test_chat_streak_example_fires_once(self):
        engine, _ = _engine()
        results = [engine.on_chat_turn() for _ in range(6)]
        non_none = [r for r in results if r is not None]
        # Only one hint emerges from six chat turns.
        assert len(non_none) == 1


class TestCalmModeSilences:
    def test_calm_mode_suppresses_every_hint(self):
        engine, _ = _engine(mode=DisplayMode.CALM)
        assert engine.on_first_error() is None
        assert engine.on_first_locator("E1") is None
        assert engine.on_display_mode_cycled() is None
        for _ in range(10):
            assert engine.on_chat_turn() is None

    def test_calm_mode_still_tracks_state_under_the_hood(self):
        # If a user starts in calm and switches to working mid-session,
        # we don't want to "reset" their streak count silently — the
        # accumulated 3 chat turns should still be there. Verify by
        # cycling 3 chat turns in calm, switching to working, and
        # confirming the very next chat turn DOES trip the hint.
        engine, state = _engine(mode=DisplayMode.CALM)
        for _ in range(3):
            assert engine.on_chat_turn() is None
        state.display_mode = DisplayMode.WORKING
        # Next chat turn — streak now at 4, threshold tripped, hint fires.
        assert engine.on_chat_turn() is not None


class TestChatStreakSemantics:
    def test_threshold_not_tripped_under_threshold(self):
        engine, _ = _engine()
        # Threshold is 3 — first two turns return None, third fires.
        assert engine.on_chat_turn() is None
        assert engine.on_chat_turn() is None
        assert engine.on_chat_turn() is not None

    def test_injection_resets_streak(self):
        engine, _ = _engine()
        engine.on_chat_turn()
        engine.on_chat_turn()
        engine.on_injection_turn()  # streak reset
        # Now we need three more chat turns before the hint can fire.
        # But it's a one-shot, so even after this it can fire once.
        assert engine.on_chat_turn() is None
        assert engine.on_chat_turn() is None
        assert engine.on_chat_turn() is not None

    def test_one_shot_holds_across_streak_resets(self):
        # After firing once, even a fresh streak must not re-fire it.
        engine, _ = _engine()
        for _ in range(3):
            engine.on_chat_turn()
        engine.on_injection_turn()
        for _ in range(10):
            assert engine.on_chat_turn() is None


class TestLocatorEmbedding:
    def test_label_is_in_returned_hint(self):
        engine, _ = _engine()
        hint = engine.on_first_locator("E1")
        assert hint is not None
        assert "E1" in hint

    def test_t_label_is_in_returned_hint(self):
        engine, _ = _engine()
        hint = engine.on_first_locator("T7")
        assert hint is not None
        assert "T7" in hint


class TestInjectionTurnIsBookkeeping:
    def test_injection_turn_returns_nothing(self):
        engine, _ = _engine()
        # Hint surface is on_chat_turn; on_injection_turn just resets state.
        assert engine.on_injection_turn() is None


class TestFiredView:
    def test_fired_property_reports_keys(self):
        engine, _ = _engine()
        engine.on_first_error()
        engine.on_first_locator("E1")
        fired = engine.fired
        assert "first_error" in fired
        assert "first_locator" in fired
        # Frozen so callers can't mutate engine state through it.
        assert isinstance(fired, frozenset)
