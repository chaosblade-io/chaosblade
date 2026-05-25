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
