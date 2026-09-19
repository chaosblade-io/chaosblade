"""Phase-7 acceptance line: REGISTER A PROVIDER, GET EVERY SEAM FOR FREE.

Every peripheral-chain capability phase-7 moved behind provider
declarations — guard-side classification (T5), injection-parameter parsing
(T3), issue-time attribution (T4), the three per-tool pass sets (T2), and
the experiment-handle contract pinning (T6) — must work for a newly
registered provider with ZERO changes to any generic-layer module. This is
the "registration is the whole feature" line: if any seam still consults a
hardcoded carrier name in a generic layer, this file is where it shows.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.providers.base import StepActionScan
from chaos_agent.agent.target_guard import infer_effective_target
from chaos_agent.agent.target_guard.types import ConfidenceLevel, EffectiveTarget


class _FullSeamFakeProvider:
    """Declares every phase-7 seam, claiming the ``my_tool`` carrier.

    A deliberately non-builtin vocabulary (widget scope, ``my_experiment``
    handle kind): nothing in the generic layers could have anticipated
    these names, so any pass here is by registration, never by hardcoding.
    """

    carrier = "my_carrier"
    injection_methods = ("my_method",)
    has_experiment_uid = True
    handle_kind = "my_experiment"
    uid_less_verdict_default = False
    is_multi_step = False
    has_deterministic_recover = False
    inject_tool_names = frozenset({"my_tool"})
    inject_kubectl_subcommands = frozenset()
    supported_targets: tuple[str, ...] = ()
    supported_actions: tuple[str, ...] = ()
    injection_binaries = frozenset()
    kubeconfig_scoped_tool_names = frozenset({"my_tool"})
    audit_scoped_tool_names = frozenset({"my_tool"})
    log_shipping_tool_names = frozenset({"my_tool"})
    # Phase-8 Form A seam: namespaces hosting injection-infrastructure
    # pods — exempt from cross-namespace drift rejection (Tier 1).
    tool_pod_namespaces = frozenset({"my-tool-ns"})
    # Create-reconcile gate (D6) declaration pair: the fake's create is
    # non-idempotent (under the gate) and ``my_read`` is its reconciliation
    # read — deliberately non-builtin vocabulary, so any gate pass here is
    # by registration, never by hardcoding.
    reconcile_create_tool_names = frozenset({"my_tool"})
    reconcile_read_tool_names = frozenset({"my_read"})

    def matches_channel(self, profile: str) -> bool:
        return True

    def classify_tool_target(self, tool_name, tool_args, raw_command):
        if tool_name != "my_tool":
            return None
        return EffectiveTarget(
            scope="widget",
            namespace="ns-x",
            names=("w1",),
            confidence=ConfidenceLevel.HIGH,
            raw_command=raw_command,
        )

    def parse_injection_params(self, tool_name, tool_args):
        if tool_name != "my_tool":
            return None
        return {"carrier": "my_carrier", "key": "value"}

    def issue_time_method(self, tool_name, tool_args, *, is_host):
        if tool_name != "my_tool":
            return None
        return "my_method"

    # Phase-8 Form B seams, upgraded from the T1.2 inert pre-declaration
    # to REAL hook logic (spec Scenario 3): the step-scan vocabulary is the
    # verb token ``my_verb`` — deliberately non-builtin, so any
    # required/executed diff naming it flowed through dispatch to THIS
    # provider, never through a builtin's shadow.
    # ``was_injection_attempted`` stays False: the fake is an
    # experiment-UID carrier, and the native back-scan vocabulary is the
    # native carriers' own.
    def scan_step_actions(self, steps, messages, *, is_teardown=None):
        required = {"my_verb": step for step in steps if "my_verb" in step}
        executed = {
            "my_verb"
            for msg in messages
            if getattr(msg, "tool_calls", None)
            and any(call.get("name") == "my_tool" for call in msg.tool_calls)
        }
        return StepActionScan(required=required, executed=executed)

    def was_injection_attempted(self, messages, *, is_teardown=None):
        return False

    # Create-reconcile seam (D6), with REAL hook logic — the same
    # registration-extensibility proof as the Form B hooks above: the
    # identity vocabulary (``ns-x`` / ``w1``) is the fake's own, so any
    # fingerprint reaching the assertions below flowed through dispatch.
    def build_reconcile_fingerprint(self, tool_name, tool_args):
        if tool_name != "my_tool":
            return None
        from chaos_agent.tools.request_identity import RequestFingerprint

        return RequestFingerprint(namespace="ns-x", target_names="w1")

    async def reconcile_hold_feedback(
        self, tool_name, fp, hold_count, block_limit,
        kubeconfig="", task_id="",
    ):
        if tool_name != "my_tool":
            return None
        from chaos_agent.tools.markers import GATE_RECONCILE_BLOCKED_MARKER

        return (
            f"{GATE_RECONCILE_BLOCKED_MARKER} fake hold #{hold_count}/"
            f"{block_limit} for {fp.target_names}",
            True,
        )

    def reconcile_batch_held_feedback(self, tool_name, other_tool_name):
        if tool_name != "my_tool":
            return None
        return (
            f"Error: tool call `{other_tool_name}` was NOT executed — a "
            f"my_tool in this same batch was held by the create-reconcile "
            f"gate, so the whole batch was held back."
        )


@pytest.fixture(autouse=True)
def _registered_fake():
    """Each test runs against a registry holding ONLY the fake provider —
    every pass below therefore flows through the fake's declarations, never
    through a builtin's shadow."""
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register(_FullSeamFakeProvider())
    yield
    FaultProviderRegistry.clear()
    FaultProviderRegistry.register_builtins()


class TestRegistrationIsTheWholeFeature:
    def test_classification_reaches_the_registered_provider(self):
        """T5 seam: ``infer_effective_target`` dispatches to the hook."""
        et = infer_effective_target("my_tool", {"anything": 1})
        assert et is not None
        assert et.scope == "widget"
        assert et.namespace == "ns-x"
        assert et.names == ("w1",)
        assert et.confidence is ConfidenceLevel.HIGH

    def test_injection_param_parsing_reaches_the_registered_provider(self):
        """T3 seam: the execute loop's param extraction dispatches."""
        parsed = FaultProviderRegistry.parse_injection_params("my_tool", {"x": 1})
        assert parsed == {"carrier": "my_carrier", "key": "value"}

    def test_issue_time_attribution_reaches_the_registered_provider(self):
        """T4 seam: ``classify_issue_time_method`` dispatches to the hook."""
        from chaos_agent.agent.nodes.execute._injection_detection import (
            classify_issue_time_method,
        )

        assert (
            classify_issue_time_method("my_tool", {"x": 1}, is_host=True) == "my_method"
        )

    def test_three_tool_sets_aggregate_the_registered_provider(self):
        """T2 seam: all three per-tool pass sets include the declaration."""
        assert "my_tool" in FaultProviderRegistry.union_tool_names(
            "kubeconfig_scoped_tool_names"
        )
        assert "my_tool" in FaultProviderRegistry.union_tool_names(
            "audit_scoped_tool_names"
        )
        assert "my_tool" in FaultProviderRegistry.union_tool_names(
            "log_shipping_tool_names"
        )

    def test_experiment_handle_pinning_reaches_the_registered_provider(self):
        """T6 seam: a non-blade_uid experiment handle pins its UID."""
        from chaos_agent.agent.result.operation_result import build_recovery_handle

        pinned = build_recovery_handle(
            {"fault_handle": {"kind": "my_experiment", "value": "exp-42"}}
        )
        assert pinned == {
            "kind": "my_experiment",
            "value": "exp-42",
            "experiment_uid": "exp-42",
        }

    def test_phase8_seam_surface_is_pre_declared(self):
        """Phase-8 protocol surface (Forms A/B) is pre-declared on the fake,
        so the ``base.py`` declarations land later with ZERO edits here
        (phase-7 1.2 precedent: ``uid_less_verdict_default``)."""
        assert _FullSeamFakeProvider.tool_pod_namespaces == frozenset({"my-tool-ns"})
        assert callable(_FullSeamFakeProvider.scan_step_actions)
        assert callable(_FullSeamFakeProvider.was_injection_attempted)

    def test_tool_pod_namespace_aggregate_reaches_the_registered_provider(self):
        """Form A seam (phase-8 T2): the Tier-1 tool-pod-namespace exemption
        set aggregates the registered provider's declaration."""
        assert FaultProviderRegistry.union_tool_names("tool_pod_namespaces") == (
            frozenset({"my-tool-ns"})
        )

    def test_tool_pod_namespace_exemption_flows_through_drift_policy(self):
        """Spec Scenario 2: the fake's declared infra namespace is exempt
        from the drift policy's secondary-namespace check with ZERO
        generic-layer changes — a cluster-scoped approval whose effective
        exec lands in the fake's tool namespace is not namespace drift."""
        from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
        from chaos_agent.agent.target_guard.types import ApprovedTarget

        approved = ApprovedTarget(
            scope="node",
            namespace="",
            secondary_scopes=("pod",),
            secondary_namespace="ns-approved",
        )
        effective = EffectiveTarget(
            scope="pod",
            namespace="my-tool-ns",
            names=("tool-pod-1",),
            confidence=ConfidenceLevel.HIGH,
            raw_command="kubectl exec tool-pod-1 -n my-tool-ns -- ...",
        )
        decision = K8sDriftPolicy().check_identity_drift(approved, effective)
        assert decision is None or "secondary namespace drift" not in decision.reason

    def test_builtin_tool_pod_namespace_union_is_chaosblade_only(self):
        """Aggregation over the four builtins: only the ChaosBlade operator
        declares an infra namespace."""
        FaultProviderRegistry.clear()
        FaultProviderRegistry.register_builtins()
        assert FaultProviderRegistry.union_tool_names("tool_pod_namespaces") == (
            frozenset({"chaosblade"})
        )

    def test_step_selfcheck_dispatches_to_registered_provider(self):
        """Spec Scenario 3 (Form B): a registered provider that implements
        ``scan_step_actions`` gets its own vocabulary diffed by the GENERIC
        ``build_injection_step_selfcheck`` with ZERO edits to
        ``_injection_detection`` — the hook-dispatch extensibility proof.
        The fake's verb ``my_verb`` is non-builtin: a reminder naming it can
        only have flowed through the ``resolve_by_method("my_method")``
        dispatch."""
        from langchain_core.messages import AIMessage

        from chaos_agent.agent.nodes.execute._injection_detection import (
            build_injection_step_selfcheck,
        )

        skill_case = (
            "**演练步骤**：\n"
            "1. 使用 my_verb 执行注入动作\n"
            "2. 使用 my_tool 复查效果\n"
        )
        out = build_injection_step_selfcheck(skill_case, [], "my_method")
        assert out is not None
        assert "Possibly not yet performed: my_verb (使用 my_verb 执行注入动作)" in out

        # Executed side: once the fake's own carrier tool ran, the verb is
        # credited and the soft reminder falls silent.
        done = [
            AIMessage(
                content="",
                tool_calls=[{"name": "my_tool", "args": {"x": 1}, "id": "tc-1"}],
            )
        ]
        assert build_injection_step_selfcheck(skill_case, done, "my_method") is None

    def test_unrelated_tools_still_fall_through_unchanged(self):
        """The generic default-deny path is untouched by the registration:
        an unclaimed tool still lands on UNKNOWN, never on the fake."""
        et = infer_effective_target("no_such_tool", {"x": 1})
        assert et.scope == "__unknown__"
        assert FaultProviderRegistry.parse_injection_params("no_such_tool", {}) is None

    # -- create-reconcile seam (blade-create-reconcile-before-retry D6) --

    def test_reconcile_declaration_pair_aggregates(self):
        """D6 declaration pair: the gate's create/read tool unions
        aggregate the registered provider's declarations."""
        assert FaultProviderRegistry.union_tool_names(
            "reconcile_create_tool_names"
        ) == frozenset({"my_tool"})
        assert FaultProviderRegistry.union_tool_names(
            "reconcile_read_tool_names"
        ) == frozenset({"my_read"})

    def test_reconcile_fingerprint_reaches_the_registered_provider(self):
        """D6 fingerprint seam: the gate's request identity for ``my_tool``
        is the fake's own (``ns-x`` / ``w1`` is registered vocabulary — a
        builtin's material could never produce it), and an unclaimed tool
        passes through as ``None`` so the registry scan continues."""
        fp = FaultProviderRegistry.build_reconcile_fingerprint("my_tool", {"x": 1})
        assert fp is not None
        assert fp.namespace == "ns-x"
        assert fp.target_names == "w1"
        assert FaultProviderRegistry.build_reconcile_fingerprint("my_read", {}) is None

    async def test_reconcile_hold_feedback_reaches_the_registered_provider(self):
        """D6 hold-feedback seam: dispatch returns the fake's
        marker-headed hold notice plus its reconciliation verdict, and an
        unclaimed tool passes through as ``None`` (no probe side effects)."""
        from chaos_agent.tools.markers import GATE_RECONCILE_BLOCKED_MARKER

        fp = FaultProviderRegistry.build_reconcile_fingerprint("my_tool", {"x": 1})
        outcome = await FaultProviderRegistry.reconcile_hold_feedback(
            "my_tool", fp, 1, 2
        )
        assert outcome is not None
        text, gate_reconciled = outcome
        assert text.startswith(GATE_RECONCILE_BLOCKED_MARKER)
        assert "fake hold #1/2" in text
        assert "w1" in text
        assert gate_reconciled is True
        assert (
            await FaultProviderRegistry.reconcile_hold_feedback("my_read", fp, 1, 2)
            is None
        )

    def test_reconcile_batch_held_feedback_reaches_the_registered_provider(self):
        """D6 batch-held seam: the fabricated notice for a batch-mate of a
        held ``my_tool`` flows through dispatch (non-execution wording
        naming the held call), and an unclaimed tool passes through."""
        text = FaultProviderRegistry.reconcile_batch_held_feedback(
            "my_tool", "my_read"
        )
        assert text is not None
        assert "NOT executed" in text
        assert "my_read" in text
        assert (
            FaultProviderRegistry.reconcile_batch_held_feedback(
                "my_read", "my_tool"
            )
            is None
        )
