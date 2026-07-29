"""``CarrierResolution``'s two-shape invariant, and gate-vocabulary coverage.

Why a separate file: ``test_screener`` asserts that each gate reports the TRUTH
through the whole node; this one asserts the narrower structural contract of the
value object those gates return, plus one meta-test over the vocabulary itself.

The invariant matters because its violation is SILENT. A malformed resolution
does not raise on use — the screener reads ``resolved`` as False, forwards an
empty ``detail``, and the guard falls back to its generic OR-template. The model
then gets a vague half-truth, which is the failure class task-866648cc was about
(a rejection that named the wrong subsystem and cost nine minutes). So the
contract is enforced at construction instead of trusted.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from chaos_agent.agent.target_guard import carriers
from chaos_agent.agent.target_guard.carriers import (
    CarrierRejectReason,
    CarrierResolution,
)
from chaos_agent.agent.target_guard.types import ConfidenceLevel, EffectiveTarget


def _effective() -> EffectiveTarget:
    return EffectiveTarget(
        scope="node", namespace="", names=("node-a",),
        blade_target="network", confidence=ConfidenceLevel.HIGH,
        raw_command="kubectl exec dbg -- chroot /host iptables -L",
    )


class TestTwoShapeInvariant:
    def test_allow_shape_is_accepted(self):
        r = CarrierResolution.allow(_effective(), {"name": "dbg", "uid": "u1"})
        assert r.resolved
        assert r.reason is None

    def test_reject_shape_is_accepted(self):
        r = CarrierResolution.reject(
            CarrierRejectReason.FAMILY_MISMATCH, "detail text", "fix text",
        )
        assert not r.resolved
        assert r.reason is CarrierRejectReason.FAMILY_MISMATCH

    def test_empty_construction_is_rejected(self):
        # Neither shape: would forward an empty detail and silently degrade.
        with pytest.raises(ValueError, match="either resolve .* or reject"):
            CarrierResolution()

    def test_resolved_without_artifact_is_rejected(self):
        # The liveness re-probe needs the artifact's identity; losing it would
        # make a resolved carrier unverifiable.
        with pytest.raises(ValueError, match="must carry the artifact dict"):
            CarrierResolution(effective=_effective())

    def test_resolved_with_non_dict_artifact_is_rejected(self):
        with pytest.raises(ValueError, match="must carry the artifact dict"):
            CarrierResolution(effective=_effective(), artifact="malformed")

    def test_both_shapes_at_once_is_rejected(self):
        with pytest.raises(ValueError, match="cannot both resolve and reject"):
            CarrierResolution(
                effective=_effective(),
                artifact={"name": "dbg"},
                reason=CarrierRejectReason.FAMILY_MISMATCH,
                detail="d",
            )

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_rejection_without_detail_is_rejected(self, blank):
        # An empty detail is what the guard's generic fallback is FOR; a gate
        # that fired must say something, or it should not have a reason.
        with pytest.raises(ValueError, match="carries no detail"):
            CarrierResolution.reject(CarrierRejectReason.NOT_PRIVILEGED, blank)

    def test_suggestion_stays_optional(self):
        # Some gates are unreachable from the forwarding branch (a non-kubectl
        # call), so a missing fix is legitimate — only ``detail`` is mandatory.
        r = CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC, "not an exec at all",
        )
        assert r.suggestion == ""

    def test_screener_factories_satisfy_the_invariant(self):
        # These are built at the call site (screener) rather than by a gate, so
        # they are the likeliest to drift out of shape.
        stale = CarrierResolution.stale("dbg-1")
        assert stale.reason is CarrierRejectReason.CARRIER_STALE
        assert stale.detail and stale.suggestion

        vf = CarrierResolution.verification_failed("dbg-1", RuntimeError("boom"))
        assert vf.reason is CarrierRejectReason.VERIFICATION_FAILED
        assert "RuntimeError" in vf.detail
        assert vf.suggestion

        err = CarrierResolution.errored(TimeoutError("t"))
        assert err.reason is CarrierRejectReason.RESOLUTION_ERROR
        assert "TimeoutError" in err.detail


class TestGateVocabularyIsFullyWired:
    """Every declared gate must actually be constructed somewhere.

    A reason nobody raises is dead vocabulary: it looks like coverage in the
    enum while the condition it names silently falls into a different gate's
    wording. This catches that at the moment the enum grows, which is cheaper
    than finding it in a drill log.
    """

    @staticmethod
    def _constructed_reasons() -> set[str]:
        tree = ast.parse(inspect.getsource(carriers))
        found: set[str] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "CarrierRejectReason"
            ):
                found.add(node.attr)
        # The enum's own member definitions are Assign targets, not attribute
        # accesses, so nothing here is self-satisfied by the declaration.
        return found

    def test_every_reason_has_a_construction_site(self):
        declared = {m.name for m in CarrierRejectReason}
        constructed = self._constructed_reasons()
        unused = declared - constructed
        assert not unused, (
            f"declared but never raised: {sorted(unused)} — either wire the gate "
            "up or drop it from the enum"
        )

    def test_retryable_set_only_names_real_reasons(self):
        # Guards against a typo'd or removed member silently disabling the
        # live-discovery fallback.
        for reason in carriers.LIVE_DISCOVERY_RETRYABLE_REASONS:
            assert isinstance(reason, CarrierRejectReason)

    def test_retryable_set_excludes_command_level_verdicts(self):
        # Retrying these through discovery would overwrite the facts that
        # produced them (synthetic artifact is always active, family-less).
        for name in ("FAMILY_MISMATCH", "NO_BOUNDED_RECOVERY", "CARRIER_NOT_ACTIVE"):
            assert (
                CarrierRejectReason[name]
                not in carriers.LIVE_DISCOVERY_RETRYABLE_REASONS
            ), f"{name} must not trigger a live cluster read"
