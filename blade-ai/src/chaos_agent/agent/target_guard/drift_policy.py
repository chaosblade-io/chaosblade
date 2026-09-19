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
from .classifier import CLUSTER_SCOPED_KINDS, canonicalise_kind

logger = logging.getLogger(__name__)


# Cluster-scoped kinds skip the namespace comparison — they live
# outside any namespace, so ``approved.namespace`` and
# ``effective.namespace`` are both expected to be empty.
# Single-sourced in the classifier since R21/G-5 (re-exported here for
# the drift consumers; the classifier's ``is_cluster_scoped_kind`` is
# the canonical predicate form).

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
    ``is_namespace_wide`` is set. (The screener closes the gap DATA-side
    before this check: it resolves the live labels<->names correspondence
    with a bounded cached probe and pins / refreshes the selectors, so a
    cross that genuinely picks the approved pods never reaches here —
    see ``_resolve_label_pod_names`` in ``tool_screener``.)
    """
    if not approved.labels:
        return False
    if not effective.labels:
        return False
    for k, v in approved.labels.items():
        if effective.labels.get(k) != v:
            return False
    return True


def _is_generation_successor(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> bool:
    """Would every effective pod name be a controller-owned successor of
    a frozen owner workload?

    A controller-owned pod's name is DERIVED from its direct controller
    (``<deployment>-<rs-hash>-<pod-hash>``, ``<statefulset>-<ordinal>``,
    ``<job>-<hash>``) — that derivation is the API server's create
    semantics, not a guess, so prefix-matching against the frozen owner
    set is a deterministic identity check: the anchor comes from
    freeze-time discovery (``discover_owner_names`` — both the labels
    channel and the names→ownerReferences channel), the prefix contract
    from the Kubernetes resource model. Consistent with the module's
    discipline, the policy never guesses from pod names — it only ever
    consults frozen anchors. The trailing ``-`` in the prefix keeps a
    mere name-sharing workload (``web`` vs ``webapp-1``) from matching.
    """
    if not approved.owner_names or not effective.names:
        return False
    prefixes = tuple(f"{owner}-" for owner in approved.owner_names if owner)
    if not prefixes:
        return False
    return all(
        any(n.startswith(p) for p in prefixes)
        for n in effective.names
    )


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
    if approved.fault_target:
        bits.append(f"fault_target={approved.fault_target}")
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
        # ---- 3.5 Infrastructure-vehicle exemption ---------------------------
        # An exec into an injection vehicle is access to the injection
        # MACHINERY, not an operation on the fault target: the exec'd pod
        # can never match the approved target's identity, so every
        # comparison below can only produce a false drift. Vehicle identity
        # is established DATA-side by the screener (task-registered
        # artifacts, the task's exec tool pod, or live label-selector
        # discovery against the cluster) and arrives here only as this
        # flag — the policy itself never guesses from pod names. Banned /
        # escape / readonly screening of the inner command already happened
        # in the classifier before this point.
        if effective.is_vehicle_exec:
            return None

        # ---- 3.6 Case-manifest mechanism writes -----------------------------
        # ONE additive branch ahead of the victim comparison. When the
        # settled case carries a ``mechanism_writes`` manifest and approval
        # froze it into ``mechanism_entries``, a call whose canonicalised
        # scope+namespace matches an ACTIVE entry's domain is judged by that
        # entry ALONE: names subset (or, for a prefix entry, every name
        # starting with the prefix) passes — anything else is drift with
        # manifest attribution. A call matching no entry's domain falls
        # through to the existing rules below, byte-identically.
        # Function-local import: ``mechanism_writes`` imports
        # ``CLUSTER_SCOPED_KINDS`` from this module at load time, so a
        # module-level back-import would cycle (same discipline as the
        # providers import further down).
        if approved.mechanism_entries:
            from .mechanism_writes import (
                match_mechanism_entries,
                names_within_entries,
            )

            entries = match_mechanism_entries(approved, effective)
            if entries:
                # Entries sharing one domain are ALTERNATIVES with the
                # OBJECT WRITE as the authorization unit: every effective
                # name covered by some entry → in-contract; any foreign
                # name → drift with manifest attribution (the rejection
                # names every entry so the human can tell under-declaration
                # from over-reach).
                if names_within_entries(entries, effective):
                    # In-contract mechanism write. Identity checking is
                    # done for this call — the carrier-agnostic checks
                    # (fault-type lock etc.) still run in the guard.
                    return None
                return GuardDecision(
                    verdict=GuardVerdict.REJECT_DRIFT,
                    reason=(
                        f"mechanism write outside the case manifest: "
                        f"effective names {list(effective.names)} not within "
                        f"manifest entries "
                        f"[{'; '.join(e.describe() for e in entries)}]; either "
                        f"the case under-declares its mechanism (edit the "
                        f"case's mechanism_writes frontmatter) or the plan "
                        f"over-reached (re-plan within the declared write set)"
                    ),
                    effective=effective,
                    suggestion=_build_suggestion(approved),
                )

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
            # However, blade_create targeting nodes (fault_target set) is a
            # real scope escalation and must still be blocked.
            if effective_scope in CLUSTER_SCOPED_KINDS and effective.fault_target:
                return GuardDecision(
                    verdict=GuardVerdict.REJECT_DRIFT,
                    reason=f"scope drift: blade {effective.fault_target} targets {effective_scope} under {approved_scope} approval",
                    effective=effective,
                    suggestion=_build_suggestion(approved),
                )
            if effective_scope not in CLUSTER_SCOPED_KINDS:
                check_ns = (approved.secondary_namespace or "default").strip()
                effective_ns = (effective.namespace or "default").strip()
                # Exempt tool pod namespaces (e.g. "chaosblade") for cluster-scoped
                # approved targets: node-scope faults legitimately need access to
                # injection infrastructure (exec into tool pods for carrier
                # operations). The exemption set is provider-declared
                # (``tool_pod_namespaces``) and unioned here — a module-level
                # providers import would cycle (providers register target_guard
                # consumers at import time), so this stays function-local.
                from chaos_agent.agent.providers import FaultProviderRegistry

                is_tool_ns = effective_ns in FaultProviderRegistry.union_tool_names(
                    "tool_pod_namespaces"
                )
                if check_ns != effective_ns and not is_tool_ns:
                    reason = (
                        f"secondary namespace drift: approved={check_ns} "
                        f"effective={effective_ns}"
                    )
                    if not approved.mechanism_entries:
                        # Cross-domain mechanism write with no case manifest:
                        # rejected exactly as today, plus the manifest-missing
                        # attribution so the finding routes to the drill-loop
                        # archive for backfill instead of being retried blind.
                        reason += (
                            "; cross-domain mechanism writes require a case "
                            "manifest (mechanism_writes frontmatter) authored "
                            "by the case and approved on the confirmation card "
                            "— manifest missing for this case"
                        )
                    return GuardDecision(
                        verdict=GuardVerdict.REJECT_DRIFT,
                        reason=reason,
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
                if effective.is_recovery_carrier and not effective.fault_target:
                    # Recovery-carrier pod under a SAME-scope approval (a
                    # scope=pod victim): the carrier is a NEW pod whose
                    # name can never equal the approved victim's, so the
                    # names/labels comparison below is a structural false
                    # drift. Scope already matched (same kind — not the
                    # secondary/owner paths) and the namespace check above
                    # anchored the carrier to the victim's namespace; the
                    # carrier's security boundary is its SHAPE (the
                    # five-condition fail-closed classifier check: name
                    # prefix, --restart=Never, bounded sleep skeleton,
                    # image whitelist, flag/overrides whitelist) plus
                    # task-side registration — never the name alone
                    # (design D7, recovery-carrier-standard). A
                    # workload-scoped victim (deployment) already reaches
                    # the same outcome via its pod secondary scope; this
                    # branch gives a pod-scoped victim the same anchoring.
                    pass
                else:
                    names_ok = _check_names_subset(approved, effective)
                    labels_ok = _check_labels_superset(approved, effective)
                    if not names_ok and not labels_ok:
                        # Generation-successor exemption (case #39): a
                        # pod-scope approval whose mechanism deletes the
                        # pod and lets the controller recreate it ALWAYS
                        # operates on a renamed successor — the names
                        # subset can never match, and the plan itself
                        # typically predicted the rename. This is the
                        # exact mirror of the owner-scope branch above:
                        # operating on the pod's OWNER (e.g. scale
                        # deployment, which affects EVERY replica) has
                        # always been legal, so "the workload's pods"
                        # were already inside the approved blast radius —
                        # accepting the owner's recreated (or sibling)
                        # pod here adds no surface the owner-scope path
                        # had not already granted. Scoped the same way:
                        # same kind (validated above), same namespace
                        # (validated above); the fault-type lock and the
                        # duration anchor still apply after identity
                        # clears. No owner anchor on record → the check
                        # fails closed into the ordinary drift reject.
                        if _is_generation_successor(approved, effective):
                            logger.info(
                                "target_guard: generation successor under "
                                "frozen owners %s (effective names %s) — "
                                "identity cleared",
                                list(approved.owner_names),
                                list(effective.names),
                            )
                        else:
                            return GuardDecision(
                                verdict=GuardVerdict.REJECT_DRIFT,
                                reason=_format_name_drift_reason(
                                    approved, effective,
                                ),
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
