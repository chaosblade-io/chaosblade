"""Type definitions for target-drift guard.

The guard's job is to prevent ``execute_loop``'s LLM from silently
acting on a different resource than ``confirmation_gate`` approved.
Two complementary records and a verdict drive every decision:

  - ``ApprovedTarget`` — frozen at confirmation_gate. The "what the
    user said yes to" record. Includes both the k8s resource identity
    (scope/namespace/names/labels) AND the ChaosBlade fault family
    (fault_target). Whether the fault family is locked is governed by
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
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .mechanism_writes import MechanismWriteEntry


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
    # Tool is explicitly banned (kubectl apply/create -f <file|URL> whose
    # manifest the guard cannot see, _execute_skill_script without opt-in,
    # kubectl config write). Block, no replan attempt.
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

    @property
    def is_form_level_rejection(self) -> bool:
        """True when re-submitting the SAME call form is guaranteed to be
        rejected again — the verdict is a statement about the form's
        admissibility, so a replan must treat it as a hard constraint.

        DRIFT/BANNED/UNKNOWN qualify. STAGNANT does not: it is a repetition
        circuit-breaker that ALTERNATES by design (same reasoning as its
        member docstring), and its receipt explicitly tells the LLM to
        "adjust the tool_call and retry" — rendering it as a never-relaxing
        boundary hands the model two contradictory definitions of the same
        refusal (B76 review P1-1). ALLOW/READONLY are trivially not
        rejections.

        Single source of truth for every consumer that needs the
        "hard boundary vs evidence" split (execute_loop's replan
        constraint collector). New verdicts default to False — fail-open
        into the evidence semantics rather than falsely freezing a plan.
        """
        return self.name in _FORM_LEVEL_REJECTION_NAMES


# Member names (not values — values are wire strings, names are stable
# enum identifiers) whose rejections are form-level.
_FORM_LEVEL_REJECTION_NAMES = frozenset({
    "REJECT_DRIFT", "REJECT_BANNED", "REJECT_UNKNOWN",
})


# Sentinel scopes — the guard knows these aren't real k8s kinds. Canonical
# home since phase-7 T5 (migrated from classifier.py so the provider-side
# classifiers can import them without touching the classifier module —
# the classifier lazily imports the provider registry, so a module-level
# provider → classifier import would be circular through freeze /
# fault_registry). Consumers may still import them from classifier, which
# re-imports them for its own heavy use.
SCOPE_READONLY = "__readonly__"
SCOPE_BANNED = "__banned__"
SCOPE_UNKNOWN = "__unknown__"
SCOPE_ESCAPE = "__escape__"  # container-escape primitives (nsenter/chroot/unshare)


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
            Distinct from fault_target — a fault on a pod's JVM still
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
        fault_target: fault-carrier target axis value (``pod`` /
            ``node`` / ``cpu`` / ``mem`` / ``jvm`` / ``mysql`` / ...).
            See ``providers.chaosblade.provider.BLADE_TARGET_TO_SCOPE`` (moved out
            of ``classifier.py`` in phase-7 T5) for the mapping
            to k8s scope.
        fault_action: fault action (``fullload`` / ``burn`` /
            ``loss`` / ``delay`` / ...). Whether mismatches on this
            field trigger drift depends on ``lock_fault_type``.
        lock_fault_type: When True (default), the guard treats a
            change to ``fault_target`` (e.g. ``cpu`` → ``mem``) as
            drift even if scope/namespace/names match. ``fault_action``
            is NEVER locked by this flag — sub-action tuning
            (fullload→high) is always considered legitimate
            "method switch" autonomy.
    """

    scope: str
    namespace: str
    names: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    is_namespace_wide: bool = False
    fault_target: str = ""
    fault_action: str = ""
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
    # PVC claim names referenced by the approved pod(s), discovered at freeze
    # time (cluster query of ``spec.volumes[*].persistentVolumeClaim.claimName``).
    # Anchors the DRILL OCCUPANCY VEHICLE exception: a resource-occupancy drill
    # (e.g. RWO cloud-disk multi-attach conflict) must create a behaviourless
    # sleep pod that claims the SAME PVC the target uses, so the occupant's
    # claim set is validated against these frozen names — an occupant claiming
    # any other PVC is resource-selection drift. Empty when the approval has no
    # resolvable pod identity or the pods reference no PVCs (the occupant
    # exception then has nothing to anchor on and stays refused).
    pvc_claims: tuple[str, ...] = ()
    # Carrier-agnostic host identity. Populated when ``scope == "host"``
    # (bare-metal / VM faults). Empty for Kubernetes targets, which keep
    # using namespace/names/labels. See ``as_target()``.
    host_name: str = ""
    # Case-manifest mechanism writes — FIRST-CLASS, deliberately NOT routed
    # through ``secondary_scopes``/``secondary_namespace`` (whose comparison
    # path skips name validation, so a name-level entry there would degrade
    # to kind+namespace matching). Each entry legislates one write domain
    # OUTSIDE the victim target's own coverage: cross-namespace auxiliary
    # objects, cluster-scoped mechanism nodes, transient objects created
    # under a declared name prefix. Parsed deterministically from the settled
    # case file's ``mechanism_writes`` frontmatter (code reads the file — the
    # Agent's reading of the case prose is never an authorization input) and
    # frozen at approval together with the victim. The drift policy consults
    # these BEFORE the victim comparison: a call whose canonicalised
    # scope+namespace matches an active entry passes only when its names are
    # a subset of the entry's names (or, for a prefix entry, every name
    # starts with the prefix). Empty for every case without a manifest —
    # those runs keep today's freeze output and guard behaviour
    # byte-identically.
    mechanism_entries: "tuple[MechanismWriteEntry, ...]" = ()
    # Case-file ``recovery_channel`` legislation — the FIRST source of the
    # D3 three-source routing (openspec faultdrill-cr-channel). Parsed
    # deterministically from the settled case frontmatter at freeze time
    # (same one-directional chain as ``mechanism_entries``: code re-reads
    # the file, no LLM input point) and frozen here so the CR-channel
    # route gate consults the case's own recovery-route declaration
    # BEFORE the blade verb-vocabulary proxy. The proxy is a temporary
    # M2 stand-in that classifies by taxonomy verbs; a k8s-native
    # mechanism whose verbs happen to land in the blade vocabulary
    # (NXDOMAIN: target=network action=dns — no blade equivalent exists)
    # is only distinguishable via this declaration. ``apiserver-write``
    # = the case legislates CR-channel routing; ``""`` = no declaration
    # (every legacy case — the gate falls back to the verb proxy,
    # behaviour byte-identical to pre-declaration).
    recovery_channel: str = ""
    # Write-set approval state — the in-graph half of the D4 invariant.
    # True means the snapshot still carries manifest entries beyond the
    # victim coverage that NO knowing human has approved yet. Set at the
    # single freeze point (safety_check, when ``entries_beyond_victim``
    # is non-empty); cleared at the single approval point (the gate's
    # approved branch re-freeze, and the human-approved drift-correction
    # rebuild). ``execute_loop``'s entry sentinel terminates the run
    # before any cluster mutation while this flag survives — the
    # structural backstop for every path that reaches execution without
    # passing a confirmation card under a human's eyes (route skips,
    # ``aupdate_state(as_node=...)`` lifts, blind channel resumes, and
    # future paths not yet invented). False / absent for every case
    # without a manifest, and after any legitimate approval.
    widening_pending_approval: bool = False
    # Contract duration frozen from ``FaultSpec.duration_seconds`` at the
    # single freeze point. Anchors the duration drift net: the executor's
    # ``--timeout`` flag (experiment auto-recovery bound — layer 3 of the
    # three-layer duration guarantee) may carry operational headroom over
    # the contract value but must not exceed it unboundedly. A free-form
    # flags ``--timeout 999999`` would silently convert the user-approved
    # verification window into an unbounded fault residence time the
    # moment the task dies before its cleanup chain runs — the exact
    # scenario layer 3 exists to bound. Zero when the spec carried no
    # duration (the net then stays silent — no anchor, no comparison).
    duration_seconds: int = 0

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
        fault_target: fault-carrier target name if the call invokes
            ChaosBlade (either directly via ``blade_create`` or via
            ``kubectl exec POD -- blade create``).
        fault_action: fault action.
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
    fault_target: str = ""
    fault_action: str = ""
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
    # Exec-vehicle node binding: a HOST-level blade command inside
    # ``kubectl exec POD -- blade create ...`` (no ``k8s`` prefix) carries
    # no selector of its own — the fault lands on whatever node hosts the
    # exec'd pod, so the pod IS the node binding. The classifier records
    # the pod's identity here (structured fact from the command shape, not
    # a guess); the screener resolves the pod's nodeName against the live
    # cluster and pins ``names`` to it before the drift comparison. When
    # the resolved node is not in the approved name set — or cannot be
    # resolved at all — the comparison keeps its fail-closed review.
    exec_pod_name: str = ""
    exec_pod_namespace: str = ""
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
    # Why a kubectl-exec inner command FAILED the read-only probe test, set by
    # the classifier's exec branch from ``readonly.readonly_inner_tokens_reason``
    # (the reason view of the shared judge). Only meaningful to the READ-ONLY
    # phase screeners (phase1 / verifier / recover Layer 2): scope is still
    # "pod" — a legitimate mutation target in Phase 2 — so the cause cannot ride
    # ``reject_detail`` (whose slot is reserved for REJECT scopes and whose
    # emptiness drives ``is_hard_floor``). Empty for every non-exec call and
    # for execs whose inner command passed the probe test. Before this field
    # existed the reason was flattened to a boolean at the ``is_readonly_*``
    # API boundary and the screeners re-invented a generic "would mutate"
    # template — which read as "all exec is blocked" and pushed the model into
    # over-generalisation (task inject-a9ea4da7).
    readonly_probe_reason: str = ""
    # Mechanism-level policy ban: the call's INJECTION MECHANISM is forbidden
    # by policy regardless of how the call is reshaped. Distinct from BOTH
    # neighbours:
    #   - NOT a form issue — no compliant reshape of THIS call passes, so the
    #     "adjust and retry" guidance would send the model spiralling through
    #     doomed variants (the task-190c94e8 holder-pod retry loop).
    #   - NOT a universal hard floor — other mechanisms/kinds ARE allowed, so
    #     "stop entirely" is also wrong.
    # The honest move is to CHANGE MECHANISM, i.e. ``request_replan``. Canonical
    # example: creating a workload kind (Pod/Deployment/Job/...) via manifest
    # apply — the guard refuses to spawn new workloads because their blast
    # radius cannot be scoped. The screener renders replan guidance instead of
    # "adjust and retry" when this flag is set.
    mechanism_banned: bool = False
    # Drill occupancy vehicle: a ``kubectl apply/create`` manifest that passed
    # the OCCUPANT CONTRACT — a behaviourless (sleep-only) pod whose sole
    # purpose is to hold a scarce resource (an RWO PVC attach slot) so a
    # re-created target pod collides on it and stalls. Set by the classifier;
    # the screener then validates ``occupant_claims`` against the frozen
    # ``approved.pvc_claims`` and registers the pod as a task vehicle so its
    # later delete is exempt from drift and recover cleans it up. Identity
    # drift comparison never applies — the occupant's name is new by
    # construction and can only mismatch the approved target.
    is_vehicle_manifest: bool = False
    # PVC claim names the occupant manifest references
    # (``spec.volumes[*].persistentVolumeClaim.claimName``). Meaningful only
    # when ``is_vehicle_manifest`` is True.
    occupant_claims: tuple[str, ...] = ()
    # Recovery-carrier vehicle: a ``kubectl run`` that passed the RECOVERY
    # CARRIER SHAPE (recovery-carrier-standard) — a sleep-skeleton pod this
    # task creates inside the approved namespace to host the bounded-recovery
    # TIMER for API-plane faults (patch deployment / delete PVC / patch cm),
    # whose rollback lives in no node-local or blade host. Set by the
    # classifier; the screener registers the pod as a ``recovery_carrier``
    # vehicle artifact (task-side, zero cluster-side marker) so subsequent
    # execs into it — token probe, timer arm, re-arm — ride the vehicle
    # exemption, and finalize/recover's cleanup chain deletes the whole
    # asset stack (pod + sa/role/rolebinding family). Distinct from the
    # occupant contract: occupancy HOLDS a resource (anchor =
    # ``approved.pvc_claims``); a recovery carrier EXECUTES recovery calls
    # (anchor = in-net pod secondary scope + task-side registration).
    is_recovery_carrier: bool = False
    # Drill-target manifest: a single-Deployment ``kubectl apply -f`` that
    # passed the DRILL-TARGET CONTRACT (drill-target-contract) — the victim
    # workload this task stages itself when the approved target does not
    # exist yet (case #38: the dedicated PVC-mounting target was deleted
    # out-of-band between drills). Shape gate, classifier-side: exactly one
    # container under spec.template.spec (no initContainers), no host* /
    # privileged / capabilities / hostPath, an image from the carrier
    # allow-set, and only persistentVolumeClaim/configMap/secret volumes.
    # Identity is NOT anchored here — unlike an occupant (whose generated
    # name can never match the approval) the drill target's name IS the
    # approved identity, so the screener runs the ORDINARY drift net and
    # registers the ALLOW as an ``occupant_deployment`` vehicle artifact:
    # finalize/recover cleanup deletes it and the recovery delete rides
    # the deployment-kind exemption.
    is_drill_target_manifest: bool = False
    # Inline blade experiment cleanup over the kubectl-exec channel:
    # ``kubectl exec POD -- blade destroy <uid>`` (or ``revoke``). The
    # classifier routes this shape to SCOPE_UNKNOWN — experiment cleanup
    # is MUTATING, not read-only (the readonly bucket's probe-layer twin,
    # ``readonly.py``, has always legislated destroy/revoke as mutating;
    # a read-only verdict here let ANY UID pass with zero provenance,
    # twelfth-round finding E3) — and records the UID here so the
    # screener's provenance gate (the one the blade_destroy tool face
    # rides) can verify it against the UIDs this task created before the
    # call executes. Empty for every non-destroy blade shape and every
    # other tool. Defence-in-depth: when the screener's gate never runs,
    # the UNKNOWN scope makes the guard fail closed instead of passing
    # the cleanup through as read-only.
    blade_destroy_uid: str = ""
    # Execution-side fault duration: the ``--timeout`` value the call's
    # blade tokens carry (last-wins, matching blade's pflag semantics) in
    # SECONDS. Set by both blade surfaces — the blade_create tool's
    # free-form flags string and the inline ``kubectl exec ... blade
    # create`` tokens — so the drift net can compare it against the frozen
    # contract duration (``ApprovedTarget.duration_seconds``). Zero when
    # the call carries no ``--timeout`` (the executor then injects one
    # from the contract / minimum floor). Deliberately NOT clamped to the
    # minimum-duration floor: the guard must see the verbatim value the
    # executor would honour, so an over-large token stays visible as
    # duration drift before execution.
    timeout_seconds: int = 0

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
    "SCOPE_BANNED",
    "SCOPE_ESCAPE",
    "SCOPE_READONLY",
    "SCOPE_UNKNOWN",
]
