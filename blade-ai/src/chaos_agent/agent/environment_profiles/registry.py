"""Registry of environment profiles.

The registry is intentionally small today.  New environments register their
target authority and phase wording here instead of adding ``if profile ==``
branches to shared prompts.
"""

from __future__ import annotations

from chaos_agent.agent.environment_profiles.base import EnvironmentProfile
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S

# The statement body is shared; only the heading differs by phase. ``default``
# keeps ``## Capability Profile`` where it sits mid-prompt among peers, while the
# intent fragment leads the prompt and takes an H1 named for what it is. Other
# phases (recover, verify, baseline) are untouched — they read ``default``.
_K8S_BODY = (
    "You are operating in a Kubernetes environment. Use only the "
    "tools bound for this phase. Treat Kubernetes API observations "
    "as the authority for namespace, resource name and label identity."
)
_HOST_BODY = (
    "You are operating on a configured host. Use only the tools bound "
    "for this phase. The verified host connection is the target authority; "
    "do not assume cluster-specific resource semantics."
)
_DEFAULT_HEAD = "## Capability Profile\n"
_INTENT_HEAD = "# Bound Environment\n"

# Shared tail of the ``intent`` fragments.
#
# The intent phase needs more than the bare statement that ``default`` carries.
# A/B runs against a real failing session (10 samples per variant) showed the
# statement alone was read as background colour: the model went on to offer the
# user a menu of Kubernetes / host / python-agent faults on a Kubernetes-only
# channel (2-5 of 10 samples), then submitted a host fault that the gate refused.
#
# What removed that, without a single imperative, was stating the consequence and
# then the useful next move:
#
#   offtopic families offered   states the bound environment
#   A  statement only, late position   2-5/10          0-3/10
#   +  consequence only                 0/10           1/10   but 7/10 replies
#                                                              collapsed to an
#                                                              empty proposal
#   +  consequence + next move          0/10           8/10   1/10 empty
#
# The middle row is why the "useful opening move" sentence is here: told only
# that the environment is settled, the model had nothing to do and returned an
# empty ``<blade-fault-proposal>``. Prohibitions ("do not ask the user…") scored
# no better than this wording and contradict the project rule that prompts inform
# while the submit gate enforces.
_INTENT_TAIL = (
    "\n\nThe runtime configuration already fixed this environment for the "
    "session, so it is context you have rather than a parameter to collect. It "
    "also bounds what is injectable: a fault family requiring a different "
    "environment is refused when the intent is submitted, so naming one costs "
    "the user a wasted round."
    "\n\nGiven that, the useful opening move is to present the fault types this "
    "environment supports and continue clarifying the target and parameters. "
    "When a request needs a different environment, say so plainly and offer the "
    "closest option this environment does support."
)


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
                    "default": _DEFAULT_HEAD + _K8S_BODY,
                    "intent": _INTENT_HEAD + _K8S_BODY + _INTENT_TAIL,
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
                    "default": _DEFAULT_HEAD + _HOST_BODY,
                    "intent": _INTENT_HEAD + _HOST_BODY + _INTENT_TAIL,
                },
            )
        )


def get_environment_profile(profile_id: str) -> EnvironmentProfile | None:
    if not EnvironmentProfileRegistry.all():
        EnvironmentProfileRegistry.register_builtins()
    return EnvironmentProfileRegistry.get(profile_id)


__all__ = ["EnvironmentProfileRegistry", "get_environment_profile"]
