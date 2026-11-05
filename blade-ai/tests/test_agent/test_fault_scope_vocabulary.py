"""Regression tests for the config-object / entry-plane fault scopes.

Openspec change ``register-config-object-fault-scopes``: ``ingress`` /
``configmap`` / ``secret`` are registered as first-class fault scopes so a
"patch the victim's OWN body" config-plane fault (Ingress backend rename,
ConfigMap tamper, TLS Secret invalidation) can declare the victim's own
scope. That makes ``approved.scope == effective.scope`` and opens the
guard's name-match path — WITHOUT any ``mechanism_writes`` legislation
(which structurally cannot name a pre-existing, environment-named victim:
its only derived name axis is ``victim_node``, and materialization runs at
freeze where only the spec — not the plan's patch target — is visible).

Before the fix the approved scope was forced to downgrade (e.g. to
``service``) because ``family_for_scope('ingress')`` was None, so the
victim-own-body write died as::

    scope drift: approved=service effective=ingress

Covers tasks §4.1-§4.7. The vocabulary-execution coupling invariant at the
bottom pins that every newly-registered scope has a faultdrill assembler
REST mapping, so the vocabulary layer can never silently outrun the
execution layer again.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.providers.faultdrill.assembler import (
    _KIND_REST_MAP,
    canonical_kind,
    rest_url_for,
)
from chaos_agent.agent.spec.fault_registry import aggregate_scopes, family_for_scope
from chaos_agent.agent.spec.fault_spec import INTENT_SCOPES, FaultSpec
from chaos_agent.agent.target_guard import (
    approved_from_dict,
    freeze_approved_target_from_spec,
)
from chaos_agent.agent.target_guard.classifier import (
    canonicalise_kind,
    is_cluster_scoped_kind,
)
from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
from chaos_agent.agent.target_guard.types import EffectiveTarget

# The three scopes this change registers. Each is the DIRECT victim of a
# real catalogue case: ingress ← Ingress_入口异常_后端服务不存在;
# secret ← Ingress_入口异常_TLS证书过期 (the patched object is the TLS
# Secret, the Ingress only references it); configmap ←
# ConfigMap_内容错误_关键配置被篡改.
NEW_SCOPES = ("ingress", "configmap", "secret")


def _spec(scope: str, name: str = "drill-victim") -> FaultSpec:
    return FaultSpec(
        scope=scope, namespace="default", names=[name],
        fault_target="config", fault_action="patch",
    )


def _approved(scope: str, name: str = "drill-victim"):
    return approved_from_dict(
        freeze_approved_target_from_spec(_spec(scope, name))
    )


def _eff(scope: str, name: str, ns: str = "default") -> EffectiveTarget:
    return EffectiveTarget(scope=scope, namespace=ns, names=(name,))


# ---------------------------------------------------------------------------
# §4.1 Vocabulary derivation invariants
# ---------------------------------------------------------------------------

class TestConfigObjectScopeVocabulary:
    def test_new_scopes_in_aggregate(self):
        scopes = aggregate_scopes()
        for s in NEW_SCOPES:
            assert s in scopes, f"{s} missing from aggregate_scopes()"

    def test_new_scopes_owned_by_k8s_family(self):
        for s in NEW_SCOPES:
            fam = family_for_scope(s)
            assert fam is not None, f"{s} has no owning family"
            assert fam.family_id == "k8s_chaosblade"
            assert fam.profile == "k8s"
            # Namespaced kinds — must NOT be declared cluster-scoped, or
            # freeze would null their namespace and the guard would treat
            # the -n dimension as absent.
            assert s not in (fam.cluster_scoped or ())

    def test_intent_scopes_derives_new_scopes(self):
        # INTENT_SCOPES = aggregate_scopes() (single source), so the whole
        # vocabulary chain (CLI --scope validation, server schema, l4 enum,
        # submit_fault_intent description) inherits the three for free.
        for s in NEW_SCOPES:
            assert s in INTENT_SCOPES

    def test_spec_with_new_scope_is_complete(self):
        for s in NEW_SCOPES:
            spec = FaultSpec(
                scope=s, namespace="default", names=["x"],
                fault_target="config", fault_action="patch",
                duration_seconds=360,
            )
            assert spec.is_complete, f"scope={s} spec rejected by is_complete"

    def test_unknown_scope_still_incomplete(self):
        # Negative: registering three config-object kinds must NOT open the
        # vocabulary to arbitrary words — an unknown scope still fails the
        # family_for_scope gate.
        spec = FaultSpec(
            scope="foobar", namespace="default", names=["x"],
            fault_target="config", fault_action="patch",
            duration_seconds=360,
        )
        assert not spec.is_complete


# ---------------------------------------------------------------------------
# §4.2 / §4.3 Drift adversarial matrix — victim-own-body write
# ---------------------------------------------------------------------------

class TestVictimOwnBodyDriftMatrix:
    @pytest.mark.parametrize("scope", NEW_SCOPES)
    def test_same_kind_same_name_allows(self, scope):
        # The regression anchor: a victim-own-body patch (approved scope ==
        # effective scope == the config-object kind, name matches) is
        # ALLOWED with no mechanism_writes. This is the exact write that
        # previously died as "scope drift: approved=service effective=ingress".
        decision = K8sDriftPolicy().check_identity_drift(
            _approved(scope, "drill-victim"),
            _eff(scope, "drill-victim"),
        )
        assert decision is None, (
            f"victim-own-body write on {scope} must pass, got: "
            f"{decision.reason if decision else None}"
        )

    def test_ingress_backend_rename_scenario_allows(self):
        # The concrete 1.9 shape: patch Ingress drill-echo-ing's backend
        # service name. Approved as scope=ingress (now declarable), the
        # assembler's target_kind=Ingress canonicalises to effective
        # scope=ingress → name-match path → ALLOW.
        decision = K8sDriftPolicy().check_identity_drift(
            _approved("ingress", "drill-echo-ing"),
            _eff("ingress", "drill-echo-ing"),
        )
        assert decision is None

    @pytest.mark.parametrize("approved_scope", NEW_SCOPES)
    def test_cross_kind_write_still_rejects(self, approved_scope):
        # No over-broadening: registering the three scopes must not let a
        # write to a DIFFERENT config-object kind through. Each new scope's
        # freeze secondary_scopes is the generic namespaced companion set
        # (pod/sa/role/rolebinding) — it does NOT include the other two.
        foreign = next(s for s in NEW_SCOPES if s != approved_scope)
        decision = K8sDriftPolicy().check_identity_drift(
            _approved(approved_scope, "drill-victim"),
            _eff(foreign, "drill-victim"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"
        assert "scope drift" in decision.reason

    def test_foreign_name_same_kind_rejects(self):
        # Same kind but a different object name is resource-selection drift.
        decision = K8sDriftPolicy().check_identity_drift(
            _approved("ingress", "drill-echo-ing"),
            _eff("ingress", "some-other-ing"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"


# ---------------------------------------------------------------------------
# §4.4 freeze namespaced companion-surface generalization
# ---------------------------------------------------------------------------

class TestFreezeCompanionSurface:
    @pytest.mark.parametrize("scope", NEW_SCOPES)
    def test_namespaced_scope_gets_carrier_companion_surface(self, scope):
        # W-55-1 generalized the recovery-carrier companion grant to EVERY
        # namespaced non-host scope (not a scope allowlist), so the three new
        # scopes inherit it with zero freeze.py change.
        frozen = freeze_approved_target_from_spec(_spec(scope, "drill-victim"))
        secondary = set(frozen.get("secondary_scopes") or ())
        assert {"pod", "serviceaccount", "role", "rolebinding"} <= secondary
        # Namespace preserved (namespaced kind, not cluster-scoped).
        assert frozen["namespace"] == "default"


# ---------------------------------------------------------------------------
# §4.5 / §4.6 Execution + canonicalisation layers already carry the kinds
# ---------------------------------------------------------------------------

class TestExecutionLayerKindSupport:
    @pytest.mark.parametrize("scope", NEW_SCOPES)
    def test_assembler_rest_map_covers_scope(self, scope):
        # The assembler's REST map (URL + RBAC shape + namespaced flag) is
        # the execution-layer half of the contract; it already carried all
        # three before the vocabulary was opened.
        assert scope in _KIND_REST_MAP
        _group, _version, _plural, namespaced = _KIND_REST_MAP[scope]
        assert namespaced is True

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # The assembler's canonical_kind normalizes a recipe's
            # ``targetRef.kind`` — always the singular canonical form (the
            # catalogue recipes declare ``kind: Ingress`` / ``Secret`` /
            # ``ConfigMap``). Plural / shorthand kubectl-arg normalization is
            # the CLASSIFIER's job (covered below), not the assembler's; the
            # two alias tables are intentionally asymmetric.
            ("Ingress", "ingress"), ("ingress", "ingress"),
            ("ConfigMap", "configmap"), ("configmap", "configmap"),
            ("cm", "configmap"),
            ("Secret", "secret"), ("secret", "secret"),
        ],
    )
    def test_assembler_canonical_kind(self, raw, expected):
        assert canonical_kind(raw) == expected

    @pytest.mark.parametrize("raw", ["ingresses", "ing", "secrets"])
    def test_assembler_fails_closed_on_unmapped_plural(self, raw):
        # Fail-closed contract: a plural / shorthand the assembler's alias
        # table does not carry raises rather than guessing a REST URL — the
        # recipe must use the singular canonical kind. Safe by construction;
        # the classifier (not the assembler) absorbs kubectl-arg plurals.
        with pytest.raises(ValueError):
            canonical_kind(raw)

    def test_assembler_rest_url_for_ingress(self):
        url = rest_url_for("Ingress", "drill-echo-ing", "default")
        assert "networking.k8s.io" in url
        assert "drill-echo-ing" in url

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Ingress", "ingress"), ("ingresses", "ingress"), ("ing", "ingress"),
            ("ConfigMap", "configmap"), ("cm", "configmap"),
            ("Secret", "secret"), ("secrets", "secret"),
        ],
    )
    def test_classifier_canonicalise_kind(self, raw, expected):
        assert canonicalise_kind(raw) == expected

    @pytest.mark.parametrize("scope", NEW_SCOPES)
    def test_classifier_not_cluster_scoped(self, scope):
        assert is_cluster_scoped_kind(scope) is False


# ---------------------------------------------------------------------------
# Vocabulary ↔ execution-layer coupling invariant (spec.md: 词表-执行层脱钩被捕获)
# ---------------------------------------------------------------------------

class TestVocabularyExecutionCoupling:
    def test_every_new_scope_has_execution_layer_mapping(self):
        # A registered fault scope that the faultdrill assembler cannot
        # build a REST URL / RBAC shape for would be a vocabulary layer
        # outrunning the execution layer — the exact decoupling this change
        # must never reintroduce. Pin the coupling for the three scopes.
        for scope in NEW_SCOPES:
            assert scope in aggregate_scopes(), f"{scope} unregistered"
            assert scope in _KIND_REST_MAP, (
                f"{scope} is a registered fault scope but the faultdrill "
                f"assembler has no REST mapping for it — vocabulary and "
                f"execution layers have decoupled"
            )
            assert canonicalise_kind(scope) == scope
            assert canonical_kind(scope) == scope


# ---------------------------------------------------------------------------
# expand-k8s-case-coverage §4.2/§4.3 — cronjob victim scope (CronJob_任务异常)
# ---------------------------------------------------------------------------

class TestCronJobVictimScope:
    """``cronjob`` as a victim-own-body fault scope.

    Same class as the config objects above — the CronJob_任务异常 injections
    patch the CronJob's OWN body (``spec.suspend`` for 任务被挂起,
    ``spec.jobTemplate`` backoff for 连续失败) — so it needs the identical
    vocabulary registration to open the guard's name-match path. Two facts
    distinguish it from ingress/configmap/secret and are pinned here:

    * it is ``batch/v1`` and was MISSING from the faultdrill assembler's
      ``_KIND_REST_MAP`` (the three config objects were already carried), so
      the vocabulary registration alone would have decoupled the layers;
    * it is ALSO an owner scope (jobs→pods traversal) — an orthogonal axis
      that must not disturb the victim-self-write name-match path.
    """

    def test_in_aggregate_and_owned_by_k8s_family(self):
        assert "cronjob" in aggregate_scopes()
        fam = family_for_scope("cronjob")
        assert fam is not None
        assert fam.family_id == "k8s_chaosblade"
        assert "cronjob" not in (fam.cluster_scoped or ())

    def test_in_intent_scopes_and_spec_complete(self):
        assert "cronjob" in INTENT_SCOPES
        spec = FaultSpec(
            scope="cronjob", namespace="default", names=["drill-cj"],
            fault_target="config", fault_action="patch", duration_seconds=360,
        )
        assert spec.is_complete

    def test_victim_own_body_write_allows(self):
        # The regression anchor: patch cronjob/drill-cj approved as
        # scope=cronjob → effective scope=cronjob, name matches → ALLOW.
        decision = K8sDriftPolicy().check_identity_drift(
            _approved("cronjob", "drill-cj"),
            _eff("cronjob", "drill-cj"),
        )
        assert decision is None, (
            f"cronjob victim-own-body write must pass, got: "
            f"{decision.reason if decision else None}"
        )

    def test_foreign_name_same_kind_rejects(self):
        decision = K8sDriftPolicy().check_identity_drift(
            _approved("cronjob", "drill-cj"),
            _eff("cronjob", "other-cj"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"

    def test_cross_kind_write_rejects(self):
        # Registering cronjob must not let a write to a different kind
        # (e.g. ingress) through under a cronjob approval.
        decision = K8sDriftPolicy().check_identity_drift(
            _approved("cronjob", "drill-cj"),
            _eff("ingress", "drill-cj"),
        )
        assert decision is not None
        assert decision.verdict.value == "reject_drift"

    def test_freeze_companion_surface(self):
        # Namespaced non-host scope → W-55-1 generalized recovery-carrier
        # companion grant, zero freeze.py change.
        frozen = freeze_approved_target_from_spec(_spec("cronjob", "drill-cj"))
        secondary = set(frozen.get("secondary_scopes") or ())
        assert {"pod", "serviceaccount", "role", "rolebinding"} <= secondary
        assert frozen["namespace"] == "default"

    def test_assembler_rest_map_is_batch_v1_namespaced(self):
        # The cronjob-specific gap: unlike the config objects it was absent
        # from _KIND_REST_MAP. Pin the batch/v1 namespaced mapping.
        assert "cronjob" in _KIND_REST_MAP
        group, version, plural, namespaced = _KIND_REST_MAP["cronjob"]
        assert (group, version, plural, namespaced) == (
            "batch", "v1", "cronjobs", True
        )

    def test_assembler_canonical_kind_and_rest_url(self):
        assert canonical_kind("CronJob") == "cronjob"
        assert canonical_kind("cronjob") == "cronjob"
        url = rest_url_for("CronJob", "drill-cj", "default")
        assert "apis/batch/v1" in url
        assert "namespaces/default/cronjobs/drill-cj" in url

    def test_classifier_canonicalise_and_not_cluster_scoped(self):
        # Plural / shorthand normalization is the classifier's job.
        assert canonicalise_kind("cronjob") == "cronjob"
        assert canonicalise_kind("cronjobs") == "cronjob"
        assert canonicalise_kind("cj") == "cronjob"
        assert is_cluster_scoped_kind("cronjob") is False

    def test_vocabulary_execution_coupling(self):
        # Same coupling invariant as the config objects: a registered scope
        # the assembler cannot build a REST URL for is a decoupling.
        assert "cronjob" in aggregate_scopes()
        assert "cronjob" in _KIND_REST_MAP
        assert canonicalise_kind("cronjob") == "cronjob"
        assert canonical_kind("cronjob") == "cronjob"
