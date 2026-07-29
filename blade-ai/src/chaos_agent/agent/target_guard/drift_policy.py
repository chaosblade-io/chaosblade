"""Per-carrier target-drift policies.

The guard's job splits cleanly in two:

  - a **carrier-agnostic verdict skeleton** (sentinel scopes, missing
    approval, confidence, fault-type lock) that every carrier shares, and
  - a **carrier-specific identity check** — "is the resource this call would
    touch the same one the user approved?" — whose rules differ per carrier
    (Kubernetes compares namespace / names / labels with owner / secondary /
    tier1 exemptions; a bare host compares only the host name).

This module owns the second half as a small registry of ``DriftPolicy``
implementations keyed by capability profile (``k8s`` / ``host``). ``guard``
dispatches to ``resolve_drift_policy(profile).check_identity_drift(...)`` at the
exact position the hardcoded ``if is_host_scope(): ...return`` branch used to
sit, so adding a new carrier (cloud APIs, ...) is a new ``DriftPolicy`` plus a
registry entry — the guard skeleton stays untouched.

The K8s policy body is a verbatim move of the guard's former steps 4-6; the
host policy consults the carrier-agnostic ``TargetProtocol`` seam
(``as_target().matches()``) so host identity comparison no longer hardcodes
field access.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol

from .types import (
    ApprovedTarget,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
)
from .classifier import canonicalise_kind

logger = logging.getLogger(__name__)


# Cluster-scoped kinds skip the namespace comparison — they live
# outside any namespace, so ``approved.namespace`` and
# ``effective.namespace`` are both expected to be empty.
CLUSTER_SCOPED_KINDS: frozenset[str] = frozenset({
    "node", "pv", "namespace", "clusterrole",
    "clusterrolebinding", "storageclass",
})

# K8s ownership: approved scope → set of resource kinds that OWN it.
# When approved=pod and effective=deployment, the LLM is operating on
# the pod's owner (e.g. kubectl scale deployment) to affect the pods —
# this is a legitimate injection method, not scope drift.
OWNER_SCOPES: dict[str, frozenset[str]] = {
    "pod": frozenset({
        "deployment", "daemonset", "statefulset",
        "replicaset", "job", "cronjob",
    }),
    "deployment": frozenset({"replicaset"}),
}


# ---------------------------------------------------------------------------
# Selector subset helpers
# ---------------------------------------------------------------------------


def _check_names_subset(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> bool:
    """Is ``effective.names`` a non-empty subset of the approved name set?

    The approved name set is ``approved.names`` when the approval was
    name-based, OR ``approved.resolved_names`` when the approval was
    label-based and the label selector was resolved to concrete names at
    freeze time (e.g. an availability-zone node fault approved by a zone
    label, then executed per node name in batches). Validating against the
    resolved set lets an in-zone name batch pass while an out-of-zone name is
    still rejected — closing the false-positive labels↔names drift without
    weakening the guard (the names MUST be members of the frozen zone).

    Returns True only when:
      - the approved side has an explicit name set (``names`` or
        ``resolved_names``)
      - effective has explicit names
      - every name in effective is in that approved set

    Empty effective names means "the tool_call didn't pin a name"
    (e.g. labels-only) — we delegate to the labels check.
    """
    approved_name_set = approved.names or approved.resolved_names
    if not approved_name_set:
        return False
    if not effective.names:
        return False
    return all(n in approved_name_set for n in effective.names)


def _check_labels_superset(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> bool:
    """Is ``effective.labels`` a SUPERSET of ``approved.labels``?

    "Superset" = stricter selector. If approved selects ``app=demo``
    and effective selects ``app=demo,env=prod``, the effective set is
    a subset of the approved set (narrower) — that's safe.

    Returns False when approved has no labels (no labels-based
    approval), or when any approved key/value is missing/different in
    effective.

    Without cluster-state lookup we can't verify whether
    ``approved.names`` resolve to the same pods as ``effective.labels``
    or vice versa. Hence: labels-vs-names cross is rejected unless
    ``is_namespace_wide`` is set.
    """
    if not approved.labels:
        return False
    if not effective.labels:
        return False
    for k, v in approved.labels.items():
        if effective.labels.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Reason / suggestion formatting (for audit logs + LLM ToolMessage)
# ---------------------------------------------------------------------------


def _format_name_drift_reason(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> str:
    """Build a drift reason that distinguishes name vs label mismatch."""
    a_parts: list[str] = []
    if approved.names:
        a_parts.append(f"approved.names={list(approved.names)}")
    if approved.labels:
        a_parts.append(f"approved.labels={dict(approved.labels)}")
    if not a_parts:
        a_parts.append("approved.<no-selector>")

    e_parts: list[str] = []
    if effective.names:
        e_parts.append(f"effective.names={list(effective.names)}")
    if effective.labels:
        e_parts.append(f"effective.labels={dict(effective.labels)}")
    if not e_parts:
        e_parts.append("effective.<no-selector>")

    return "resource selection drift: " + ", ".join(a_parts) + " vs " + ", ".join(e_parts)


def _build_suggestion(approved: ApprovedTarget) -> str:
    """A short hint surfaced to the LLM in the rejection ToolMessage.

    Tells it what WAS approved so it can either correct its call or
    deliberately invoke replan rather than blindly retrying on the
    same wrong target. The carrier-agnostic ``describe()`` label is
    appended so host / cloud targets read naturally too.
    """
    bits: list[str] = [
        f"scope={approved.scope}",
        f"ns={approved.namespace or '<cluster>'}",
    ]
    if approved.names:
        bits.append(f"names={list(approved.names)}")
    if approved.labels:
        bits.append(f"labels={dict(approved.labels)}")
    if approved.blade_target:
        bits.append(f"blade_target={approved.blade_target}")
    if approved.is_namespace_wide:
        bits.append("namespace-wide=true")
    bits.append(f"target={approved.as_target().describe()}")
    return "approved target: " + ", ".join(bits)


# ---------------------------------------------------------------------------
# Drift policies (per capability profile)
# ---------------------------------------------------------------------------


class DriftPolicy(Protocol):
    """Carrier-specific identity-drift check.

    Returns a ``GuardDecision`` (a REJECT verdict) when the effective target
    drifts from the approved one, or ``None`` when identity matches and the
    guard should continue to the carrier-agnostic checks (fault-type lock,
    allow).
    """

    def check_identity_drift(
        self, approved: ApprovedTarget, effective: EffectiveTarget,
    ) -> Optional[GuardDecision]: ...


class K8sDriftPolicy:
    """Kubernetes identity drift: namespace / names / labels with owner,
    secondary-scope, tier1-exec and tool-pod-namespace exemptions.

    Verbatim relocation of the guard's former steps 4-6.
    """

    def check_identity_drift(
        self, approved: ApprovedTarget, effective: EffectiveTarget,
    ) -> Optional[GuardDecision]:
        # ---- 4. Scope (kind) check ------------------------------------------
        approved_scope = canonicalise_kind(approved.scope)
        effective_scope = canonicalise_kind(effective.scope)
        is_owner_scope = False
        is_secondary_scope = False
        if approved_scope != effective_scope:
            owners = OWNER_SCOPES.get(approved_scope, frozenset())
            secondary = set(approved.secondary_scopes or ())
            if effective_scope in owners:
                is_owner_scope = True
            elif effective_scope in secondary:
                is_secondary_scope = True
            else:
                return GuardDecision(
                    verdict=GuardVerdict.REJECT_DRIFT,
                    reason=f"scope drift: approved={approved_scope} effective={effective_scope}",
                    effective=effective,
                    suggestion=_build_suggestion(approved),
                )

        # ---- 5. Namespace check (cluster-scoped kinds exempt) ---------------
        # Tier 1 injection (kubectl exec into tool pod → blade create)
        # legitimately omits --namespace when blade v1.8.0 rejects it.
        # The actual target is identified by --names/--labels; step 6
        # (resource selection) validates identity.
        if is_secondary_scope:
            # Secondary scope (e.g. pod ops under node approval): validate
            # against secondary_namespace (preserved from FaultSpec before
            # cluster-scope clearing). Cluster-scoped effective targets
            # (node, pv) skip namespace check — they have no namespace.
            # However, blade_create targeting nodes (blade_target set) is a
            # real scope escalation and must still be blocked.
            if effective_scope in CLUSTER_SCOPED_KINDS and effective.blade_target:
                return GuardDecision(
                    verdict=GuardVerdict.REJECT_DRIFT,
                    reason=f"scope drift: blade {effective.blade_target} targets {effective_scope} under {approved_scope} approval",
                    effective=effective,
                    suggestion=_build_suggestion(approved),
                )
            if effective_scope not in CLUSTER_SCOPED_KINDS:
                check_ns = (approved.secondary_namespace or "default").strip()
                effective_ns = (effective.namespace or "default").strip()
                # Exempt tool pod namespaces (e.g. "chaosblade") for cluster-scoped
                # approved targets: node-scope faults legitimately need access to
                # injection infrastructure (exec into tool pods for blade operations).
                from chaos_agent.agent.target_guard.classifier import TOOL_POD_NAMESPACES
                is_tool_ns = effective_ns in TOOL_POD_NAMESPACES
                if check_ns != effective_ns and not is_tool_ns:
                    return GuardDecision(
                        verdict=GuardVerdict.REJECT_DRIFT,
                        reason=f"secondary namespace drift: approved={check_ns} effective={effective_ns}",
                        effective=effective,
                        suggestion=_build_suggestion(approved),
                    )
        elif effective_scope not in CLUSTER_SCOPED_KINDS and not effective.is_tier1_exec:
            approved_ns = (approved.namespace or "default").strip()
            effective_ns = (effective.namespace or "default").strip()
            if approved_ns != effective_ns:
                return GuardDecision(
                    verdict=GuardVerdict.REJECT_DRIFT,
                    reason=f"namespace drift: approved={approved_ns} effective={effective_ns}",
                    effective=effective,
                    suggestion=_build_suggestion(approved),
                )

        # ---- 6. Resource selection (names / labels) -------------------------
        # is_namespace_wide is an explicit operator opt-in saying "any
        # resource of this kind in this namespace is OK". Used for
        # demo/test envs where the user does not want to enumerate names.
        # Secondary scope: skip names/labels check — pod names cannot match
        # node names, and the namespace check above is sufficient.
        if is_secondary_scope:
            pass
        elif not approved.is_namespace_wide:
            if is_owner_scope:
                # Owner-scope: validate at instance level using
                # pre-discovered owner_names (frozen at confirmation_gate).
                if approved.owner_names and effective.names:
                    if not all(n in approved.owner_names for n in effective.names):
                        return GuardDecision(
                            verdict=GuardVerdict.REJECT_DRIFT,
                            reason=(
                                f"owner drift: effective names {list(effective.names)} "
                                f"not in approved owners {list(approved.owner_names)}"
                            ),
                            effective=effective,
                            suggestion=_build_suggestion(approved),
                        )
                elif not approved.owner_names:
                    logger.info(
                        "target_guard: no owner_names on record, namespace-only "
                        "anchoring for owner-scope (approved=%s, effective=%s/%s ns=%s)",
                        approved.scope, effective.scope,
                        effective.names, effective.namespace,
                    )
            else:
                names_ok = _check_names_subset(approved, effective)
                labels_ok = _check_labels_superset(approved, effective)
                if not names_ok and not labels_ok:
                    return GuardDecision(
                        verdict=GuardVerdict.REJECT_DRIFT,
                        reason=_format_name_drift_reason(approved, effective),
                        effective=effective,
                        suggestion=_build_suggestion(approved),
                    )

        return None


class HostDriftPolicy:
    """Bare-host identity drift: anchored by host name, not k8s selectors.

    Host faults (raw shell over a host transport, e.g. ``host_inject``) carry
    no namespace / names / labels — identity is the host name plus fault
    family. This consults the carrier-agnostic ``TargetProtocol`` seam so the
    comparison does not hardcode field access.
    """

    def check_identity_drift(
        self, approved: ApprovedTarget, effective: EffectiveTarget,
    ) -> Optional[GuardDecision]:
        a_host = (approved.host_name or "").strip()
        e_host = (effective.host_name or "").strip()
        # Only compare when BOTH sides name a host — an unclassifiable host
        # command is already gated by ToolGuard, not by identity drift.
        if a_host and e_host and not approved.as_target().matches(effective.as_target()):
            return GuardDecision(
                verdict=GuardVerdict.REJECT_DRIFT,
                reason=f"host drift: approved={a_host} effective={e_host}",
                effective=effective,
                suggestion=_build_suggestion(approved),
            )
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S  # noqa: E402

_DRIFT_POLICIES: dict[str, DriftPolicy] = {
    PROFILE_K8S: K8sDriftPolicy(),
    PROFILE_HOST: HostDriftPolicy(),
}


def resolve_drift_policy(profile: str) -> DriftPolicy:
    """Return the drift policy for a capability ``profile`` (defaults to the
    K8s policy when the profile has no registered policy)."""
    return _DRIFT_POLICIES.get(profile, _DRIFT_POLICIES[PROFILE_K8S])


__all__ = [
    "CLUSTER_SCOPED_KINDS",
    "OWNER_SCOPES",
    "DriftPolicy",
    "K8sDriftPolicy",
    "HostDriftPolicy",
    "resolve_drift_policy",
]
