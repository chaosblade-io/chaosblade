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
# ChaosbladeProvider / HostShellProvider are imported for the two POINT-NAMED
# tests (the minimal-registry resolution test / the blocks_deterministic_
# destroy contrast) — NOT for the parametrization domain: that is derived
# from the registry now (see BUILTIN_PROVIDERS below).
from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
from chaos_agent.agent.providers.host_shell.provider import HostShellProvider
from chaos_agent.config.settings import settings
from chaos_agent.agent.spec.fault_registry import (
    aggregate_cluster_scoped,
    all_families,
    family_for_scope,
)

# The built-in backends, in registration/precedence order — DERIVED, not
# hand-enumerated (P-8R). The domain is snapshotted from the live registry at
# collection time: importing ``chaos_agent.agent.providers`` (above) itself
# runs the package's automatic ``register_builtins()`` bootstrap, and no
# conftest in the chain registers anything at import time, so the snapshot is
# exactly the pure builtin set; the autouse ``_isolate_registry`` fixture's
# clear / re-register churn happens at RUN time, long after parametrization
# was frozen. A provider added to ``register_builtins`` is therefore covered
# by every parametrized tooth below with zero manual edits — the retired
# hand-enumerated tuple + "append here" comment instead silently skipped the
# whole suite for a forgotten provider (G-3, same lesson that drove F-14's
# tree derivation).
BUILTIN_PROVIDERS = tuple(
    type(p) for p in FaultProviderRegistry.all_providers()
)
_ALL_PHASES = (PLAN, EXECUTE, VERIFY, RECOVER_VERIFY)
_KNOWN_PROFILES = ("k8s", "host")


def _provider_id(cls) -> str:
    return cls().carrier


@pytest.fixture(autouse=True)
def _isolate_registry():
    # Explicit ON (the openspec faultdrill-cr-channel task-1.5 pinned
    # intent, belatedly wired here): protocol conformance covers the CR
    # channel too, and BUILTIN_PROVIDERS — snapshotted at collection
    # time under the post-flip default — matches the runtime registration
    # regardless of what an earlier test file left the flag at.
    _orig = settings.faultdrill_enabled
    settings.faultdrill_enabled = True
    FaultProviderRegistry.clear()
    try:
        yield
    finally:
        settings.faultdrill_enabled = _orig
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()


def test_parametrization_domain_is_alive():
    """Liveness anchor for the DERIVED parametrization domain (P-8R).

    ``BUILTIN_PROVIDERS`` is snapshotted from the registry, so a broken
    derivation (empty domain) fails loudly NOWHERE on its own — every
    parametrized tooth in this suite would silently collect zero cases and
    pass vacuously green. This is the one NON-parametrized tooth that reads
    the domain: an empty or collapsed snapshot goes red here with a message
    that names the failure mode. Anchor names are compared as STRINGS — no
    concrete provider import for the domain, that duplication is exactly
    what the derivation removed.
    """
    names = [cls.__name__ for cls in BUILTIN_PROVIDERS]
    assert names, (
        "BUILTIN_PROVIDERS is EMPTY — the registry snapshot derivation is "
        "broken and every parametrized tooth in this suite is silently "
        "passing vacuously green"
    )
    assert "K8sNativeProvider" in names, names  # derivation is real, not stub
    # Today's builtin set: blade / k8s_native / host_shell / blade_python.
    assert len(names) >= 4, names


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
def test_six_scan_hooks_accept_is_teardown_matcher(provider_cls):
    """P3 协议统一性牙（R17/G-2）：registry 的 detect_method 分发
    （detect / injection_recency）与 agent seams（issue_disproven /
    scan_step_actions / was_injection_attempted /
    was_fault_create_attempted）都无条件传 ``is_teardown`` kwarg。
    覆写钩子漏收参数 ⇒ 生产路径 runtime TypeError（刀3 实证：
    K8sNativeProvider.was_fault_create_attempted 曾漏收，仅因 recover
    测试恰好路过才当场抓到；其余四钩子按归因分发，新 provider 无
    专属测试时签名漂移静默进生产）。本牙把炸点挪到测试期：六钩子
    各以 matcher 实参空调用（全部纯消息扫描、无副作用），签名
    漂移即红——兑现本套件「新增 backend 要么满足契约要么在此响亮
    失败」的章程。"""
    from chaos_agent.agent.execution_artifacts import make_teardown_matcher

    prov = provider_cls()
    matcher = make_teardown_matcher([])
    prov.detect([], is_host=False, is_teardown=matcher)
    prov.injection_recency([], is_host=False, is_teardown=matcher)
    prov.issue_disproven([], is_teardown=matcher)
    prov.scan_step_actions([], [], is_teardown=matcher)
    prov.was_injection_attempted([], is_teardown=matcher)
    prov.was_fault_create_attempted(
        [], injection_method=None, is_teardown=matcher
    )


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
    carriers (name alignment invariant that makes the scope bridge resolvable).

    "Real" accepts BOTH registration states: a carrier in the builtin
    registry snapshot, OR a carrier with a REGISTERED DECLARATION whose
    provider registration is flag-gated (dark launch —
    ``faultdrill_cr`` while ``faultdrill_enabled`` is off: declaration
    registered at the assembly point, provider structurally absent).
    ``resolve_by_scope`` skips absent carriers by design (its own test
    below), so the bridge stays resolvable; the declaration registry
    keeps the typo protection (an entry in NEITHER set is still a
    hard failure)."""
    from chaos_agent.agent.spec import fault_registry

    families = all_families()
    assert families  # at least the built-in k8s + host families
    builtin_carriers = {cls().carrier for cls in BUILTIN_PROVIDERS}
    declared_carriers = set(fault_registry._CARRIER_VOCAB)
    for family in families:
        assert isinstance(family.carrier_types, tuple)
        assert family.carrier_types
        for carrier in family.carrier_types:
            assert isinstance(carrier, str) and carrier.strip() == carrier and carrier
            assert carrier in builtin_carriers or carrier in declared_carriers


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


# ---------------------------------------------------------------------------
# Pillar 4 — recover handle closed loop (phase-3: build → materialize →
# resolve → dispatch, per carrier)
# ---------------------------------------------------------------------------

# Pre-handle attribution facts per carrier (the checkpoint shape each
# backend must still hydrate), with the delivery variants for carriers that
# inject through more than one channel (chaosblade: in-cluster exec vs host
# binary — one per channel profile).
_LEGACY_FACTS = {
    "chaosblade": (
        {"experiment_uid": "uid-cb-k8s", "injection_method": "kubectl_exec"},
        {"experiment_uid": "uid-cb-host", "injection_method": "host_blade"},
    ),
    "k8s_native": ({"injection_method": "kubectl_native"},),
    "host_shell": ({"injection_method": "host_native"},),
    "chaosblade_python": (
        {"experiment_uid": "uid-py", "injection_method": "python_agent"},
    ),
    # The CR channel's values-stage handle is kind-bearing and minimal
    # (two-stage hydration: the ns/name ``value`` is the MESSAGES stage's
    # job, fed by the applied manifest) — exactly the checkpoint shape
    # ``build_fault_handle`` must hydrate from bare attribution facts.
    "faultdrill_cr": ({"injection_method": "faultdrill_cr"},),
}


def _legacy_facts(provider_cls):
    return _LEGACY_FACTS[provider_cls().carrier]


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_build_fault_handle_matches_declared_kind(provider_cls):
    prov = provider_cls()
    for facts in _legacy_facts(provider_cls):
        handle = prov.build_fault_handle(facts)
        assert handle and handle["kind"] == prov.handle_kind


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_handle_roundtrip_through_materialize_resolve_and_dispatch(provider_cls):
    """The phase-3 closed loop: legacy facts → ``build_fault_handle`` →
    (durable | hydrated) state → ``materialize_fault_handle`` →
    ``resolve_by_handle_kind`` lands back on the owning backend; the recover
    dispatch's identity handle carries exactly what the dispatched backend
    consumes (the experiment UID for UID carriers, the attribution handle
    for UID-less ones)."""
    from chaos_agent.agent.state import materialize_fault_handle

    FaultProviderRegistry.register_builtins()
    prov = provider_cls()
    for facts in _legacy_facts(provider_cls):
        # Work on a copy — the shared module-level fixture tuples must stay
        # pristine for the other parametrised runs (phase-14 G4 retired the
        # in-place hydrate normalisation; build_fault_handle is read-only).
        facts = dict(facts)
        expected_uid = facts.get("experiment_uid") or ""
        built = prov.build_fault_handle(facts)
        # Durable handle wins verbatim; legacy-only state hydrates back to
        # the same ownership.
        assert materialize_fault_handle({"fault_handle": built}) == built
        assert materialize_fault_handle(facts) == built
        # Kind-based resolution lands on the owning backend.
        resolved = FaultProviderRegistry.resolve_by_handle_kind(built)
        assert resolved is not None and resolved.carrier == prov.carrier
        # Recover dispatch consumes the same identity.
        dispatched, identity = FaultProviderRegistry.resolve_fault_dispatch(facts)
        if prov.has_experiment_uid:
            # Registration-order experiment claim: the UID routes to an
            # experiment carrier whose destroy domain owns it (the legacy
            # OS-carrier contract) — the identity IS the experiment handle
            # (kind renamed to "experiment_uid" in phase-14 G7).
            assert identity and identity.get("kind") == "experiment_uid"
            assert identity.get("value") == expected_uid
            assert dispatched.has_experiment_uid
        else:
            assert identity == built
            assert dispatched.carrier == prov.carrier


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_recover_hook_family_contract(provider_cls):
    """Structural contract of the recover hook family: the deterministic
    capability flag agrees with the handle kind (UID carriers own a
    programmatic destroy; the CR channel owns a deterministic handle
    replay — recover reads the CR and re-applies restorePatches, the
    intent durable in cluster state), and every hook returns its declared
    shape on an empty state (never raising, never naming a carrier)."""
    prov = provider_cls()
    assert isinstance(prov.has_deterministic_recover, bool)
    assert prov.has_deterministic_recover == (
        prov.handle_kind in ("experiment_uid", "faultdrill_cr")
    )
    assert isinstance(prov.blocks_deterministic_destroy({}), bool)
    assert prov.blocks_deterministic_destroy({}) is False
    # Bare retry destroy: declared for every backend, empty for carriers with
    # no programmatic destroy (checked async by the callers only).
    import inspect as _inspect

    assert callable(prov.layer1_raw_destroy)
    assert _inspect.iscoroutinefunction(prov.layer1_raw_destroy)
    assert isinstance(prov.recovery_vehicle({}), str)
    assert prov.recovery_vehicle({}) == ""
    assert isinstance(prov.recovery_facts_render({}, spec_params={}), str)
    assert prov.recovery_facts_render({}, spec_params={}) == ""
    layer1_sentinel = object()
    assert (
        prov.merge_deterministic_recover_verdict(layer1_sentinel, {})
        is layer1_sentinel
    )
    assert isinstance(prov.layer1_recover_guidance({}, ""), str)
    assert prov.layer1_recover_guidance({}, "") == ""
    assert isinstance(prov.layer2_facts_note({}), str)
    assert prov.layer2_facts_note({}) == ""


def test_blocks_deterministic_destroy_marks_incluster_delivery_only():
    """The in-cluster exec delivery is the ONLY state that blocks the
    deterministic destroy; the host-binary delivery always runs it."""
    cb = ChaosbladeProvider()
    assert cb.blocks_deterministic_destroy({"injection_method": "kubectl_exec"}) is True
    assert cb.blocks_deterministic_destroy({"injection_method": "host_blade"}) is False


# ---------------------------------------------------------------------------
# Pillar 5 — verify hook family (phase-4: layer1_verify + the attempted
# judgement, per carrier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider_cls",
    BUILTIN_PROVIDERS,
    ids=_provider_id,
)
def test_verify_hook_family_contract(provider_cls):
    """Structural contract of the verify hook family (phase-4 spec,
    verify-chain-provider-protocol R5): the deterministic Layer-1
    verification hook is a coroutine function every backend owns, and the
    attempted-but-no-UID judgement hook exists as a SYNC callable (it runs
    inline inside the verify/recover guards)."""
    import inspect as _inspect

    prov = provider_cls()
    assert isinstance(prov, FaultProvider)
    assert callable(prov.layer1_verify)
    assert _inspect.iscoroutinefunction(prov.layer1_verify)
    assert callable(prov.was_fault_create_attempted)
    assert not _inspect.iscoroutinefunction(prov.was_fault_create_attempted)


def test_fake_provider_satisfies_verify_hook_family():
    """The suite's test double satisfies the same verify hook contract (the
    runtime_checkable Protocol already asserts the surface; this pins the
    coroutine form the verify chain's await relies on)."""
    import inspect as _inspect

    from .test_registry import _FakeProvider

    fake = _FakeProvider("chaosblade", ("host_blade",))
    assert isinstance(fake, FaultProvider)
    assert callable(fake.layer1_verify)
    assert _inspect.iscoroutinefunction(fake.layer1_verify)
    assert callable(fake.was_fault_create_attempted)
    assert not _inspect.iscoroutinefunction(fake.was_fault_create_attempted)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_reconcile_hook_family_contract(provider_cls):
    """Structural contract of the create-reconcile hook family
    (blade-create-reconcile-before-retry D6): the hold-feedback hook is a
    coroutine function every backend owns (the registry seam AWAITS it —
    a sync implementation would raise on the intercept path), the
    fingerprint/batch-held hooks are sync callables, and the declaration
    pair is always a frozenset of tool names (possibly empty: backends
    outside the gate pin it empty, which is the explicit default)."""
    import inspect as _inspect

    prov = provider_cls()
    assert callable(prov.build_reconcile_fingerprint)
    assert not _inspect.iscoroutinefunction(prov.build_reconcile_fingerprint)
    assert callable(prov.reconcile_hold_feedback)
    assert _inspect.iscoroutinefunction(prov.reconcile_hold_feedback)
    assert callable(prov.reconcile_batch_held_feedback)
    assert not _inspect.iscoroutinefunction(prov.reconcile_batch_held_feedback)
    for attr in ("reconcile_create_tool_names", "reconcile_read_tool_names"):
        decl = getattr(prov, attr)
        assert isinstance(decl, frozenset)
        assert all(isinstance(n, str) and n for n in decl)


def test_fake_provider_satisfies_reconcile_hook_family():
    """The suite's test double satisfies the same reconcile hook contract
    (the coroutine form the registry seam's await relies on)."""
    import inspect as _inspect

    from .test_registry import _FakeProvider

    fake = _FakeProvider("chaosblade", ("host_blade",))
    assert isinstance(fake, FaultProvider)
    assert _inspect.iscoroutinefunction(fake.reconcile_hold_feedback)
    assert not _inspect.iscoroutinefunction(fake.build_reconcile_fingerprint)
    assert not _inspect.iscoroutinefunction(fake.reconcile_batch_held_feedback)


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_result_shape_hook_family_contract(provider_cls):
    """Structural contract of the result-shape verdict family
    (``agent/tool_verdicts.py``): the declaration is a frozenset of tool
    names (possibly empty — a text-dialect carrier is read by the generic
    verdict and pins it empty), and the hook is a SYNC callable that
    ABSTAINS (returns ``None``) on a shape it does not own.

    The abstain-on-unowned pin is the load-bearing half. The registry routes
    by declaration membership, so a hook that answered on a tool it does not
    declare would be unreachable — but a hook that answered on its OWN tool
    with a verdict for an unreadable body would turn "I don't recognise this"
    into a claim, and this family's whole reason to exist is that such a
    claim was once inverted into a success verdict (a compacted receipt must
    abstain, not assert)."""
    import inspect as _inspect

    prov = provider_cls()
    decl = getattr(prov, "result_shape_tool_names")
    assert isinstance(decl, frozenset)
    assert all(isinstance(n, str) and n for n in decl)
    assert callable(prov.tool_result_error_text)
    assert not _inspect.iscoroutinefunction(prov.tool_result_error_text)

    # Unowned tool: abstain regardless of content.
    assert prov.tool_result_error_text("__not_this_carriers_tool__", "") is None
    assert (
        prov.tool_result_error_text(
            "__not_this_carriers_tool__", '{"status": "failed"}'
        )
        is None
    )
    # Owned tool, unreadable body (a compacted receipt is JSON-shaped but
    # truncated by bytes): abstain — unknown is not success AND not failure.
    for tool_name in decl:
        assert prov.tool_result_error_text(tool_name, "") is None
        assert (
            prov.tool_result_error_text(tool_name, '{"status": "failed", "er')
            is None
        )


def test_fake_provider_satisfies_result_shape_hook_family():
    """The suite's test double satisfies the same result-shape contract (the
    runtime_checkable Protocol asserts the surface; this pins the abstain)."""
    import inspect as _inspect

    from .test_registry import _FakeProvider

    fake = _FakeProvider("chaosblade", ("host_blade",))
    assert isinstance(fake, FaultProvider)
    assert fake.result_shape_tool_names == frozenset()
    assert not _inspect.iscoroutinefunction(fake.tool_result_error_text)
    assert fake.tool_result_error_text("host_blade", '{"status": "failed"}') is None


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_declared_result_shape_tools_are_the_carriers_own(provider_cls):
    """A declared result-shape tool must be a tool the carrier actually
    contributes — a stale name would silently route nobody's verdicts here
    while the real tool stayed invisible to the framework (the failure mode
    this seam exists to prevent, reappearing one level up)."""
    prov = provider_cls()
    declared = set(getattr(prov, "result_shape_tool_names") or ())
    if not declared:
        return
    own: set[str] = set()
    for phase in (PLAN, EXECUTE, VERIFY, RECOVER_VERIFY):
        for tool in prov.tools(phase) or ():
            own.add(getattr(tool, "name", "") or "")
    unknown = declared - own
    assert not unknown, (
        f"{prov.carrier} declares result shapes for tools it does not "
        f"contribute: {sorted(unknown)}"
    )


@pytest.mark.parametrize("provider_cls", BUILTIN_PROVIDERS, ids=_provider_id)
def test_uidless_carriers_pin_attempted_false_semantics(provider_cls):
    """Semantic pin (spec R3): UID-less carriers (k8s_native / host_shell)
    MUST keep ``was_fault_create_attempted`` at False — the attempted-but-
    no-UID judgement belongs to the experiment-recording carrier; a True
    from a UID-less carrier would wrongly fire verify's warning branch and
    recover's failed terminal branch. The explicit mirror return (not the
    protocol default) IS the pin — even attempted-looking blade evidence in
    the history must not flip it."""
    from langchain_core.messages import ToolMessage

    prov = provider_cls()
    if prov.has_experiment_uid:
        pytest.skip("pin applies to UID-less carriers only")
    attempted_looking = [
        ToolMessage(
            content='{"code": 500, "success": false, "error": "boom"}',
            name="blade_create",
            tool_call_id="tc-att",
        ),
    ]
    assert prov.was_fault_create_attempted([], None) is False
    assert prov.was_fault_create_attempted(attempted_looking, None) is False
    assert prov.was_fault_create_attempted(attempted_looking, "kubectl_native") is False
