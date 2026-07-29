"""Tests for ``chaos_agent.tools.guard_gateway``.

The gateway does not change either guard's policy — it only guarantees a single
uniform :class:`GuardFeedback` shape out of both layers. These tests pin:

  - :meth:`GuardGateway.check_command` delegates to ToolGuard and preserves the
    differentiated verdict (hard floor vs reshape).
  - :func:`decision_to_feedback` maps every target-layer verdict to the right
    constraint + hard-floor flag (identity drift is the one never-relaxed floor;
    a banned verdict WITH a suggestion is a reshapeable path, not a dead-end).
  - :meth:`GuardGateway.check_target` returns both the routing decision and the
    rendered feedback.
"""

from __future__ import annotations

from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
)
from chaos_agent.tools.guard import ToolGuard
from chaos_agent.tools.guard_feedback import ViolatedConstraint
from chaos_agent.tools.guard_gateway import (
    GuardGateway,
    decision_to_feedback,
    get_guard_gateway,
)


class TestCheckCommand:
    """check_command funnels ToolGuard's verdict through GuardFeedback."""

    def setup_method(self):
        self.gw = GuardGateway(tool_guard=ToolGuard())

    def test_safe_command_allowed(self):
        fb = self.gw.check_command(["kubectl", "get", "pods"])
        assert fb.allowed is True

    def test_rm_rf_is_hard_floor(self):
        fb = self.gw.check_command(["dd", "if=/dev/zero", "of=/dev/sda"])
        assert fb.allowed is False
        assert fb.is_hard_floor is True
        assert fb.constraint == ViolatedConstraint.DESTRUCTIVE_FLOOR

    def test_pipe_is_reshapeable_not_hard_floor(self):
        fb = self.gw.check_command(["kubectl", "get", "pods", "|", "wc", "-l"])
        assert fb.allowed is False
        assert fb.is_hard_floor is False
        assert fb.constraint == ViolatedConstraint.UNSUPPORTED_FORM

    def test_unknown_binary_reported(self):
        fb = self.gw.check_command(["python", "-c", "print(1)"])
        assert fb.allowed is False
        assert fb.constraint == ViolatedConstraint.UNKNOWN_BINARY
        assert fb.offending == "python"


class TestDecisionToFeedback:
    """Every target verdict maps to the right constraint + hard-floor flag."""

    def test_allow(self):
        d = GuardDecision(verdict=GuardVerdict.ALLOW, reason="ok")
        fb = decision_to_feedback(d)
        assert fb.allowed is True
        assert fb.constraint == ViolatedConstraint.NONE

    def test_readonly(self):
        d = GuardDecision(verdict=GuardVerdict.READONLY, reason="ro")
        assert decision_to_feedback(d).allowed is True

    def test_drift_is_identity_hard_floor(self):
        d = GuardDecision(
            verdict=GuardVerdict.REJECT_DRIFT,
            reason="scope drift: approved=pod effective=node",
            suggestion="narrow to the approved pod",
        )
        fb = decision_to_feedback(d)
        assert fb.allowed is False
        assert fb.constraint == ViolatedConstraint.IDENTITY_DRIFT
        assert fb.is_hard_floor is True
        assert fb.compliant_form == "narrow to the approved pod"

    def test_banned_with_suggestion_is_reshapeable(self):
        # The flipped escape case: banned, but a viable path is named.
        d = GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason="host-escape primitive not cleared",
            suggestion="run via approved debug pod + bounded reversal",
        )
        fb = decision_to_feedback(d)
        assert fb.allowed is False
        assert fb.constraint == ViolatedConstraint.UNSUPPORTED_FORM
        assert fb.is_hard_floor is False

    def test_banned_without_suggestion_is_destructive_floor(self):
        d = GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason="tool is in the banned list",
        )
        fb = decision_to_feedback(d)
        assert fb.constraint == ViolatedConstraint.DESTRUCTIVE_FLOOR
        assert fb.is_hard_floor is True

    def test_unknown_is_not_a_dead_end(self):
        d = GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason="could not classify tool_call",
            suggestion="state the target explicitly",
        )
        fb = decision_to_feedback(d)
        assert fb.constraint == ViolatedConstraint.UNKNOWN
        assert fb.is_hard_floor is False


class TestCheckTarget:
    """check_target returns both the routing decision and the feedback."""

    def test_returns_decision_and_feedback(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("web-1",))
        effective = EffectiveTarget(
            scope="pod",
            namespace="ns",
            names=("web-1",),
            confidence=ConfidenceLevel.HIGH,
            raw_command="blade create pod ...",
        )
        decision, feedback = GuardGateway().check_target(effective, approved)
        assert isinstance(decision, GuardDecision)
        assert decision.verdict == GuardVerdict.ALLOW
        assert feedback.allowed is True


class TestSingleton:
    def test_get_guard_gateway_is_singleton(self):
        assert get_guard_gateway() is get_guard_gateway()
