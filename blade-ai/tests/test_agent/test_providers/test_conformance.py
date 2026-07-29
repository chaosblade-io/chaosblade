"""Phase 3 conformance suite for every registered FaultProvider.

Where ``test_builtin_providers.py`` locks in the behaviour-equivalent migration
of individual chokepoints, this suite parametrises over ALL built-in providers
and asserts the *structural contract* every backend must honour — so adding a
new execution backend either satisfies the contract or fails here loudly.

Three contract pillars (mirrors plan §六 阶段 3):

1. Interface completeness — the runtime-checkable Protocol plus the stable-id
   invariants (non-empty ``carrier`` / ``injection_methods``; unique carriers).
2. ``injection_method`` unique mapping — every claimed method resolves back to
   exactly its own provider via ``resolve_by_method`` (the LIVE production path,
   used by ``_verifier_layer1``), and no method is claimed by two providers.
3. carrier <-> FaultFamily meshing — every family declares ``carrier_types``
   (an ordered candidate list) whose entries are real provider carriers, and
   ``resolve_by_scope`` returns those candidate providers (in precedence order)
   for every scope the family owns. ``resolve_primary_by_scope`` returns the
   first. See ``docs/design/fault-provider-contract.md`` for the candidate
   semantics (a single scope may be served by several backends, so the bridge
   is intentionally multi-valued).
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.providers import (
    EXECUTE,
    PLAN,
    RECOVER_VERIFY,
    VERIFY,
    FaultProvider,
    FaultProviderRegistry,
    ProviderPrompts,
)
from chaos_agent.agent.providers.chaosblade import ChaosbladeProvider
from chaos_agent.agent.providers.chaosblade_python import ChaosbladePythonProvider
from chaos_agent.agent.providers.host_shell import HostShellProvider
from chaos_agent.agent.providers.k8s_native import K8sNativeProvider
from chaos_agent.agent.spec.fault_registry import (
    aggregate_cluster_scoped,
    all_families,
    family_for_scope,
)

# The built-in backends, in registration/precedence order. A new provider added
# to ``register_builtins`` should be appended here so the whole suite covers it.
BUILTIN_PROVIDERS = (
    ChaosbladeProvider, K8sNativeProvider, HostShellProvider,
    ChaosbladePythonProvider,
)
_ALL_PHASES = (PLAN, EXECUTE, VERIFY, RECOVER_VERIFY)
_KNOWN_PROFILES = ("k8s", "host")


def _provider_id(cls) -> str:
    return cls().carrier


@pytest.fixture(autouse=True)
def _isolate_registry():
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


# ---------------------------------------------------------------------------
# Pillar 1 — interface completeness (per-provider)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_satisfies_protocol(provider_cls):
    assert isinstance(provider_cls(), FaultProvider)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_carrier_is_stable_nonempty_id(provider_cls):
    carrier = provider_cls().carrier
    assert isinstance(carrier, str) and carrier.strip() == carrier and carrier


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_injection_methods_nonempty_tuple_of_strings(provider_cls):
    methods = provider_cls().injection_methods
    assert isinstance(methods, tuple) and methods
    assert all(isinstance(m, str) and m for m in methods)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_matches_channel_is_a_subset_of_known_profiles(provider_cls):
    prov = provider_cls()
    # At least one known profile is served, and nothing outside the known set.
    served = [p for p in _KNOWN_PROFILES if prov.matches_channel(p)]
    assert served
    assert prov.matches_channel("bogus") is False


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_capability_attrs_are_bools(provider_cls):
    prov = provider_cls()
    assert isinstance(prov.has_experiment_uid, bool)
    assert isinstance(prov.is_multi_step, bool)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_tools_returns_a_list_for_every_phase(provider_cls):
    prov = provider_cls()
    for phase in _ALL_PHASES:
        tools = prov.tools(phase)
        assert isinstance(tools, list)
    # An unknown phase contributes nothing (never raises).
    assert prov.tools("no_such_phase") == []


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_prompt_fragments_returns_provider_prompts(provider_cls):
    assert isinstance(provider_cls().prompt_fragments(), ProviderPrompts)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_required_params_always_carries_the_intent_triple(provider_cls):
    prov = provider_cls()
    # Whatever the scope, the (scope, target, action) triple is mandatory.
    for scope in ("pod", "node", "host"):
        assert {"scope", "target", "action"}.issubset(prov.required_params(scope))


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_required_params_gates_namespace_by_cluster_scope(provider_cls):
    prov = provider_cls()
    cluster_scoped = aggregate_cluster_scoped()
    # A namespaced scope requires namespace; a cluster-scoped one never does.
    assert "namespace" in prov.required_params("pod")
    for scope in ("node", "host"):
        if scope in cluster_scoped:
            assert "namespace" not in prov.required_params(scope)


# ---------------------------------------------------------------------------
# Pillar 2 — injection_method unique mapping (registry-wide)
# ---------------------------------------------------------------------------


def test_carriers_are_globally_unique():
    carriers = [cls().carrier for cls in BUILTIN_PROVIDERS]
    assert len(carriers) == len(set(carriers))


def test_every_injection_method_is_claimed_by_exactly_one_provider():
    seen: dict[str, str] = {}
    for cls in BUILTIN_PROVIDERS:
        prov = cls()
        for method in prov.injection_methods:
            assert method not in seen, (
                f"injection_method {method!r} claimed by both "
                f"{seen.get(method)!r} and {prov.carrier!r}"
            )
            seen[method] = prov.carrier


def test_resolve_by_method_round_trips_every_claimed_method():
    FaultProviderRegistry.register_builtins()
    for cls in BUILTIN_PROVIDERS:
        prov = cls()
        for method in prov.injection_methods:
            resolved = FaultProviderRegistry.resolve_by_method(method)
            assert resolved is not None
            assert resolved.carrier == prov.carrier


def test_method_index_covers_exactly_the_union_of_claimed_methods():
    FaultProviderRegistry.register_builtins()
    claimed = {
        m for cls in BUILTIN_PROVIDERS for m in cls().injection_methods
    }
    for method in claimed:
        assert FaultProviderRegistry.resolve_by_method(method) is not None
    # Unknown methods never resolve.
    assert FaultProviderRegistry.resolve_by_method("definitely_not_a_method") is None


# ---------------------------------------------------------------------------
# Pillar 3 — carrier <-> FaultFamily meshing (candidate-based)
# ---------------------------------------------------------------------------


def test_every_family_declares_aligned_carrier_types():
    """Each family's ``carrier_types`` is a non-empty tuple of real provider
    carriers (name alignment invariant that makes the scope bridge resolvable)."""
    families = all_families()
    assert families  # at least the built-in k8s + host families
    builtin_carriers = {cls().carrier for cls in BUILTIN_PROVIDERS}
    for family in families:
        assert isinstance(family.carrier_types, tuple)
        assert family.carrier_types
        for carrier in family.carrier_types:
            assert isinstance(carrier, str) and carrier.strip() == carrier and carrier
            assert carrier in builtin_carriers


def test_resolve_by_scope_returns_registered_candidates_in_precedence_order():
    """The bridge: ``resolve_by_scope`` returns the registered providers for a
    family's ``carrier_types`` in precedence order for every scope it owns, and
    ``resolve_primary_by_scope`` returns the first candidate."""
    FaultProviderRegistry.register_builtins()
    for family in all_families():
        expected = [
            FaultProviderRegistry.get(c)
            for c in family.carrier_types
            if FaultProviderRegistry.get(c) is not None
        ]
        assert expected  # name alignment guarantees at least one built-in
        for scope in family.scopes:
            assert FaultProviderRegistry.resolve_by_scope(scope) == expected
            assert FaultProviderRegistry.resolve_primary_by_scope(scope) is expected[0]


def test_resolve_by_scope_skips_unregistered_carriers():
    """A carrier listed by a family but absent from the registry is skipped,
    not surfaced as ``None`` — proving the candidate filter is registration-aware."""
    # Register only the host_shell backend; the k8s family's carriers
    # (chaosblade / k8s_native) are absent, the host family's chaosblade is
    # absent but host_shell is present.
    FaultProviderRegistry.register(HostShellProvider())
    # host family carrier_types = ("chaosblade", "host_shell") → only host_shell.
    host_candidates = FaultProviderRegistry.resolve_by_scope("host")
    assert [p.carrier for p in host_candidates] == ["host_shell"]
    # k8s family carriers all absent → empty.
    assert FaultProviderRegistry.resolve_by_scope("pod") == []


def test_family_for_scope_owns_every_aggregated_scope():
    """Vocabulary integrity: every scope surfaced to intent has an owning family
    (so ``resolve_by_scope`` at least reaches a family before the carrier hop)."""
    from chaos_agent.agent.spec.fault_registry import aggregate_scopes

    for scope in aggregate_scopes():
        assert family_for_scope(scope) is not None
