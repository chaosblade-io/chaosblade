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
    blade_uid: str = ""


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
    """Per-fault-type profile. The only slot is the programmatic post-injection
    effect check; all verification knowledge lives in the data layer (skill case
    + knowledge docs), never here."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]: ...


class _DefaultProfile:
    """Neutral profile: no programmatic post-injection check. Fault types whose
    effect the verifier observes purely via the skill case / knowledge docs need
    no entry in the registry and fall back to this."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]:
        return ()


class _DiskProfile(_DefaultProfile):
    """Disk faults expose a deterministic fill / burn effect check that the
    execute node runs and hands to the verifier as authoritative evidence."""

    def post_injection_checks(self, ctx: VerificationContext) -> tuple[PostCheckSpec, ...]:
        # Effect-check functions live in the ``execute._effect_checks`` leaf
        # module. Dispatch is action-precise so only the matching check runs
        # (the functions keep a defensive target/action self-guard regardless).
        if ctx.action == "fill":
            return (PostCheckSpec("disk_fill_post_check", _verify_disk_fill_effect),)
        if ctx.action == "burn":
            return (PostCheckSpec("disk_burn_post_check", _verify_disk_burn_effect),)
        return ()


_DEFAULT_PROFILE = _DefaultProfile()

# Registry keyed by fault target. Only fault types with a programmatic
# post-injection effect check need an entry; everything else falls back to the
# neutral default — their verification is driven entirely by the skill case and
# knowledge docs.
_PROFILE_REGISTRY: dict[str, VerificationProfile] = {
    "disk": _DiskProfile(),
}


def resolve_verification_profile(target: str | None) -> VerificationProfile:
    """Return the verification profile for a fault ``target`` (``_DefaultProfile``
    when the target has no registered profile)."""
    return _PROFILE_REGISTRY.get(target or "", _DEFAULT_PROFILE)


__all__ = [
    "VerificationContext",
    "VerificationProfile",
    "PostCheckSpec",
    "resolve_verification_profile",
]
