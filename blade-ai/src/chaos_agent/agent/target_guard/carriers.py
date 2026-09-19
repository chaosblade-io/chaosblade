"""Map registered helper resources back to their approved target.

A debug pod is an execution carrier, not the resource receiving the fault.
This module recognises host-level exec calls only when the pod was created by
the current task, still has the same Kubernetes UID, and runs on the approved
node.  Arbitrary chroot/nsenter calls remain unclassified and fail closed.

Rejections carry their OWN cause
--------------------------------
Resolution used to answer with ``tuple | None``, which collapsed a dozen
independent refusal conditions — pod not registered, not privileged, wrong
fault family, no bounded reversal, … — into a single ``None``. The screener
then had to GUESS which one fired, and guessed "pod not registered" for all of
them. task-866648cc spent nine minutes proving its debug pod was legitimate
(phase Running, ``privileged: true``) because the guard told it the pod was the
problem, when the real gate was a missing self-reversal on an otherwise
correct ``tc netem`` command.

So a refusal now reports itself: :class:`CarrierResolution` carries the
:class:`CarrierRejectReason` that fired, a factual ``detail``, and the
compliant form for THAT gate. Callers forward it; they never infer it. This
mirrors what ``classifier`` already does for banned/unknown scopes — the cause
is recorded where it is OBSERVED, not where it is consumed.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from dataclasses import dataclass
from enum import Enum
from typing import Any

from chaos_agent.agent.execution_artifacts import find_active_debug_pod
from chaos_agent.agent.target_guard.recoverability import (
    assess as assess_recoverability,
)
from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
)


class CarrierRejectReason(str, Enum):
    """Which carrier-resolution gate refused the call.

    Values are stable identifiers so tests and audit records can assert on the
    GATE rather than on prose that may be reworded.
    """

    #: Not a host-carrier shaped call at all (wrong tool / subcommand / unparseable).
    NOT_A_HOST_EXEC = "not_a_host_exec"
    #: No approval snapshot to compare against.
    NO_APPROVAL = "no_approval"
    #: No active debug-pod artifact matches the exec target.
    POD_NOT_REGISTERED = "pod_not_registered"
    #: Live probe could not confirm the pod (absent, or channel unreadable).
    POD_NOT_DISCOVERABLE = "pod_not_discoverable"
    #: The carrier container is not privileged — no host access regardless of profile.
    NOT_PRIVILEGED = "not_privileged"
    #: The artifact does not pin a node, so the host being entered is unknown.
    NO_NODE_BINDING = "no_node_binding"
    #: The pod's node is not among the approved target names.
    NODE_NOT_APPROVED = "node_not_approved"
    #: The carrier is cleaned/failed, or has an armed rollback still pending.
    CARRIER_NOT_ACTIVE = "carrier_not_active"
    #: The host command does not map to the approved fault family.
    FAMILY_MISMATCH = "family_mismatch"
    #: The mutation has no paired reversal, so the recover graph cannot undo it.
    NO_BOUNDED_RECOVERY = "no_bounded_recovery"
    #: The command carries no mutation verb, so it reads as read-only
    #: inspection — but its compound form could not be statically proven
    #: read-only (B34: a ``iptables -S | grep`` probe told to add ``-D``).
    READONLY_FORM_UNPROVEN = "readonly_form_unproven"
    #: A live re-read succeeded and disagreed: the carrier is not the registered pod.
    CARRIER_STALE = "carrier_stale"
    #: The liveness re-read could not be completed, so identity stays unconfirmed.
    VERIFICATION_FAILED = "verification_failed"
    #: Resolution itself raised — cause unknown, so the call fails closed.
    RESOLUTION_ERROR = "resolution_error"


# Compliant forms, one per gate. Defined BEFORE ``CarrierResolution`` so the
# factories below can name them at class-body time if they ever need to, and so
# a reader meets the vocabulary before the code that uses it. Kept beside the
# reasons so a new gate cannot be added without deciding what the model should
# do about it.
_SUGGEST_REGISTER_POD = (
    "Create the carrier through `kubectl debug node/<approved-node> "
    "--profile=sysadmin --image=<pullable-image> -- sleep 3600` and exec "
    "through THAT pod. An exec through any other pod cannot be cleared, "
    "because the guard has no record of which host it enters."
)
_SUGGEST_APPROVED_NODE = (
    "Target a node that is in the approved set: create the debug pod on an "
    "approved node and exec through it."
)
_SUGGEST_FAMILY = (
    "Express the fault with a binary of the APPROVED fault family "
    "(network → iptables/tc/nft, or a timeout-bounded nc -l / socat "
    "TCP-LISTEN listener for a "
    "port-occupation fault; disk → dd/fallocate/fio; cpu|mem → stress-ng; "
    "process → kill, a one-shot crictl stop for a discrete restart, a "
    "timer-armed bounded crictl-stop loop for a sustained terminate-style "
    "fault, or a timer-armed cgroup-freezer FROZEN write for a "
    "suspend-style fault). Inspection "
    "commands (crictl ps/pods/inspect, ip addr, ps) are not injections — if "
    "you only need to look, use "
    "a read-only probe instead. Note the SECOND requirement that applies once "
    "the family is right: a host mutation must also self-recover, so pair it "
    "with its own reversal behind a time bound in the same call. Doing both at "
    "once avoids a second rejection."
)
# The banned-verb face of the FAMILY_MISMATCH gate (see the empty-family
# branch in ``_resolve_carrier_from_artifact``): task inject-055c86cc had
# the model re-issue a process-family crictl-stop loop three times in a row
# because its terminator used ``systemctl stop`` while the rejection kept
# saying "does not map to any fault family" — the family verb was right
# there in the command, so the model had nothing actionable. The fix must
# name what to do with the banned verb; the family shapes the model already
# used are re-taught by ``_SUGGEST_FAMILY``, appended at the call site.
#
# The MECHANISM is generic (any banned verb gets named; the guidance is
# assembled per hit verb); the GUIDANCE ENTRIES are per-verb substitutes
# written only where a verified incident established the correct rewrite.
# A rejection must carry ONLY the replacements matching the verbs actually
# hit — showing the truncate recipe to a ``curl`` misuse would misdirect the
# model exactly the way the old nameless rejection did. Verbs without an
# entry fall back to the generic rewrite principle below.
_SUGGEST_BANNED_VERB_HEAD = (
    "Remove the banned verb from the command and re-express that one step "
    "with the fault family's own binaries"
)
# An entry is either a plain string (family-neutral) or a dict mapping a
# family name to its variant plus ``"*"`` as the fallback for every other
# family — the family is part of the FIX, not just the diagnosis: a rewrite
# that is canonical for one family can be a fresh rejection in another.
# Evidence: the 2026-08-26 network-isolation drill — under a 'network'
# approval the model followed the unconditional systemctl→pkill advice, and
# the pkill (a process-family verb) voided the network family match, turning
# one rejection into a loop.
_BANNED_VERB_GUIDANCE = {
    # inject-055c86cc: the model tore down its own systemd-run transient
    # unit with ``systemctl stop``; for a PROCESS fault the family-canonical
    # teardown is pkill. Under any other approval pkill is a foreign family
    # verb that voids the match the same way systemctl did — there the
    # canonical shape is in-chain self-recovery, which leaves no transient
    # unit to manage at all.
    "systemctl": {
        "process": (
            "a transient unit armed via systemd-run is stopped by pkill-ing "
            "its payload process (the unit dies with its processes and the "
            "timer still fires), never by systemctl"
        ),
        "*": (
            "do NOT substitute pkill for systemctl here — pkill is a "
            "process-family verb and voids the match against the approved "
            "'{family}' fault the same way. Keep self-recovery in-chain "
            "instead: pair the mutation with its own inverse behind a sleep "
            "in the same call (<mutation> && sleep <N> && <inverse>), so no "
            "transient unit is left to manage"
        ),
    },
    "rm": (
        "a filled file is reclaimed by the fault family's own reversal "
        "(truncate -s 0 / fallocate -d), never by rm"
    ),
}


def _banned_verb_suggestion(
    banned_verbs: tuple[str, ...], approved_family: str = ""
) -> str:
    """Assemble the banned-verb suggestion for exactly the verbs hit.

    ``approved_family`` picks the family-scoped variant of an entry: the
    substitute a fault family considers canonical differs by family, and
    advising a foreign family's verb manufactures the next rejection.
    """
    hints = []
    for verb in banned_verbs:
        entry = _BANNED_VERB_GUIDANCE.get(verb)
        if entry is None:
            continue
        if isinstance(entry, dict):
            entry = entry.get(approved_family) or entry["*"]
        hints.append(entry.format(family=approved_family or "<empty>"))
    if not hints:
        return _SUGGEST_BANNED_VERB_HEAD + "."
    return _SUGGEST_BANNED_VERB_HEAD + ": " + "; ".join(hints) + "."


_SUGGEST_ACTIVE_CARRIER = (
    "Wait for the armed rollback to elapse, or create a fresh debug pod on "
    "the approved node and exec the next mutation through that one."
)
_SUGGEST_FRESH_CARRIER = (
    "Create a fresh debug pod on the approved node with `kubectl debug "
    "node/<approved-node> --profile=sysadmin --image=<pullable-image> -- sleep "
    "3600` and exec through the new pod."
)
_SUGGEST_EXEC_SHAPE = (
    "Re-issue the exec in the shape the guard can read: "
    "`<pod> -n <namespace> -- <host-entry> <command>`, where <host-entry> is "
    "`chroot /host`, `nsenter ...`, `unshare ...` or a `/host/...` binary. "
    "The pod name must come first and `--` must separate it from the command."
)
_SUGGEST_NO_APPROVAL = (
    "A host operation needs an approved target to be checked against. Confirm "
    "the fault intent first so an approval is on record, then re-issue the "
    "exec."
)


@dataclass(frozen=True)
class CarrierResolution:
    """Outcome of resolving a host exec through an execution carrier.

    Exactly one of the two shapes is populated, and ``__post_init__`` ENFORCES
    that rather than merely documenting it:

    - resolved  — ``effective`` + ``artifact`` set, ``reason`` is None.
    - rejected  — ``reason`` + non-empty ``detail`` set, ``effective`` is None.

    The check is not ceremony. A malformed third shape (say ``effective`` set
    but ``artifact`` forgotten, or a bare ``CarrierResolution()``) does not
    crash: the screener reads ``resolved`` as False, forwards an EMPTY detail,
    and the guard falls back to its generic template — i.e. the model is told a
    vague half-truth, which is the exact failure class this module was written
    to remove. Failing loudly at construction keeps that from being reachable
    by a future caller who bypasses the factories.

    ``artifact`` is a SHARED, MUTABLE reference into
    ``state["execution_artifacts"]`` (``find_active_debug_pod`` returns the
    stored dict itself, not a copy), so ``frozen=True`` protects the binding,
    not the dict. Consumers must treat it as read-only; the artifact lifecycle
    is owned by ``execution_artifacts``.
    """

    effective: EffectiveTarget | None = None
    artifact: dict | None = None
    reason: CarrierRejectReason | None = None
    detail: str = ""
    suggestion: str = ""

    def __post_init__(self) -> None:
        if self.reason is None:
            if self.effective is None:
                raise ValueError(
                    "CarrierResolution must either resolve (effective+artifact) "
                    "or reject (reason+detail); got neither"
                )
            if not isinstance(self.artifact, dict):
                raise ValueError(
                    "a resolved CarrierResolution must carry the artifact dict "
                    "it resolved through; downstream code needs its identity "
                    f"for the liveness re-probe (got {type(self.artifact).__name__})"
                )
            return
        if self.effective is not None:
            raise ValueError(
                "CarrierResolution cannot both resolve and reject: "
                f"reason={self.reason.value} was set alongside an effective target"
            )
        if not self.detail.strip():
            raise ValueError(
                f"rejection {self.reason.value} carries no detail; the caller "
                "forwards this text verbatim to the model, and an empty one "
                "degrades to the generic template this class exists to replace"
            )

    @property
    def resolved(self) -> bool:
        return self.effective is not None and self.reason is None

    @classmethod
    def allow(
        cls, effective: EffectiveTarget, artifact: dict,
    ) -> "CarrierResolution":
        return cls(effective=effective, artifact=artifact)

    @classmethod
    def reject(
        cls,
        reason: CarrierRejectReason,
        detail: str,
        suggestion: str = "",
    ) -> "CarrierResolution":
        return cls(reason=reason, detail=detail, suggestion=suggestion)

    @classmethod
    def stale(cls, pod_name: str) -> "CarrierResolution":
        """A completed live re-read DISAGREED with the registered identity.

        Observed by the screener (it owns the re-probe), so the constructor
        lives here to keep every carrier rejection's wording in one place. Use
        :meth:`verification_failed` when the re-read did not complete — claiming
        a mismatch we never observed is the same lie this module exists to stop.
        """
        return cls.reject(
            CarrierRejectReason.CARRIER_STALE,
            f"a live re-read of carrier pod '{pod_name}' no longer matches the "
            "identity registered for this task (uid / node / namespace / "
            "privileged), so it may have been recreated or replaced",
            _SUGGEST_FRESH_CARRIER,
        )

    @classmethod
    def verification_failed(
        cls, pod_name: str, exc: BaseException,
    ) -> "CarrierResolution":
        """The liveness re-read RAISED, so identity is unconfirmed — not wrong.

        Distinct from :meth:`stale` on purpose: "the pod changed" and "we could
        not look" are different facts, and only one of them was observed.
        """
        return cls.reject(
            CarrierRejectReason.VERIFICATION_FAILED,
            f"the liveness re-read of carrier pod '{pod_name}' could not be "
            f"completed ({exc.__class__.__name__}), so its identity is "
            "unconfirmed — the guard fails closed rather than assume it is "
            "still the registered pod",
            _SUGGEST_FRESH_CARRIER,
        )

    @classmethod
    def errored(cls, exc: BaseException) -> "CarrierResolution":
        """Resolution raised — report that fact rather than inventing a gate."""
        return cls.reject(
            CarrierRejectReason.RESOLUTION_ERROR,
            "carrier resolution could not complete "
            f"({exc.__class__.__name__}), so the host this exec would enter "
            "could not be verified",
            _SUGGEST_REGISTER_POD,
        )


#: Gates where a live cluster read may still find a usable carrier that this
#: task never registered (``kubectl debug`` can time out before emitting its
#: metadata marker). ``RESOLUTION_ERROR`` is here because the screener wraps a
#: raised resolution in it — cause unknown, so the cluster is still worth asking.
#:
#: Of the gates that can REACH this decision, the exclusions split in two:
#:
#: * CARRIER_NOT_ACTIVE / FAMILY_MISMATCH are a SAFETY boundary.
#:   ``discover_unregistered_carrier`` synthesises an artifact with
#:   ``status="active"`` and an empty ``operation_family``, so retrying through
#:   it would OVERWRITE the very facts that produced the verdict — re-admitting
#:   a ``recovery_armed`` carrier for a second mutation before its rollback
#:   fires, or letting a disk-registered carrier run a network fault. Before
#:   this set existed every rejection fell through to discovery, so both
#:   bypasses were reachable.
#: * NOT_A_HOST_EXEC / NO_APPROVAL / NOT_PRIVILEGED / NO_NODE_BINDING /
#:   NO_BOUNDED_RECOVERY / READONLY_FORM_UNPROVEN cannot be overturned by a
#:   read: the first two are about the request, the last two about the
#:   command, and the middle two read pod-spec facts captured live at
#:   creation time and immutable thereafter.
#:
#: POD_NOT_DISCOVERABLE / NODE_NOT_APPROVED / CARRIER_STALE /
#: VERIFICATION_FAILED are deliberately absent: they are produced BY discovery
#: or by the post-resolution liveness probe, so they never exist at this
#: decision point and listing them either way would be noise.
LIVE_DISCOVERY_RETRYABLE_REASONS = frozenset({
    CarrierRejectReason.POD_NOT_REGISTERED,
    CarrierRejectReason.RESOLUTION_ERROR,
})


_FAMILY_ALIASES = {
    "memory": "mem",
}

_HOST_ENTRY = ("chroot", "nsenter", "unshare")
_SHELL_WRAPPERS = ("sh", "bash", "ash", "dash", "/bin/sh", "/bin/bash")
# Fault binaries shared with the k8s classifier's pod-scoped fault-binary
# branch (providers/k8s_native/classifier.py imports this set). The probe
# vocabulary that used to live beside it was single-sourced to the shared
# judge ``tools.readonly._classify_argv`` at the B34 fix.
_FAULT_BINARIES = frozenset({
    "iptables", "ip6tables", "nft", "tc", "stress", "stress-ng", "dd",
    "fallocate", "fio",
})


def _host_entry_tokens(inner: list[str]) -> list[str]:
    """Unwrap a single ``sh -c "<script>"`` layer to reach the real entry token.

    Only one wrapper layer is unwrapped on purpose: nested shells beyond that
    are unusual for injection and must keep failing closed.
    """
    if inner and inner[0] in _SHELL_WRAPPERS and "-c" in inner:
        idx = inner.index("-c")
        if idx + 1 < len(inner):
            try:
                nested = shlex.split(inner[idx + 1])
            except ValueError:
                return inner
            if nested:
                return nested
    return inner


def effective_target_from_registered_carrier(
    tool_name: str,
    tool_args: Any,
    artifacts: list[dict] | None,
    approved: ApprovedTarget | None,
) -> CarrierResolution:
    """Resolve a host exec through an active debug-pod artifact."""
    if tool_name != "kubectl" or not isinstance(tool_args, dict):
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            f"'{tool_name}' is not a kubectl exec, so no execution carrier applies",
        )
    if tool_args.get("subcommand") != "exec":
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            f"kubectl subcommand '{tool_args.get('subcommand') or '<empty>'}' "
            "is not 'exec', so no execution carrier applies",
        )
    if approved is None:
        return CarrierResolution.reject(
            CarrierRejectReason.NO_APPROVAL,
            "no approved target is on record for this task, so a host "
            "operation cannot be matched against one",
            _SUGGEST_NO_APPROVAL,
        )
    parsed = _parse_host_exec(tool_args.get("v_args", ""))
    if parsed is None:
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            "the exec could not be parsed into '<pod> [-n ns] -- <host-entry> "
            "...' form (missing '--' separator, pod name, or host-entry token)",
            _SUGGEST_EXEC_SHAPE,
        )
    pod_name, namespace, host_command = parsed
    artifact = find_active_debug_pod(artifacts, pod_name, namespace)
    if artifact is None:
        return CarrierResolution.reject(
            CarrierRejectReason.POD_NOT_REGISTERED,
            f"pod '{pod_name}' is not a debug-pod artifact registered by this "
            "task (created via `kubectl debug node`), so the host it enters is "
            "unverified",
            _SUGGEST_REGISTER_POD,
        )
    return _resolve_carrier_from_artifact(
        artifact, pod_name, namespace, host_command, approved,
    )


def _resolve_carrier_from_artifact(
    artifact: dict,
    pod_name: str,
    namespace: str,
    host_command: str,
    approved: ApprovedTarget,
) -> CarrierResolution:
    """Shared validation logic for registered and discovered carriers."""
    # ``privileged`` is a NECESSARY condition, not a sufficient one, and the
    # gate is deliberately built on the necessary half only.
    #
    # Measured in task-866648cc: a debug pod reported
    # ``securityContext: {"privileged": true}`` and ``chroot /host`` still
    # returned ``Operation not permitted``, while ``ls /host/proc`` succeeded —
    # so the /host mount was present and the failure was the process UID (a
    # non-root process holds no capabilities even inside a privileged
    # container; the pod spec does not show an image's own ``USER``).
    #
    # Two consequences, both intentional:
    #   - do NOT read this check as "privileged ⇒ host access works". It only
    #     rules out carriers that certainly cannot reach the host.
    #   - do NOT infer "chroot failed ⇒ the mount is missing". That inference
    #     is what kept task-866648cc probing pod phase and securityContext
    #     instead of the process identity.
    # Requiring ``--profile=sysadmin`` would not close the gap either (it sets
    # privileged, not the UID), so the profile is still not hard-required; some
    # clusters (e.g. ACK) grant privileged by default. Whether host entry
    # actually works is only knowable by attempting it.
    if artifact.get("privileged") is not True:
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_PRIVILEGED,
            f"this task recorded carrier pod '{pod_name}' as NOT privileged when "
            "it was created, and a non-privileged container cannot enter host "
            "namespaces",
            _SUGGEST_REGISTER_POD,
        )
    namespace = str(artifact.get("namespace") or namespace)

    target = artifact.get("target") or {}
    node_name = str(target.get("name") or "")
    if target.get("scope") != "node" or not node_name:
        return CarrierResolution.reject(
            CarrierRejectReason.NO_NODE_BINDING,
            f"this task's record for carrier pod '{pod_name}' does not pin it to "
            f"a node (recorded scope={target.get('scope') or '<empty>'}, "
            f"node={node_name or '<empty>'}), so the host this exec would enter "
            "is unidentified",
            _SUGGEST_REGISTER_POD,
        )

    readonly_probe = is_readonly_host_probe(host_command)
    # A carrier with an armed rollback may still be inspected, but it must not
    # receive a second mutation before the first rollback deadline.
    if artifact.get("status") != "active" and not (
        readonly_probe and artifact.get("status") == "recovery_armed"
    ):
        # ``status`` is THIS AGENT'S ledger of the carrier (active / cleaned /
        # failed / recovery_armed) — NOT the pod's Kubernetes phase, which the
        # artifact tracks separately under ``phase``. The wording has to say so:
        # a message reading "pod X is in status recovery_armed" invites the model
        # to run ``kubectl get pod X``, see ``Running``, and conclude the guard
        # is wrong about its own pod. task-866648cc burned turns on exactly that
        # move (checking phase, then securityContext) because the rejection
        # pointed at the pod instead of at the record.
        return CarrierResolution.reject(
            CarrierRejectReason.CARRIER_NOT_ACTIVE,
            f"this task's record for carrier pod '{pod_name}' is "
            f"'{artifact.get('status') or '<unset>'}', not 'active' (an internal "
            "carrier-lifecycle state, not the pod's Kubernetes phase)"
            + (
                " — a rollback timer is already armed on it, so it must not "
                "receive a second mutation until that timer fires"
                if artifact.get("status") == "recovery_armed"
                else ""
            ),
            _SUGGEST_ACTIVE_CARRIER,
        )

    operation_family = classify_host_operation(host_command)
    approved_family = _normalise_family(approved.fault_target)
    artifact_family = _normalise_family(artifact.get("operation_family", ""))
    if readonly_probe:
        operation_family = approved_family
    elif not operation_family:
        # An empty family has TWO causes with different fixes, and only the
        # classifier knows which fired: a banned verb (word-level and
        # fail-closed — even a command that ALSO carries the approved
        # family's binary is voided by one) versus no family verb at all.
        # Lumping both into "does not map to any fault family" is what made
        # task inject-055c86cc a three-rejection loop: the model's command
        # contained the family verb all along, and the message gave it
        # nothing to act on. Name the banned verb when one is present.
        banned_verbs = find_banned_host_verbs(host_command)
        if banned_verbs:
            verbs = ", ".join(f"'{verb}'" for verb in banned_verbs)
            return CarrierResolution.reject(
                CarrierRejectReason.FAMILY_MISMATCH,
                f"the host command contains banned verb(s) {verbs} — a banned "
                f"verb never maps to a fault family (this voids the match "
                f"even though the family's own binary is present), so the "
                f"command cannot be matched against the approved "
                f"'{approved_family or '<empty>'}' fault: {host_command}",
                _banned_verb_suggestion(banned_verbs, approved_family)
                + " " + _SUGGEST_FAMILY,
            )
        return CarrierResolution.reject(
            CarrierRejectReason.FAMILY_MISMATCH,
            f"the host command does not map to any fault family, so it cannot "
            f"be matched against the approved '{approved_family or '<empty>'}' "
            f"fault: {host_command}",
            _SUGGEST_FAMILY,
        )
    elif operation_family != approved_family:
        return CarrierResolution.reject(
            CarrierRejectReason.FAMILY_MISMATCH,
            f"the host command is a '{operation_family}' fault but the "
            f"approved fault family is '{approved_family or '<empty>'}'",
            _SUGGEST_FAMILY,
        )
    if artifact_family and operation_family != artifact_family:
        return CarrierResolution.reject(
            CarrierRejectReason.FAMILY_MISMATCH,
            f"carrier pod '{pod_name}' was registered for the "
            f"'{artifact_family}' fault family, but this command is "
            f"'{operation_family}'",
            _SUGGEST_FAMILY,
        )
    if not readonly_probe:
        # ``assess`` — not the boolean wrapper — because it already knows the
        # PER-FAMILY reason: network wants "iptables -D matching every -I/-A",
        # disk wants "truncate -s 0 / fallocate -d (never rm)", process wants a
        # paired kill -CONT. Taking only the boolean and writing our own hint
        # here is how this suggestion came to recommend two forms that the very
        # same check rejects (a bare `systemd-run --on-active` with no forward
        # mutation, and `rm` as a disk reclaim). The observer states the cause.
        #
        # ``has_registered_rollback`` is deliberately NOT passed: the system
        # has no registration API for host-mutation rollback handles — the
        # ``recovery_armed`` artifact status is a CONSEQUENCE of an observed
        # inline arm, never an alternative to it (see the recoverability
        # module docstring). The seam stays opt-in for the day one exists.
        recoverability = assess_recoverability(host_command, operation_family)
        if not recoverability.recoverable:
            if recoverability.readonly_unproven:
                # B34: zero mutation verbs means "add a paired inverse" is
                # nonsense guidance — there is nothing to invert. Name the
                # real cause (the compound form is not provably read-only)
                # and give the two honest directions. Still fail-closed.
                return CarrierResolution.reject(
                    CarrierRejectReason.READONLY_FORM_UNPROVEN,
                    "the compound host command carries no detectable "
                    "mutation verb, so it reads as read-only inspection — "
                    "but a compound form (chained / piped / redirected / "
                    "expanded) cannot be statically proven read-only on "
                    f"this gate: {host_command}",
                    "Split read-only inspection into single-statement probes "
                    "— one command per exec (e.g. `chroot /host iptables -S "
                    "INPUT`); the exec channel returns stdout, so no "
                    "chaining or redirect is needed. If the command is "
                    "actually a mutation, spell it with the standard verbs "
                    "(-I/-A paired with a matching -D) so the reversal "
                    "check can verify it.",
                )
            _missing = "; ".join(recoverability.missing) or "a bounded, reversible form"
            return CarrierResolution.reject(
                CarrierRejectReason.NO_BOUNDED_RECOVERY,
                "the host mutation does not self-recover, so the recover graph "
                f"would have no way to undo it — missing {_missing}: {host_command}",
                (
                    f"Add {_missing}. The command itself is accepted; only the "
                    "missing reversal blocks it."
                ),
            )

    return CarrierResolution.allow(
        EffectiveTarget(
            scope="node",
            namespace="",
            names=(node_name,),
            fault_target=operation_family,
            confidence=ConfidenceLevel.HIGH,
            raw_command=f"kubectl exec {pod_name} -n {namespace} -- {host_command}",
        ),
        artifact,
    )


# Live carrier-discovery probe rides the in-band API path an in-progress
# network fault is intermittently severing, so a single ``kubectl get pod`` can
# time out even though the pod exists. Retry only TRANSIENT failures with a
# short backoff; a genuine ``NotFound`` is authoritative and returned at once so
# a real scope escape still fails closed without added latency.
_PROBE_MAX_ATTEMPTS = 3
_PROBE_BACKOFF_SECONDS = (0.5, 1.0)


def _probe_error_is_absence(error: str) -> bool:
    """Whether a probe error means the pod genuinely does not exist."""
    low = error.lower()
    return "notfound" in low or "not found" in low


async def _probe_debug_pod_with_backoff(
    pod_name: str, namespace: str, state: dict,
) -> tuple[dict, str]:
    """Read a debug pod's metadata, retrying transient (flaky-channel) failures.

    Returns the same ``(metadata, error)`` contract as ``_debug_pod_metadata``.
    A run of timeouts means "channel flaky", not "pod gone", so we retry; a
    ``NotFound`` short-circuits immediately.
    """
    from chaos_agent.tools.kubectl import _debug_pod_metadata

    kubeconfig = str(state.get("kubeconfig") or "")
    last: tuple[dict, str] = ({}, "")
    for attempt in range(_PROBE_MAX_ATTEMPTS):
        # Exactly the 3-arg signature (W-56-2/4): connection context rides on
        # TransportTarget.from_state({}) → settings, synced upstream by
        # sync_kubewiz_runtime — there is no 4th/5th parameter to pass.
        metadata, error = await _debug_pod_metadata(
            pod_name, namespace, kubeconfig,
        )
        if metadata and not error:
            return metadata, ""
        if error and _probe_error_is_absence(error):
            return {}, error
        last = (metadata, error)
        if attempt < _PROBE_MAX_ATTEMPTS - 1:
            await asyncio.sleep(_PROBE_BACKOFF_SECONDS[attempt])
    return last


async def discover_unregistered_carrier(
    tool_name: str,
    tool_args: Any,
    state: dict,
    approved: ApprovedTarget | None,
) -> CarrierResolution:
    """Live-discover an unregistered debug pod for carrier resolution.

    Called when ``effective_target_from_registered_carrier`` rejects with
    ``POD_NOT_REGISTERED`` but the call is a host carrier call
    (chroot/nsenter).  Does a live ``kubectl get pod`` to check if the pod is a
    privileged debug pod on an approved node.  Handles the race condition where
    ``kubectl debug`` timed out before emitting ``[debug-pod-meta: ...]`` — by
    exec time the pod is guaranteed to exist (the LLM saw it in
    ``kubectl get pods``).
    """
    if tool_name != "kubectl" or not isinstance(tool_args, dict):
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            f"'{tool_name}' is not a kubectl exec, so no execution carrier applies",
        )
    if tool_args.get("subcommand") != "exec":
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            f"kubectl subcommand '{tool_args.get('subcommand') or '<empty>'}' "
            "is not 'exec', so no execution carrier applies",
        )
    if approved is None:
        return CarrierResolution.reject(
            CarrierRejectReason.NO_APPROVAL,
            "no approved target is on record for this task, so a host "
            "operation cannot be matched against one",
            _SUGGEST_NO_APPROVAL,
        )
    parsed = _parse_host_exec(tool_args.get("v_args", ""))
    if parsed is None:
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_A_HOST_EXEC,
            "the exec could not be parsed into '<pod> [-n ns] -- <host-entry> "
            "...' form (missing '--' separator, pod name, or host-entry token)",
            _SUGGEST_EXEC_SHAPE,
        )
    pod_name, namespace, host_command = parsed

    metadata, error = await _probe_debug_pod_with_backoff(
        pod_name, namespace or "default", state,
    )
    if error or not metadata:
        return CarrierResolution.reject(
            CarrierRejectReason.POD_NOT_DISCOVERABLE,
            f"pod '{pod_name}' is not registered by this task and a live read "
            f"could not confirm it: {error or 'no metadata returned'}",
            _SUGGEST_REGISTER_POD,
        )
    if not metadata.get("privileged"):
        return CarrierResolution.reject(
            CarrierRejectReason.NOT_PRIVILEGED,
            f"pod '{pod_name}' was found live but its container is not "
            "privileged, so it cannot enter host namespaces",
            _SUGGEST_REGISTER_POD,
        )
    node_name = metadata.get("node") or ""
    if not node_name:
        return CarrierResolution.reject(
            CarrierRejectReason.NO_NODE_BINDING,
            f"pod '{pod_name}' was found live but reports no node, so the host "
            "being entered is unidentified",
            _SUGGEST_REGISTER_POD,
        )

    # The pod's node must be in the approved target list.
    approved_names = set(approved.names or ())
    if node_name not in approved_names:
        return CarrierResolution.reject(
            CarrierRejectReason.NODE_NOT_APPROVED,
            f"pod '{pod_name}' runs on node '{node_name}', which is not in the "
            f"approved target set ({', '.join(sorted(approved_names)) or 'none'})",
            _SUGGEST_APPROVED_NODE,
        )

    # P5: shape-parity with registry artifacts (this synthetic dict never
    # enters ``execution_artifacts`` — it feeds the shared validation only —
    # but recording ``kind`` keeps every debug_pod-shaped literal aligned
    # with the kind-records contract).
    synthetic_artifact = {
        "type": "debug_pod",
        "kind": "pod",
        "status": "active",
        "name": pod_name,
        "namespace": metadata.get("namespace") or namespace or "default",
        "uid": metadata.get("uid") or "",
        "target": {"scope": "node", "name": node_name},
        "privileged": True,
        "operation_family": "",
    }
    return _resolve_carrier_from_artifact(
        synthetic_artifact, pod_name, namespace, host_command, approved,
    )


def is_host_carrier_call(tool_name: str, tool_args: Any) -> bool:
    """Return whether a call attempts to enter host namespaces via kubectl."""
    if tool_name != "kubectl" or not isinstance(tool_args, dict):
        return False
    if tool_args.get("subcommand") != "exec":
        return False
    try:
        args = shlex.split(str(tool_args.get("v_args") or ""))
    except ValueError:
        # A malformed command containing an explicit host-entry token remains
        # safety-sensitive and must not inherit the generic fail-open policy.
        raw = str(tool_args.get("v_args") or "").lower()
        return any(token in raw for token in _HOST_ENTRY) or "/host/" in raw
    if "--" not in args:
        return False
    # R45: the slice must start at the TRUE separator (pflag is value-FIRST)
    # — a ``--`` in a value slot is that flag's VALUE, and slicing there
    # read the line's remaining tokens as if they were the inner command
    # (``exec pod -c -- -- chroot /host bash`` missed the host entry while
    # every client runs it).
    from chaos_agent.tools._readonly_facts import exec_separator_index

    separator = exec_separator_index(args)
    if separator is None:
        # Every ``--`` was swallowed as a value: no true separator, so the
        # deprecated path would run the tokens after the ENTRY. Keep carrier
        # treatment (fail closed) when a host-entry token appears at all —
        # the same containment the unlexable-input branch above applies.
        raw = str(tool_args.get("v_args") or "").lower()
        return any(token in raw for token in _HOST_ENTRY) or "/host/" in raw
    inner = args[separator + 1:]
    entry = _host_entry_tokens(inner)
    return bool(
        entry
        and (
            entry[0] in _HOST_ENTRY
            or entry[0].startswith("/host/")
        )
    )


async def registered_carrier_is_current(artifact: dict, state: dict) -> bool:
    """Re-read the pod and reject stale/recreated execution carriers.

    The security boundary is IDENTITY, not liveness: ``uid`` matching the
    registered artifact already rules out a recreated/hijacked pod (a recreated
    pod gets a fresh uid), and ``node`` + ``namespace`` + ``privileged`` pin it
    to the approved host-escape target. ``phase``/``ready`` are deliberately NOT
    hard requirements: once the injection drives the target node to
    Unknown/NodeLost, the API server reports the (still-existing) debug pod as
    not-Ready / phase!=Running, which would otherwise turn "injection worked" into
    a false "carrier unavailable" rejection. A dead pod simply fails the exec at
    runtime — it is not a scope escape — so liveness is only a degraded signal.
    """
    from chaos_agent.tools.kubectl import _debug_pod_metadata

    # Exactly the 3-arg signature (W-56-2/4, #56 msg#179): the former 5-arg
    # call raised TypeError on EVERY re-read, which the fail-closed liveness
    # gate then rendered as "carrier unavailable" → REJECT_BANNED even while
    # the carrier was healthy. Connection context rides on
    # TransportTarget.from_state({}) → settings (sync_kubewiz_runtime), so
    # there is nothing extra to pass.
    metadata, error = await _debug_pod_metadata(
        str(artifact.get("name") or ""),
        str(artifact.get("namespace") or ""),
        str(state.get("kubeconfig") or ""),
    )
    if error or not metadata:
        return False
    target = artifact.get("target") or {}
    return bool(
        metadata.get("uid") == artifact.get("uid")
        and metadata.get("node") == target.get("name")
        and metadata.get("namespace") == artifact.get("namespace")
        and metadata.get("privileged") is True
    )


def is_readonly_host_probe(command: str) -> bool:
    """Recognise a narrow set of host capability/identity inspections.

    Accepts a single probe, a ``sh -c '<payload>'`` / ``bash -c`` wrapped probe
    (one wrapper layer, unwrapped structurally), and probes chained with
    ``;`` / ``&&`` / ``||`` / ``|`` — but ONLY when EVERY resulting segment
    (pipeline stage) is itself an approved read-only probe, judged by the
    shared per-binary vocabulary (``tools.readonly._classify_argv`` — the
    same judge the classifier fast path and the kubectl_read tool layer
    use; single-sourced at the B34 fix, which admitted the pipe: a
    pipeline whose every stage is read-only mutates nothing). A redirect,
    command substitution, variable expansion, backgrounding, backtick, or
    any non-probe segment fails closed.

    Judged on bashfacts STRUCTURE by ``_probe_facts`` (design 4.6 face 3);
    the legacy shlex+substring chain was deleted at the engine flip.
    """
    from chaos_agent.agent.target_guard._probe_facts import (
        is_readonly_host_probe_facts,
    )

    return is_readonly_host_probe_facts(command)


# Word-level banned verbs on the host: mutating verbs whose blast radius
# the family regexes cannot bound (rm/chmod/reboot/...), arbitrary-code
# interpreters (python/perl/curl/wget), or systemctl — whose benign uses
# (stopping a unit THIS TASK armed) are indistinguishable at word level
# from stopping kubelet. Kept word-level and fail-closed on purpose;
# ``find_banned_host_verbs`` exists so a rejection can NAME the verb
# instead of leaving the model to guess among every word it used.
_BANNED_HOST_VERBS = re.compile(
    r"(^|[\s;&|/])(rm|mv|cp|chmod|chown|curl|wget|python[0-9.]*|perl|"
    r"systemctl|reboot|shutdown|mount|umount|mkfs(?:\.[a-z0-9]+)?|tee)"
    r"([\s;&|]|$)"
)


def _surfaced_text(command: str) -> str:
    """The lowered text the family and banned-verb regexes judge on.

    ``classify_host_operation`` and ``find_banned_host_verbs`` MUST judge
    the same text: if one unwrapped a quoted systemd-run payload and the
    other did not, the classifier could reject a command BECAUSE of a
    banned verb while the reporter finds none to name — the unactionable
    rejection this pair exists to prevent. Sharing is by construction,
    not by hope.
    """
    from chaos_agent.agent.target_guard._probe_facts import host_payload_tokens_facts

    payload = host_payload_tokens_facts(command) or []
    lowered = command.lower()
    if payload:
        lowered = f"{lowered} {' '.join(payload).lower()}"
    # A ``systemd-run`` timer's payload is the command the TIMER runs at its
    # deadline — for the host carrier it usually sits in a quoted
    # ``sh -c 'kill -CONT $(…)'`` script that no host-entry unwrap covers
    # (that machinery knows chroot/nsenter/unshare, not bare systemd-run).
    # Surface the quoted script text so the family regexes below can see the
    # payload's verbs; WITHOUT this the family resolves empty and the fault
    # -type lock silently loses its pin (verified: the 进程假死挂起 timer).
    if re.search(r"(^|[\s/])systemd-run(\s|$)", lowered):
        quoted = " ".join(
            frag for pair in re.findall(r"'([^']*)'|\"([^\"]*)\"", command)
            for frag in pair if frag
        )
        if quoted:
            lowered = f"{lowered} {quoted.lower()}"
    return lowered


def find_banned_host_verbs(command: str) -> tuple[str, ...]:
    """Banned verbs present in ``command``, in first-occurrence order.

    A banned verb is why ``classify_host_operation`` returns ``""`` even
    when the command ALSO carries the approved family's binary — task
    inject-055c86cc burned three consecutive rejections (and with them the
    whole drill) on a process-family crictl-stop loop whose terminator
    used ``systemctl stop`` while the message kept saying "does not map to
    any fault family". Naming the verb turns that guess-loop into one fix.
    """
    verbs: list[str] = []
    for match in _BANNED_HOST_VERBS.finditer(_surfaced_text(command)):
        verb = match.group(2)
        if verb not in verbs:
            verbs.append(verb)
    return tuple(verbs)


def classify_host_operation(command: str) -> str:
    """Classify a host command into the approved fault family."""
    lowered = _surfaced_text(command)
    if _BANNED_HOST_VERBS.search(lowered):
        return ""
    families: set[str] = set()
    if re.search(r"(^|[\s/])(iptables|ip6tables|nft|tc)(\s|$)", lowered):
        families.add("network")
    # ``nc`` in LISTEN mode occupies the port while it runs — the documented
    # port-occupation fault (skill case Node_网络故障_节点端口占用). Client-mode
    # nc carries no ``-l`` and is not a fault: it does not match this shape.
    if re.search(r"\bnc\b[^;&|\n]{0,20}-l\b", lowered):
        families.add("network")
    # ``socat`` with a TCP-LISTEN address is the same fault in the
    # socat vocabulary (same skill case: nc is absent on this cluster's
    # hosts, so the case law settled on socat — the guard's enforcement
    # face now matches it). ONLY the LISTEN address form is a fault, and
    # only when the same socat segment carries no EXEC:/SYSTEM:/SHELL:
    # address — those turn the listener into arbitrary command execution
    # (``socat TCP-LISTEN:9100 EXEC:/bin/sh`` is the classic bind-shell),
    # and the dangerous-address scan is bounded to the same ``; & |``-
    # delimited segment so a legit ``kill X; socat TCP-LISTEN:...``
    # compound is not voided by unrelated segments. The scan carries NO
    # character cap of its own: the former {0,200} window was an
    # implementation seam — 200+ chars of option padding pushed the
    # dangerous address past the scan while the LISTEN form still
    # matched, adopting a timeout-bounded bind-shell as the
    # port-occupation family (found by the 2026-09-10 self-audit of the
    # #32 extension). The segment bound — set by ``[^;&|\n]`` itself —
    # is the whole boundary.
    if re.search(r"\bsocat\b[^;&|\n]{0,60}\btcp[46]?-listen:", lowered) and not re.search(
        # Both danger scans stay INSIDE the socat segment. SHELL may
        # appear bare (socat address syntax makes the colon and its
        # argument optional); EXEC:/SYSTEM: require their colon.
        r"\bsocat\b[^;&|\n]*\b(?:(?:exec|system):|shell\b)", lowered,
    ):
        families.add("network")
    if re.search(r"(^|[\s/])(dd|fallocate|fio)(\s|$)", lowered):
        families.add("disk")
    if re.search(r"(^|[\s/])(stress|stress-ng)(\s|$)", lowered):
        if re.search(r"--(vm|vm-bytes|brk|malloc)\b", lowered):
            families.add("mem")
        if re.search(r"--cpu\b", lowered):
            families.add("cpu")
    if re.search(r"(^|[\s/])(kill|pkill|killall)(\s|$)", lowered):
        families.add("process")
    # ``crictl stop`` terminates every process in the container — the
    # documented kubectl-native equivalent of a process kill (skill case
    # Pod_进程被杀死 path B). ``crictl ps/pods/inspect`` stay read-only: they
    # do not match this shape. One-shot or not is decided by the
    # recoverability gate, not here.
    if re.search(r"\bcrictl\s+stop\b", lowered):
        families.add("process")
    # A cgroup-freezer state write suspends or resumes every process in the
    # container — the documented process-suspend mechanism (skill cases
    # Pod_进程异常_进程被挂起 / Container_进程异常_Sidecar进程被挂起). Whether the
    # freeze is timer-armed is decided by the recoverability gate, not here.
    if re.search(
        r"\becho\s+(?:frozen|thawed)\b[^;&|\n]*>|>\s*\S*freezer\.state\b", lowered,
    ):
        families.add("process")
    return next(iter(families)) if len(families) == 1 else ""


def host_operation_has_bounded_recovery(
    command: str,
    family: str,
    *,
    has_registered_rollback: bool = False,
) -> bool:
    """Require a bounded lifetime before a host-level mutation is allowed.

    Thin wrapper over :func:`recoverability.assess` — kept for its existing
    callers/tests. The structural judgement (ANY bounded-timer form paired with
    the family's inverse, or a registered rollback) now lives in
    ``recoverability`` and no longer hinges on the exact ``--on-active`` flag
    the old literal demanded.
    """
    return assess_recoverability(
        command, family, has_registered_rollback=has_registered_rollback,
    ).recoverable


def _parse_host_exec(v_args: str) -> tuple[str, str, str] | None:
    try:
        args = shlex.split(v_args)
    except ValueError:
        return None
    if "--" not in args:
        return None
    # R45: the same value-aware locator the shape check below rides — a
    # ``--`` swallowed as a flag's VALUE is not the separator, and slicing
    # there handed the walk the text of a flag value while the real
    # host-entry payload sat further right.
    from chaos_agent.tools._readonly_facts import (
        exec_separator_index,
        exec_separator_shape,
    )

    separator = exec_separator_index(args)
    if separator is None:
        return None
    outer = args[:separator]
    inner = args[separator + 1:]
    if not outer or not inner:
        return None

    namespace = ""
    pod_name = ""
    skip_next = False
    for index, token in enumerate(outer):
        if skip_next:
            skip_next = False
            continue
        if token in ("-n", "--namespace") and index + 1 < len(outer):
            namespace = outer[index + 1]
            skip_next = True
            continue
        if token.startswith("--namespace="):
            namespace = token.split("=", 1)[1]
            continue
        if token in ("-c", "--container"):
            skip_next = True
            continue
        if not token.startswith("-") and not pod_name:
            pod_name = token

    # R45: positionals beyond the entry are not free space — kubectl debug
    # resolves every one as a SEPARATE target and old exec clients run them
    # as the command head, so the host operation this gate is about to
    # verify (registered pod / approved node) is not the one that runs.
    # Refuse the shape before the artifact/privilege walk can bless it.
    positionals, _after = exec_separator_shape(args)
    if len(positionals) > 1:
        return None

    entry = _host_entry_tokens(inner)
    if not pod_name or not entry or not (
        entry[0] in _HOST_ENTRY or entry[0].startswith("/host/")
    ):
        return None
    return pod_name, namespace, shlex.join(inner)


def _normalise_family(value: str) -> str:
    value = str(value or "").lower()
    return _FAMILY_ALIASES.get(value, value)


__all__ = [
    "LIVE_DISCOVERY_RETRYABLE_REASONS",
    "CarrierRejectReason",
    "CarrierResolution",
    "classify_host_operation",
    "discover_unregistered_carrier",
    "effective_target_from_registered_carrier",
    "find_banned_host_verbs",
    "host_operation_has_bounded_recovery",
    "is_readonly_host_probe",
    "is_host_carrier_call",
    "registered_carrier_is_current",
]
