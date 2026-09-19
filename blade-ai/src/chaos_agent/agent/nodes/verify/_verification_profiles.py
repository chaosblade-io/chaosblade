"""Per-fault-type post-injection effect-check seam.

Where ``FaultProvider`` owns *how a fault is injected / recovered*, a
``VerificationProfile`` owns the ONE thing about verification that must live in
code: the **programmatic post-injection effect check** — the deterministic
measurement the execute node runs right after injection and feeds to the
verifier as authoritative evidence (e.g. sampling disk I/O throughput to prove a
burn is active). This is runtime measurement, not knowledge.

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

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from chaos_agent.agent.nodes.execute._effect_checks import (
    _verify_disk_burn_effect,
    _verify_disk_fill_effect,
)
from chaos_agent.agent.nodes.verify._deterministic_rules import (
    CPU_FULLLOAD_RULE,
    DISK_BURN_RULE,
    DISK_FILL_RULE,
    MEM_LOAD_RULE,
    PROCESS_KILL_RULE,
    DeterministicRule,
)


@dataclass
class VerificationContext:
    """Inputs a profile's post-injection check may need. Assembled at the call
    site (execute node) from local state; ``action`` selects the check."""

    scope: str = ""
    target: str = ""
    action: str = ""
    parsed_flags: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    tool_pod_name: str | None = None
    kubeconfig: str = ""
    experiment_uid: str = ""


@dataclass(frozen=True)
class PostCheckSpec:
    """Declarative post-injection effect check owned by a fault profile.

    ``result_key`` is the ``result[...]`` key the check's non-empty return is
    stored under; ``fn`` is the async effect-check function. The execute node
    iterates these instead of hardcoding one call per fault type, so a new
    fault's post-check is a new declaration on its profile.
    """

    result_key: str
    fn: Callable[..., Awaitable[dict | None]]


class VerificationProfile(Protocol):
    """Per-fault-type profile. Two slots are programmatic runtime assets:
    the post-injection effect check (execute-time measurement) and the
    deterministic verdict rules (finalize-time adjudication of measured
    numbers). All verification knowledge lives in the data layer (skill
    case + knowledge docs), never here."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]: ...

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]: ...


class _DefaultProfile:
    """Neutral profile: no programmatic post-injection check, no
    deterministic rule. Fault types whose effect the verifier observes
    purely via the skill case / knowledge docs need no entry in the
    registry and fall back to this."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]:
        return ()

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return ()


class _DiskProfile(_DefaultProfile):
    """Disk faults expose a deterministic fill / burn effect check that the
    execute node runs and hands to the verifier as authoritative evidence.
    Both families carry deterministic verdict rules: burn (measured I/O
    ACTIVE → programmatic passed) and fill (measured usage reaches the
    injected percent/size target)."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]:
        # Effect-check functions live in the ``execute._effect_checks`` leaf
        # module. Dispatch is action-precise so only the matching check runs
        # (the functions keep a defensive target/action self-guard regardless).
        if ctx.action == "fill":
            return (PostCheckSpec("disk_fill_post_check", _verify_disk_fill_effect),)
        if ctx.action == "burn":
            return (PostCheckSpec("disk_burn_post_check", _verify_disk_burn_effect),)
        return ()

    def deterministic_rules(self) -> tuple[DeterministicRule, ...]:
        return (DISK_BURN_RULE, DISK_FILL_RULE)


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


_DEFAULT_PROFILE = _DefaultProfile()

# Registry keyed by fault target. Only fault types with a programmatic
# post-injection effect check or a deterministic verdict rule need an
# entry; everything else falls back to the neutral default — their
# verification is driven entirely by the skill case and knowledge docs.
_PROFILE_REGISTRY: dict[str, VerificationProfile] = {
    "disk": _DiskProfile(),
    "process": _ProcessProfile(),
    "cpu": _CpuProfile(),
    "mem": _MemProfile(),
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
    "VerificationContext",
    "VerificationProfile",
    "PostCheckSpec",
    "resolve_verification_profile",
    "resolve_deterministic_rules",
]
