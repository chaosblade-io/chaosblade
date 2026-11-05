"""Per-fault-type deterministic verdict rules.

Where ``FaultProvider`` owns *how a fault is injected / recovered*, a
``VerificationProfile`` owns the ONE thing about verification that must live in
code: the **deterministic verdict rule** — the finalize-time adjudication that
turns the numbers the ReAct loop already gathered (the metric timeline) into a
verdict when the comparison is mechanical (a RestartCount delta, disk usage
reaching the injected target). This is runtime adjudication, not knowledge.

There is deliberately NO programmatic post-injection *sampling* seam here. What
evidence to gather (which mount to ``df``, whether to sample ``diskstats``) is a
cognitive act that belongs to the ReAct loop, not a per-fault-type probe the
framework hardcodes — such probes cannot be enumerated completely and would
duplicate the loop's job. The profile only adjudicates once the loop has
produced the metric timeline; a family whose effect needs no mechanical
comparison carries no rule and is judged entirely by the LLM.

Verification KNOWLEDGE (how to observe a fault, partition/overlay semantics, DNS
mechanism, transient-fault rules, event filtering, ...) is deliberately NOT
here. It lives in the data layer:
  - the **skill use-case** — case-specific ``注入验证`` / ``恢复验证`` steps,
    embedded verbatim as the verifier's PRIMARY AUTHORITY;
  - the **knowledge docs** — shared, per-domain knowledge (channel-aware),
    loaded on demand via ``read_knowledge_resource``.

Keeping knowledge out of code removes the former triplication (knowledge doc +
skill case + hardcoded Python strings) and the channel-blindness that hardcoded
strings forced (they had to grow ``is_host`` branches the docs already handled).
"""

from __future__ import annotations

from typing import Protocol

from chaos_agent.agent.nodes.verify._deterministic_rules import (
    CPU_FULLLOAD_RULE,
    DEPLOYMENT_SCALE_DOWN_RULE,
    DISK_FILL_RULE,
    MEM_LOAD_RULE,
    PROCESS_KILL_RULE,
    STS_PARTITION_RULE,
    DeterministicRule,
)


class VerificationProfile(Protocol):
    """Per-fault-type profile. One slot is a programmatic runtime asset: the
    deterministic verdict rules (finalize-time adjudication of measured
    numbers). All verification knowledge lives in the data layer (skill
    case + knowledge docs), never here."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]: ...


class _DefaultProfile:
    """Neutral profile: no deterministic rule. Fault types whose effect the
    verifier observes purely via the skill case / knowledge docs need no entry
    in the registry and fall back to this."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return ()


class _DiskProfile(_DefaultProfile):
    """Disk faults carry the fill verdict rule: measured usage reaching the
    injected percent/size target — anchored on the committed fault handle /
    Layer-1 Success and read off the metric timeline the loop gathered —
    adjudicates to passed. Disk *burn* carries NO rule: its effect (sustained
    write I/O) is judged by the ReAct loop from the timeline, not a hardcoded
    programmatic diskstats probe (the former post-injection sampling seam was
    retired — sampling is the loop's cognitive job, not enumerable in code)."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (DISK_FILL_RULE,)


class _ProcessProfile(_DefaultProfile):
    """Process faults have no programmatic post-injection check — their
    effect lands in pod-restart semantics, observable through the metric
    timeline (RestartCount / Container ID). The kill family carries the
    deterministic verdict rule calibrated on #25 / #25-R."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (PROCESS_KILL_RULE,)


class _CpuProfile(_DefaultProfile):
    """CPU family: the rule is DECLARED but not calibrated (extreme-shape-
    only samples — see the rule's module docstring). evaluate is pinned to
    unknown, so the declaration changes nothing at runtime; it marks the
    typed extension point that calibration data will switch on."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (CPU_FULLLOAD_RULE,)


class _MemProfile(_DefaultProfile):
    """Memory family: same declared-but-uncalibrated state as CPU (zero
    pod-scope samples; the one node-scope sample landed exactly on the
    injected percent — an extreme shape, not a threshold calibration)."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (MEM_LOAD_RULE,)


class _StatefulSetProfile(_DefaultProfile):
    """StatefulSet family: carries the partition-stall rule, whose match key
    is the FROZEN author-declared ``("statefulset", "partition-stall")`` from
    the case's ``deterministic_verdict`` frontmatter (openspec mechanism-
    invariant-trust-boundary) — NOT the drift-prone LLM triple
    (``rollingupdate``/``partition-stuck``), which is why the profile is keyed
    by ``statefulset``. The rule is DECLARED but pinned to unknown (single
    case, no second calibration sample), so resolution changes nothing at
    runtime; it marks the typed extension point Phase 3 calibration flips on."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (STS_PARTITION_RULE,)


class _DeploymentProfile(_DefaultProfile):
    """Deployment family: carries the scale-down rule, whose match key is the
    FROZEN author-declared ``("deployment", "scale-down")`` from the case's
    ``deterministic_verdict`` frontmatter (openspec mechanism-invariant-
    trust-boundary task 6.3) — NOT the drift-prone LLM triple, which is why
    the profile is keyed by ``deployment``. Second declarative field-state
    family after StatefulSet partition. The rule is DECLARED but pinned to
    unknown (single case #34, no second calibration sample), so resolution
    changes nothing at runtime; it marks the typed extension point a
    calibration gate flips on."""

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (DEPLOYMENT_SCALE_DOWN_RULE,)


_DEFAULT_PROFILE = _DefaultProfile()

# Registry keyed by fault target. Only fault types with a deterministic verdict
# rule need an entry; everything else falls back to the neutral default — their
# verification is driven entirely by the skill case and knowledge docs.
_PROFILE_REGISTRY: dict[str, VerificationProfile] = {
    "disk": _DiskProfile(),
    "process": _ProcessProfile(),
    "cpu": _CpuProfile(),
    "mem": _MemProfile(),
    "statefulset": _StatefulSetProfile(),
    "deployment": _DeploymentProfile(),
}


def resolve_verification_profile(target: str | None) -> VerificationProfile:
    """Return the verification profile for a fault ``target`` (``_DefaultProfile``
    when the target has no registered profile)."""
    return _PROFILE_REGISTRY.get(target or "", _DEFAULT_PROFILE)


def resolve_deterministic_rules(
    target: str | None, action: str | None,
) -> tuple[DeterministicRule, ...]:
    """Deterministic rules whose ``(fault_target, fault_action)`` match key
    equals the resolved fault identity.

    Families without a declared rule return ``()`` — the finalize pipeline
    then runs with zero rule interference (LLM verdict untouched, byte-
    identical to the pre-rule-layer behaviour)."""
    if not target or not action:
        return ()
    rules = resolve_verification_profile(target).deterministic_rules()
    return tuple(r for r in rules if r.match == (target, action))


__all__ = [
    "VerificationProfile",
    "resolve_verification_profile",
    "resolve_deterministic_rules",
]
