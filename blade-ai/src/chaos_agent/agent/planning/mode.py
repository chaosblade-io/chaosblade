"""Run-mode policy for interactive plan construction."""

from __future__ import annotations

from chaos_agent.agent.spec.fault_spec import FaultSpec

PLANNING_MODE_GUIDED = "guided"
PLANNING_MODE_EXPERT = "expert"
_VALID_MODES = frozenset((PLANNING_MODE_GUIDED, PLANNING_MODE_EXPERT))


def resolve_planning_mode(state: dict, spec: FaultSpec | None) -> str:
    """Resolve an explicit mode, otherwise promote complete input to expert."""
    requested = str(state.get("planning_mode") or "").lower()
    if requested in _VALID_MODES:
        return requested
    return PLANNING_MODE_EXPERT if spec is not None and spec.is_complete else PLANNING_MODE_GUIDED


__all__ = ["PLANNING_MODE_GUIDED", "PLANNING_MODE_EXPERT", "resolve_planning_mode"]
