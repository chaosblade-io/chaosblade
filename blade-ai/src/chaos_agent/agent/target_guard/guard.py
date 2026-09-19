"""Compare an EffectiveTarget against the ApprovedTarget.

The guard is the policy core of the target-drift subsystem. Inputs:

  - ``ApprovedTarget`` — the user-approved snapshot frozen at
    confirmation_gate.
  - ``EffectiveTarget`` — what the in-flight tool_call would actually
    do (produced by ``classifier.infer_effective_target``).

Output:

  - ``GuardDecision`` with one of five verdicts. The guard body is a
    **carrier-agnostic skeleton**; the identity comparison (namespace /
    names / labels for k8s, host name for bare hosts) is delegated to a
    per-carrier ``DriftPolicy`` (see ``drift_policy``). The decision order
    (short-circuiting at the first hit) is:

    1. Sentinel scopes first (READONLY / BANNED / UNKNOWN) — these
       bypass comparison entirely.
    2. ``approved is None`` defence — guard called without prior
       approval is a wiring bug; we default-deny.
    3. UNKNOWN confidence on a real scope → REJECT_UNKNOWN.
    4. **cross-profile drift** — approved a host, effective a k8s
       resource (or vice versa) is coarse drift.
    5. **carrier identity drift** — the ``DriftPolicy`` for the target's
       profile compares scope / namespace / names / labels (k8s) or host
       name (host).
    6. **fault-type checks** — a scope change crossing FAULT FAMILIES
       (``_cross_family_scope_change``) needs a usable fault_target as the
       only remaining discriminator; then the **fault_target lock**
       (``_fault_type_lock_drift``) compares fault types when
       ``approved.lock_fault_type`` is True AND both sides carry a
       fault_target. Method switches (kubectl-native ↔ blade) are
       intentionally NOT drift.
    6.5 **duration anchor** (``_duration_anchor_drift``) — an execution
       ``--timeout`` inflating the frozen contract ``duration_seconds``
       beyond the headroom ceiling is drift: the timeout bounds the
       experiment's auto-recovery, and an inflated bound converts the
       approved window into unbounded fault residence.

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

from chaos_agent.agent.spec.fault_registry import profile_of_scope

from .classifier import (
    SCOPE_BANNED,
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
    canonicalise_kind,
)
from .drift_policy import (
    CLUSTER_SCOPED_KINDS,
    OWNER_SCOPES,
    _build_suggestion,
    resolve_drift_policy,
)
from .types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardDecision,
    GuardVerdict,
)

logger = logging.getLogger(__name__)


# Duration-headroom ceiling for ``_duration_anchor_drift``: the execution
# ``--timeout`` may carry this much operational margin over the frozen
# contract duration before the inflation counts as drift. 2x keeps every
# skill-advised margin legal while catching the unbounded rewrites
# (999999s ≈ 11.5 days against a 300s contract).
_DURATION_DRIFT_FACTOR = 2


def _fault_type_lock_drift(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> Optional[GuardDecision]:
    """Carrier-agnostic fault-TYPE lock (not method).

    Only compare when BOTH sides carry a fault_target. Switching between
    blade and kubectl-native methods on the same target is method autonomy,
    not drift — that is the explicit requirement from the user spec
    ("方式可以变, 身份不能变"). Shared by every carrier (k8s and host).

    One exception, and it is the reason this check is load-bearing rather than
    cosmetic: when the SCOPE changed within a profile (see
    ``_same_profile_scope_change_needs_fault_type``), the fault type is the ONLY
    remaining evidence that the call is still the approved experiment, so an
    unusable comparison must fail closed instead of falling through to ALLOW.
    """
    if approved.lock_fault_type:
        a_bt = (approved.fault_target or "").lower()
        e_bt = (effective.fault_target or "").lower()
        if a_bt and e_bt and a_bt != e_bt:
            return GuardDecision(
                verdict=GuardVerdict.REJECT_DRIFT,
                reason=f"fault_target drift: approved={a_bt} effective={e_bt}",
                effective=effective,
                suggestion=f"approved fault type is {a_bt}; trigger replan to switch types",
            )
    return None


def _cross_family_scope_change(
    approved: ApprovedTarget, effective: EffectiveTarget,
) -> Optional[GuardDecision]:
    """Fail closed on a scope change that crosses FAULT FAMILIES.

    Step 4 rejects a change of PROFILE, but a profile can hold more than one
    family. Within the k8s family (``pod`` / ``node`` / ``container`` /
    ``deployment`` / ``statefulset`` / ``daemonset`` / ``service``) a scope change
    is legitimate and expected: approving a Deployment and injecting into the Pod
    it owns is the documented owner relationship, validated by the identity
    policy in step 5.

    ``host`` and ``python`` are DIFFERENT families that happen to share the host
    profile, and no ownership relates them. Approving "iptables DROP on h1" while
    executing "delay every Redis GET inside a python process" — or the reverse,
    where an in-process delay becomes a host-wide firewall rule — are different
    experiments with different blast radii. The identity policy cannot separate
    them either: it compares host names, and a python-agent call carries none.

    The fault TYPE is therefore the only discriminator left, so it must be
    USABLE. When either side lacks a ``fault_target`` there is no evidence the
    call is the approved experiment, and the guard refuses rather than allowing a
    match it cannot prove.

    Reachability: ``target`` is a required intent parameter for every scope, so a
    complete FaultSpec always carries one and this branch stays dormant in normal
    traffic. It exists for paths that bypass normal construction — a restored
    checkpoint, an external SDK caller, or a future family added to an existing
    profile without its own identity policy.
    """
    approved_scope = (approved.scope or "").lower()
    effective_scope = (effective.scope or "").lower()
    if not approved_scope or not effective_scope:
        return None
    if approved_scope == effective_scope:
        return None

    from chaos_agent.agent.spec.fault_registry import family_for_scope

    approved_family = family_for_scope(approved_scope)
    effective_family = family_for_scope(effective_scope)
    if approved_family is None or effective_family is None:
        return None  # unregistered scope — step 4 / step 5 own that case
    if approved_family.family_id == effective_family.family_id:
        return None  # same family: owner relationships are step 5's business

    if (approved.fault_target or "").strip() and (
        effective.fault_target or ""
    ).strip():
        # Comparable: ``_fault_type_lock_drift`` renders the verdict.
        return None
    return GuardDecision(
        verdict=GuardVerdict.REJECT_DRIFT,
        reason=(
            f"fault family changed (approved={approved_scope} "
            f"effective={effective_scope}) and the fault type cannot be "
            f"compared (approved fault_target="
            f"{approved.fault_target or '<empty>'}, effective fault_target="
            f"{effective.fault_target or '<empty>'})"
        ),
        effective=effective,
        suggestion=_build_suggestion(approved),
    )


def _duration_anchor_drift(
    approved: ApprovedTarget,
    effective: EffectiveTarget,
) -> Optional[GuardDecision]:
    """REJECT when the execution timeout inflates the contract duration.

    Compares ``effective.timeout_seconds`` (the verbatim ``--timeout`` the
    call's blade tokens carry, last-wins per pflag) against the frozen
    contract duration (``approved.duration_seconds``, from
    ``FaultSpec.duration_seconds`` at the single freeze point). Silent
    when either side is zero — no anchor, no comparison (the executor
    then injects the contract-derived timeout itself, and a sub-floor
    contract stays verbatim per the DNS-hijack discipline). Only the
    two blade surfaces populate ``timeout_seconds``, so the net is
    naturally scoped to blade fault injection.

    The headroom factor exists because the timeout bounds the
    experiment's AUTO-RECOVERY — layer 3 of the three-layer duration
    guarantee — and legitimately carries operational margin (skills
    advise slack for slow clusters). Beyond it, an inflated timeout is
    not margin but a rewrite of the user-approved verification window
    into an unbounded fault residence time, which survives the task's
    own death and a failed cleanup chain (twelfth-round finding E4:
    ``--timeout 999999`` rode the free-form flags string past every
    anchor).
    """
    if not approved.duration_seconds or not effective.timeout_seconds:
        return None
    ceiling = approved.duration_seconds * _DURATION_DRIFT_FACTOR
    if effective.timeout_seconds > ceiling:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_DRIFT,
            reason=(
                f"duration drift: execution --timeout "
                f"{effective.timeout_seconds}s exceeds the approved contract "
                f"duration {approved.duration_seconds}s beyond the "
                f"{_DURATION_DRIFT_FACTOR}x headroom ceiling ({ceiling}s); "
                f"the timeout bounds the experiment's auto-recovery, so an "
                f"inflated value converts the approved verification window "
                f"into unbounded fault residence when the task dies before "
                f"its cleanup chain runs"
            ),
            effective=effective,
            suggestion=(
                f"Keep --timeout within {_DURATION_DRIFT_FACTOR}x the approved "
                f"duration ({approved.duration_seconds}s), or omit it and let "
                f"the executor derive the bound from the contract."
            ),
        )
    return None


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
        # The classifier knows WHY the call is banned (which subcommand, an
        # invisible ``-f`` file, a kubeconfig write, ...). Surface that precise
        # cause when present. Even without it, echo the raw command so the
        # reason can never fully mask what was rejected.
        banned_detail = (effective.reject_detail or "").strip()
        if not banned_detail:
            banned_detail = "tool is in the banned list"
            if effective.raw_command:
                banned_detail = f"{banned_detail}: {effective.raw_command}"
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason=banned_detail,
            effective=effective,
            # The compliant alternative, when one exists, likewise comes from
            # the classifier — only it knows which ban fired and what the
            # whitelist is. An EMPTY value is a statement, not an omission: it
            # says no drill form exists (``kubectl certificate``), which is what
            # ``guard_gateway`` reads to report a boundary instead of a
            # reshapeable call.
            suggestion=(effective.reject_suggestion or "").strip(),
        )
    if effective.scope == SCOPE_ESCAPE:
        # A container-escape primitive (chroot/nsenter/unshare) reaching the
        # guard means carrier resolution in the screener did NOT clear it —
        # either it is not running through an approved, current, privileged
        # debug pod on the approved node, or the host mutation is not
        # self-recovering. This is NOT a parsing failure and NOT a permanent
        # dead-end: it is a viable path once expressed correctly. The old
        # reason was deliberately worded to make the model "conclude this path
        # is unviable" and give up — the exact opposite of restoring its
        # perception. Tell the truth AND the compliant form instead.
        #
        # ``reject_detail`` / ``reject_suggestion`` are recorded by whoever
        # observed the refusal — the screener's carrier gate
        # (``carriers.CarrierResolution``, which supplies BOTH) or the
        # classifier's static escape branch (which supplies only the detail).
        # The two fallbacks below are therefore what keeps cause and fix
        # describing the SAME condition: a carrier gate replaces both, the
        # classifier replaces only the cause and its generic escape wording
        # already matches the generic fix, and with nothing recorded both fall
        # back together.
        #
        # The fallback reason keeps its OR-form on purpose: with the real gate
        # unknown, naming one condition would be a guess, and a guess is what
        # sent task-866648cc chasing its debug pod's legitimacy for nine
        # minutes.
        escape_detail = (effective.reject_detail or "").strip() or (
            "it is not running through an approved, current, privileged debug "
            "pod on the approved node, OR the host mutation is not "
            "self-recovering"
        )
        escape_suggestion = (effective.reject_suggestion or "").strip() or (
            "This path IS available once expressed correctly: run the host "
            "operation through an approved-node privileged debug pod — "
            "`kubectl exec <debug-pod> -- <host-entry> ...`, where "
            "<host-entry> is ANY accepted primitive (`chroot /host ...`, "
            "`nsenter -t 1 -m -u -n -i ...`, or `unshare ...`) — and make "
            "the mutation self-recover by pairing the forward command with "
            "its own inverse behind a time bound "
            "(`<mutation> && sleep <N> && <inverse>`), or by registering a "
            "rollback handle. A timer with no forward mutation does not "
            "qualify, and the accepted inverse is family-specific."
        )
        return GuardDecision(
            verdict=GuardVerdict.REJECT_BANNED,
            reason=f"host-escape primitive not cleared: {escape_detail}",
            effective=effective,
            # No "this is not a dead-end" coda here — the screener's rejection
            # renderer (``tool_screener._format_rejection_for_llm``) appends it
            # for every retryable verdict, so adding it would duplicate the
            # sentence in the ToolMessage the model reads.
            suggestion=escape_suggestion,
        )
    if effective.scope == SCOPE_UNKNOWN:
        # Prefer the classifier's specific cause (unknown tool, no subcommand
        # verb, unknown subcommand, ...); fall back to echoing the raw command.
        unknown_detail = (effective.reject_detail or "").strip()
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason=(
                f"could not classify tool_call: {unknown_detail}"
                if unknown_detail
                else f"could not classify tool_call: {effective.raw_command}"
            ),
            effective=effective,
            # Same two-channel rule as the BANNED / ESCAPE branches: when the
            # classifier recorded a fix for the SPECIFIC cause it found, use it.
            #
            # The fallback below only fits ONE kind of unknown — "the target was
            # not stated". It is actively misleading for the others: an
            # unrecognised TOOL name told to "state the target explicitly" sends
            # the model back to add scope/namespace arguments and re-issue the
            # same non-existent tool, which is the retry loop this guard is
            # supposed to break. Cause and fix must name the same thing.
            suggestion=(effective.reject_suggestion or "").strip() or (
                "State the target explicitly so it can be checked against the "
                "approved one: scope + namespace + name/labels for kubectl, or "
                "--target plus matchers for a blade command."
            ),
        )

    # ---- 2. Defence: real scope but no approval on record -----------------
    if approved is None:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason="no approved target on record",
            effective=effective,
        )

    # ---- 3. UNKNOWN confidence on a real scope ---------------------------
    # Classifier returned a guessed scope without enough info — refuse. Same
    # treatment as the SCOPE_UNKNOWN branch above: surface the classifier's
    # specific cause (which argument was missing) rather than only the verdict.
    # "classifier confidence=unknown" on its own tells the model that something
    # was unparseable but not WHAT, and this branch (unlike the ``approved is
    # None`` one) gets no compensating note from the screener's renderer, so a
    # missing detail here reaches the model as a dead end with no lead.
    if effective.confidence == ConfidenceLevel.UNKNOWN:
        conf_detail = (effective.reject_detail or "").strip()
        return GuardDecision(
            verdict=GuardVerdict.REJECT_UNKNOWN,
            reason=(
                f"classifier confidence=unknown: {conf_detail}"
                if conf_detail
                else f"classifier confidence=unknown for {effective.raw_command}"
            ),
            effective=effective,
            suggestion=(
                "Reissue the call with the missing argument(s) filled in — the "
                "guard cannot compare a target it could not parse, so this is a "
                "form issue, not a blocked target."
            ),
        )

    # ---- 4. Cross-profile drift ------------------------------------------
    # A host approval never legitimises a k8s-resource call (and vice versa),
    # and likewise for any third profile a family declares: profiles are
    # mutually exclusive owners. Reject before dispatch so each DriftPolicy only
    # ever sees same-profile pairs.
    approved_scope = canonicalise_kind(approved.scope)
    effective_scope = canonicalise_kind(effective.scope)
    approved_profile = profile_of_scope(approved_scope)
    effective_profile = profile_of_scope(effective_scope)
    if approved_profile != effective_profile:
        return GuardDecision(
            verdict=GuardVerdict.REJECT_DRIFT,
            reason=f"scope drift: approved={approved_scope} effective={effective_scope}",
            effective=effective,
            suggestion=_build_suggestion(approved),
        )

    # ---- 5. Carrier identity drift (delegated to the profile's policy) ----
    decision = resolve_drift_policy(effective_profile).check_identity_drift(approved, effective)
    if decision is not None:
        return decision

    # ---- 6. Fault-type checks (cross-family scope change + blade lock) ----
    # A scope change that crosses FAULT FAMILIES (host ↔ python) survives step 4
    # and carries no comparable identity in step 5, so the fault type is the last
    # discriminator: require it to be usable before trusting it. Same-family
    # changes (Deployment → its Pod) are step 5's owner check, not drift.
    scope_change_decision = _cross_family_scope_change(approved, effective)
    if scope_change_decision is not None:
        return scope_change_decision
    lock_decision = _fault_type_lock_drift(approved, effective)
    if lock_decision is not None:
        return lock_decision

    # ---- 6.5 Duration anchor (execution timeout vs contract duration) ----
    duration_decision = _duration_anchor_drift(approved, effective)
    if duration_decision is not None:
        return duration_decision

    # ---- 7. Allow (log if LOW confidence so we can audit) ---------------
    if effective.confidence == ConfidenceLevel.LOW:
        logger.info(
            "target_guard: accepting LOW-confidence call %s (matches approved %s)",
            effective.raw_command, approved.as_target().describe(),
        )

    return GuardDecision(
        verdict=GuardVerdict.ALLOW,
        reason="effective target matches approved",
        effective=effective,
    )


__all__ = [
    "CLUSTER_SCOPED_KINDS",
    "OWNER_SCOPES",
    "target_drift_guard",
]
