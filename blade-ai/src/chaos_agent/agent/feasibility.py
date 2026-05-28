"""Injection feasibility assessment — pre-confirm headroom check.

Determines whether a fault injection can physically produce an
observable effect given the target's current resource usage vs the
injection parameters. E.g. if Pod memory is at 92.5% of its limit
and injection targets 98%, there's only 5.4% headroom — the effect
is physically unobservable.

Architecture: Protocol/Registry pattern (identical to target_health.py).
- FeasibilityChecker Protocol — one per resource dimension (mem/cpu/disk)
- _REGISTRY dict — dispatch on spec.blade_target
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
    from chaos_agent.agent.fault_spec import FaultSpec

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
    blade_target: str

    async def assess(
        self, spec: "FaultSpec", kubeconfig: str
    ) -> FeasibilityReport | None: ...


_REGISTRY: dict[str, FeasibilityChecker] = {}


def register_feasibility_checker(checker: FeasibilityChecker) -> None:
    _REGISTRY[checker.blade_target] = checker
    logger.info(
        "registered feasibility checker: blade_target=%s", checker.blade_target
    )


async def assess_feasibility(
    spec: "FaultSpec",
    kubeconfig: str = "",
) -> FeasibilityReport | None:
    """Single entry point — safety_check calls this once per turn.

    Returns None when no checker exists for the blade_target or the
    checker cannot determine feasibility (missing data). None is
    equivalent to OK — fail-open.
    """
    checker = _REGISTRY.get(spec.blade_target)
    if checker is None:
        return None
    try:
        if spec.blade_target in ("mem", "cpu"):
            from chaos_agent.agent._feasibility_checkers import is_metrics_server_available
            if not await is_metrics_server_available(kubeconfig):
                return FeasibilityReport(
                    severity=FeasibilitySeverity.SKIPPED,
                    headroom=0.0,
                    current_value="",
                    limit_value="",
                    target_value="",
                    message="metrics-server unavailable — headroom check skipped",
                    recommendation="Install metrics-server for pre-injection feasibility assessment",
                )
        return await checker.assess(spec, kubeconfig)
    except Exception as exc:
        logger.warning(
            "feasibility checker failed for blade_target=%s: %s",
            spec.blade_target,
            exc,
        )
        return None


# Import checkers to trigger registration at module load time.
from chaos_agent.agent._feasibility_checkers import register_all as _register_all  # noqa: E402

_register_all()
