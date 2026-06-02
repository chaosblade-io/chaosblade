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

from chaos_agent.agent.target_guard.classifier import (
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
)
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
)


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
