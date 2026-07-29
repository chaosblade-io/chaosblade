"""A2 wiring tests — provider prompt fragments + required-params union.

These assert that the two *live* pre-injection consumers of the provider
surface are actually wired:

- ``prompt_fragments()`` → the identity fragment a backend contributes is
  assembled into the shared pre-injection prompts (inject / execute). A backend
  adds language by *implementing the method*, not by editing any shared prompt
  section. (Post-injection verify / recover wording lives in
  ``verify_prompt_note`` / ``recover_layer2_context``, keyed on injection_method,
  and is inserted by the nodes — not via this fragment channel.)
- ``required_params(scope)`` → intent completeness consults the order-preserving
  union across every registered backend, so a backend with an extra mandatory
  parameter surfaces it automatically.

Behaviour-preservation guard: all built-in providers return empty fragments, so
the wiring is transparent — the assembled prompt with only built-ins registered
is byte-identical to the prompt when an all-empty extra provider is added, and
never contains a contributed marker.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.prompts.builders import (
    build_execute_system_prompt,
    build_inject_system_prompt,
    build_verifier_prompt,
)
from chaos_agent.agent.prompts.sections.recovery import (
    build_recover_verifier_system_prompt,
)
from chaos_agent.agent.providers import (
    FaultProviderRegistry,
    ProviderPrompts,
    RecoverResult,
)
from chaos_agent.agent.result.verdict import Layer1Result, Layer1Status
from chaos_agent.agent.spec.fault_registry import required_intent_params

_IDENTITY_MARKER = "<<IDENTITY-FRAGMENT-MARKER>>"
_VERIFY_MARKER = "<<VERIFY-FRAGMENT-MARKER>>"
_RECOVER_MARKER = "<<RECOVER-FRAGMENT-MARKER>>"


class _FragmentProvider:
    """Fake backend that contributes non-empty prompt fragments + an extra
    required param, so the wiring is observable."""

    carrier = "fragment_fake"
    injection_methods = ("fragment_method",)
    has_experiment_uid = False
    is_multi_step = False
    inject_tool_names: frozenset[str] = frozenset()
    inject_kubectl_subcommands: frozenset[str] = frozenset()
    supported_targets: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = ()
    injection_binaries: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        profiles: tuple[str, ...] = ("k8s", "host"),
        prompts: ProviderPrompts | None = None,
        extra_params: tuple[str, ...] = (),
    ) -> None:
        self._profiles = profiles
        self._prompts = prompts or ProviderPrompts(identity=_IDENTITY_MARKER)
        self._extra_params = extra_params

    def matches_channel(self, profile: str) -> bool:
        return profile in self._profiles

    def required_params(self, scope: str) -> list[str]:
        return [*required_intent_params(scope), *self._extra_params]

    def tools(self, phase):
        return []

    def detect(self, messages, blade_uid, *, is_host):
        return None

    async def layer1_verify(self, state, **kwargs) -> Layer1Result:
        return Layer1Result(status=Layer1Status.SKIPPED, details="fake")

    def verify_prompt_note(self, injection_method, *, injection_pod_name=None) -> str:
        return ""

    def recover_layer2_context(
        self, state, layer1, *, is_deterministic, blade_uid, is_host_scope
    ) -> tuple[str, str]:
        return "", ""

    async def recover(self, state, handle, **kwargs) -> RecoverResult:
        return RecoverResult(status="skipped")

    def prompt_fragments(self) -> ProviderPrompts:
        return self._prompts


@pytest.fixture(autouse=True)
def _isolate_registry():
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


# ---------------------------------------------------------------------------
# prompt_fragments wiring — contributed fragments appear in the shared prompts
# ---------------------------------------------------------------------------


def test_identity_fragment_reaches_inject_and_execute_prompts():
    FaultProviderRegistry.register_builtins()
    FaultProviderRegistry.register(_FragmentProvider())

    inject = build_inject_system_prompt(skill_catalog="- s: skill", profile="k8s")
    execute = build_execute_system_prompt(skill_catalog="- s: skill", profile="k8s")
    assert _IDENTITY_MARKER in inject
    assert _IDENTITY_MARKER in execute


def test_fragment_gated_by_channel_profile():
    """A provider that does not match the requested profile contributes nothing."""
    FaultProviderRegistry.register_builtins()
    FaultProviderRegistry.register(_FragmentProvider(profiles=("host",)))

    # Requesting the k8s profile → the host-only provider is filtered out.
    assert _IDENTITY_MARKER not in build_inject_system_prompt(
        skill_catalog="- s: skill", profile="k8s"
    )
    # Requesting the host profile → it contributes.
    assert _IDENTITY_MARKER in build_inject_system_prompt(
        skill_catalog="- s: skill", profile="host"
    )


# ---------------------------------------------------------------------------
# Behaviour-preservation guard — empty fragments change nothing byte-for-byte
# ---------------------------------------------------------------------------


def test_builtin_fragments_are_transparent_byte_for_byte():
    """Built-ins return empty fragments, so the assembled prompt is identical
    whether or not an all-empty extra provider participates, and never carries a
    contributed marker."""
    FaultProviderRegistry.register_builtins()
    baseline_inject = build_inject_system_prompt(
        skill_catalog="- s: skill", profile="k8s"
    )
    baseline_verify = build_verifier_prompt(profile="k8s")
    baseline_recover = build_recover_verifier_system_prompt(profile="k8s")

    # Add a provider whose fragments are all empty → must add nothing.
    FaultProviderRegistry.register(
        _FragmentProvider(prompts=ProviderPrompts())
    )
    assert build_inject_system_prompt(
        skill_catalog="- s: skill", profile="k8s"
    ) == baseline_inject
    assert build_verifier_prompt(profile="k8s") == baseline_verify
    assert build_recover_verifier_system_prompt(profile="k8s") == baseline_recover

    for prompt in (baseline_inject, baseline_verify, baseline_recover):
        assert _IDENTITY_MARKER not in prompt
        assert _VERIFY_MARKER not in prompt
        assert _RECOVER_MARKER not in prompt


# ---------------------------------------------------------------------------
# union_required_params — order-preserving union across backends
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("scope", ["pod", "node", "host"])
def test_union_equals_single_list_for_builtins(scope):
    """All built-ins delegate to ``required_intent_params``, so the union equals
    that single list (order-preserving dedupe collapses the duplicates)."""
    FaultProviderRegistry.register_builtins()
    assert (
        FaultProviderRegistry.union_required_params(scope)
        == required_intent_params(scope)
    )


def test_union_surfaces_extra_param_from_a_backend():
    """A backend requiring an extra param (e.g. a cloud backend needing
    ``region``) surfaces it in the union — automatically flagged by intent
    completeness with no shared-code edit."""
    FaultProviderRegistry.register_builtins()
    FaultProviderRegistry.register(_FragmentProvider(extra_params=("region",)))

    union = FaultProviderRegistry.union_required_params("pod")
    assert "region" in union
    # Order-preserving: the shared triple still leads, the extra is appended.
    assert union[: len(required_intent_params("pod"))] == required_intent_params("pod")


def test_union_self_bootstraps_builtins_on_empty_registry():
    """Mirrors ``detect_method``: consulting the union on an empty registry
    registers the built-ins rather than returning an empty list."""
    FaultProviderRegistry.clear()
    assert (
        FaultProviderRegistry.union_required_params("pod")
        == required_intent_params("pod")
    )
