"""PR-E9 — Just-in-time hint engine.

Surfaces a single-line tip in response to specific session signals
(first error, first locator, chat-only streak, first display-mode
cycle). Each hint fires at most once per session; calm mode silences
every hint because the user has explicitly opted out of decorative
output. The goal is "useful nudge once, then never again" — banner
blindness is the failure mode we're avoiding, so we'd rather miss a
teachable moment than burn the user's attention on a repeat.

The engine is a passive observer: callers feed it events
(``on_chat_turn`` / ``on_first_error`` / ``on_first_locator`` /
``on_display_mode_cycled``) and the engine returns either a hint
string the caller renders via ``renderer.system(...)`` or ``None``
when no hint applies. Wiring lives in ``tui/app.py``.
"""

from __future__ import annotations

from chaos_agent.tui import strings
from chaos_agent.tui.state import DisplayMode, SessionState


class JITHintEngine:
    """Decides when to surface a learning hint, then never again."""

    # Three consecutive non-injection turns is enough to be a real
    # streak (one-off chats happen routinely) but not so many that the
    # nudge arrives long after the user gave up trying.
    CHAT_STREAK_THRESHOLD = 3

    def __init__(self, state: SessionState) -> None:
        self._state = state
        self._fired: set[str] = set()
        self._chat_streak: int = 0

    @property
    def fired(self) -> frozenset[str]:
        """Read-only view of which hints have already fired."""
        return frozenset(self._fired)

    def _silenced(self) -> bool:
        """Calm mode = explicit opt-out from decorative output."""
        mode = getattr(self._state, "display_mode", DisplayMode.WORKING)
        return mode == DisplayMode.CALM

    def _claim(self, key: str) -> bool:
        """Mark `key` as fired; return True iff it hadn't fired before."""
        if key in self._fired:
            return False
        self._fired.add(key)
        return True

    def on_chat_turn(self) -> str | None:
        """Called after a non-injection turn — may return the example hint.

        We bump the streak unconditionally (so calm-mode users still
        accumulate state and would catch the hint if they switched
        to working mid-session), but only return the hint when not
        silenced and the threshold tripped this turn.
        """
        self._chat_streak += 1
        if self._silenced():
            return None
        if self._chat_streak < self.CHAT_STREAK_THRESHOLD:
            return None
        if not self._claim("chat_streak_example"):
            return None
        return strings.HINT_CHAT_TRY_EXAMPLE

    def on_injection_turn(self) -> None:
        """Reset the chat streak — a real injection means user found syntax."""
        self._chat_streak = 0

    def on_first_error(self) -> str | None:
        if self._silenced():
            return None
        if not self._claim("first_error"):
            return None
        return strings.HINT_FIRST_ERROR

    def on_first_locator(self, label: str) -> str | None:
        """Caller passes the locator string (e.g. ``E1``) to embed in the tip."""
        if self._silenced():
            return None
        if not self._claim("first_locator"):
            return None
        return strings.HINT_FIRST_LOCATOR.format(label=label)

    def on_display_mode_cycled(self) -> str | None:
        """First Ctrl-G / /mode triggers the explainer; subsequent ones are quiet."""
        if self._silenced():
            return None
        if not self._claim("display_mode_cycled"):
            return None
        return strings.HINT_DISPLAY_MODE_USAGE
