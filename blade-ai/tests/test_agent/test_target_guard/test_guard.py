"""Tests for ``chaos_agent.agent.target_guard.guard``.

Each test constructs an ``ApprovedTarget`` and an ``EffectiveTarget``
explicitly (no classifier round-trip) so the verdict is a function of
the policy alone, not of classifier quirks.

16+ scenarios from the implementation plan:

  1.  same scope/ns/names → ALLOW
  2.  approved has [A,B], effective has [A] → ALLOW (subset)
  3.  approved [A], effective [B] → REJECT_DRIFT (different pod)
  4.  approved [A], effective [A,C] → REJECT_DRIFT (superset = drift)
  5.  cross-namespace → REJECT_DRIFT
  6.  cross-scope (pod → node) → REJECT_DRIFT
  7.  labels strict-superset → ALLOW
  8.  labels different value → REJECT_DRIFT
  9.  cross-selector type (approved=names, effective=labels) → REJECT_DRIFT
  10. is_namespace_wide → ALLOW any name in ns
  11. cluster-scoped (node) ignores namespace
  12. default-ns normalisation: "" matches "default"
  13. lock_fault_type=True + different blade_target → REJECT_DRIFT
  14. lock_fault_type=False + different blade_target → ALLOW
  15. method switch (blade → kubectl scale) with same target → ALLOW
  16. SCOPE_READONLY → READONLY verdict
  17. SCOPE_BANNED → REJECT_BANNED
  18. SCOPE_UNKNOWN / UNKNOWN confidence → REJECT_UNKNOWN
  19. approved=None defence → REJECT_UNKNOWN
  20. LOW confidence still allowed when scope/ns/names match
  21. blade_action change with lock_fault_type=True → ALLOW
       (only TYPE is locked, ACTION is method autonomy)
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_guard.classifier import (
    SCOPE_BANNED,
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
)


class TestEscapeReasonPassthrough:
    """Reject scopes relay the classifier/screener's PRECISE cause, not a
    generic template.

    Regression for task-40c934fb: a batch-2 exec through a non-approved pod was
    rejected with an ambiguous OR-template that hid which condition tripped, and
    banned calls reported only "tool is in the banned list". The guard must
    surface the specific ``reject_detail`` its origin recorded; when absent, it
    falls back to the generic wording (no regression).

    Scope note (task-866648cc): these tests cover the guard's TRANSPORT only —
    that whatever detail it is handed reaches the model intact. They hand-build
    ``EffectiveTarget`` and therefore say nothing about whether the detail is
    TRUE. That gap is why a screener that guessed "pod not registered" for every
    carrier gate passed CI for six days. The truthfulness of each gate's cause is
    asserted where the cause is produced —
    ``test_screener.TestCarrierRejectReasonIsTruthful``.
    """

    _DETAIL = "carrier pod 'debugger-1' is not a privileged container"

    def test_escape_detail_surfaced_verbatim(self):
        eff = EffectiveTarget(
            scope=SCOPE_ESCAPE, namespace="",
            confidence=ConfidenceLevel.UNKNOWN,
            raw_command="kubectl(exec chaosblade-tool-x -- chroot /host iptables)",
            reject_detail=self._DETAIL,
        )
        d = target_drift_guard(eff, ApprovedTarget(scope="node", namespace=""))
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert self._DETAIL in d.reason
        # The generic OR-template must not be appended alongside a known cause.
        # This guards the ``or`` short-circuit: concatenating both halves would
        # put two different explanations in one reason, and the model would have
        # to pick. (The earlier version of this assertion was sound; what was
        # wrong was the fixture it was paired with — ``_DETAIL`` used to hold the
        # misattributed wording, which made a lie look like the expected value.)
        assert "OR the host mutation is not self-recovering" not in d.reason

    def test_escape_suggestion_surfaced_verbatim(self):
        """The gate's own fix reaches the model, not the generic catch-all.

        Cause and fix must describe the SAME condition: task-866648cc's
        rejection paired a "your pod is unapproved" reason with a suggestion
        stating the command form was already acceptable, so the two halves
        contradicted each other and the model believed the half that was wrong.
        """
        suggestion = (
            "Keep this command and pair it with its own reversal so the fault "
            "expires on its own"
        )
        eff = EffectiveTarget(
            scope=SCOPE_ESCAPE, namespace="",
            confidence=ConfidenceLevel.UNKNOWN,
            raw_command="kubectl(exec debugger-1 -- chroot /host tc qdisc add)",
            reject_detail="the host mutation carries no paired reversal",
            reject_suggestion=suggestion,
        )
        d = target_drift_guard(eff, ApprovedTarget(scope="node", namespace=""))
        assert suggestion in d.suggestion
        # The catch-all must not be appended alongside a gate-specific fix: it
        # is what told the model "<host-entry> is ANY accepted primitive" while
        # the real gate was the missing reversal.
        assert "ANY accepted primitive" not in d.suggestion

    def test_escape_fallback_generic_reason_when_no_detail(self):
        """With no gate recorded, the OR-form is the honest answer.

        Naming a single condition here would be a guess. The generic template is
        vague but true, and vague-but-true beats specific-but-wrong: the latter
        actively steers the model at the wrong subsystem.
        """
        eff = EffectiveTarget(
            scope=SCOPE_ESCAPE, namespace="",
            confidence=ConfidenceLevel.UNKNOWN,
            raw_command="chroot /host iptables",
        )
        d = target_drift_guard(eff, ApprovedTarget(scope="node", namespace=""))
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert "OR the host mutation is not self-recovering" in d.reason
        # Both halves fall back together, so the pair stays self-consistent.
        assert "ANY accepted primitive" in d.suggestion

    def test_banned_detail_surfaced_verbatim(self):
        detail = "kubectl subcommand 'delete' is explicitly banned (too dangerous to classify)"
        eff = EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl(delete pods --all)",
            reject_detail=detail,
        )
        d = target_drift_guard(eff, ApprovedTarget(scope="pod", namespace="ns"))
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.reason == detail
        assert d.reason != "tool is in the banned list"

    def test_banned_fallback_generic_reason_when_no_detail(self):
        eff = EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl(proxy)",
        )
        d = target_drift_guard(eff, ApprovedTarget(scope="pod", namespace="ns"))
        assert d.verdict == GuardVerdict.REJECT_BANNED
        # No precise cause recorded: still not fully masked — the raw command
        # is echoed alongside the generic wording.
        assert d.reason.startswith("tool is in the banned list")
        assert "kubectl(proxy)" in d.reason


# ---------------------------------------------------------------------------
# Sentinel scope short-circuits
# ---------------------------------------------------------------------------


class TestSentinelScopes:
    def test_readonly_passes_through(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(
            scope=SCOPE_READONLY, namespace="",
            raw_command="kubectl(get pods)",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.READONLY
        assert d.is_allow

    def test_banned_rejects(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(
            scope=SCOPE_BANNED, namespace="",
            raw_command="kubectl(apply -f x.yaml)",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.is_reject

    def test_unknown_scope_rejects(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(
            scope=SCOPE_UNKNOWN, namespace="",
            raw_command="weird_tool(...)",
            confidence=ConfidenceLevel.UNKNOWN,
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_UNKNOWN

    def test_unknown_confidence_on_real_scope_rejects(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        # scope parsed but confidence=UNKNOWN — refuse defensively.
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            confidence=ConfidenceLevel.UNKNOWN,
            raw_command="x()",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_UNKNOWN


class TestApprovedNoneDefence:
    def test_no_approval_on_real_scope_rejects(self):
        # Defence-in-depth: if the screener calls guard without an
        # approval (wiring bug), we must NOT default-allow.
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            raw_command="kubectl(delete pod/a)",
        )
        d = target_drift_guard(effective, approved=None)
        assert d.verdict == GuardVerdict.REJECT_UNKNOWN


class TestZoneLabelNameBatchDrift:
    """AZ label-approved node fault, executed per node name in batches.

    Regression for task-40c934fb: the guard used to reject the labels↔names
    cross as 'resource selection drift'. With the zone label resolved to
    concrete node names at freeze time (``approved.resolved_names``), an
    in-zone name batch must pass while an out-of-zone name is still rejected.
    """

    _ZONE = {"topology.kubernetes.io/zone": "az-b"}

    def _approved(self):
        return ApprovedTarget(
            scope="node", namespace="",
            labels=dict(self._ZONE),
            resolved_names=("node-1", "node-2", "node-3"),
            blade_target="network", blade_action="drop",
            lock_fault_type=False,
        )

    def test_in_zone_name_batch_not_drift(self):
        effective = EffectiveTarget(
            scope="node", namespace="",
            names=("node-1", "node-2"),
            raw_command="kubectl(debug node/node-1)",
        )
        d = target_drift_guard(effective, self._approved())
        assert d.verdict != GuardVerdict.REJECT_DRIFT

    def test_out_of_zone_name_rejected(self):
        effective = EffectiveTarget(
            scope="node", namespace="",
            names=("node-99",),
            raw_command="kubectl(debug node/node-99)",
        )
        d = target_drift_guard(effective, self._approved())
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_labels_only_without_resolved_still_cross_rejected(self):
        # No resolved_names frozen (e.g. resolution failed) → the labels↔names
        # cross is still rejected, preserving the pre-fix safe default.
        approved = ApprovedTarget(
            scope="node", namespace="",
            labels=dict(self._ZONE),
            blade_target="network", blade_action="drop",
            lock_fault_type=False,
        )
        effective = EffectiveTarget(
            scope="node", namespace="",
            names=("node-1",),
            raw_command="kubectl(debug node/node-1)",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_unit_names_subset_uses_resolved_names(self):
        from chaos_agent.agent.target_guard.drift_policy import _check_names_subset
        approved = self._approved()
        assert _check_names_subset(
            approved,
            EffectiveTarget(scope="node", namespace="", names=("node-2",)),
        ) is True
        assert _check_names_subset(
            approved,
            EffectiveTarget(scope="node", namespace="", names=("node-2", "node-99")),
        ) is False


class TestPodLabelNameBatchDrift:
    """Pod fault approved by app labels, executed by pod names.

    Same label↔name cross as the AZ node case, generalised to pod scope: the
    pod labels are resolved to concrete pod names at freeze time, so an
    in-selector pod-name batch passes while an out-of-selector pod is rejected.
    """

    def _approved(self):
        return ApprovedTarget(
            scope="pod", namespace="prod",
            labels={"app": "checkout"},
            resolved_names=("checkout-abc", "checkout-def", "checkout-ghi"),
            blade_target="network", blade_action="loss",
            lock_fault_type=False,
        )

    def test_in_selector_pod_batch_not_drift(self):
        effective = EffectiveTarget(
            scope="pod", namespace="prod",
            names=("checkout-abc", "checkout-def"),
            raw_command="kubectl(delete pod checkout-abc -n prod)",
        )
        d = target_drift_guard(effective, self._approved())
        assert d.verdict != GuardVerdict.REJECT_DRIFT

    def test_out_of_selector_pod_rejected(self):
        effective = EffectiveTarget(
            scope="pod", namespace="prod",
            names=("other-app-xyz",),
            raw_command="kubectl(delete pod other-app-xyz -n prod)",
        )
        d = target_drift_guard(effective, self._approved())
        assert d.verdict == GuardVerdict.REJECT_DRIFT


# ---------------------------------------------------------------------------
# Same target → ALLOW
# ---------------------------------------------------------------------------


class TestSameTarget:
    def test_identical_scope_ns_names(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="ns", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_names_subset_is_allowed(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a", "b"))
        effective = EffectiveTarget(scope="pod", namespace="ns", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Cross-resource drift → REJECT_DRIFT
# ---------------------------------------------------------------------------


class TestCrossResourceDrift:
    def test_different_pod_name_rejected(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="ns", names=("b",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "selection drift" in d.reason

    def test_superset_of_names_rejected(self):
        # approved [A], effective [A, C] — even though A is in
        # approved, C is not, so the call would touch unapproved
        # resources.
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="ns", names=("a", "c"))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_cross_namespace_rejected(self):
        approved = ApprovedTarget(scope="pod", namespace="prod", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="staging", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "namespace drift" in d.reason

    def test_cross_scope_rejected(self):
        # User approved pod; LLM tries to act on node.
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="node", namespace="", names=("node-1",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "scope drift" in d.reason

    def test_cross_kind_deployment_vs_pod_allowed_owner(self):
        """deployment is an owner of pod — operating on a deployment to
        affect its pods is a legitimate injection method, not drift."""
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="deployment", namespace="ns", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Labels: strict-subset OK, value drift REJECT, cross-selector REJECT
# ---------------------------------------------------------------------------


class TestLabelsSelector:
    def test_exact_match_labels(self):
        approved = ApprovedTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo"},
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo"},
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_effective_strictly_narrower_allowed(self):
        # approved selects app=demo; effective adds env=prod to narrow.
        # Effective's resource set is a SUBSET of approved's → safe.
        approved = ApprovedTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo"},
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo", "env": "prod"},
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_different_value_rejected(self):
        approved = ApprovedTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo"},
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns",
            labels={"app": "other"},
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_missing_required_key_rejected(self):
        # approved requires app=demo AND env=prod; effective only has app
        approved = ApprovedTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo", "env": "prod"},
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns",
            labels={"app": "demo"},
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_cross_selector_type_rejected(self):
        # Without cluster lookup we can't prove labels resolve to
        # approved names — reject.
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", labels={"app": "demo"},
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_approved_labels_effective_names_rejected(self):
        approved = ApprovedTarget(
            scope="pod", namespace="ns", labels={"app": "demo"},
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT


# ---------------------------------------------------------------------------
# Namespace-wide opt-in
# ---------------------------------------------------------------------------


class TestNamespaceWide:
    def test_namespace_wide_allows_any_name(self):
        approved = ApprovedTarget(
            scope="pod", namespace="ns",
            is_namespace_wide=True,
        )
        # Effective picks an arbitrary pod in the same ns — OK.
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("random-pod",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_namespace_wide_still_blocks_cross_namespace(self):
        approved = ApprovedTarget(
            scope="pod", namespace="prod",
            is_namespace_wide=True,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="staging", names=("p1",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_namespace_wide_still_blocks_non_owner_cross_scope(self):
        """namespace_wide allows any resource within the scope — but a
        non-owner scope (service vs pod) is still drift."""
        approved = ApprovedTarget(
            scope="pod", namespace="ns", is_namespace_wide=True,
        )
        effective = EffectiveTarget(
            scope="service", namespace="ns", names=("svc1",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_namespace_wide_allows_owner_cross_scope(self):
        """namespace_wide + owner scope (deployment vs pod) → allowed."""
        approved = ApprovedTarget(
            scope="pod", namespace="ns", is_namespace_wide=True,
        )
        effective = EffectiveTarget(
            scope="deployment", namespace="ns", names=("d1",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Cluster-scoped resources skip namespace comparison
# ---------------------------------------------------------------------------


class TestClusterScoped:
    def test_node_ignores_namespace(self):
        # Both ApprovedTarget and EffectiveTarget store namespace=""
        # for cluster-scoped kinds — the guard skips ns comparison.
        approved = ApprovedTarget(scope="node", namespace="", names=("n1",))
        effective = EffectiveTarget(scope="node", namespace="", names=("n1",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_node_ignores_accidental_namespace(self):
        # Defence: even if one side has a stray ns on a cluster-scoped
        # kind, the comparison is skipped.
        approved = ApprovedTarget(scope="node", namespace="", names=("n1",))
        effective = EffectiveTarget(scope="node", namespace="default", names=("n1",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_node_cross_name_rejected(self):
        approved = ApprovedTarget(scope="node", namespace="", names=("n1",))
        effective = EffectiveTarget(scope="node", namespace="", names=("n2",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT


# ---------------------------------------------------------------------------
# Namespace default normalisation
# ---------------------------------------------------------------------------


class TestDefaultNsNormalisation:
    def test_empty_matches_default(self):
        approved = ApprovedTarget(scope="pod", namespace="default", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_default_matches_empty(self):
        approved = ApprovedTarget(scope="pod", namespace="", names=("a",))
        effective = EffectiveTarget(scope="pod", namespace="default", names=("a",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Fault-type lock (lock_fault_type)
# ---------------------------------------------------------------------------


class TestBladeTargetLock:
    def test_lock_on_diff_blade_target_rejected(self):
        # User approved CPU burn; LLM tries memory burn — TYPE drift.
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu", blade_action="fullload",
            lock_fault_type=True,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="mem", blade_action="ram",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "blade_target drift" in d.reason

    def test_unlock_allows_blade_target_change(self):
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu", lock_fault_type=False,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="mem",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_method_switch_blade_to_kubectl_allowed(self):
        # Approved blade cpu; LLM switches to kubectl scale on same
        # pod (effective has NO blade_target). That's method autonomy,
        # not type drift — must ALLOW.
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu", lock_fault_type=True,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="",  # kubectl scale doesn't carry blade target
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_method_switch_kubectl_to_blade_allowed(self):
        # Reverse: approved was a kubectl scale (no blade); LLM
        # switches to blade. We allow — narrowing a non-fault approval
        # into a typed fault is in-scope autonomy.
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="", lock_fault_type=True,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_action_change_allowed_when_type_locked(self):
        # User approved cpu fullload; LLM dials to cpu high — same
        # TYPE, different ACTION. Always allowed (action is not locked
        # by lock_fault_type).
        approved = ApprovedTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu", blade_action="fullload",
            lock_fault_type=True,
        )
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            blade_target="cpu", blade_action="high",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Low-confidence tolerance
# ---------------------------------------------------------------------------


class TestLowConfidence:
    def test_low_confidence_still_allowed_when_target_matches(self):
        # LOW confidence (e.g. nested kubectl exec) is acceptable as
        # long as the parsed target matches. The guard logs but does
        # not reject.
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(
            scope="pod", namespace="ns", names=("a",),
            confidence=ConfidenceLevel.LOW,
            raw_command="kubectl(exec a -- kubectl get pods)",
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW


# ---------------------------------------------------------------------------
# Suggestion strings (for LLM-facing rejection messages)
# ---------------------------------------------------------------------------


class TestSuggestionFormatting:
    def test_suggestion_includes_approved_summary(self):
        approved = ApprovedTarget(
            scope="pod", namespace="prod", names=("a", "b"),
            blade_target="cpu",
        )
        effective = EffectiveTarget(
            scope="pod", namespace="prod", names=("c",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        # Suggestion should mention scope, ns, approved names
        assert "scope=pod" in d.suggestion
        assert "ns=prod" in d.suggestion
        assert "['a', 'b']" in d.suggestion
        assert "blade_target=cpu" in d.suggestion

    def test_cluster_scoped_suggestion(self):
        approved = ApprovedTarget(scope="node", namespace="", names=("n1",))
        effective = EffectiveTarget(scope="node", namespace="", names=("n2",))
        d = target_drift_guard(effective, approved)
        assert "ns=<cluster>" in d.suggestion


class TestBannedRejectionSeparatesCauseFromWayForward:
    """A BANNED verdict must answer both questions, in their own fields.

    ``reason`` says why it was refused, ``suggestion`` says what to do instead.
    Before this split, six of the seven bans said only WHY ("outside the
    target-scoped operation model") and the seventh folded its way forward into
    the prose of ``reject_detail``. Two of them additionally referred to a
    whitelist the classifier owns without naming it — the same shape of failure
    that made a ``kubectl label`` rejection unactionable in task-c758cdbd.

    Only the classifier can supply the alternative: which ban fired, and what
    the whitelist holds, is its knowledge. The guard sees a ``__banned__``
    sentinel and forwards.
    """

    APPROVED = ApprovedTarget(scope="pod", namespace="ns", names=("p",))

    def _decide(self, tool, args):
        return target_drift_guard(infer_effective_target(tool, args), self.APPROVED)

    @pytest.mark.parametrize("tool,args,expected", [
        # kubeconfig write → per-call flags (same answer the ToolGuard layer gives)
        ("kubectl", {"subcommand": "config", "v_args": "use-context other"},
         "--context / --kubeconfig"),
        # proxy → the subcommands already carry the connection
        ("kubectl", {"subcommand": "proxy", "v_args": "--port=8080"},
         "No tunnel is needed"),
        # -f file → stdin_data
        ("kubectl", {"subcommand": "apply", "v_args": "-f /tmp/x.yaml"},
         "stdin_data"),
        # manifest without kind → declare one
        ("kubectl", {"subcommand": "apply", "v_args": "-f -", "stdin_data": "foo: bar"},
         "Declare an explicit 'kind:'"),
        # non-whitelisted kind → why workloads are refused
        ("kubectl", {"subcommand": "apply", "v_args": "-f -",
                     "stdin_data": "kind: Deployment"},
         "blast radius"),
        # skill script → use the classifiable tools
        ("_execute_skill_script", {"script": "x.sh"}, "kubectl / blade tools"),
    ])
    def test_ban_with_an_alternative_states_it(self, tool, args, expected):
        d = self._decide(tool, args)
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert expected in d.suggestion, d.suggestion
        # The cause stays in reason and does NOT absorb the alternative.
        assert d.reason
        assert expected not in d.reason

    @pytest.mark.parametrize("tool,args", [
        ("kubectl", {"subcommand": "apply", "v_args": "-f /tmp/x.yaml"}),
        ("kubectl", {"subcommand": "apply", "v_args": "-f -", "stdin_data": "foo: bar"}),
        ("kubectl", {"subcommand": "apply", "v_args": "-f -",
                     "stdin_data": "kind: Deployment"}),
    ])
    def test_manifest_bans_name_the_accepted_kinds(self, tool, args):
        """Never say "only whitelisted kinds" without listing them.

        The list is rendered from ``ALLOWED_MANIFEST_KINDS`` so it cannot go
        stale relative to the check that enforces it.
        """
        from chaos_agent.agent.target_guard.classifier import ALLOWED_MANIFEST_KINDS

        d = self._decide(tool, args)
        for kind in ALLOWED_MANIFEST_KINDS:
            assert kind in d.suggestion, kind

    def test_certificate_ban_stays_a_dead_end(self):
        """An empty suggestion is a STATEMENT: no drill form exists.

        ``guard_gateway`` derives ``is_hard_floor`` from it, so filling this in
        with a placeholder would downgrade a real boundary to "reshape and
        retry" and send the model looking for a way around CSR approval.
        """
        from chaos_agent.tools.guard_gateway import decision_to_feedback

        d = self._decide("kubectl", {"subcommand": "certificate", "v_args": "approve c"})
        assert d.verdict == GuardVerdict.REJECT_BANNED
        assert d.suggestion == ""
        assert decision_to_feedback(d).is_hard_floor is True

    @pytest.mark.parametrize("tool,args", [
        ("kubectl", {"subcommand": "config", "v_args": "use-context other"}),
        ("kubectl", {"subcommand": "proxy", "v_args": "--port=8080"}),
        ("kubectl", {"subcommand": "apply", "v_args": "-f /tmp/x.yaml"}),
        ("_execute_skill_script", {"script": "x.sh"}),
    ])
    def test_ban_with_an_alternative_is_not_a_dead_end(self, tool, args):
        """The mirror of the case above: a stated alternative must reach the
        model as "reshapeable", or the suggestion contradicts the verdict."""
        from chaos_agent.tools.guard_gateway import decision_to_feedback

        fb = decision_to_feedback(self._decide(tool, args))
        assert fb.is_hard_floor is False
        assert fb.compliant_form


class TestUnparseableCallNamesTheMissingArgument:
    """``confidence=unknown`` must say WHICH argument was missing.

    The verdict alone ("classifier confidence=unknown for <cmd>") tells the
    model that something could not be parsed but not what, and this branch —
    unlike the ``approved is None`` one, which the screener's renderer annotates
    — gets no compensating note downstream. So it used to reach the model as a
    dead end with no lead, while the SCOPE_UNKNOWN branch right above it already
    forwarded the classifier's ``reject_detail`` and offered a way forward.

    The cause is available at the source: the python-app classifier knows which
    of ``target``/``action`` is absent, and the host classifier knows the
    ``command`` was empty.
    """

    APPROVED = ApprovedTarget(
        scope="host", namespace="", names=("h1",), blade_target="network",
    )

    @pytest.mark.parametrize("tool,args,expected", [
        ("host_inject", {"command": ""}, "empty 'command'"),
        ("blade_python_create", {"target": "redis"}, "missing action"),
        ("blade_python_create", {"action": "delay"}, "missing target"),
        ("blade_python_create", {}, "missing target and action"),
    ])
    def test_reason_names_the_missing_argument(self, tool, args, expected):
        effective = infer_effective_target(tool, args)
        d = target_drift_guard(effective, self.APPROVED)
        assert d.verdict == GuardVerdict.REJECT_UNKNOWN
        assert expected in d.reason, d.reason
        # And a form issue must offer the way forward, not just the diagnosis.
        assert d.suggestion
        assert "form issue" in d.suggestion

    @pytest.mark.parametrize("tool,args", [
        ("host_inject", {"command": "iptables -A INPUT -j DROP"}),
        ("blade_python_create", {"target": "redis", "action": "delay"}),
    ])
    def test_complete_call_still_classifies_high(self, tool, args):
        """The added detail must not leak into a successful classification."""
        effective = infer_effective_target(tool, args)
        assert effective.confidence == ConfidenceLevel.HIGH
        assert effective.reject_detail == ""


class TestEveryRejectionOffersAWayForward:
    """A rejection the model can act on must say HOW.

    ``suggestion`` is the only field carrying that, and the screener renders it
    verbatim. A rejection with neither a suggestion nor a downstream annotation
    reads as an unexplained wall — the failure mode this module's own
    ``SCOPE_ESCAPE`` comment warns about ("deliberately worded to make the model
    conclude this path is unviable — the exact opposite of restoring its
    perception").

    The one deliberate exception is ``approved is None``: the screener's
    renderer annotates that verdict itself (it is passed
    ``approved_missing=approved is None``), so the guidance reaches the model
    from there instead.

    A BANNED verdict with NO suggestion is also legitimate, but only when no
    compliant form exists at all — see
    ``TestBannedRejectionSeparatesCauseFromWayForward``, which pins both halves
    (``kubectl certificate`` stays empty; the other six must state theirs).
    """

    @pytest.mark.parametrize("effective,approved", [
        # scope drift across profiles
        (EffectiveTarget(scope="pod", namespace="ns", names=("p",)),
         ApprovedTarget(scope="host", namespace="", names=("h",))),
        # same-profile name drift
        (EffectiveTarget(scope="pod", namespace="ns", names=("other",)),
         ApprovedTarget(scope="pod", namespace="ns", names=("p",))),
        # namespace drift
        (EffectiveTarget(scope="pod", namespace="other", names=("p",)),
         ApprovedTarget(scope="pod", namespace="ns", names=("p",))),
        # unclassifiable call
        (EffectiveTarget(scope=SCOPE_UNKNOWN, namespace="", raw_command="mystery"),
         ApprovedTarget(scope="pod", namespace="ns", names=("p",))),
        # unknown confidence on a real scope
        (EffectiveTarget(scope="host", namespace="", raw_command="host_inject()",
                         confidence=ConfidenceLevel.UNKNOWN),
         ApprovedTarget(scope="host", namespace="", names=("h",))),
    ])
    def test_rejection_carries_a_suggestion(self, effective, approved):
        d = target_drift_guard(effective, approved)
        assert d.is_reject, d.verdict
        assert d.suggestion, (
            f"{d.verdict.value} ({d.reason}) gives the model nothing to act on"
        )


# ---------------------------------------------------------------------------
# is_reject / is_allow predicates
# ---------------------------------------------------------------------------


class TestVerdictPredicates:
    def test_allow_predicates(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        for scope, expected_verdict in [
            ("pod", GuardVerdict.ALLOW),
            (SCOPE_READONLY, GuardVerdict.READONLY),
        ]:
            eff = EffectiveTarget(scope=scope, namespace="ns", names=("a",))
            d = target_drift_guard(eff, approved)
            assert d.verdict == expected_verdict
            assert d.is_allow
            assert not d.is_reject

    def test_reject_predicates(self):
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        for scope, expected_verdict in [
            (SCOPE_BANNED, GuardVerdict.REJECT_BANNED),
            (SCOPE_UNKNOWN, GuardVerdict.REJECT_UNKNOWN),
        ]:
            confidence = (ConfidenceLevel.UNKNOWN
                          if scope == SCOPE_UNKNOWN else ConfidenceLevel.HIGH)
            eff = EffectiveTarget(
                scope=scope, namespace="ns",
                confidence=confidence,
            )
            d = target_drift_guard(eff, approved)
            assert d.verdict == expected_verdict
            assert d.is_reject
            assert not d.is_allow


# ---------------------------------------------------------------------------
# Tier 1 exec into tool pod — namespace bypass
# ---------------------------------------------------------------------------


class TestTier1ToolPodExec:
    """Tier 1 injection: kubectl exec into chaosblade tool pod → blade create.

    When blade v1.8.0 rejects --namespace for some subcommands, the
    inner blade command omits it. The guard must NOT reject as namespace
    drift — the names/labels check still validates target identity.
    """

    def test_tier1_skips_namespace_check(self):
        approved = ApprovedTarget(
            scope="pod", namespace="cms-demo",
            names=("accounting-6fbdb464c7-qn2vr",),
            blade_target="network",
        )
        effective = EffectiveTarget(
            scope="pod", namespace="",
            names=("accounting-6fbdb464c7-qn2vr",),
            blade_target="network",
            blade_action="drop",
            is_tier1_exec=True,
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_tier1_still_checks_names(self):
        approved = ApprovedTarget(
            scope="pod", namespace="cms-demo",
            names=("accounting-6fbdb464c7-qn2vr",),
            blade_target="network",
        )
        effective = EffectiveTarget(
            scope="pod", namespace="",
            names=("OTHER-pod-xyz",),
            blade_target="network",
            blade_action="drop",
            is_tier1_exec=True,
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_tier1_still_checks_scope(self):
        approved = ApprovedTarget(
            scope="pod", namespace="cms-demo",
            names=("accounting-6fbdb464c7-qn2vr",),
        )
        effective = EffectiveTarget(
            scope="node", namespace="",
            names=("some-node",),
            is_tier1_exec=True,
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_owner_scope_daemonset_vs_pod_allowed(self):
        approved = ApprovedTarget(scope="pod", namespace="kube-system",
                                  labels={"k8s-app": "kube-dns"})
        effective = EffectiveTarget(scope="daemonset", namespace="kube-system",
                                    names=("coredns",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_owner_scope_statefulset_vs_pod_allowed(self):
        approved = ApprovedTarget(scope="pod", namespace="ns",
                                  labels={"app": "mysql"})
        effective = EffectiveTarget(scope="statefulset", namespace="ns",
                                    names=("mysql",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_owner_scope_with_owner_names_allows_correct_deployment(self):
        """When owner_names is populated, effective name must be in the set."""
        approved = ApprovedTarget(
            scope="pod", namespace="kube-system",
            labels={"k8s-app": "kube-dns"},
            owner_names=("coredns",),
        )
        effective = EffectiveTarget(
            scope="deployment", namespace="kube-system",
            names=("coredns",),
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_owner_scope_with_owner_names_rejects_wrong_deployment(self):
        """When owner_names is populated, wrong deployment name is REJECTED."""
        approved = ApprovedTarget(
            scope="pod", namespace="cms-demo",
            labels={"opentelemetry.io/name": "cart"},
            owner_names=("cart",),
        )
        effective = EffectiveTarget(
            scope="deployment", namespace="cms-demo",
            names=("coredns",),  # wrong!
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "owner drift" in d.reason

    def test_non_owner_scope_node_vs_pod_rejected(self):
        """node is NOT an owner of pod — real scope drift."""
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="node", namespace="", names=("node-1",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_non_owner_scope_service_vs_pod_rejected(self):
        """service is NOT an owner of pod."""
        approved = ApprovedTarget(scope="pod", namespace="ns", names=("a",))
        effective = EffectiveTarget(scope="service", namespace="ns", names=("svc",))
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT

    def test_non_tier1_still_rejects_namespace_drift(self):
        approved = ApprovedTarget(
            scope="pod", namespace="cms-demo",
            names=("accounting-6fbdb464c7-qn2vr",),
        )
        effective = EffectiveTarget(
            scope="pod", namespace="default",
            names=("accounting-6fbdb464c7-qn2vr",),
            is_tier1_exec=False,
        )
        d = target_drift_guard(effective, approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT


class TestHostScope:
    """Host-scope faults (host_inject) are anchored by host_name + fault
    family, bypassing the k8s namespace/names/labels checks (steps 5-6)."""

    def _approved(self, **kw):
        base = dict(
            scope="host", namespace="", names=("node-1",),
            host_name="node-1", blade_target="network",
            blade_action="loss", lock_fault_type=True,
        )
        base.update(kw)
        return ApprovedTarget(**base)

    def _effective(self, **kw):
        base = dict(
            scope="host", namespace="", host_name="",
            blade_target="network", confidence=ConfidenceLevel.HIGH,
            raw_command="host_inject(...)",
        )
        base.update(kw)
        return EffectiveTarget(**base)

    def test_matching_host_family_allows(self):
        d = target_drift_guard(self._effective(), self._approved())
        assert d.verdict == GuardVerdict.ALLOW

    def test_fault_family_drift_rejected(self):
        # approved=network, effective=process → blade_target lock trips.
        d = target_drift_guard(
            self._effective(blade_target="process"), self._approved(),
        )
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "blade_target drift" in d.reason

    def test_host_name_drift_rejected(self):
        d = target_drift_guard(
            self._effective(host_name="node-2"), self._approved(),
        )
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "host drift" in d.reason

    def test_empty_effective_names_not_rejected_by_k8s_selector(self):
        # Regression: host_inject carries no k8s names; the old step-6
        # names/labels check would REJECT_DRIFT. Host branch bypasses it.
        d = target_drift_guard(self._effective(names=()), self._approved())
        assert d.verdict == GuardVerdict.ALLOW

    def test_host_effective_under_k8s_approval_is_scope_drift(self):
        approved = ApprovedTarget(scope="pod", namespace="prod", names=("p1",))
        d = target_drift_guard(self._effective(), approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "scope drift" in d.reason

    def test_k8s_effective_under_host_approval_is_scope_drift(self):
        # Reverse cross-profile direction: a host approval must never
        # legitimise a k8s-resource call. Dispatched before either
        # per-carrier DriftPolicy sees the pair.
        effective = EffectiveTarget(
            scope="pod", namespace="prod", names=("p1",),
            confidence=ConfidenceLevel.HIGH, raw_command="kubectl(...)",
        )
        d = target_drift_guard(effective, self._approved())
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "scope drift" in d.reason


class TestCrossFamilyScopeChange:
    """``host`` and ``python`` share the host profile, so a swap between them
    survives the profile comparison (step 4) and carries no comparable host
    identity (step 5). The fault TYPE is the only discriminator left.

    Concretely: approving "iptables DROP on h1" and executing "delay every Redis
    GET inside a python process" — or the reverse, where an in-process delay
    becomes a host-wide firewall rule — are different experiments with different
    blast radii. When the fault type cannot be compared, the guard must refuse
    rather than allow a match it cannot prove.
    """

    def _python_call(self) -> EffectiveTarget:
        return infer_effective_target(
            "blade_python_create",
            {"target": "redis", "action": "delay", "flags": "--time 500"},
        )

    def _host_call(self) -> EffectiveTarget:
        return infer_effective_target(
            "host_inject", {"command": "iptables -A INPUT -j DROP"},
        )

    def test_host_approval_rejects_python_call_without_fault_type(self):
        approved = ApprovedTarget(scope="host", namespace="", names=("h1",))
        d = target_drift_guard(self._python_call(), approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "fault family changed" in d.reason

    def test_python_approval_rejects_host_call_without_fault_type(self):
        approved = ApprovedTarget(scope="python", namespace="", names=())
        d = target_drift_guard(self._host_call(), approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "fault family changed" in d.reason

    def test_comparable_fault_types_still_reported_as_type_drift(self):
        """When both sides carry a blade_target the existing lock renders the
        verdict — the new check must not shadow its clearer message."""
        approved = ApprovedTarget(
            scope="host", namespace="", names=("h1",),
            blade_target="network", blade_action="loss",
        )
        d = target_drift_guard(self._python_call(), approved)
        assert d.verdict == GuardVerdict.REJECT_DRIFT
        assert "blade_target drift" in d.reason

    def test_same_scope_is_untouched_even_without_fault_type(self):
        """The check must key on a scope CHANGE, not on a missing blade_target:
        a normal host drill that never recorded one stays allowed."""
        approved = ApprovedTarget(scope="host", namespace="", names=("h1",))
        d = target_drift_guard(self._host_call(), approved)
        assert d.verdict == GuardVerdict.ALLOW

    def test_matching_python_drill_is_allowed(self):
        approved = ApprovedTarget(
            scope="python", namespace="", names=(),
            blade_target="redis", blade_action="delay",
        )
        d = target_drift_guard(self._python_call(), approved)
        assert d.verdict == GuardVerdict.ALLOW
