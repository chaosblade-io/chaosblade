"""``kubectl set`` was unclassifiable, and refusals did not say why.

Both defects had the same signature: a call the guard could not parse became
``SCOPE_UNKNOWN``, which ``target_drift_guard`` turns into ``REJECT_UNKNOWN`` and
the screener turns into a fabricated error ToolMessage. The call never ran, and
the advice the model received ("State the target explicitly") was unactionable
because the target WAS explicit. Reissuing the same call is the only move that
advice suggests, which is the loop shape we keep chasing.

  1. ``kubectl set <sub-resource>`` — ``set`` is the only whitelisted write verb
     whose first positional is the FIELD, not the resource. The generic resource
     classifier read ``image`` as the kind, ``_is_known_kind`` rejected it, and
     every ``kubectl set`` call became unclassifiable — while ``set`` sits in
     BOTH ``ToolGuard.KUBECTL_ALLOWED_SUBCOMMANDS`` and
     ``K8sNativeProvider.inject_kubectl_subcommands``, i.e. gate ① runs it and
     the provider calls it an injection carrier. That is the
     "unexecutable-by-construction" combination the whitelist's own docstring
     warns about.

  2. Refusals with an empty ``reject_detail``. ``target_drift_guard`` falls back
     to echoing the raw command, so the model learned THAT parsing failed but
     never WHICH argument was missing. All 16 ``SCOPE_UNKNOWN`` construction
     sites in the classifier now name their own cause.

Cluster-verified before changing anything (kubewiz channel, pre cluster):
  - ``kubectl set image <deploy> <container>=<img> --dry-run=client -o name`` →
    exit 0, target resolved — so kubectl accepts the form the guard refused.
  - ``kubectl set <deploy> --dry-run=client`` (no sub-resource) → exit 1,
    ``error: unknown flag: --dry-run`` — so the bare form is NOT a valid
    operation and UNKNOWN remains the right verdict for it.

Not covered here, on purpose: stacked short flags (``kubectl exec -it <pod>``).
A rule admitting them was written, verified and then reverted — see
``_is_valueless_flag``'s docstring for why. ``-it`` therefore still lands in
UNKNOWN, but now with a reason attached.
"""

import pytest

from chaos_agent.agent.target_guard.classifier import (
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.types import GuardVerdict


def classify(subcommand: str, v_args: str):
    return infer_effective_target(
        "kubectl", {"subcommand": subcommand, "v_args": v_args}
    )


# ---------------------------------------------------------------------------
# 1. kubectl set <sub-resource>
# ---------------------------------------------------------------------------


class TestKubectlSet:
    @pytest.mark.parametrize("subresource", [
        "image", "env", "resources", "serviceaccount", "sa", "subject", "selector",
    ])
    def test_subresource_is_stripped_and_target_resolved(self, subresource):
        eff = classify("set", f"{subresource} deployment/demo -n arms-prom app=v")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)
        assert eff.namespace == "arms-prom"

    def test_the_documented_injection_command(self):
        """``Pod_镜像拉取失败_容器镜像被篡改`` kubectl-native fallback, injection."""
        eff = classify(
            "set", "image deployment/demo -n arms-prom app=nginx:non-existent-tag"
        )
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_the_documented_recovery_command(self):
        """Same case, recovery — the reverse of the line above."""
        eff = classify("set", "image deployment/demo -n arms-prom app=nginx:1.21")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_subresource_after_a_flag(self):
        """kubectl accepts the flag first; the sub-resource is not positional-fixed."""
        eff = classify("set", "-n arms-prom image deployment/demo app=nginx:x")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)
        assert eff.namespace == "arms-prom"

    def test_two_positional_kind_name_form(self):
        eff = classify("set", "image deployment demo -n arms-prom app=nginx:x")
        assert eff.scope == "deployment"
        assert eff.names == ("demo",)

    def test_wrong_target_still_carries_its_identity(self):
        """Drift detection needs the name the call ACTUALLY addresses."""
        eff = classify("set", "image deployment/OTHER -n arms-prom app=nginx:x")
        assert eff.names == ("OTHER",)

    def test_unknown_subresource_is_refused(self):
        eff = classify("set", "bogus deployment/demo -n arms-prom")
        assert eff.scope == SCOPE_UNKNOWN

    def test_bare_set_without_subresource_is_refused(self):
        """Cluster-verified as not a valid operation (exit 1, unknown flag)."""
        eff = classify("set", "deployment/demo -n arms-prom")
        assert eff.scope == SCOPE_UNKNOWN


class TestNameLessSelectionFailsClosed:
    """``--all`` / ``-l`` forms must be REFUSED, never allowed with a wrong target.

    A known limitation of the shared resource classifier, inherited rather than
    introduced: with no explicit resource name, ``positionals[1]`` falls on the
    trailing ``key=value`` argument and becomes the "name". Measured on the
    pre-existing verbs too — ``kubectl label deployments --all -n ns k=v`` yields
    ``names=('k=v',)`` — so this is not specific to ``set``.

    It is not fixed here because it predates this change and touches verbs outside
    its scope. What matters is the DIRECTION of the error: a bogus name mismatches
    the approved one, so the drift guard refuses. These tests pin that direction.
    Refusing ``--all`` is also the right answer on its own terms — its blast radius
    is unbounded, which is precisely what an approved target is supposed to bound.

    If someone later "fixes" the name parsing, this class must keep passing: the
    verdict may become a clearer rejection, but it must never become ALLOW.
    """

    APPROVED = {
        "scope": "deployment", "namespace": "ns",
        "names": ["demo"], "blade_target": "pod",
    }

    def _verdict(self, subcommand: str, v_args: str):
        from chaos_agent.agent.target_guard.freeze import approved_from_dict
        from chaos_agent.agent.target_guard.guard import target_drift_guard

        effective = classify(subcommand, v_args)
        return target_drift_guard(effective, approved_from_dict(self.APPROVED)).verdict

    @pytest.mark.parametrize(("subcommand", "v_args"), [
        ("set", "image deployments --all -n ns app=nginx:x"),
        ("set", "image deployment -l app=demo -n ns app=nginx:x"),
        ("set", "env deployments --all -n ns KEY=v"),
        # Pre-existing verbs with the same shape — the limitation is shared.
        ("label", "deployments --all -n ns k=v"),
        ("annotate", "deployments --all -n ns k=v"),
    ])
    def test_nameless_selection_is_never_allowed(self, subcommand, v_args):
        assert self._verdict(subcommand, v_args) is not GuardVerdict.ALLOW

    def test_the_explicit_form_is_still_allowed(self):
        """Control: the limitation must not make legitimate calls unusable."""
        assert self._verdict(
            "set", "image deployment/demo -n ns app=nginx:x"
        ) is GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# 2. Every refusal names its own cause
# ---------------------------------------------------------------------------


class TestUnknownVerdictsCarryAReason:
    """``REJECT_UNKNOWN`` with an empty detail is a dead end with no lead.

    ``target_drift_guard`` falls back to echoing the raw command, so the model
    was told THAT parsing failed but never WHICH argument was missing — and the
    generic suggestion asks it to state a target that was already stated. These
    assert a cause exists and names the missing piece, not its exact wording.
    """

    @pytest.mark.parametrize(("subcommand", "v_args", "needle"), [
        ("set", "bogus deployment/demo -n arms-prom", "sub-resource"),
        ("set", "-n arms-prom", "sub-resource"),
        ("debug", "--image=x -n arms-prom", "node/"),
        ("cordon", "", "node"),
        ("patch", "--type=merge -p {}", "kind"),
        ("taint", "nodes", "node"),
        ("exec", "-n arms-prom -- ls /", "pod"),
        ("frobnicate", "whatever", "subcommand"),
    ])
    def test_detail_is_present_and_points_at_the_gap(
        self, subcommand, v_args, needle,
    ):
        eff = classify(subcommand, v_args)
        assert eff.scope == SCOPE_UNKNOWN
        detail = (eff.reject_detail or "").strip()
        assert detail, f"kubectl {subcommand} {v_args} refused with no reason"
        assert needle in detail, f"detail does not mention {needle!r}: {detail!r}"

    def test_unknown_set_subresource_lists_the_valid_ones(self):
        """A refusal the model can act on names the accepted values."""
        eff = classify("set", "bogus deployment/demo -n arms-prom")
        detail = eff.reject_detail or ""
        assert "image" in detail
        assert "env" in detail
