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
from chaos_agent.agent.providers.base import DestroyOutcome
from chaos_agent.agent.result.verdict import Layer1Result, Layer1Status


class _FakeProvider:
    """Minimal FaultProvider implementation for registry tests."""

    has_experiment_uid = False
    # Phase-7 T1: required for runtime_checkable protocol conformance (the
    # registry's claim-4 fallback reads it via getattr with a False default).
    uid_less_verdict_default = False
    handle_kind = ""
    is_multi_step = False
    has_deterministic_recover = False
    inject_tool_names: frozenset[str] = frozenset()
    inject_kubectl_subcommands: frozenset[str] = frozenset()
    supported_targets: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = ()
    injection_binaries: frozenset[str] = frozenset()
    # Phase-7 T2: the three per-tool pass sets — runtime_checkable protocol
    # conformance requires the attributes to exist (the registry's
    # union_tool_names reads them via getattr with an empty default).
    kubeconfig_scoped_tool_names: frozenset[str] = frozenset()
    audit_scoped_tool_names: frozenset[str] = frozenset()
    log_shipping_tool_names: frozenset[str] = frozenset()
    # Phase-8 Form A: same conformance requirement for the Tier-1
    # tool-pod-namespace exemption set.
    tool_pod_namespaces: frozenset[str] = frozenset()
    # Create-reconcile gate (D6): same conformance requirement for the
    # gate's declaration pair — the fake stays outside the gate.
    reconcile_create_tool_names: frozenset[str] = frozenset()
    reconcile_read_tool_names: frozenset[str] = frozenset()

    # Phase-8 Form B: same conformance requirement for the optional
    # vocabulary hooks — the fake stays inert (a non-participating
    # backend; the generic layer getattr-skips a missing hook).
    def scan_step_actions(self, steps, messages):
        return None

    def was_injection_attempted(self, messages):
        return False

    def build_reconcile_fingerprint(self, tool_name, tool_args):
        # Create-reconcile seam (D6): default None pinned structurally
        # (runtime_checkable conformance requires the method).
        return None

    async def reconcile_hold_feedback(
        self,
        tool_name,
        fp,
        hold_count,
        block_limit,
        kubeconfig="",
        task_id="",
    ):
        # Create-reconcile seam (D6): default None pinned structurally.
        return None

    def reconcile_batch_held_feedback(self, tool_name, other_tool_name):
        # Create-reconcile seam (D6): default None pinned structurally.
        return None

    async def verify_landing_readback(self, messages, state, *, kubeconfig=""):
        # Landing readback seam (faultdrill-cr-channel task 2.1): default
        # None pinned structurally (runtime_checkable conformance requires
        # the coroutine method).
        return None

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

    def detect(self, messages, *, is_host):
        return self.injection_methods[0] if messages else None

    def injection_recency(self, messages, *, is_host):
        return 0 if messages else -1

    def build_fault_handle(self, values):
        return None

    def build_handle_from_messages(self, messages, retired=None, values=None):
        return None

    def extract_experiment_id(self, messages, retired=None):
        return ""

    def created_experiment_ids(self, messages, state):
        return set()

    # Destroy three-table unification: the destroyed-* hooks pinned
    # structurally (runtime_checkable conformance requires the methods) —
    # the fake stays inert (a non-participating backend; the generic layer
    # getattr-skips a missing hook).
    def destroyed_experiment_ids(self, messages):
        return set()

    def destroyed_proven_experiment_ids(self, messages):
        return set()

    def parse_injection_params(self, tool_name, tool_args):
        # Phase-7 T3: issue-time extraction hook — default None pinned
        # structurally (runtime_checkable conformance requires the method).
        return None

    def issue_time_method(self, tool_name, tool_args, *, is_host):
        # Phase-7 T4: issue-time attribution hook — default None pinned
        # structurally (runtime_checkable conformance requires the method).
        return None

    def classify_tool_target(self, tool_name, tool_args, raw_command):
        # Phase-7 T5: guard-side classification hook — default None pinned
        # structurally (runtime_checkable conformance requires the method).
        return None

    def issue_disproven(self, messages):
        return False

    async def rollback_handle(self, handle, **kwargs):
        return ""

    def was_fault_create_attempted(self, messages, injection_method=None):
        # UID-less fake — the protocol default pinned structurally (the
        # conformance isinstance check requires the attribute to exist).
        return False

    async def layer1_verify(self, state, **kwargs) -> Layer1Result:
        return Layer1Result(status=Layer1Status.SKIPPED, details=f"fake:{self.carrier}")

    async def layer1_raw_destroy(self, uid, kubeconfig="") -> str:
        return ""

    def classify_destroy_output(self, output):
        # Destroy three-table unification: default fail-closed FAILED pinned
        # structurally (runtime_checkable conformance requires the method) —
        # a hook-less carrier surfaces, never silently retires.
        return DestroyOutcome.FAILED

    async def layer1_destroy(
        self, uid, kubeconfig="", *, messages=None, injection_method=None
    ) -> Layer1Result:
        return Layer1Result(status=Layer1Status.SKIPPED, details=f"fake:{self.carrier}")

    def recovery_vehicle(self, state):
        return ""

    def blocks_deterministic_destroy(self, state, messages=None):
        return False

    def recovery_facts_render(self, state, *, spec_params=None):
        return ""

    def merge_deterministic_recover_verdict(self, layer1, state, part_override=None):
        return layer1

    def layer1_recover_guidance(
        self, state, experiment_uid, *, combo_native=False, combo_part=None
    ):
        return ""

    def layer2_facts_note(self, state):
        return ""

    def verify_prompt_note(self, injection_method, *, injection_pod_name=None) -> str:
        return ""

    def recover_layer2_context(
        self, state, layer1, *, is_deterministic, blade_uid, is_host_scope
    ) -> tuple[str, str]:
        return "", ""

    async def recover(self, state, handle) -> RecoverResult:
        return RecoverResult(level="skipped")

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


@pytest.mark.asyncio
async def test_ensure_crd_seam_dispatches_to_the_hooked_provider():
    # Installability seam (faultdrill-cr-channel D2/D7): dispatch is
    # getattr-optional — only a provider that OWNS the install question
    # answers; the verdict passes through as a provider-neutral dict.
    class _InstallableFake(_FakeProvider):
        async def ensure_crd(self, kubeconfig=""):
            self.seen_kubeconfig = kubeconfig
            return {"usable": True, "status": "ready"}

    fake = _InstallableFake("faultdrill_cr", ("kubectl_native",))
    FaultProviderRegistry.register(fake)
    verdict = await FaultProviderRegistry.ensure_crd(kubeconfig="/tmp/k")
    assert verdict == {"usable": True, "status": "ready"}
    assert fake.seen_kubeconfig == "/tmp/k"


@pytest.mark.asyncio
async def test_ensure_crd_seam_none_when_no_provider_claims_install():
    # Every backend but the CR channel omits the hook — the seam's None
    # means "nobody claims the install responsibility", which the route
    # gate treats as pass-through (the apply's own error family governs).
    FaultProviderRegistry.register(_FakeProvider("chaosblade", ("host_blade",)))
    assert await FaultProviderRegistry.ensure_crd() is None


# -- fault-handle orchestration (carrier-neutral entry points) ---------------


class TestHandleOrchestration:
    """The registry's three carrier-neutral seams over the built-in backends:
    legacy hydration (``derive_handle_from_legacy``), experiment-id extraction
    (``extract_experiment_uid``) and rollback dispatch (``rollback_handle``)."""

    def test_derive_handle_prefers_method_attributed_provider(self):
        """A ``python_agent`` attribution must claim the UID via the
        Python-agent backend even though ChaosBlade registers first and also
        claims bare ``blade_uid`` facts."""
        FaultProviderRegistry.register_builtins()
        handle = FaultProviderRegistry.derive_handle_from_legacy(
            {
                "experiment_uid": "uid-py",
                "injection_method": "python_agent",
            }
        )
        assert handle == {
            "kind": "experiment_uid",
            "value": "uid-py",
            "method": "python_agent",
        }

    def test_derive_handle_native_method_needs_no_uid(self):
        FaultProviderRegistry.register_builtins()
        handle = FaultProviderRegistry.derive_handle_from_legacy(
            {
                "injection_method": "kubectl_native",
            }
        )
        assert handle == {"kind": "native", "method": "kubectl_native"}

    def test_derive_handle_unattributed_uid_falls_back_to_registration_order(self):
        FaultProviderRegistry.register_builtins()
        handle = FaultProviderRegistry.derive_handle_from_legacy(
            {
                "experiment_uid": "uid-x",
            }
        )
        assert handle == {"kind": "experiment_uid", "value": "uid-x", "method": ""}

    def test_derive_handle_no_facts_returns_none(self):
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.derive_handle_from_legacy({}) is None

    def test_extract_experiment_uid_scans_blade_evidence(self):
        from langchain_core.messages import ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"77a1b2c3d4e5f607"}',
                name="blade_create",
                tool_call_id="c1",
            ),
        ]
        assert (
            FaultProviderRegistry.extract_experiment_uid(
                msgs,
                is_host=False,
            )
            == "77a1b2c3d4e5f607"
        )
        # UID-less channel facts never produce an id.
        assert FaultProviderRegistry.extract_experiment_uid([], is_host=False) == ""

    @pytest.mark.asyncio
    async def test_rollback_handle_dispatches_by_kind(self, monkeypatch):
        FaultProviderRegistry.register_builtins()
        # Native kinds decline a synchronous rollback (recover graph's job).
        assert (
            await FaultProviderRegistry.rollback_handle(
                {"kind": "native", "method": "kubectl_native"},
            )
            == ""
        )
        # Unknown kinds decline too — never fed to a wrong backend.
        assert await FaultProviderRegistry.rollback_handle({"kind": "bogus"}) == ""
        # The blade_uid kind dispatches to ChaosBlade's blade_destroy.
        from chaos_agent.agent.providers.chaosblade import cli as blade_tools_mod

        class _FakeDestroy:
            async def ainvoke(self, args):
                return f"destroyed {args['uid']}"

        monkeypatch.setattr(blade_tools_mod, "blade_destroy", _FakeDestroy())
        suffix = await FaultProviderRegistry.rollback_handle(
            {"kind": "experiment_uid", "value": "uid-1"},
            kubeconfig="",
        )
        assert suffix == " (auto-rolled back experiment_uid=uid-1)"

    @pytest.mark.asyncio
    async def test_rollback_handle_prefers_method_attribution_over_kind_order(self):
        """Two backends sharing a kind: an attributed handle must reach its
        owning backend even when another kind-owner registered first; a
        legacy (method-less) handle falls back to registration order."""
        calls: list[str] = []

        class _RecordingProvider(_FakeProvider):
            async def rollback_handle(self, handle, **kwargs):
                calls.append(self.carrier)
                return f"rolled back by {self.carrier}"

        first = _RecordingProvider("chaosblade", ("host_blade",))
        first.handle_kind = "experiment_uid"
        second = _RecordingProvider("python_agent_backend", ("python_agent",))
        second.handle_kind = "experiment_uid"
        FaultProviderRegistry.register(first)
        FaultProviderRegistry.register(second)

        suffix = await FaultProviderRegistry.rollback_handle(
            {"kind": "experiment_uid", "value": "uid-9", "method": "python_agent"},
        )
        assert suffix == "rolled back by python_agent_backend"
        assert calls == ["python_agent_backend"]

        calls.clear()
        suffix = await FaultProviderRegistry.rollback_handle(
            {"kind": "experiment_uid", "value": "uid-9"},
        )
        assert suffix == "rolled back by chaosblade"
        assert calls == ["chaosblade"]


class TestRecoverDispatchMatrix:
    """``resolve_fault_dispatch`` ownership order, pinned as the neutral
    equivalent of the legacy routing (``blade_uid`` → experiment carrier,
    else method backend, else the UID-less default)."""

    def test_pure_experiment_routes_to_experiment_carrier(self):
        FaultProviderRegistry.register_builtins()
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"experiment_uid": "uid-1", "injection_method": "host_blade"}
        )
        assert provider.carrier == "chaosblade"
        assert identity == {
            "kind": "experiment_uid",
            "value": "uid-1",
            "method": "host_blade",
        }

    def test_explicit_experiment_handle_in_state_is_reused(self):
        """A state-carried experiment-kind handle IS the claim — no rebuild
        from legacy fields (a rebuilt one would lose the precise method)."""
        FaultProviderRegistry.register_builtins()
        handle = {"kind": "experiment_uid", "value": "uid-1", "method": "host_blade"}
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"experiment_uid": "uid-1", "fault_handle": handle}
        )
        assert provider.carrier == "chaosblade"
        assert identity is handle

    def test_native_method_routes_to_native_backend_with_native_handle(self):
        FaultProviderRegistry.register_builtins()
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"injection_method": "kubectl_native"}
        )
        assert provider.carrier == "k8s_native"
        assert identity == {"kind": "native", "method": "kubectl_native"}

        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"injection_method": "host_native"}
        )
        assert provider.carrier == "host_shell"
        assert identity == {"kind": "native", "method": "host_native"}

    def test_combo_routes_to_experiment_carrier_despite_native_attribution(self):
        """Combo ownership: the experiment claim outranks the native
        attribution — the deterministic destroy must reach the experiment
        carrier (leaking it would orphan a live experiment)."""
        FaultProviderRegistry.register_builtins()
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {
                "experiment_uid": "uid-combo",
                "injection_method": "kubectl_native",
                "combo_native_issued": True,
            }
        )
        assert provider.carrier == "chaosblade"
        assert identity == {
            "kind": "experiment_uid",
            "value": "uid-combo",
            "method": "kubectl_native",
        }

    def test_python_agent_experiment_routes_to_first_registered_carrier(self):
        """Legacy contract: a claimed experiment routes to the FIRST
        registered experiment carrier regardless of method attribution
        (mirrors the pre-dispatch ``if blade_uid:`` routing)."""
        FaultProviderRegistry.register_builtins()
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"experiment_uid": "uid-py", "injection_method": "python_agent"}
        )
        assert provider.carrier == "chaosblade"
        assert identity == {
            "kind": "experiment_uid",
            "value": "uid-py",
            "method": "python_agent",
        }

    def test_no_facts_default_to_uidless_verdict_backend(self):
        FaultProviderRegistry.register_builtins()
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch({})
        assert provider.carrier == "k8s_native"
        assert identity is None

    def test_message_history_uid_routes_to_experiment_carrier(self):
        """Defense seam (claim 2): with the durable facts absent (heavily
        compacted legacy checkpoints) a live experiment UID recovered from
        the message history routes to the experiment carrier's destroy —
        the legacy ``if blade_uid:`` contract, now living inside the
        dispatch so every downstream re-dispatch agrees."""
        from langchain_core.messages import ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"aa1b2c3d4e5f6078"}',
                name="blade_create",
                tool_call_id="c1",
            ),
        ]
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"messages": msgs}
        )
        assert provider.carrier == "chaosblade"
        assert identity == {
            "kind": "experiment_uid",
            "value": "aa1b2c3d4e5f6078",
            "method": "",
        }

    def test_message_history_uid_outranks_native_attribution(self):
        """A native attribution with a message-history UID is combo evidence
        (claim 2 outranks claims 3/4): the live experiment still needs the
        deterministic destroy."""
        from langchain_core.messages import ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"aa1b2c3d4e5f6078"}',
                name="blade_create",
                tool_call_id="c1",
            ),
        ]
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {"injection_method": "kubectl_native", "messages": msgs}
        )
        assert provider.carrier == "chaosblade"
        # The durable native method is echoed into the recovered handle
        # (combo evidence: experiment claim + native attribution).
        assert identity == {
            "kind": "experiment_uid",
            "value": "aa1b2c3d4e5f6078",
            "method": "kubectl_native",
        }

    def test_state_experiment_claim_outranks_message_history(self):
        """The message scan is the WEAKEST evidence source: a durable
        experiment claim (claim 1) wins before the scan is consulted."""
        from langchain_core.messages import ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"uid-stale"}',
                name="blade_create",
                tool_call_id="c1",
            ),
        ]
        provider, identity = FaultProviderRegistry.resolve_fault_dispatch(
            {
                "experiment_uid": "uid-durable",
                "injection_method": "host_blade",
                "messages": msgs,
            }
        )
        assert provider.carrier == "chaosblade"
        assert identity == {
            "kind": "experiment_uid",
            "value": "uid-durable",
            "method": "host_blade",
        }


class TestResolveByHandleKind:
    def test_method_attribution_outranks_kind_registration_order(self):
        FaultProviderRegistry.register_builtins()
        provider = FaultProviderRegistry.resolve_by_handle_kind(
            {"kind": "experiment_uid", "value": "u", "method": "python_agent"}
        )
        assert provider.carrier == "chaosblade_python"

    def test_kind_fallback_for_methodless_legacy_handles(self):
        FaultProviderRegistry.register_builtins()
        provider = FaultProviderRegistry.resolve_by_handle_kind(
            {"kind": "experiment_uid", "value": "u"}
        )
        assert provider.carrier == "chaosblade"

    def test_mismatched_method_falls_back_to_kind(self):
        """A combo-built experiment handle carries the NATIVE method — the
        kind must still resolve to the experiment carrier, never to the
        native backend whose kind differs."""
        FaultProviderRegistry.register_builtins()
        provider = FaultProviderRegistry.resolve_by_handle_kind(
            {"kind": "experiment_uid", "value": "u", "method": "kubectl_native"}
        )
        assert provider.carrier == "chaosblade"

    def test_empty_or_unknown_returns_none(self):
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.resolve_by_handle_kind(None) is None
        assert FaultProviderRegistry.resolve_by_handle_kind({}) is None
        assert FaultProviderRegistry.resolve_by_handle_kind({"kind": "bogus"}) is None


class TestIsExperimentHandle:
    """Phase-7 T6: pinning membership is the owning provider's declaration.

    The consumer (``build_recovery_handle``) must not compare kind strings —
    it asks the registry, which asks the handle-owning provider's
    ``has_experiment_uid``. These lock the judgement itself; the pinned
    consumer shape is locked in test_operation_result."""

    def test_builtin_blade_handle_is_an_experiment_handle(self):
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.is_experiment_handle(
            {"kind": "experiment_uid", "value": "u"}
        )

    def test_declared_experiment_kind_pins_by_registration_alone(self):
        """The scenario the T6 generalisation exists for: a future carrier
        with a NON-blade_uid ``handle_kind`` needs zero consumer changes to
        be recognised as an experiment handle."""
        p = _FakeProvider("future_exp", ("future_method",))
        p.handle_kind = "my_experiment"
        p.has_experiment_uid = True
        FaultProviderRegistry.register(p)
        assert FaultProviderRegistry.is_experiment_handle(
            {"kind": "my_experiment", "value": "exp-1"}
        )

    def test_uidless_native_kind_is_not_an_experiment_handle(self):
        p = _FakeProvider("native_like", ("some_method",))
        p.handle_kind = "native"
        FaultProviderRegistry.register(p)
        assert not FaultProviderRegistry.is_experiment_handle(
            {"kind": "native", "value": "u"}
        )

    def test_empty_or_unknown_handle_is_not_an_experiment_handle(self):
        FaultProviderRegistry.register_builtins()
        assert not FaultProviderRegistry.is_experiment_handle(None)
        assert not FaultProviderRegistry.is_experiment_handle({})
        assert not FaultProviderRegistry.is_experiment_handle({"kind": "bogus"})


class TestDeriveHandleFromMessages:
    def test_blade_uid_in_history_is_claimed(self):
        from langchain_core.messages import ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"88a1b2c3d4e5f607"}',
                name="blade_create",
                tool_call_id="c1",
            ),
        ]
        assert FaultProviderRegistry.derive_handle_from_messages(msgs, {}) == {
            "kind": "experiment_uid",
            "value": "88a1b2c3d4e5f607",
            "method": "",
        }

    def test_destroyed_uid_is_not_claimed(self):
        from langchain_core.messages import AIMessage, ToolMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"88a1b2c3d4e5f607"}',
                name="blade_create",
                tool_call_id="c1",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blade_destroy",
                        "args": {"uid": "88a1b2c3d4e5f607"},
                        "id": "c2",
                    }
                ],
            ),
        ]
        assert FaultProviderRegistry.derive_handle_from_messages(msgs, {}) is None

    def test_empty_history_yields_none(self):
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.derive_handle_from_messages([], {}) is None


def test_extract_kubectl_exec_pod_name_dispatches_to_the_delivery_owner():
    """Phase-8 T4 seam: the kubectl-exec delivery-pod extraction is
    dispatched through the registry (``resolve_by_method("kubectl_exec")``
    -> the ChaosBlade backend's instance method), replacing the execute
    loop's direct lazy import of the concrete module. Byte-equivalent to
    the module-level function the verifier suite pins, and safely ``None``
    when the resolved owner does not implement the hook (the
    resolve-then-getattr defensive shape, same as ``rollback_handle``)."""
    from langchain_core.messages import AIMessage, ToolMessage

    from chaos_agent.agent.providers.chaosblade.provider import (
        extract_kubectl_exec_pod_name as module_fn,
    )

    FaultProviderRegistry.register_builtins()
    msgs = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "kubectl",
                    "args": {
                        "subcommand": "exec",
                        "v_args": (
                            "otel-c-tool-abc123 -n chaosblade -- blade "
                            "create k8s pod-cpu fullload"
                        ),
                    },
                    "id": "tc-1",
                }
            ],
        ),
        ToolMessage(
            content='{"code":200,"success":true,"result":"a0f2357a939a9bb8"}',
            name="kubectl",
            tool_call_id="tc-1",
        ),
    ]
    try:
        provider = FaultProviderRegistry.resolve_by_method("kubectl_exec")
        assert provider is not None
        assert (
            FaultProviderRegistry.extract_kubectl_exec_pod_name(msgs)
            == provider.extract_kubectl_exec_pod_name(msgs)
            == module_fn(msgs)
            == "otel-c-tool-abc123"
        )

        # A registry whose ``kubectl_exec`` owner does not implement the hook
        # (or no owner at all) yields None instead of raising.
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register(_FakeProvider("chaosblade", ("kubectl_exec",)))
        assert FaultProviderRegistry.extract_kubectl_exec_pod_name(msgs) is None
        FaultProviderRegistry.clear()
        assert FaultProviderRegistry.extract_kubectl_exec_pod_name(msgs) is None
    finally:
        # Phase-8 registry-state hygiene: restore the builtins even when an
        # assertion fails — a leaked empty/fake registry poisons later tests
        # (the same debt the phase-8 full-suite run exposed in test_factory).
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()


class TestDestroyedExperimentIdsSeam:
    """phase-13 —— 销毁扫描接缝（spec: detection-import-boundary）。

    ``destroyed_experiment_ids`` 是 union 语义的死亡过滤接缝
    （``created_experiment_ids`` provenance union 的对偶）：任一
    UID-bearing 载体发出的 destroy 都计入，无通道过滤——替代通用层
    （replan seam）对 ``detection.scan_destroyed_uids`` 的直连。
    """

    def test_seam_equals_carrier_scan_on_blade_messages(self):
        """spec「销毁扫描结果集合相等」：接缝（逐 provider union）对
        blade_destroy 消息集返回与载体权威函数相同的集合（双 blade 系
        provider 都扫同一 ``blade_destroy`` 词汇，union 后仍相等）。"""
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_destroyed_uids,
        )

        FaultProviderRegistry.register_builtins()
        msgs = [
            ToolMessage(
                content='{"code":200,"success":true,"result":"uid-1"}',
                name="blade_create",
                tool_call_id="c1",
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blade_destroy",
                        "args": {"uid": "uid-1"},
                        "id": "d1",
                    }
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blade_destroy",
                        "args": {"uid": "uid-2"},
                        "id": "d2",
                    }
                ],
            ),
        ]
        assert (
            FaultProviderRegistry.destroyed_experiment_ids(msgs)
            == scan_destroyed_uids(msgs)
            == {"uid-1", "uid-2"}
        )

    def test_second_carrier_destroy_unioned(self):
        """spec「第二 UID-bearing 载体的销毁被仲裁纳入」：注册带销毁扫描
        hook 的 fake provider，其扫到的 UID 并入 union（逐 provider 聚合
        可扩展，无通道过滤——fake 的销毁不依赖任何通道上下文）。"""

        class _DestroyScanningFake(_FakeProvider):
            has_experiment_uid = True

            def destroyed_experiment_ids(self, messages):
                return {"fake-destroyed-uid"}

        FaultProviderRegistry.register_builtins()
        FaultProviderRegistry.register(
            _DestroyScanningFake("fake_exp", ("fake_experiment",))
        )
        assert FaultProviderRegistry.destroyed_experiment_ids([]) == {
            "fake-destroyed-uid"
        }

    def test_uid_less_and_hook_less_providers_contribute_nothing(self):
        """UID-less 载体（无销毁概念）与未实现 hook 的 provider 贡献空集
        ——getattr-skip 模式，第三方 backend 不实现 hook 也能安全共存。"""

        class _HooklessExperimentFake(_FakeProvider):
            # has_experiment_uid=True 但不实现 destroyed_experiment_ids
            # ——getattr-skip 路径。
            has_experiment_uid = True

        FaultProviderRegistry.register(
            _HooklessExperimentFake("hookless_exp", ("hookless_exp_method",))
        )
        assert FaultProviderRegistry.destroyed_experiment_ids([]) == set()


class TestRecoverExperimentUidFromSessionSeam:
    """phase-13 —— session 恢复接缝（spec: detection-import-boundary, D2）。

    任务文件持久化的消息是纯 dict（无通道上下文）；接缝编排三层：
    langchain 转换 → 逐 UID-bearing provider 提取（无通道过滤）→
    载体自有 dict fallback（``extract_experiment_id_from_session_dict``）。
    替代通用层（task_snapshot）对 verify/detection 的直连。
    """

    # T1 快照的同款钉扎值（真实 UUID 形态——提取器的 regex 契约）。
    _OS_UID = "aaaaaaaa-1111-4222-8333-444444444444"
    _PY_UID = "bbbbbbbb-1111-4222-8333-444444444444"

    def test_seam_equals_snapshot_pinned_values(self):
        """spec「session 恢复逐值相等」：混合 fixture 两方向（「最靠后者
        胜出」语义）+ dict-only fallback + 空输入——与 phase-13 T1 快照
        （tasks 1.2）钉扎值逐值相等，T4 改道的对照组。"""
        os_create = '{{"code":200,"success":true,"result":"{}"}}'.format(self._OS_UID)
        py_create = '{{"code":200,"success":true,"result":"{}"}}'.format(self._PY_UID)

        FaultProviderRegistry.register_builtins()

        # 混合两方向：逐 provider 首 claim 必须复现「最靠后者胜出」。
        py_last = [
            {
                "type": "tool",
                "name": "blade_create",
                "content": os_create,
                "tool_call_id": "c1",
            },
            {
                "type": "tool",
                "name": "blade_python_create",
                "content": py_create,
                "tool_call_id": "c2",
            },
        ]
        os_last = list(reversed(py_last))
        assert (
            FaultProviderRegistry.recover_experiment_uid_from_session(py_last)
            == self._PY_UID
        )
        assert (
            FaultProviderRegistry.recover_experiment_uid_from_session(os_last)
            == self._OS_UID
        )

        # dict-only：langchain 转换后无 name → 全家桶不认 → 接缝第三层
        # （载体自有 dict hook）命中。
        dict_only = [
            {
                "type": "tool_execution",
                "detail": {
                    "command": "blade create k8s pod-cpu fullload",
                    "stdout_preview": os_create,
                },
            }
        ]
        assert (
            FaultProviderRegistry.recover_experiment_uid_from_session(dict_only)
            == self._OS_UID
        )

        # 空输入与非 list 输入。
        assert FaultProviderRegistry.recover_experiment_uid_from_session([]) == ""
        assert FaultProviderRegistry.recover_experiment_uid_from_session(None) == ""
        assert (
            FaultProviderRegistry.recover_experiment_uid_from_session("not-a-list")
            == ""
        )

    def test_channel_missing_does_not_miss_host_channel_evidence(self):
        """spec「channel 缺失时不漏 python 家族提取」（tasks 4.5）：接缝
        签名无通道参数——host-only UID-bearing provider 的证据仍被咨询
        并提取；对照带通道过滤的 ``extract_experiment_uid(is_host=False)``
        同证据被滤除——证明无通道过滤是接缝的结构性行为，而非靠双通道
        provider 的巧合覆盖。"""

        class _HostOnlyExtractingFake(_FakeProvider):
            has_experiment_uid = True

            def __init__(self):
                super().__init__(
                    "host_only_exp", ("host_only_method",), profiles=("host",)
                )

            def extract_experiment_id(self, messages, retired=None):
                return "host-only-uid" if messages else ""

        FaultProviderRegistry.register_builtins()
        FaultProviderRegistry.register(_HostOnlyExtractingFake())
        session = [{"type": "human", "content": "evidence"}]

        # 无通道上下文（任务文件不记录通道）：host-only provider 被咨询
        # 并 claim——不漏检。
        assert (
            FaultProviderRegistry.recover_experiment_uid_from_session(session)
            == "host-only-uid"
        )

        # 反事实对照：同证据在 K8S 通道过滤下被滤除（有通道接缝返回空）。
        from langchain_core.messages import HumanMessage

        assert (
            FaultProviderRegistry.extract_experiment_uid(
                [HumanMessage(content="evidence")], is_host=False
            )
            == ""
        )

    def test_uid_less_and_hook_less_providers_contribute_nothing(self):
        """UID-less 载体与未实现任何 session 提取路径的 provider 贡献
        空——两层（extract_experiment_id / dict hook）均为 getattr-skip
        可选路径，第三方 backend 不实现 hook 也能安全共存。"""

        class _HooklessExperimentFake(_FakeProvider):
            # has_experiment_uid=True：两层 hook 都会被咨询，但
            # extract_experiment_id 继承默认返回 ""，dict hook 不存在
            # （getattr-skip 路径）。
            has_experiment_uid = True

        FaultProviderRegistry.register(
            _HooklessExperimentFake("hookless_exp", ("hookless_exp_method",))
        )
        session = [
            {
                "type": "tool",
                "name": "blade_create",
                "content": '{"code":200,"success":true,"result":"x"}',
                "tool_call_id": "c1",
            }
        ]
        assert FaultProviderRegistry.recover_experiment_uid_from_session(session) == ""


class TestClassifyDestroyOutput:
    """B76 review G — the destroy-decision authority, now three-state.

    A FALSE retire would hide a LIVE experiment from every future recovery
    (retirement is excluded from all live-liability reads), which is strictly
    worse than the orphan the sweep exists to prevent — so only a PROVEN
    successful destroy may retire, and every ambiguous shape fails closed.
    The classifier lives in the carrier's verify module: the single decision
    source the registry sweep and the verify-replan retire filter both
    consume (the seam's own prefix table was a THIRD table judging the same
    output — deleted in the unification).
    """

    @staticmethod
    def _classify(output):
        from chaos_agent.agent.providers.chaosblade.verify import (
            classify_destroy_output,
        )

        return classify_destroy_output(output)

    def test_empty_output_fails_closed(self):
        assert self._classify("") is DestroyOutcome.FAILED
        assert self._classify(None) is DestroyOutcome.FAILED
        assert self._classify("   ") is DestroyOutcome.FAILED

    def test_error_and_failed_prefixes_fail(self):
        # The blade CLI surfaces failures this way (blade_destroy's output
        # contract). "Error: record not found" is the convergence-valve
        # signal — NOT_FOUND, still never a retire without a proof.
        assert (
            self._classify("Error: record not found") is DestroyOutcome.NOT_FOUND
        )
        assert self._classify("failed to destroy") is DestroyOutcome.FAILED

    def test_json_success_false_fails(self):
        assert (
            self._classify('{"success": false, "error": "boom"}')
            is DestroyOutcome.FAILED
        )
        assert self._classify('{"success": false}') is DestroyOutcome.FAILED

    def test_success_shapes_pass(self):
        assert (
            self._classify('{"code": 200, "success": true, "result": "u"}')
            is DestroyOutcome.SUCCESS
        )
        assert self._classify("destroyed uid-x") is DestroyOutcome.SUCCESS

    def test_not_found_prose_inside_a_success_receipt_stays_success(self):
        """SUCCESS is decided first: "not found" wording inside a SUCCESS
        receipt is evidence prose, not a convergence-valve signal."""
        assert (
            self._classify('{"code": 200, "result": "record not found in db"}')
            is DestroyOutcome.SUCCESS
        )

    def test_garbage_output_fails_closed(self):
        """Behaviour pin (the unification's direction): a non-JSON
        no-keyword output used to retire on the seam's old table while the
        authority's predicate kept the UID live — both fail closed now."""
        assert self._classify("random garbage") is DestroyOutcome.FAILED


class _SweepFake(_FakeProvider):
    """UID-bearing carrier with a recording destroy hook."""

    has_experiment_uid = True

    def __init__(
        self,
        *,
        destroy_output='{"code":200,"success":true}',
        blocks=False,
        raise_on_destroy=None,
        destroyed_verdict=False,
    ):
        super().__init__("sweep_fake", ("sweep_method",))
        self.destroy_calls: list[str] = []
        self.status_probes: list[str] = []
        self._destroy_output = destroy_output
        self._blocks = blocks
        self._raise = raise_on_destroy
        self._destroyed_verdict = destroyed_verdict

    async def layer1_raw_destroy(self, uid, kubeconfig=""):
        self.destroy_calls.append(uid)
        if self._raise is not None:
            raise self._raise
        return self._destroy_output

    def classify_destroy_output(self, output):
        # Single-source verdict: the fake routes its destroy output through
        # the REAL authority classifier so fixture semantics match the
        # production wiring the sweep now consumes.
        from chaos_agent.agent.providers.chaosblade.verify import (
            classify_destroy_output as _authority,
        )

        return _authority(output)

    async def experiment_destroyed(self, uid, kubeconfig=""):
        self.status_probes.append(uid)
        return self._destroyed_verdict

    def blocks_deterministic_destroy(self, state, messages=None):
        return self._blocks


def _sweep_state(owned, *, retired=None, method="sweep_method"):
    return {
        "messages": [],
        "owned_experiment_uids": list(owned),
        "retired_experiment_uids": retired,
        "injection_method": method,
    }


class TestSweepLiveLiabilities:
    """B76 review G — the liability-axis safety-net seam.

    The single ``experiment_uid`` slot is last-write-wins (correct for
    attribution), so a superseded experiment's recovery claim is erased the
    moment a newer create lands; this sweep is the carrier-neutral seam
    where the append-only birth registry turns back into real destroys.
    """

    @pytest.mark.asyncio
    async def test_residuals_destroyed_and_retired(self):
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1", "uid-2"])
        )
        assert retired == ["uid-1", "uid-2"]
        assert failures == []
        assert fake.destroy_calls == ["uid-1", "uid-2"]

    @pytest.mark.asyncio
    async def test_excluded_uid_belongs_to_the_main_flow(self):
        """The recover finale excludes the identity UID the main Layer-1
        flow (and the retry below it) already owns — the sweep must not
        race it."""
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-main", "uid-old"]), exclude_uid="uid-main"
        )
        assert retired == ["uid-old"]
        assert failures == []
        assert fake.destroy_calls == ["uid-old"]

    @pytest.mark.asyncio
    async def test_no_residuals_is_a_noop_and_idempotent(self):
        """Normal single-experiment task: the live set minus the exclusion
        is empty — zero destroy calls. And retired UIDs leave the live set,
        so a second sweep after a retire record lands is again a no-op
        (idempotence across finalize passes)."""
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-main"]), exclude_uid="uid-main"
        )
        assert retired == []
        assert failures == []
        assert fake.destroy_calls == []

        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-main", "uid-done"], retired=["uid-done"]),
            exclude_uid="uid-main",
        )
        assert retired == []
        assert failures == []
        assert fake.destroy_calls == []

    @pytest.mark.asyncio
    async def test_failed_destroy_output_is_not_retired(self):
        """False-retire guard: an Error:/failed destroy output keeps the UID
        in the liability set (fail-open into the liability view) so the next
        sweep / a re-run recover retries it."""
        fake = _SweepFake(destroy_output="Error: record not found")
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == []
        assert failures == ["uid-1: Error: record not found"]

    @pytest.mark.asyncio
    async def test_destroy_exception_reported_not_raised(self):
        fake = _SweepFake(raise_on_destroy=RuntimeError("blade missing"))
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == []
        assert failures == ["uid-1: blade missing"]

    @pytest.mark.asyncio
    async def test_in_cluster_delivery_degrades_to_guidance_without_destroy(self):
        """kubectl-exec delivery: the host-side destroy cannot reach a
        CRD-created experiment ("record not found" soft failure) — surfacing
        the UIDs with the kubectl-exec vehicle beats a soft-failed destroy
        that would falsely retire a LIVE experiment."""
        fake = _SweepFake(blocks=True)
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == []
        assert fake.destroy_calls == []
        assert len(failures) == 1
        assert "uid-1" in failures[0]
        assert "kubectl exec" in failures[0]

    @pytest.mark.asyncio
    async def test_no_experiment_carrier_dispatch_surfaces_every_residual(self):
        """No UID-bearing carrier claims the facts: every residual is
        surfaced as a failure (never silently dropped, never falsely
        retired)."""
        FaultProviderRegistry.register(_FakeProvider("native_only", ("native_method",)))
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"], method="native_method")
        )
        assert retired == []
        assert failures == ["uid-1: no experiment carrier dispatched"]


class TestCreatedExperimentIdsDurableSource:
    """B76 review G — the whitelist's durable-source upgrade: the
    append-only birth registry keeps proving provenance across compaction
    and contract replacement (the legacy single slot only ever held the
    NEWEST UID, so a superseded experiment lost even the authority to be
    destroyed once compaction removed its create message).

    Round-20 Q4 flip: the durable read-side now gates on the UID shape
    (``_UID_SHAPE_RE``) at the trust-chain end, so the fixtures carry
    legal hex16 shapes (the pre-r20 placeholders ``uid-new`` etc. were
    non-shaped strings the gate now refuses)."""

    def test_compacted_history_keeps_superseded_uid_whitelisted(self):
        FaultProviderRegistry.register_builtins()
        state = {
            "experiment_uid": "a1b2c3d4e5f60718",
            "owned_experiment_uids": ["deadbeef00000002", "a1b2c3d4e5f60718"],
        }
        # Compaction boundary: no create messages at all.
        assert FaultProviderRegistry.created_experiment_ids([], state) == {
            "deadbeef00000002",
            "a1b2c3d4e5f60718",
        }

    def test_pre_g_shape_loses_the_superseded_uid(self):
        """Counterfactual pin (the bug this fix closes): without the birth
        registry the compacted whitelist holds only the newest UID."""
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.created_experiment_ids(
            [], {"experiment_uid": "a1b2c3d4e5f60718"}
        ) == {"a1b2c3d4e5f60718"}

    def test_python_agent_attribution_still_excluded_from_blade_slot(self):
        """The single-slot durable source stays carrier-scoped: a
        ``python_agent`` attribution owns its legacy field via its own
        provider, so the blade carrier must not claim it (the owned registry
        is carrier-neutral and unaffected)."""
        FaultProviderRegistry.register_builtins()
        state = {
            "experiment_uid": "f00dface12345678",
            "injection_method": "python_agent",
            "owned_experiment_uids": ["f00dface12345678"],
        }
        assert FaultProviderRegistry.created_experiment_ids([], state) == {
            "f00dface12345678",
        }


class TestDestroyedProvenExperimentIds:
    """B76 review I1 — the death-registration scan: output-PROVEN kills in
    BOTH delivery forms, fail-closed on doubt.

    The issued-scan (``destroyed_experiment_ids``) treats a destroy CALL as
    terminal — the right conservatism for attribution. The retire LEDGER
    needs the higher bar: retirement excludes a UID from every live-
    liability read, so only a paired successful output may register
    (probe_b76_round9.py A1)."""

    @staticmethod
    def _pair(tool: str, args: dict, output: str, call_id: str = "call-d1"):
        from langchain_core.messages import AIMessage, ToolMessage

        return [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": tool,
                        "args": args,
                        "id": call_id,
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(content=output, tool_call_id=call_id, name=tool),
        ]

    def test_host_blade_destroy_with_success_output_registers(self):
        FaultProviderRegistry.register_builtins()
        msgs = self._pair(
            "blade_destroy",
            {"uid": "uid-1"},
            '{"code": 200, "success": true, "result": "success"}',
        )
        assert FaultProviderRegistry.destroyed_proven_experiment_ids(msgs) == {
            "uid-1",
        }

    def test_kubectl_exec_vehicle_registers(self):
        """I1c: the in-cluster destroy vehicle (kubectl tool_call carrying
        ``blade destroy <uid>`` in v_args) used to be invisible to the
        issued-scan; the proven-scan must see it. Round-14 F1 翻案：the
        issued-scan now sees the inline face too — issued = terminal is
        channel-neutral (a destroy sent through the vehicle the registry
        itself instructs is just as terminal as one sent through the
        blade_destroy tool)."""
        FaultProviderRegistry.register_builtins()
        msgs = self._pair(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": "pod-x -n chaosblade -- blade destroy bb00cc11dd22ee33",
            },
            '{"code": 200, "success": true}',
        )
        assert FaultProviderRegistry.destroyed_proven_experiment_ids(msgs) == {
            "bb00cc11dd22ee33",
        }
        # …and the issued-scan sees the vehicle form too (round-14 F1).
        assert FaultProviderRegistry.destroyed_experiment_ids(msgs) == {
            "bb00cc11dd22ee33",
        }

    def test_failed_output_does_not_register(self):
        """Fail-closed: a destroy whose paired output proves FAILURE keeps
        the UID out of the ledger (a false retire hides a LIVE experiment
        from every future recovery)."""
        FaultProviderRegistry.register_builtins()
        msgs = self._pair(
            "blade_destroy",
            {"uid": "uid-1"},
            '{"code": 500, "success": false}',
        )
        assert FaultProviderRegistry.destroyed_proven_experiment_ids(msgs) == set()

    def test_error_prefix_output_does_not_register(self):
        FaultProviderRegistry.register_builtins()
        msgs = self._pair(
            "blade_destroy",
            {"uid": "uid-1"},
            "Error: destroy failed",
        )
        assert FaultProviderRegistry.destroyed_proven_experiment_ids(msgs) == set()

    def test_unpaired_call_does_not_register(self):
        """No ToolMessage behind the call (compaction split / synthetic
        rebuild) → no proof → no registration."""
        from langchain_core.messages import AIMessage

        FaultProviderRegistry.register_builtins()
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "blade_destroy",
                        "args": {"uid": "uid-1"},
                        "id": "call-d1",
                        "type": "tool_call",
                    }
                ],
            )
        ]
        assert FaultProviderRegistry.destroyed_proven_experiment_ids(msgs) == set()


class TestSweepKubeconfigResolution:
    """Round-32 K1 — the sweep resolves its destroy's kubeconfig through
    the graph-wide three-level fallback (state > spec > settings), the
    SAME contract the injection chain's create runs under. The old bare
    state read silently re-homed every settlement destroy onto blade's
    own default cluster whenever the CLI entry seeded the state key
    empty — a liability could never clear on the cluster that birthed
    it."""

    @pytest.mark.asyncio
    async def test_empty_state_key_falls_through_to_settings(self, monkeypatch):
        """The CLI entry shape (no --kubeconfig flag → state key empty,
        real path in settings): the dispatched destroy must carry the
        settings value, not the bare ""."""
        monkeypatch.setattr(
            "chaos_agent.config.settings.settings.kubeconfig_path",
            "/tmp/r32-sweep-settings-kc",
        )
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        captured: dict[str, str] = {}

        async def _rec(uid, kubeconfig=""):
            captured[uid] = kubeconfig
            return '{"code":200,"success":true}'

        fake.layer1_raw_destroy = _rec
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == ["uid-1"]
        assert failures == []
        assert captured == {"uid-1": "/tmp/r32-sweep-settings-kc"}

    @pytest.mark.asyncio
    async def test_state_kubeconfig_key_passthrough(self):
        """A state that carries the key verbatim (the merged-resolved
        caller form, round-31's verify-replan): the resolver returns it
        unchanged — the merge stays idempotent, no double resolution can
        re-home it."""
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        captured: dict[str, str] = {}

        async def _rec(uid, kubeconfig=""):
            captured[uid] = kubeconfig
            return '{"code":200,"success":true}'

        fake.layer1_raw_destroy = _rec
        state = {**_sweep_state(["uid-1"]), "kubeconfig": "/tmp/r32-state-kc"}
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            state
        )
        assert retired == ["uid-1"]
        assert failures == []
        assert captured == {"uid-1": "/tmp/r32-state-kc"}


class TestSweepDeathRegistrationAbsorption:
    """B76 review I1c (A3) — the sweep absorbs message-side PROVEN deaths
    into its local retired view before computing residuals, covering
    histories execute_loop's registration seam never saw (legacy sessions,
    LLM destroys inside the recover graph's own Layer-1 flow)."""

    @pytest.mark.asyncio
    async def test_proven_death_in_messages_is_absorbed_without_destroy(self):
        from langchain_core.messages import AIMessage, ToolMessage

        # Builtins supply the proven-death scan hooks (the fake carrier has
        # none); the fake still owns the dispatch so any real destroy would
        # be recorded — proving the absorption left nothing to destroy.
        FaultProviderRegistry.register_builtins()
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": "pod-x -n chaosblade -- blade destroy aa11bb22cc33dd44",
                        },
                        "id": "call-k1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"code": 200, "success": true}',
                tool_call_id="call-k1",
                name="kubectl",
            ),
        ]
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            {
                "messages": msgs,
                "owned_experiment_uids": ["aa11bb22cc33dd44"],
                "retired_experiment_uids": [],
                "injection_method": "sweep_method",
            }
        )
        assert retired == []
        assert failures == []
        assert fake.destroy_calls == []

    @pytest.mark.asyncio
    async def test_unproven_destroy_in_messages_stays_live(self):
        """Fail-closed control: a kubectl-exec destroy whose output FAILED
        is not absorbed — and the vehicle is invisible to the issued-scan,
        so the UID stays live and the sweep must retry the destroy itself.
        (A host-channel blade_destroy would be excluded by the issued-scan
        already — that is the SAME-graph conservatism, not the I1 gap.)"""
        from langchain_core.messages import AIMessage, ToolMessage

        FaultProviderRegistry.register_builtins()
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        msgs = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": "pod-x -n chaosblade -- blade destroy uid-1",
                        },
                        "id": "call-k1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content='{"code": 500, "success": false}',
                tool_call_id="call-k1",
                name="kubectl",
            ),
        ]
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            {
                "messages": msgs,
                "owned_experiment_uids": ["uid-1"],
                "retired_experiment_uids": [],
                "injection_method": "sweep_method",
            }
        )
        assert retired == ["uid-1"]
        assert failures == []
        assert fake.destroy_calls == ["uid-1"]


class TestSweepConvergenceValve:
    """B76 review I2/I2b (C) — the sweep's not-found convergence valve: a
    repeat-destroy of an already-dead experiment surfaces record-not-found;
    without an escape the UID never retires and every re-run recover
    repeats the same destroy+failure forever."""

    @pytest.mark.asyncio
    async def test_not_found_failure_with_status_proof_retires(self):
        fake = _SweepFake(
            destroy_output="Error: record not found",
            destroyed_verdict=True,
        )
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == ["uid-1"]
        assert failures == []
        assert fake.status_probes == ["uid-1"]

    @pytest.mark.asyncio
    async def test_not_found_failure_without_status_proof_stays_failed(self):
        """Status says still Running (or the check fails) → fail-closed: the
        failure is the honest verdict, no false retire."""
        fake = _SweepFake(
            destroy_output="Error: record not found",
            destroyed_verdict=False,
        )
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == []
        assert failures == ["uid-1: Error: record not found"]

    @pytest.mark.asyncio
    async def test_non_not_found_failure_never_consults_status(self):
        """A hard failure (cluster unreachable) is not a death smell — the
        valve must not even probe, let alone retire."""
        fake = _SweepFake(
            destroy_output='{"code": 500, "success": false, "error": "cluster unreachable"}',
            destroyed_verdict=True,
        )
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"])
        )
        assert retired == []
        assert len(failures) == 1
        assert fake.status_probes == []

    @pytest.mark.asyncio
    async def test_retire_after_valve_converges_the_rerun(self):
        """End-to-end death-loop closure: after the valve retires, a re-run
        sweep (the caller appended the retire) is a no-op."""
        fake = _SweepFake()
        FaultProviderRegistry.register(fake)
        retired, failures = await FaultProviderRegistry.sweep_live_liabilities(
            _sweep_state(["uid-1"], retired=["uid-1"])
        )
        assert retired == []
        assert failures == []
        assert fake.destroy_calls == []


class TestDeathVerdictSingleSource:
    """B76 review K2/L1 — the proven-death JSON verdict is single-source.

    The verdict must apply the SAME predicate the project's authority
    parser (``parse_blade_destroy_output``) applies: ``success`` truthy or
    ``code == 200``. The pre-L1 table refused only ``success is False`` —
    a key-less error JSON (``{"code": 500, "error": ...}``) was death
    here and FAILED at the authority: a second, looser table drifts
    exactly the way the readonly double-judge drifted.
    """

    def test_canonical_success_proves_death(self):
        from chaos_agent.agent.providers.chaosblade.recover import (
            parse_blade_destroy_output,
        )
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        proves = _destroy_output_proves_death(
            '{"code": 200, "success": true, "result": "success"}',
        )
        assert proves
        assert (
            parse_blade_destroy_output(
                '{"code": 200, "success": true}',
            )[0]
            == "passed"
        )

    def test_canonical_failure_refuses_death(self):
        from chaos_agent.agent.providers.chaosblade.recover import (
            parse_blade_destroy_output,
        )
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        output = '{"code": 500, "success": false, "error": "rpc timeout"}'
        assert not _destroy_output_proves_death(output)
        assert parse_blade_destroy_output(output)[0] == "failed"

    def test_keyless_error_json_refuses_death(self):
        """The K2 fork closed: authority says failed, so does the verdict."""
        from chaos_agent.agent.providers.chaosblade.recover import (
            parse_blade_destroy_output,
        )
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        output = '{"code": 500, "error": "rpc timeout"}'
        assert not _destroy_output_proves_death(output)
        assert parse_blade_destroy_output(output)[0] == "failed"

    def test_code_200_without_success_key_proves_death(self):
        """The authority's second acceptance arm (``code == 200``) is
        honoured too — both arms aligned, not just the success arm."""
        from chaos_agent.agent.providers.chaosblade.recover import (
            parse_blade_destroy_output,
        )
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        output = '{"code": 200, "result": "ok"}'
        assert _destroy_output_proves_death(output)
        assert parse_blade_destroy_output(output)[0] == "passed"

    def test_non_dict_json_is_fail_closed(self):
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        assert not _destroy_output_proves_death('["success"]')
        assert not _destroy_output_proves_death('"success"')
        assert not _destroy_output_proves_death("42")

    def test_non_json_vocabulary_fallback_preserved(self):
        """The authority's non-JSON fallback arm (success/destroyed
        wording) stays available for plain-text blade output."""
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        assert _destroy_output_proves_death("destroy success")
        assert _destroy_output_proves_death("experiment destroyed")
        assert not _destroy_output_proves_death("ok")
        assert not _destroy_output_proves_death("command queued")


class TestDeathVerdictFrameworkReceipts:
    """B76 review K4/L2 — framework-synthesized receipts are refused
    STRUCTURALLY.

    Four framework paths answer a tool_call that NEVER EXECUTED with a
    synthesized ToolMessage (screener REJECTION/DEFERRED, replan ``Not
    executed``). Pre-L2 these were excluded only by vocabulary coincidence;
    the fix gates them by their contract PREFIXES — a reworded receipt body
    can no longer forge a death certificate.
    """

    def _proves(self, output: str) -> bool:
        from chaos_agent.agent.providers.chaosblade.verify import (
            _destroy_output_proves_death,
        )

        return _destroy_output_proves_death(output)

    def test_replan_receipt_refused(self):
        assert not self._proves(
            "Not executed: a replan was requested in the same turn, so the "
            "current plan is being abandoned before this call ran.",
        )

    def test_screener_deferred_receipt_refused(self):
        assert not self._proves(
            "[screener] DEFERRED — kubectl was NOT rejected; re-issue it.",
        )

    def test_screener_rejection_receipt_refused(self):
        assert not self._proves(
            "[target_guard] REJECT_UNKNOWN — blade_destroy UID was not "
            "produced by this task's blade_create",
        )

    def test_mutated_receipt_body_still_refused(self):
        """Mutation pin (has teeth): a future copy-edit adding success
        wording to a deferred receipt must NOT flip the verdict — the
        prefix gate is structural, not lexical."""
        assert not self._proves(
            "[screener] DEFERRED — re-issue this call and it will run successfully",
        )

    def test_mutated_replan_receipt_still_refused(self):
        assert not self._proves(
            "Not executed: the call succeeded on a previous turn and this "
            "is a stale receipt",
        )

    def test_scan_ignores_framework_answered_destroy_calls(self):
        """End-to-end: a kubectl destroy vehicle answered by the screener
        (deferred) contributes NOTHING to the proven set."""
        from langchain_core.messages import AIMessage, ToolMessage

        from chaos_agent.agent.providers.chaosblade.verify import (
            scan_destroyed_proven_uids,
        )

        messages = [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "kubectl",
                        "args": {
                            "subcommand": "exec",
                            "v_args": "pod-x -n chaosblade -- blade destroy uid-e1",
                        },
                        "id": "call-k1",
                        "type": "tool_call",
                    }
                ],
            ),
            ToolMessage(
                content=(
                    "[screener] DEFERRED — re-issue this call and it will "
                    "run successfully"
                ),
                tool_call_id="call-k1",
                name="kubectl",
            ),
        ]
        assert scan_destroyed_proven_uids(messages) == set()
