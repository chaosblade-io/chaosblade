"""Environment capability profiles used by prompt and tool assembly.

An environment profile answers where a fault runs.  It is deliberately
separate from :mod:`chaos_agent.agent.providers`, which answers how a fault is
injected and recovered.  A ChaosBlade provider can operate on both Kubernetes
and a host, while the target authority and observation tools differ.
"""

from chaos_agent.agent.environment_profiles.base import EnvironmentProfile
from chaos_agent.agent.environment_profiles.registry import (
    EnvironmentProfileRegistry,
    get_environment_profile,
)

__all__ = [
    "EnvironmentProfile",
    "EnvironmentProfileRegistry",
    "get_environment_profile",
]
