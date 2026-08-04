"""The confirm gate's wait window and how it reports expiry.

The window exists to reclaim a pending future after the user walks away — it is
not there to hurry a decision. A 1h default turned out to be too short in
practice: a confirmation card pops up, the operator gets pulled into something
else, and by the time they come back the turn has been collected and the run has
to start over. 6h keeps the resource-reclaim purpose while surviving a normal
workday interruption.
"""

import asyncio

import pytest

from chaos_agent.config.settings import Settings, settings
from chaos_agent.server.routes.turn_interrupt import (
    ConfirmTimeout,
    wait_for_confirmation,
)


class _Store:
    """Minimal stand-in for the turn store: a future that never resolves."""

    def __init__(self) -> None:
        self.cancelled = False
        self._fut: asyncio.Future | None = None

    def register_interrupt(self, _turn_id: str):
        if self._fut is None:
            self._fut = asyncio.get_running_loop().create_future()
        return self._fut

    def cancel_interrupt(self, _turn_id: str) -> None:
        self.cancelled = True


class TestDefaultWindow:
    def test_default_is_six_hours(self):
        assert settings.confirm_wait_timeout == 21600

    def test_env_override_still_wins(self, monkeypatch):
        """Operators wanting a stricter window must still be able to set one."""
        monkeypatch.setenv("BLADE_AI_CONFIRM_WAIT_TIMEOUT", "1800")
        assert Settings().confirm_wait_timeout == 1800


class TestExpiryMessage:
    """Report the window in the unit it was configured in.

    With a 6h default the minutes-only rendering said "360 min", which reads like
    a miscalculation rather than a setting. Driven through the real generator: a
    non-positive ``timeout`` expires on the first pass while the message still
    formats the value that was passed in, so the assertion reads the source's
    output instead of recomputing the format.
    """

    @pytest.mark.parametrize(("timeout", "expected"), [
        (-300.0, "5 min"),       # a 5-minute window, already elapsed
        (-1800.0, "30 min"),
        (0.0, "0 min"),
    ])
    def test_minute_windows_render_as_minutes(self, timeout, expected):
        message = asyncio.run(self._expire(timeout))
        assert f"({expected})" in message, message

    @pytest.mark.parametrize(("timeout", "expected"), [
        (-7200.0, "2h"),
        (-21600.0, "6h"),        # the new default
    ])
    def test_hour_windows_render_as_hours(self, timeout, expected):
        """``mins >= 120`` switches to hours — the reason this fix exists.

        Rendered from ``abs(timeout)`` so an already-elapsed window reports its
        SIZE ("6h"), not a negative remainder ("-360 min").
        """
        message = asyncio.run(self._expire(timeout))
        assert f"({expected})" in message, message
        assert "min)" not in message, message

    def test_cleanup_runs_on_timeout(self):
        """The whole point of the window: the pending future is reclaimed."""
        store = _Store()

        async def drive() -> None:
            gen = wait_for_confirmation(store, "t1", 0.0)
            with pytest.raises(ConfirmTimeout):
                async for _ in gen:
                    pass

        asyncio.run(drive())
        assert store.cancelled

    @staticmethod
    async def _expire(timeout: float) -> str:
        gen = wait_for_confirmation(_Store(), "t1", timeout)
        with pytest.raises(ConfirmTimeout) as exc:
            async for _ in gen:
                pass
        return str(exc.value)
