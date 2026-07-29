"""Unified guard feedback contract.

Every guard decision — command-safety (:class:`~chaos_agent.tools.guard.ToolGuard`)
and identity / recoverability (``agent.target_guard``) — funnels its verdict
through :class:`GuardFeedback` so the model always receives the SAME shape of
truth instead of one layer raising an opaque exception string and another
fabricating a differently-worded ToolMessage.

First-principles intent (why this module exists):

  The guard's job on a rejection is NOT to hand the model a canned label
  ("Dangerous pattern detected"). It is to restore the model's perception of
  what actually happened: which specific rule fired, the real token/value that
  triggered it, and whether the path is a hard floor (never permitted) or just
  a form issue (reshape and retry). The powerful model does the reasoning; the
  guard only tells the truth, in full, differentiated.

So the load-bearing fields are ``reason`` + ``offending`` + ``is_hard_floor``.
``compliant_form`` is OPTIONAL — a deterministic hint we fill only where we are
certain of the alternative (e.g. the exec-form pipe fact). We never fabricate a
prescriptive fix just to fill the field; that would be the guard thinking for
the model instead of informing it.

Placed in the ``tools`` layer (not ``agent.target_guard``) on purpose: it is a
stdlib-only leaf that BOTH the low-level ``ToolGuard`` and the higher-level
target guard import, so it can be shared without an import cycle
(``agent`` may depend on ``tools``, never the reverse at module-load time).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ViolatedConstraint(str, Enum):
    """Which class of constraint a rejection violated.

    Machine-readable companion to the human ``reason``. String-valued so it
    serialises cleanly into audit logs / SSE detail. The distinction the model
    most needs is encoded orthogonally in :attr:`GuardFeedback.is_hard_floor`
    (dead-end vs reshape-and-retry) — the enum names the CATEGORY.
    """

    # Cleared every check — safe to run.
    NONE = "none"
    # Binary is not on the whitelist (unknown / interpreter / shell).
    UNKNOWN_BINARY = "unknown_binary"
    # A supported binary used in an unsupported FORM: a disallowed
    # subcommand, a shell metacharacter (pipe/redirect/chain) that cannot
    # execute in exec-form, a magnitude cap, a config write. Reshape and
    # retry — the intent is usually expressible a supported way.
    UNSUPPORTED_FORM = "unsupported_form"
    # A hard, non-negotiable safety floor: raw block-device writes, rm -rf,
    # shell-interpreter pipes, command substitution, reboot-class verbs.
    # Never permitted regardless of recoverability — a true dead-end.
    DESTRUCTIVE_FLOOR = "destructive_floor"
    # Identity drift: the call would act on a resource outside the approved
    # blast radius. The boundary the guard must never relax.
    IDENTITY_DRIFT = "identity_drift"
    # A mutation the recovery graph could not confirm it can undo (no bounded
    # timer, no paired inverse, no registered rollback). Add a bound / inverse
    # / rollback registration and retry — NOT a dead-end.
    NOT_RECOVERABLE = "not_recoverable"
    # Classifier could not make sense of the call (missing field, malformed).
    UNKNOWN = "unknown"


@dataclass
class GuardFeedback:
    """The single feedback shape returned to (and rendered for) the model.

    Attributes:
        allowed: True when the call cleared this guard.
        constraint: Which :class:`ViolatedConstraint` fired (``NONE`` on allow).
        reason: The real, differentiated explanation — which rule fired and
            why. Never a generic catch-all; it names the specific cause.
        offending: The concrete token / value that tripped the rule, echoed
            back so the model sees exactly what to change.
        is_hard_floor: True → the path is never permitted (stop trying this
            approach). False → a form/recoverability issue the model can fix
            and retry.
        compliant_form: OPTIONAL deterministic hint toward a compliant form.
            Empty unless we are certain of the alternative.
    """

    allowed: bool
    constraint: ViolatedConstraint = ViolatedConstraint.NONE
    reason: str = "OK"
    offending: str = ""
    is_hard_floor: bool = False
    compliant_form: str = ""

    def render_for_llm(self) -> str:
        """Compose the model-facing text.

        Kept deliberately compact — long rejections waste context. ``reason``
        already carries the differentiated cause and the offending token; we
        only append the optional ``compliant_form`` when present.
        """
        if self.allowed:
            return "OK"
        parts = [self.reason]
        if self.compliant_form:
            parts.append(self.compliant_form)
        return " ".join(p for p in parts if p)

    def as_tuple(self) -> tuple[bool, str]:
        """Backward-compatible ``(is_allowed, reason)`` adapter."""
        return self.allowed, self.render_for_llm()


__all__ = ["GuardFeedback", "ViolatedConstraint"]
