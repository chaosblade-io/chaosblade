"""Type definitions for target-drift guard.

The guard's job is to prevent ``execute_loop``'s LLM from silently
acting on a different resource than ``confirmation_gate`` approved.
Two complementary records and a verdict drive every decision:

  - ``ApprovedTarget`` — frozen at confirmation_gate. The "what the
    user said yes to" record. Includes both the k8s resource identity
    (scope/namespace/names/labels) AND the ChaosBlade fault family
    (blade_target). Whether the fault family is locked is governed by
    ``lock_fault_type`` so operators can dial strictness.
  - ``EffectiveTarget`` — inferred from each in-flight ``tool_call``.
    Reflects what the call would ACTUALLY do, after parsing kubectl
    flags, recursing into ``kubectl exec POD -- ...`` payloads, and
    mapping ChaosBlade ``--target`` to k8s scope.
  - ``GuardDecision`` — the result of comparing the two. Carries the
    verdict, a human-readable reason for audit logs, the parsed
    effective target (for the replan path to write into state), and
    an optional suggestion for the LLM ("you tried X, the approved
    is Y; either narrow to Y or trigger replan").

Design note on tuples vs lists: ``names`` is a ``tuple`` and ``labels``
is captured by deep-copying into a regular dict, because both records
are conceptually FROZEN snapshots. The reducer should never mutate
them in place — instead, replan paths construct a fresh
``ApprovedTarget``. We use ``frozen=True`` on the dataclass to make
that policy enforced by the language rather than convention.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class GuardVerdict(str, Enum):
    """The four outcomes ``target_drift_guard`` can return.

    String-valued enum so audit logs and the wire format (SSE event
    detail) can carry the verdict without custom serialization."""

    # Tool call cleared all checks — safe to forward to ToolNode.
    ALLOW = "allow"
    # Tool is read-only (kubectl get/describe/top/logs/etc) — no
    # target check needed, pass through.
    READONLY = "readonly"
    # Tool would act on a resource different from approved — block,
    # trigger replan + re-confirm.
    REJECT_DRIFT = "reject_drift"
    # Tool is explicitly banned (kubectl apply -f, _execute_skill_script
    # without opt-in, kubectl config write). Block, no replan attempt.
    REJECT_BANNED = "reject_banned"
    # Tool is unknown to the classifier (new MCP tool, unrecognised
    # kubectl subcommand). Default-deny posture — block + log so the
    # operator notices and adds explicit classification.
    REJECT_UNKNOWN = "reject_unknown"
    # Call is legitimate but was refused because it kept repeating with no
    # progress. Distinct from the verdicts above ON PURPOSE: those say the call
    # is not permitted in this shape at all, this one says the shape is fine and
    # only the repetition is not. Sharing REJECT_UNKNOWN taught the model the
    # wrong lesson — that the tool was unavailable — and additionally attracted
    # the "no approved target on record" note, which has nothing to do with
    # stagnation. The refusal also ALTERNATES, so unlike the others it is not a
    # statement about the call's admissibility.
    REJECT_STAGNANT = "reject_stagnant"


class ConfidenceLevel(str, Enum):
    """How sure the classifier is about its EffectiveTarget answer.

    HIGH — args parsed unambiguously (e.g. ``blade_create`` with
        explicit ``scope``+``names``, or ``kubectl scale deploy/X -n
        ns``).
    LOW  — args parsed by best-effort heuristic with at least one
        guess (e.g. namespace defaulted to "default" because no
        ``-n`` flag was present; or kubectl exec inner cmd is a
        plain shell command we can't fully analyse).
    UNKNOWN — classifier couldn't make sense of the args at all
        (malformed kubectl, missing required field). Pair with
        ``REJECT_UNKNOWN`` verdict.
    """

    HIGH = "high"
    LOW = "low"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ApprovedTarget:
    """A frozen snapshot of "what the user approved".

    Populated by ``confirmation_gate`` when the user accepts a plan;
    consumed by ``target_drift_guard`` on every tool_call in
    ``execute_loop``. Cleared on TURN_DONE / TURN_ABORTED / replan
    (replan re-issues a fresh approval at the next confirmation_gate).

    Fields:
        scope: K8s resource kind, normalised to canonical singular
            (``pod`` / ``node`` / ``deployment`` / ``service`` / ...).
            Distinct from blade_target — a fault on a pod's JVM still
            has scope=pod (the resource being acted on).
        namespace: The k8s namespace. Empty string for cluster-scoped
            resources (node, pv, namespace itself). Empty namespace
            on a namespace-scoped scope is NORMALISED to "default" by
            the guard so kubectl's implicit-default-ns behaviour
            matches.
        names: Tuple of explicit resource names. Empty tuple means
            "labels-based" or "namespace-wide" selection (see
            ``labels`` and ``is_namespace_wide``).
        labels: Label selector ({app: demo, env: prod}). Used when
            ``names`` is empty. Empty dict means "no label selector".
        is_namespace_wide: True when the approved scope is the whole
            namespace (both ``names`` and ``labels`` empty). The
            guard then accepts any explicit name in that namespace
            without further checking.
        blade_target: ChaosBlade ``--target`` value (``pod`` /
            ``node`` / ``cpu`` / ``mem`` / ``jvm`` / ``mysql`` / ...).
            See ``classifier.BLADE_TARGET_TO_SCOPE`` for the mapping
            to k8s scope.
        blade_action: ChaosBlade action (``fullload`` / ``burn`` /
            ``loss`` / ``delay`` / ...). Whether mismatches on this
            field trigger drift depends on ``lock_fault_type``.
        lock_fault_type: When True (default), the guard treats a
            change to ``blade_target`` (e.g. ``cpu`` → ``mem``) as
            drift even if scope/namespace/names match. ``blade_action``
            is NEVER locked by this flag — sub-action tuning
            (fullload→high) is always considered legitimate
            "method switch" autonomy.
    """

    scope: str
    namespace: str
    names: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    is_namespace_wide: bool = False
    blade_target: str = ""
    blade_action: str = ""
    lock_fault_type: bool = True
    # Owner resource names discovered at freeze time. When approved
    # scope=pod, this contains the names of Deployments/DaemonSets/etc.
    # whose selector matches the approved labels. Used by the guard to
    # validate owner-scope operations at the instance level.
    owner_names: tuple[str, ...] = ()
    # Concrete resource names the approved LABEL selector resolves to at
    # freeze time (cluster query). For a label-approved scope (e.g. an
    # availability-zone node fault approved by
    # ``labels={topology.kubernetes.io/zone: ...}``), execution legitimately
    # fans out per-name (kubectl-native needs one debug Pod per node, and the
    # skill batches by node name). Without this, the guard would compare a
    # labels-only approval against name-based execution — a label↔name cross
    # it rejects as "resource selection drift" (false positive). The guard
    # validates ``effective.names ⊆ resolved_names`` so in-zone name batches
    # pass while out-of-zone names are still rejected. Empty when the approval
    # was name-based or the label could not be resolved.
    resolved_names: tuple[str, ...] = ()
    # Cross-scope operations: kubectl-native faults (cordon + delete pod)
    # may need to operate on a different scope than the primary one.
    # When scope=node, secondary_scopes=("pod",) allows pod-level
    # operations (e.g. delete pod on the target node).
    secondary_scopes: tuple[str, ...] = ()
    # Namespace for secondary scope operations. Node scope is cluster-
    # scoped (namespace=""), but secondary pod operations need a namespace.
    # Preserved from FaultSpec.namespace before cluster-scope clearing.
    secondary_namespace: str = ""
    # Carrier-agnostic host identity. Populated when ``scope == "host"``
    # (bare-metal / VM faults). Empty for Kubernetes targets, which keep
    # using namespace/names/labels. See ``as_target()``.
    host_name: str = ""

    def as_target(self):
        """Return the carrier-agnostic :class:`TargetProtocol` view.

        Delegates identity / describe to ``target_protocol`` so callers
        needing a carrier-neutral handle don't hardcode K8s field access.
        The guard's existing K8s comparison logic is unaffected.
        """
        from chaos_agent.agent.target_guard.target_protocol import (
            HostTarget,
            K8sTarget,
        )
        if self.scope == "host":
            return HostTarget(
                scope="host",
                host_name=self.host_name or (self.names[0] if self.names else ""),
            )
        return K8sTarget(
            scope=self.scope,
            namespace=self.namespace,
            names=self.names,
            labels=dict(self.labels),
        )


@dataclass(frozen=True)
class EffectiveTarget:
    """A frozen snapshot of "what this tool_call would actually do".

    Constructed by ``classifier.infer_effective_target`` from a raw
    LangChain tool_call. The guard compares this against the
    ApprovedTarget to decide drift.

    Fields:
        scope: K8s resource kind the call WOULD act on. For
            ``kubectl exec POD -- blade create node-cpu --node X``
            this is "node" (the inner blade target) NOT "pod" — the
            classifier RECURSES into the exec payload.
        namespace: Same normalisation rules as ApprovedTarget.
        names: Resource names the call would touch. Tuple for
            immutability and hashability.
        labels: Label selector the call would use, if any.
        blade_target: ChaosBlade target name if the call invokes
            ChaosBlade (either directly via ``blade_create`` or via
            ``kubectl exec POD -- blade create``).
        blade_action: ChaosBlade action.
        confidence: How sure we are. LOW + UNKNOWN must be treated
            with extra suspicion by the guard (default-deny on
            UNKNOWN; reject-drift threshold tightened on LOW).
        raw_command: The original tool_call's name + args, kept as a
            string for audit logs. Always populated.
    """

    scope: str
    namespace: str
    names: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    blade_target: str = ""
    blade_action: str = ""
    confidence: ConfidenceLevel = ConfidenceLevel.HIGH
    raw_command: str = ""
    # Tier 1 injection: kubectl exec into a tool pod (chaosblade ns)
    # then blade create inside. The inner blade command may not carry
    # --namespace (blade v1.8.0 rejects it for some subcommands).
    # Guard skips namespace check; names/labels check still validates
    # target identity.
    is_tier1_exec: bool = False
    # Infrastructure-vehicle exec: ``kubectl exec`` into an injection
    # vehicle — a pod this task registered as its own machinery (debug_pod
    # artifact, ``kubectl_exec_pod_name``, debug-pod-meta tags) or one the
    # screener live-discovered as ChaosBlade tooling via the shared
    # label-selector discovery. Set by the screener, which has state and
    # cluster access; the classifier stays stateless and never guesses
    # vehicles from pod names. The exec'd pod is machinery for reaching the
    # fault target, NOT the fault target itself, so identity drift
    # comparison does not apply. Inner-command classification (banned /
    # escape / readonly) still runs in full.
    is_vehicle_exec: bool = False
    # Fault-binary mutation inside a kubectl exec (tc/iptables/stress/...).
    # A pod-scoped mutation whose namespace containment the static
    # classifier cannot prove for privileged / hostNetwork pods. Identity
    # review is deliberately RETAINED even when the exec'd pod is a known
    # vehicle — the screener's vehicle exemption must not swallow this
    # shape, and ``_apply_drift_correction`` still refuses to rewrite the
    # spec toward a vehicle if such a drift is human-approved.
    fault_binary_mutation: bool = False
    # Carrier-agnostic host identity — populated when ``scope == "host"``.
    # Empty for Kubernetes targets. See ``as_target()``.
    host_name: str = ""
    # Precise, non-editorialized cause for a REJECT scope (``__banned__`` /
    # ``__escape__`` / ``__unknown__``), set by whoever classified it (the
    # classifier for banned/unknown subcommands, the screener for an
    # unresolved host-escape carrier). Surfaced verbatim in the guard's reason
    # so the model gets the ACTUAL cause instead of a generic template
    # ("tool is in the banned list" / a chroot OR-template). Empty when the
    # origin did not record a specific cause — the guard then falls back to its
    # generic wording.
    reject_detail: str = ""
    # The compliant alternative for that same REJECT, kept SEPARATE from the
    # cause. The two answer different questions ("why was this refused" vs
    # "what should I do instead") and land in different GuardDecision fields
    # (``reason`` vs ``suggestion``), which the screener renders distinctly.
    #
    # Only the origin can supply it: whether a way forward exists, and what it
    # is, depends on WHICH ban fired — the classifier knows the subcommand and
    # the whitelist, the guard sees only a ``__banned__`` sentinel. Folding it
    # into ``reject_detail`` (as ``kubectl apply -f`` once did) hides it inside
    # prose for one case while six others said nothing at all.
    #
    # EMPTY IS MEANINGFUL: it declares "no compliant form exists" — a genuine
    # dead-end such as ``kubectl certificate``. ``guard_gateway`` derives
    # ``is_hard_floor`` from exactly this ("a suggestion means the guard knows a
    # compliant path exists, so this is a form issue"), so never fill it just to
    # avoid an empty field.
    reject_suggestion: str = ""

    def as_target(self):
        """Return the carrier-agnostic :class:`TargetProtocol` view."""
        from chaos_agent.agent.target_guard.target_protocol import (
            HostTarget,
            K8sTarget,
        )
        if self.scope == "host":
            return HostTarget(
                scope="host",
                host_name=self.host_name or (self.names[0] if self.names else ""),
            )
        return K8sTarget(
            scope=self.scope,
            namespace=self.namespace,
            names=self.names,
            labels=dict(self.labels),
        )


@dataclass
class GuardDecision:
    """The verdict ``target_drift_guard`` returns for one tool_call.

    Distinct from ApprovedTarget / EffectiveTarget in being MUTABLE
    — callers may attach extra fields (e.g. duration_ms for tracing)
    without copying the whole record. The other two are frozen
    because their identity matters.
    """

    verdict: GuardVerdict
    # Short human-readable reason. Goes to audit log + the LLM-facing
    # ToolGuardError message ("rejected because: X"). Should be
    # specific enough that a human reviewing logs can recreate the
    # decision without re-running the classifier.
    reason: str
    # Parsed effective target, when the classifier succeeded.
    # Replan path reads this to update ``state.target`` so the next
    # agent_loop iteration plans for the LLM's intended resource
    # (which the user can then approve or override at the new
    # confirmation_gate).
    effective: Optional[EffectiveTarget] = None
    # Optional "here's what would have been allowed" hint, surfaced
    # to the LLM in the rejection ToolMessage. Helps it learn vs.
    # silent ratelimit-style rejection.
    suggestion: str = ""

    @property
    def is_reject(self) -> bool:
        """Convenience predicate — all REJECT_* verdicts roll up."""
        return self.verdict in (
            GuardVerdict.REJECT_DRIFT,
            GuardVerdict.REJECT_BANNED,
            GuardVerdict.REJECT_UNKNOWN,
        )

    @property
    def is_allow(self) -> bool:
        """Convenience predicate — both pass-through verdicts."""
        return self.verdict in (GuardVerdict.ALLOW, GuardVerdict.READONLY)


__all__ = [
    "ApprovedTarget",
    "ConfidenceLevel",
    "EffectiveTarget",
    "GuardDecision",
    "GuardVerdict",
]
