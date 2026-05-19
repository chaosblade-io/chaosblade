"""FCAT (Fault Context Adaptation Table): declarative, rule-driven context adaptation.

Lets existing graph nodes adapt behavior based on {fault type x target characteristics
x environment constraints} without per-node hardcoding.

Design principles (from first-principles review):
  - Defect 1 fix: rules specify mode ("direct"|"llm"|"both"), LLM-mode hooks added
  - Defect 2 fix: rules describe generic patterns, not case-specific patches
  - Defect 3 fix: knowledge-type content uses dimension declarations, not hardcoded commands
  - Defect 4 fix: combines_with supports multi-rule combinations
  - Defect 7 fix: lookup_adaptations() filters by rule_type to prevent cross-concern coupling
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Core data structure
# ---------------------------------------------------------------------------

@dataclass
class VerificationGap:
    """A detected trust gap in verification that may trigger re-verification.

    P2 verification integrity guard supports five gap types:
    - step_gap: missing verification steps
    - layer1_contradiction: blade reports Success but 0 resources affected
    - layer2_layer1_conflict: Layer2 conclusion contradicts Layer1 facts
    - baseline_used_check: baseline available but BaselineUsed=false (Gap D)
    - primary_evidence_consistency: PrimaryEvidenceObserved=false but Overall=verified (Gap E)
    """

    gap_type: str
    """'step_gap' | 'layer1_contradiction' | 'layer2_layer1_conflict' | 'baseline_used_check' | 'primary_evidence_consistency'."""

    description: str
    """Human-readable description of the gap."""

    missing_steps: list[int] | None = None
    """Step numbers not covered (only for step_gap type)."""


@dataclass
class AdaptationDirective:
    """Single adaptation directive: when condition matches, guide a node to adjust behavior."""

    id: str
    """Unique rule identifier, e.g. 'P0-param-safety-burn-lowmem'."""

    target_node: str
    """Node that should apply this rule: direct_execute | execute_loop | safety_check | baseline_capture | verifier."""

    mode: str
    """Execution mode: 'direct' | 'llm' | 'both'.  Defect-1 fix: LLM mode must be covered."""

    rule_type: str
    """Category for partition filtering (Defect-7 fix):
    'param_override' | 'conflict_escalation' | 'baseline_supplement' | 'verification_integrity_guard'
    """

    condition: Callable[[str, str, str, dict], bool]
    """(scope, target, action, target_metadata) -> bool."""

    action: dict
    """Type-specific payload.  Knowledge-type content uses dimension declarations
    (Defect-3 fix), not hardcoded commands."""

    priority: int = 0
    """Higher priority applied first."""

    description: str = ""
    """Human-readable description of what this rule does."""

    combines_with: list[str] = field(default_factory=list)
    """IDs of rules that can be safely applied simultaneously (Defect-4 fix)."""


# ---------------------------------------------------------------------------
# Shared utility functions
# ---------------------------------------------------------------------------

_BURN_DEFAULT_SIZE = 100  # 100MB blocks (matches direct_execute._BURN_DEFAULT_SIZE)
_BURN_MINIMUM_SIZE = 20  # 最小可观测 burn 大小（fail-safe 默认值）
_OOMKILL_RISK_THRESHOLD_MB = 512


def compute_safe_burn_size(
    pod_memory_limit_mb: int | None,
    pod_memory_usage_mb: int | None = None,
) -> int:
    """Compute a safe --size value for pod-disk-burn given the pod memory limit.

    The --size parameter controls the dd block size (bs).  ChaosBlade creates
    separate dd processes for read and write (--read --write), each allocating
    a buffer of ``bs`` bytes.  Total dd memory = size * 2 (read + write).

    Formula when actual usage is known:
        available = limit - usage
        size <= available / dd_process_count / safety_factor
        → size <= (limit - usage) / 3   (2 dd procs + 50% safety margin)

    Formula when usage is unknown (conservative):
        Reserve 70% of limit for the application itself
        (empirically, .NET/JVM apps routinely use 60-80% of their limit)
        → size <= limit * 0.30 / 2 = limit * 0.15 → limit // 7 (integer)

    Fail-closed: when memory is unknown, return minimum safe value (20),
    not the aggressive default (100).
    """
    if pod_memory_limit_mb is None or pod_memory_limit_mb <= 0:
        return _BURN_MINIMUM_SIZE  # 20 — 未知内存时使用最小安全值

    if pod_memory_usage_mb is not None and pod_memory_usage_mb > 0:
        # Usage-based: calculate from actual available memory
        available = pod_memory_limit_mb - pod_memory_usage_mb
        if available <= 0:
            # Pod already at/over limit — use minimum only
            return _BURN_MINIMUM_SIZE
        # Divide by 3: 2 dd processes + 50% safety margin
        safe_size = max(_BURN_MINIMUM_SIZE, available // 3)
    else:
        # Limit-only: reserve 70% for app, split 30% across 2 dd processes
        safe_size = max(_BURN_MINIMUM_SIZE, pod_memory_limit_mb // 7)
    return min(_BURN_DEFAULT_SIZE, safe_size)


# ---------------------------------------------------------------------------
# Lookup function (rule_type partition filtering — Defect-7 fix)
# ---------------------------------------------------------------------------

def lookup_adaptations(
    scope: str,
    target: str,
    action: str,
    target_metadata: dict,
    rule_type: str | None = None,
) -> list[AdaptationDirective]:
    """Return matching adaptation directives sorted by priority (descending).

    When *rule_type* is specified, only rules of that type are returned —
    nodes must not cross-query unrelated rule types.
    """
    results: list[AdaptationDirective] = []
    for rule in _FAULT_CONTEXT_ADAPTATIONS:
        if rule_type and rule.rule_type != rule_type:
            continue
        try:
            if rule.condition(scope, target, action, target_metadata):
                results.append(rule)
        except Exception:
            logger.debug("FCAT condition error for %s", rule.id, exc_info=True)
    results.sort(key=lambda r: r.priority, reverse=True)
    return results


# ---------------------------------------------------------------------------
# Default FCAT rule table (5 rules, v3)
# ---------------------------------------------------------------------------

_FAULT_CONTEXT_ADAPTATIONS: list[AdaptationDirective] = [
    # P0: param safety boundary guard (generic pattern — Defect-2 fix)
    AdaptationDirective(
        id="P0-param-safety-burn-lowmem",
        target_node="direct_execute",
        mode="both",
        rule_type="param_override",
        condition=lambda s, t, a, m: (
            s == "pod" and t == "disk" and a == "burn"
            and (m.get("pod_memory_limit_mb") is None
                 or m.get("pod_memory_limit_mb") < _OOMKILL_RISK_THRESHOLD_MB)
        ),
        action={
            "param_overrides": {"size": "auto"},
            "safety_rationale": "burn --size exceeds pod memory safety boundary",
        },
        priority=10,
        combines_with=["P0-evidence-snapshot"],
        description="Low-memory pod: auto-reduce burn --size to prevent OOMKill",
    ),

    # P0-evidence-snapshot: capture evidence after blade_create (Defect-4 fix)
    AdaptationDirective(
        id="P0-evidence-snapshot",
        target_node="direct_execute",
        mode="both",
        rule_type="param_override",
        condition=lambda s, t, a, m: (
            s == "pod" and t == "disk" and a == "burn"
            and (m.get("pod_memory_limit_mb") is None
                 or m.get("pod_memory_limit_mb") < _OOMKILL_RISK_THRESHOLD_MB)
        ),
        action={
            "evidence_capture": True,
            "snapshot_commands": ["ls -lah /tmp", "df -h"],
            "snapshot_delay_seconds": 3,
            "safety_rationale": (
                "low-mem pod may OOMKill during burn, "
                "capture evidence before potential crash"
            ),
        },
        priority=5,
        combines_with=["P0-param-safety-burn-lowmem"],
        description="Low-memory pod: capture quick evidence snapshot after blade_create",
    ),

    # P1: same-target same-action overlay injection -> force confirm
    # condition: always registered; safety_check determines if escalation is needed
    # by checking conflict_details.same_action_as_request before querying FCAT.
    AdaptationDirective(
        id="P1-same-target-same-action-confirm",
        target_node="safety_check",
        mode="both",
        rule_type="conflict_escalation",
        condition=lambda s, t, a, m: True,  # always registered; trigger logic is inside safety_check
        action={
            "escalation": "confirm_required",
            "reason": "Same target with same action already has an active experiment",
        },
        priority=10,
        description="Same-target same-action overlay: escalate from warning to confirm_required",
    ),

    # P2: verification integrity guard (expanded triggers — Defect-5 fix)
    AdaptationDirective(
        id="P2-verification-integrity-guard",
        target_node="verifier",
        mode="both",
        rule_type="verification_integrity_guard",
        condition=lambda s, t, a, m: True,  # always registered; trigger logic is inside the node
        action={
            "enforce_step_coverage": True,
            "detect_layer1_contradiction": True,
            "detect_layer2_layer1_conflict": True,
            "max_reverify_attempts": 1,
        },
        priority=0,
        description="Verification integrity guard: auto-reverify on step gaps or evidence contradictions",
    ),

    # P3: baseline context enrichment (dimension declarations — Defect-2+3 fix)
    AdaptationDirective(
        id="P3-baseline-enrich-disk-io",
        target_node="baseline_capture",
        mode="both",
        rule_type="baseline_supplement",
        condition=lambda s, t, a, m: t == "disk" and a == "burn",
        action={
            "dimensions": ["io_utilization", "io_iowait"],
            "knowledge_source": "fault-verification-strategies.md#L483-514",
        },
        priority=5,
        description="Disk-burn: supplement baseline with I/O metrics from knowledge doc",
    ),
]
