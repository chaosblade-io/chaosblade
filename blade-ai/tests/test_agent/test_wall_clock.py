"""Tests for Patch C — wall-clock timeout in router."""

from __future__ import annotations

import time

import pytest

from chaos_agent.agent.router import (
    _wall_clock_exceeded,
    mark_wall_clock_timeout,
    should_continue_agent_loop,
    should_continue_execute_loop,
    should_continue_recover_verifier,
    should_continue_verifier,
)
from chaos_agent.errors import FailureReason


# ---------------------------------------------------------------------------
# _wall_clock_exceeded helper
# ---------------------------------------------------------------------------


class TestWallClockHelper:
    def test_disabled_when_budget_zero(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 0)
        # Even if a stamp exists from 1 hour ago, budget=0 disables.
        state = {"pipeline_started_at": time.time() - 3600}
        assert _wall_clock_exceeded(state) is False

    def test_disabled_when_no_stamp(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        # No stamp = node hasn't run yet, guard must be a no-op.
        assert _wall_clock_exceeded({}) is False
        assert _wall_clock_exceeded({"pipeline_started_at": 0}) is False
        assert _wall_clock_exceeded({"pipeline_started_at": 0.0}) is False

    def test_within_budget(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        state = {"pipeline_started_at": time.time() - 30}
        assert _wall_clock_exceeded(state) is False

    def test_exceeds_budget(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        state = {"pipeline_started_at": time.time() - 90}
        assert _wall_clock_exceeded(state) is True

    def test_negative_budget_treated_as_zero(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", -1)
        state = {"pipeline_started_at": time.time() - 9999}
        assert _wall_clock_exceeded(state) is False


# ---------------------------------------------------------------------------
# Router gates honour wall-clock
# ---------------------------------------------------------------------------


def _expired_state(extra: dict | None = None) -> dict:
    """Build a state where pipeline_started_at is 10 minutes ago."""
    s = {"pipeline_started_at": time.time() - 600}
    if extra:
        s.update(extra)
    return s


class TestRouterGatesRespectWallClock:
    @pytest.fixture(autouse=True)
    def _set_short_budget(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)

    def test_agent_loop_returns_reject(self):
        # Even if everything else looks like 'continue', wall-clock wins
        state = _expired_state({"agent_loop_count": 0, "messages": []})
        assert should_continue_agent_loop(state) == "reject"

    def test_execute_loop_returns_end(self):
        state = _expired_state({"execute_loop_count": 0, "messages": []})
        assert should_continue_execute_loop(state) == "end"

    def test_verifier_returns_done(self):
        state = _expired_state({"verifier_loop_count": 0, "messages": []})
        assert should_continue_verifier(state) == "done"

    def test_recover_verifier_returns_done(self):
        state = _expired_state({"verifier_loop_count": 0, "messages": []})
        assert should_continue_recover_verifier(state) == "done"


class TestRouterGatesPassThroughWhenWithinBudget:
    """Confirm wall-clock does NOT short-circuit normal routing when
    we're still within budget — the router should fall through to its
    pre-existing logic."""

    @pytest.fixture(autouse=True)
    def _set_long_budget(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 3600)

    def test_agent_loop_unchanged(self):
        state = {
            "pipeline_started_at": time.time() - 5,
            "agent_loop_count": 0,
            "messages": [],
        }
        # No tool_calls, no skill_name, no plan → router default 'continue'
        assert should_continue_agent_loop(state) == "continue"

    def test_execute_loop_unchanged(self):
        state = {
            "pipeline_started_at": time.time() - 5,
            "execute_loop_count": 0,
            "messages": [],
        }
        # No tool_calls + no blade_uid → 'continue' (existing behaviour)
        assert should_continue_execute_loop(state) == "continue"


class TestMarkWallClockTimeout:
    """Patch C — node-side state writer that complements the router gate."""

    def test_no_op_when_within_budget(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        state = {"pipeline_started_at": time.time() - 5}
        result = {"agent_loop_count": 1}
        out = mark_wall_clock_timeout(state, result)
        assert out is result  # in-place chain
        assert "error" not in out
        assert "failure_reason" not in out

    def test_writes_when_exceeded(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        state = {"pipeline_started_at": time.time() - 600}
        result = {"agent_loop_count": 1}
        out = mark_wall_clock_timeout(state, result)
        assert "wall-clock timeout" in out["error"]
        assert out["failure_reason"] == FailureReason.WALL_CLOCK_TIMEOUT.value

    def test_existing_error_preserved(self, monkeypatch):
        """LLM-detected failures are more specific than 'we ran out of time'."""
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 60)
        state = {"pipeline_started_at": time.time() - 600}
        result = {
            "error": "blade create failed: permission denied",
            "failure_reason": "execution_failed: rbac",
        }
        out = mark_wall_clock_timeout(state, result)
        # Pre-existing values win
        assert out["error"] == "blade create failed: permission denied"
        assert out["failure_reason"] == "execution_failed: rbac"

    def test_disabled_budget_never_writes(self, monkeypatch):
        from chaos_agent.config.settings import settings
        monkeypatch.setattr(settings, "max_inject_seconds", 0)
        state = {"pipeline_started_at": time.time() - 9999}
        result = {}
        out = mark_wall_clock_timeout(state, result)
        assert out == {}
