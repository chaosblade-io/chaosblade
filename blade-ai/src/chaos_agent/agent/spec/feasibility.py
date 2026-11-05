"""Injection feasibility assessment — pre-confirm headroom check.

Determines whether a fault injection can physically produce an
observable effect given the target's current resource usage vs the
injection parameters. E.g. if Pod memory is at 92.5% of its limit
and injection targets 98%, there's only 5.4% headroom — the effect
is physically unobservable.

Architecture: Protocol/Registry pattern (identical to target_health.py).
- FeasibilityChecker Protocol — one per resource dimension (mem/cpu/disk)
- _REGISTRY dict — dispatch on spec.fault_target
- assess_feasibility() — single entry point, fail-open

Purely advisory by default. When settings.feasibility_check_block_on_impossible
is True, severity=impossible upgrades safety_status to rejected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from chaos_agent.agent.spec.fault_spec import FaultSpec
    from chaos_agent.transports import TransportTarget

logger = logging.getLogger(__name__)


class FeasibilitySeverity(Enum):
    OK = "ok"
    TIGHT = "tight"
    IMPOSSIBLE = "impossible"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class FeasibilityReport:
    severity: FeasibilitySeverity
    headroom: float
    current_value: str
    limit_value: str
    target_value: str
    message: str
    recommendation: str

    def to_dict(self) -> dict:
        return {
            "severity": self.severity.value,
            "headroom": round(self.headroom, 4),
            "current_value": self.current_value,
            "limit_value": self.limit_value,
            "target_value": self.target_value,
            "message": self.message,
            "recommendation": self.recommendation,
        }


class FeasibilityChecker(Protocol):
    fault_target: str
    # Whether this dimension's probe depends on the cluster metrics-server
    # (``kubectl top``). Declared per-checker so ``assess_feasibility`` never
    # hard-codes which fault_targets need it — the metrics-server pre-check is
    # driven by this flag instead of a fixed ("mem","cpu") tuple.
    requires_metrics_server: bool

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str,
        *, xport_target: TransportTarget | None = None,
    ) -> FeasibilityReport | None: ...


_REGISTRY: dict[str, FeasibilityChecker] = {}


def register_feasibility_checker(checker: FeasibilityChecker) -> None:
    _REGISTRY[checker.fault_target] = checker
    logger.info(
        "registered feasibility checker: fault_target=%s", checker.fault_target
    )


# ---------------------------------------------------------------------------
# (profile, target) probe seam — the "how/where to read" axis
# ---------------------------------------------------------------------------
#
# A checker owns the environment-independent PHYSICS (headroom → severity +
# the human-facing message). The DATA SOURCE (how to read current/limit) is the
# only environment-dependent step, so it lives behind a ``FeasibilityProbe``
# keyed by ``(profile, target)``:
#   - profile axis (k8s / host): kubectl reads vs host_read reads. The k8s
#     probe internally still picks the node-vs-pod data source (a legitimate
#     per-scope difference); the host probe reads the bare host.
#   - target axis (mem / cpu / disk): what physical quantity to read.
# This mirrors the baseline ``_baseline_profiles`` / ``_HOST_BASELINE_COMMANDS``
# two-axis matrix. ``network`` is NOT a headroom dimension (it is an
# availability check), so it has no probe — its checker probes inline and is
# fail-open on the host profile.


@dataclass(frozen=True)
class Measurement:
    """Raw, environment-independent feasibility numbers a checker turns into a
    headroom verdict. Per-dimension semantics:
      - mem:  current = usage MiB,  limit = limit MiB
      - cpu:  current = usage mCPU, limit = capacity mCPU
      - disk: current = usage %,    limit = total GiB (display only)
    ``limit`` is ``None`` when the dimension has no meaningful ceiling.
    """

    current: float
    limit: float | None = None


class FeasibilityProbe(Protocol):
    profile: str  # "k8s" | "host"
    target: str   # "mem" | "cpu" | "disk"

    async def measure(
        self, spec: "FaultSpec", kubeconfig: str,
        *, xport_target: TransportTarget | None = None,
    ) -> Measurement | None: ...


_PROBE_REGISTRY: dict[tuple[str, str], FeasibilityProbe] = {}


def register_feasibility_probe(probe: FeasibilityProbe) -> None:
    _PROBE_REGISTRY[(probe.profile, probe.target)] = probe
    logger.info(
        "registered feasibility probe: profile=%s target=%s",
        probe.profile, probe.target,
    )


def resolve_feasibility_probe(profile: str, target: str) -> FeasibilityProbe | None:
    """Return the probe for ``(profile, target)`` or ``None`` (fail-open)."""
    return _PROBE_REGISTRY.get((profile, target))


def profile_for_spec(spec: "FaultSpec") -> str:
    """Map a spec to its capability profile (:data:`PROFILE_K8S` |
    :data:`PROFILE_HOST` | any third profile a family declares).

    Registry-driven and N-ary: delegates to ``profile_of_scope``, which reads
    the owning family's declared ``profile``. Mirrors ``transports.profile_of``
    but keyed on the intent scope, which is what feasibility has pre-injection.
    """
    from chaos_agent.agent.spec.fault_registry import profile_of_scope

    return profile_of_scope(spec.scope)


async def assess_feasibility(
    spec: "FaultSpec",
    kubeconfig: str = "",
    *,
    xport_target: TransportTarget | None = None,
    network_mechanism: str = "",
) -> FeasibilityReport | None:
    """Single entry point — safety_check calls this once per turn.

    Return semantics (essence-B unified guard: "could not determine" is a
    FIRST-CLASS state, never laundered into "feasible"):
      - ``None`` — the check does NOT APPLY: no checker is registered for the
        fault_target, the spec is incomplete, or the target legitimately has
        no ceiling to assess headroom against. Treated as OK downstream
        (advisory-only, fail-open) — but reserved for genuine not-applicable.
      - ``SKIPPED`` report — the check RAN but could not reach a verdict:
        metrics-server unavailable, target unresolvable, the usage/capacity
        read returned nothing, or the checker raised. VISIBLE, never silent.
      - ``OK`` / ``TIGHT`` / ``IMPOSSIBLE`` — an actual headroom verdict.

    ``network_mechanism`` is the case frontmatter's declared injection
    mechanism for ``network`` faults (``netfilter`` / ``userspace-bind`` /
    ``resolver``; ``""`` = undeclared). It is forwarded ONLY to a checker that
    opts in via the ``consumes_network_mechanism`` capability flag — the same
    per-checker-flag dispatch as ``requires_metrics_server``, so this entry
    point never hard-codes which fault_target cares (the network availability
    checker uses it to skip the netfilter-only iptables/interface probes for
    non-netfilter mechanisms). Undeclared/empty → the consumer fails safe to
    the netfilter preconditions (byte-identical to pre-declaration behaviour).
    """
    checker = _REGISTRY.get(spec.fault_target)
    if checker is None:
        return None
    try:
        from chaos_agent.agent.spec.fault_registry import is_host_scope
        if checker.requires_metrics_server and not is_host_scope(spec.scope):
            from chaos_agent.agent.spec._feasibility_checkers import is_metrics_server_available
            if not await is_metrics_server_available(
                kubeconfig, target=xport_target,
            ):
                return FeasibilityReport(
                    severity=FeasibilitySeverity.SKIPPED,
                    headroom=0.0,
                    current_value="",
                    limit_value="",
                    target_value="",
                    message="metrics-server unavailable — headroom check skipped",
                    recommendation="Install metrics-server for pre-injection feasibility assessment",
                )
        if getattr(checker, "consumes_network_mechanism", False):
            return await checker.assess(
                spec, kubeconfig, xport_target=xport_target,
                network_mechanism=network_mechanism,
            )
        return await checker.assess(spec, kubeconfig, xport_target=xport_target)
    except Exception as exc:
        logger.warning(
            "feasibility checker failed for fault_target=%s: %s",
            spec.fault_target,
            exc,
        )
        # Essence-B unified guard: a REGISTERED checker that raised could not
        # determine feasibility — that is a capability loss, not evidence of
        # feasibility. Returning ``None`` here would launder the error into
        # "None ≡ OK" (a silent green on the confirm card). Surface it as a
        # first-class SKIPPED, matching the metrics-server-unavailable and
        # probe-read-failure paths. (``checker is None`` above — no checker
        # exists — stays ``None``: that is genuine not-applicable, not failure.)
        return FeasibilityReport(
            severity=FeasibilitySeverity.SKIPPED,
            headroom=0.0,
            current_value="",
            limit_value="",
            target_value="",
            message=(
                f"feasibility check errored for target '{spec.fault_target}': "
                f"{exc} — headroom check skipped"
            ),
            recommendation=(
                "The checker raised before producing a verdict; treat headroom "
                "as unknown and verify the target's capacity manually before "
                "injecting"
            ),
        )


# Import checkers to trigger registration at module load time.
from chaos_agent.agent.spec._feasibility_checkers import register_all as _register_all  # noqa: E402

_register_all()
