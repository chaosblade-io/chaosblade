"""Profile-registered, bounded resource prefetch for guided planning."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from chaos_agent.agent.capabilities.context import AgentCapabilityContext
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S


@dataclass(frozen=True)
class GuidedPrefetch:
    tool_name: str
    args: dict


Prefetcher = Callable[[FaultSpec | None, AgentCapabilityContext], GuidedPrefetch | None]


def _k8s_prefetch(spec: FaultSpec | None, context: AgentCapabilityContext) -> GuidedPrefetch | None:
    if "kubectl_read" not in context.active_tool_names:
        return None
    return GuidedPrefetch(
        tool_name="kubectl_read",
        args={
            "subcommand": "get",
            "v_args": (
                f"pods -n {spec.namespace} -o wide"
                if spec and spec.namespace
                else "namespaces"
            ),
        },
    )


def _host_prefetch(spec: FaultSpec | None, context: AgentCapabilityContext) -> GuidedPrefetch | None:
    if "host_read" not in context.active_tool_names:
        return None
    # ``hostname`` is read-only and establishes that subsequent guided options
    # refer to the connected host rather than a guessed resource name.
    return GuidedPrefetch(tool_name="host_read", args={"command": "hostname"})


GUIDED_PREFETCHERS: dict[str, Prefetcher] = {
    PROFILE_K8S: _k8s_prefetch,
    PROFILE_HOST: _host_prefetch,
}


def build_guided_prefetch(
    spec: FaultSpec | None,
    context: AgentCapabilityContext,
) -> GuidedPrefetch | None:
    """Return the single safe prefetch registered for this profile."""
    prefetcher = GUIDED_PREFETCHERS.get(context.profile)
    return prefetcher(spec, context) if prefetcher else None


__all__ = ["GuidedPrefetch", "GUIDED_PREFETCHERS", "build_guided_prefetch"]
