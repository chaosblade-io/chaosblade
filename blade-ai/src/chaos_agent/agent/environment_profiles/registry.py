"""Registry of environment profiles.

The registry is intentionally small today.  New environments register their
target authority and phase wording here instead of adding ``if profile ==``
branches to shared prompts.
"""

from __future__ import annotations

from chaos_agent.agent.environment_profiles.base import EnvironmentProfile
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S


class EnvironmentProfileRegistry:
    _profiles: dict[str, EnvironmentProfile] = {}

    @classmethod
    def register(cls, profile: EnvironmentProfile) -> None:
        cls._profiles[profile.profile_id] = profile

    @classmethod
    def get(cls, profile_id: str) -> EnvironmentProfile | None:
        return cls._profiles.get(profile_id)

    @classmethod
    def all(cls) -> tuple[EnvironmentProfile, ...]:
        return tuple(cls._profiles.values())

    @classmethod
    def clear(cls) -> None:
        cls._profiles.clear()

    @classmethod
    def register_builtins(cls) -> None:
        cls.register(
            EnvironmentProfile(
                profile_id=PROFILE_K8S,
                target_authority=(
                    "Target identity comes from read-only Kubernetes API observations. "
                    "Namespace, resource names and labels remain unverified until "
                    "the current environment reports them."
                ),
                phase_fragments={
                    "default": (
                        "## Capability Profile\n"
                        "You are operating in a Kubernetes environment. Use only the "
                        "tools bound for this phase. Treat Kubernetes API observations "
                        "as the authority for namespace, resource name and label identity."
                    ),
                },
            )
        )
        cls.register(
            EnvironmentProfile(
                profile_id=PROFILE_HOST,
                target_authority=(
                    "Target identity comes from the configured and verified host connection. "
                    "There is no Kubernetes namespace or workload discovery in this environment."
                ),
                phase_fragments={
                    "default": (
                        "## Capability Profile\n"
                        "You are operating on a configured host. Use only the tools bound "
                        "for this phase. The verified host connection is the target authority; "
                        "do not assume cluster-specific resource semantics."
                    ),
                },
            )
        )


def get_environment_profile(profile_id: str) -> EnvironmentProfile | None:
    if not EnvironmentProfileRegistry.all():
        EnvironmentProfileRegistry.register_builtins()
    return EnvironmentProfileRegistry.get(profile_id)


__all__ = ["EnvironmentProfileRegistry", "get_environment_profile"]
