"""Single funnel for both guard layers.

The codebase runs two complementary guards on every mutating tool_call:

  1. **command safety** — :class:`~chaos_agent.tools.guard.ToolGuard`: is the
     raw command a permitted binary in a supported, non-destructive form?
  2. **identity / recoverability** — ``agent.target_guard.target_drift_guard``:
     does the call stay inside the approved blast radius, and can the mutation
     be undone?

Historically the two spoke different dialects: ToolGuard rejections surfaced as
a raised ``ToolGuardError`` string, target-guard rejections as a fabricated
``ToolMessage`` with its own wording. Same job (tell the model why it was
blocked), two inconsistent shapes.

:class:`GuardGateway` is the single place both funnel through. It does NOT
change either guard's policy — it only guarantees a uniform
:class:`~chaos_agent.tools.guard_feedback.GuardFeedback` out, so every caller
renders the same differentiated truth (which rule fired, the offending token,
hard floor vs reshape-and-retry) instead of a layer-specific ad-hoc string.

Placement: lives in ``tools`` (not ``agent``) so the command-execution call
sites (``tools.shell.run_command``, ``transports.executor``) can import it
without an ``agent`` dependency. The target-guard entry lazily imports
``agent.target_guard`` at call time — there is no module-load cycle because the
import only fires when a target check is actually requested (the same deferred-
import discipline ToolGuard already uses for the provider registry).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from chaos_agent.tools.guard_feedback import GuardFeedback, ViolatedConstraint

if TYPE_CHECKING:
    from chaos_agent.agent.target_guard.types import (
        ApprovedTarget,
        EffectiveTarget,
        GuardDecision,
    )
    from chaos_agent.tools.guard import ToolGuard


def decision_to_feedback(decision: "GuardDecision") -> GuardFeedback:
    """Adapt a target-layer :class:`GuardDecision` to a :class:`GuardFeedback`.

    Verdict → constraint mapping (identity boundary is the one hard, never-
    relaxed floor; everything else is reshape-and-retry, so ``is_hard_floor``
    stays False and the model keeps its exploration space):

      - ALLOW / READONLY      → allowed, no constraint.
      - REJECT_DRIFT          → IDENTITY_DRIFT, hard floor (the boundary the
                                guard must never relax).
      - REJECT_BANNED         → UNSUPPORTED_FORM when a suggestion names a
                                viable path (e.g. a container-escape primitive
                                that IS reachable via an approved debug pod),
                                else DESTRUCTIVE_FLOOR.
      - REJECT_UNKNOWN        → UNKNOWN, not a dead-end (state the target /
                                add the missing field and retry).

    The decision's ``suggestion`` becomes ``compliant_form`` verbatim — it is
    already the target layer's actionable "here is what would pass" hint.
    """
    # Local import: GuardVerdict is an ``agent`` type; keep this module's
    # load-time footprint ``tools``-only.
    from chaos_agent.agent.target_guard.types import GuardVerdict

    verdict = decision.verdict
    if verdict in (GuardVerdict.ALLOW, GuardVerdict.READONLY):
        return GuardFeedback(allowed=True)

    suggestion = decision.suggestion or ""
    if verdict == GuardVerdict.REJECT_DRIFT:
        constraint = ViolatedConstraint.IDENTITY_DRIFT
        is_hard_floor = True
    elif verdict == GuardVerdict.REJECT_BANNED:
        # A suggestion means the guard knows a compliant path exists, so this
        # is a form issue, not an absolute dead-end (the flipped escape case).
        if suggestion:
            constraint = ViolatedConstraint.UNSUPPORTED_FORM
            is_hard_floor = False
        else:
            constraint = ViolatedConstraint.DESTRUCTIVE_FLOOR
            is_hard_floor = True
    else:  # REJECT_UNKNOWN
        constraint = ViolatedConstraint.UNKNOWN
        is_hard_floor = False

    return GuardFeedback(
        allowed=False,
        constraint=constraint,
        reason=decision.reason,
        is_hard_floor=is_hard_floor,
        compliant_form=suggestion,
    )


class GuardGateway:
    """The single entry both guard layers funnel through.

    Two stages, deliberately kept as two methods because they run at genuinely
    different points in the pipeline with different inputs available:

      - :meth:`check_command` runs at execution time — the raw argv is known,
        the approved target is not needed (command safety is identity-agnostic).
      - :meth:`check_target` runs at screening/planning time — the effective
        and approved targets are known.

    Both return a :class:`GuardFeedback`; :meth:`check_target` also returns the
    underlying :class:`GuardDecision` so the screener can still route (drift
    interrupt / retry) on the verdict while rendering from the feedback.
    """

    def __init__(self, tool_guard: Optional["ToolGuard"] = None) -> None:
        self._tool_guard = tool_guard

    @property
    def tool_guard(self) -> "ToolGuard":
        # An explicitly injected guard (tests) wins. Otherwise defer to
        # ``tools.shell.get_tool_guard`` on EVERY access rather than caching:
        # that function already owns the process-wide singleton, and not
        # caching here keeps the gateway honest under test patching of
        # ``get_tool_guard`` (a cached mock would otherwise leak across tests).
        if self._tool_guard is not None:
            return self._tool_guard
        from chaos_agent.tools.shell import get_tool_guard

        return get_tool_guard()

    def check_command(self, cmd: list[str]) -> GuardFeedback:
        """Command-safety verdict as unified feedback (delegates ToolGuard)."""
        return self.tool_guard.evaluate(cmd)

    def check_target(
        self,
        effective: "EffectiveTarget",
        approved: Optional["ApprovedTarget"],
    ) -> tuple["GuardDecision", GuardFeedback]:
        """Identity / recoverability verdict.

        Returns ``(decision, feedback)``: the raw decision drives routing, the
        feedback is the uniform shape rendered to the model.
        """
        from chaos_agent.agent.target_guard.guard import target_drift_guard

        decision = target_drift_guard(effective, approved)
        return decision, decision_to_feedback(decision)


_GATEWAY: Optional[GuardGateway] = None


def get_guard_gateway() -> GuardGateway:
    """Process-wide :class:`GuardGateway` singleton."""
    global _GATEWAY
    if _GATEWAY is None:
        _GATEWAY = GuardGateway()
    return _GATEWAY


__all__ = ["GuardGateway", "decision_to_feedback", "get_guard_gateway"]
