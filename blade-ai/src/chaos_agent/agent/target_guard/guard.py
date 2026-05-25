"""Compare an EffectiveTarget against the ApprovedTarget.

The guard is the policy core of the target-drift subsystem. Inputs:

  - ``ApprovedTarget`` — the user-approved snapshot frozen at
    confirmation_gate.
  - ``EffectiveTarget`` — what the in-flight tool_call would actually
    do (produced by ``classifier.infer_effective_target``).

Output:

  - ``GuardDecision`` with one of five verdicts. The decision order
    (short-circuiting at the first hit) is:

    1. Sentinel scopes first (READONLY / BANNED / UNKNOWN) — these
       bypass comparison entirely.
    2. ``approved is None`` defence — guard called without prior
       approval is a wiring bug; we default-deny.
    3. UNKNOWN confidence on a real scope → REJECT_UNKNOWN.
    4. **scope mismatch** — coarsest drift (user approved pod, LLM
       trying node).
    5. **namespace mismatch** — after default-normalisation and the
       cluster-scoped exception.
    6. **names / labels subset** — finest-grained "same kind+ns but
       different resource" check.
    7. **blade_target lock** — only when ``approved.lock_fault_type``
       is True AND both sides carry a blade_target. Method switches
       (kubectl-native ↔ blade) are intentionally NOT drift.

Why this order: cheap rejects first, so audit logs surface the
coarsest reason. Saying "scope drift pod→node" is more useful to an
operator than "labels drift" if the kind is also wrong.

Why low-confidence is treated specially: the classifier can fail in
two ways. ``UNKNOWN`` means it gave up entirely (malformed args, new
tool, escape attempt) — that's a default-deny case. ``LOW`` means it
parsed but had to guess (defaulted namespace, opaque shell command in
``kubectl exec``) — we still compare, but log so operators can spot
recurring low-confidence patterns and tighten the classifier.
"""

from __future__ import annotations

import logging
from typing import Optional

from .classifier import (
    SCOPE_BANNED,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
)
from .types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
)

logger = logging.getLogger(__name__)


# Cluster-scoped kinds skip the namespace comparison — they live
# outside any namespace, so ``approved.namespace`` and
# ``effective.namespace`` are both expected to be empty.
CLUSTER_SCOPED_KINDS: frozenset[str] = frozenset({
    "node", "pv", "namespace", "clusterrole",
    "clusterrolebinding", "storageclass",
})


def target_drift_guard(
    effective: EffectiveTarget,
    approved: Optional[ApprovedTarget],
) -> GuardDecision:
    """Decide whether ``effective`` matches the approved target.

    Args:
        effective: parsed from the LLM's tool_call (via
            ``classifier.infer_effective_target``).
        approved: snapshot frozen at confirmation_gate. ``None`` means
            no approval is on record — the caller (e.g. the screener
            node) should ordinarily not reach the guard in that state,
            but we default-deny here as defence-in-depth.

    Returns:
        GuardDecision — see ``types.GuardDecision`` for fields.
    """
    # ---- 1. Sentinel scopes -----------------------------------------------
    if effective.scope == SCOPE_READONLY:
        return GuardDecision(
            verdict=GuardVerdict.READONLY,
            reason="tool is read-only",
            effective=effective,
        )
    if effective.scope == SCOPE_BANNED:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason="tool is in the banned list",
            effective=effective,
        )
    if effective.scope == SCOPE_UNKNOWN:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason=f"could not classify tool_call: {effective.raw_command}",
            effective=effective,
        )

    # ---- 2. Defence: real scope but no approval on record -----------------
    if approved is None:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason="no approved target on record",
            effective=effective,
        )

    # ---- 3. UNKNOWN confidence on a real scope ---------------------------
    # Classifier returned a guessed scope without enough info — refuse.
    if effective.confidence == ConfidenceLevel.UNKNOWN:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason=f"classifier confidence=unknown for {effective.raw_command}",
            effective=effective,
        )

    # ---- 4. Scope (kind) check ------------------------------------------
    approved_scope = canonicalise_kind(approved.scope)
    effective_scope = canonicalise_kind(effective.scope)
    if approved_scope != effective_scope:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_DRIFT,
            reason=f"scope drift: approved={approved_scope} effective={effective_scope}",
            effective=effective,
            suggestion=_build_suggestion(approved),
        )

    # ---- 5. Namespace check (cluster-scoped kinds exempt) ---------------
    if effective_scope not in CLUSTER_SCOPED_KINDS:
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
    if not approved.is_namespace_wide:
        names_ok = _check_names_subset(approved, effective)
        labels_ok = _check_labels_superset(approved, effective)
        if not names_ok and not labels_ok:
            return GuardDecision(
                verdict=GuardVerdict.REJECT_DRIFT,
                reason=_format_name_drift_reason(approved, effective),
                effective=effective,
                suggestion=_build_suggestion(approved),
            )

    # ---- 7. Blade target lock (fault TYPE, not method) ------------------
    # Only compare when BOTH sides carry a blade_target. Switching
    # between blade and kubectl-native methods on the same target is
    # method autonomy, not drift — that is the explicit requirement
    # from the user spec ("方式可以变, 身份不能变").
    if approved.lock_fault_type:
        a_bt = (approved.blade_target or "").lower()
        e_bt = (effective.blade_target or "").lower()
        if a_bt and e_bt and a_bt != e_bt:
            return GuardDecision(
                verdict=GuardVerdict.REJECT_DRIFT,
                reason=f"blade_target drift: approved={a_bt} effective={e_bt}",
                effective=effective,
                suggestion=f"approved fault type is {a_bt}; trigger replan to switch types",
            )

    # ---- 8. Allow (log if LOW confidence so we can audit) ---------------
    if effective.confidence == ConfidenceLevel.LOW:
        logger.info(
            "target_guard: accepting LOW-confidence call %s (matches approved %s/%s)",
            effective.raw_command, approved.scope, approved.namespace,
        )

    return GuardDecision(
        verdict=GuardVerdict.ALLOW,
        reason="effective target matches approved",
        effective=effective,
    )


# ---------------------------------------------------------------------------
# Selector subset helpers
# ---------------------------------------------------------------------------


def _check_names_subset(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> bool:
    """Is ``effective.names`` a non-empty subset of ``approved.names``?

    Returns True only when:
      - approved has explicit names (not labels-only or namespace-wide)
      - effective has explicit names
      - every name in effective is in approved

    Empty effective names means "the tool_call didn't pin a name"
    (e.g. labels-only) — we delegate to the labels check.
    """
    if not approved.names:
        return False
    if not effective.names:
        return False
    return all(n in approved.names for n in effective.names)


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
    same wrong target.
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
    return "approved target: " + ", ".join(bits)


__all__ = [
    "CLUSTER_SCOPED_KINDS",
    "target_drift_guard",
]
