"""Phase 0 skeleton tests for the FaultProvider registry.

These exercise the registry mechanics only (register / resolve / applicable /
scope-bridge) with lightweight fake providers — no built-in providers are wired
yet, so there is no behaviour to preserve here. Phase 1 adds the concrete
ChaosBlade / K8sNative providers and a conformance suite over them.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.providers import (
    FaultProvider,
    FaultProviderRegistry,
    ProviderPrompts,
    RecoverResult,
)
from chaos_agent.agent.result.verdict import Layer1Result, Layer1Status


class _FakeProvider:
    """Minimal FaultProvider implementation for registry tests."""

    has_experiment_uid = False
    is_multi_step = False
    inject_tool_names: frozenset[str] = frozenset()
    inject_kubectl_subcommands: frozenset[str] = frozenset()
    supported_targets: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = ()
    injection_binaries: frozenset[str] = frozenset()

    def __init__(
        self,
        carrier: str,
        methods: tuple[str, ...],
        *,
        profiles: tuple[str, ...] = ("k8s",),
    ) -> None:
        self.carrier = carrier
        self.injection_methods = methods
        self._profiles = profiles

    def matches_channel(self, profile: str) -> bool:
        return profile in self._profiles

    def required_params(self, scope: str) -> list[str]:
        return ["scope", "target", "action"]

    def tools(self, phase):
        return []

    def detect(self, messages, blade_uid, *, is_host):
        return self.injection_methods[0] if blade_uid else None

    def injection_recency(self, messages, blade_uid, *, is_host):
        return 0 if blade_uid else -1

    async def layer1_verify(self, state, **kwargs) -> Layer1Result:
        return Layer1Result(status=Layer1Status.SKIPPED, details=f"fake:{self.carrier}")

    def verify_prompt_note(self, injection_method, *, injection_pod_name=None) -> str:
        return ""

    def recover_layer2_context(
        self, state, layer1, *, is_deterministic, blade_uid, is_host_scope
    ) -> tuple[str, str]:
        return "", ""

    async def recover(self, state, handle) -> RecoverResult:
        return RecoverResult(status="skipped", details=f"fake:{self.carrier}")

    def prompt_fragments(self) -> ProviderPrompts:
        return ProviderPrompts()


@pytest.fixture(autouse=True)
def _isolate_registry():
    """Each test starts with an empty registry; teardown restores the built-in
    set so the process-default (established at ``providers`` import) is left in
    place for later test files that rely on a populated registry."""
    FaultProviderRegistry.clear()
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


def test_fake_provider_satisfies_protocol():
    # runtime_checkable Protocol — structural conformance check.
    assert isinstance(_FakeProvider("chaosblade", ("host_blade",)), FaultProvider)


def test_register_and_all_providers_preserve_order():
    a = _FakeProvider("chaosblade", ("host_blade", "kubectl_exec"))
    b = _FakeProvider("k8s_native", ("kubectl_native",))
    FaultProviderRegistry.register(a)
    FaultProviderRegistry.register(b)
    assert FaultProviderRegistry.all_providers() == (a, b)


def test_resolve_by_method_maps_each_claimed_method():
    a = _FakeProvider("chaosblade", ("host_blade", "kubectl_exec"))
    b = _FakeProvider("k8s_native", ("kubectl_native",))
    FaultProviderRegistry.register(a)
    FaultProviderRegistry.register(b)

    assert FaultProviderRegistry.resolve_by_method("host_blade") is a
    assert FaultProviderRegistry.resolve_by_method("kubectl_exec") is a
    assert FaultProviderRegistry.resolve_by_method("kubectl_native") is b


def test_resolve_by_method_unknown_or_none_returns_none():
    FaultProviderRegistry.register(_FakeProvider("chaosblade", ("host_blade",)))
    assert FaultProviderRegistry.resolve_by_method("does_not_exist") is None
    assert FaultProviderRegistry.resolve_by_method(None) is None
    assert FaultProviderRegistry.resolve_by_method("") is None


def test_register_overwrites_same_carrier_and_reindexes():
    old = _FakeProvider("chaosblade", ("host_blade",))
    new = _FakeProvider("chaosblade", ("kubectl_exec",))
    FaultProviderRegistry.register(old)
    FaultProviderRegistry.register(new)
    # Only one provider under the carrier; the index reflects the new methods.
    assert FaultProviderRegistry.all_providers() == (new,)
    assert FaultProviderRegistry.resolve_by_method("kubectl_exec") is new
    assert FaultProviderRegistry.resolve_by_method("host_blade") is None


def test_duplicate_method_last_registration_wins(caplog):
    a = _FakeProvider("chaosblade", ("shared_method",))
    b = _FakeProvider("k8s_native", ("shared_method",))
    FaultProviderRegistry.register(a)
    FaultProviderRegistry.register(b)
    # b registered last → wins the ambiguous method.
    assert FaultProviderRegistry.resolve_by_method("shared_method") is b


def test_applicable_filters_by_channel_profile():
    cb = _FakeProvider("chaosblade", ("host_blade",), profiles=("k8s", "host"))
    kn = _FakeProvider("k8s_native", ("kubectl_native",), profiles=("k8s",))
    host = _FakeProvider("host_shell", ("host_native",), profiles=("host",))
    for p in (cb, kn, host):
        FaultProviderRegistry.register(p)

    assert FaultProviderRegistry.applicable("k8s") == [cb, kn]
    assert FaultProviderRegistry.applicable("host") == [cb, host]


def test_resolve_by_scope_bridges_via_fault_family():
    # The built-in k8s_chaosblade family declares carrier_types starting with
    # "chaosblade" and owns scope "pod"; register a provider under that carrier
    # and confirm the scope→family→carrier→provider bridge resolves it as a
    # candidate (and as the primary).
    prov = _FakeProvider("chaosblade", ("host_blade",))
    FaultProviderRegistry.register(prov)
    assert prov in FaultProviderRegistry.resolve_by_scope("pod")
    assert FaultProviderRegistry.resolve_primary_by_scope("pod") is prov


def test_resolve_by_scope_unknown_scope_returns_empty():
    FaultProviderRegistry.register(_FakeProvider("chaosblade", ("host_blade",)))
    assert FaultProviderRegistry.resolve_by_scope("no_such_scope") == []
    assert FaultProviderRegistry.resolve_by_scope(None) == []
    assert FaultProviderRegistry.resolve_primary_by_scope("no_such_scope") is None
    assert FaultProviderRegistry.resolve_primary_by_scope(None) is None


def test_clear_empties_registry():
    FaultProviderRegistry.register(_FakeProvider("chaosblade", ("host_blade",)))
    FaultProviderRegistry.clear()
    assert FaultProviderRegistry.all_providers() == ()
    assert FaultProviderRegistry.resolve_by_method("host_blade") is None
