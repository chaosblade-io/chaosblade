"""ChaosBlade create-reconcile judgment material (providers/chaosblade/reconcile).

blade-create-reconcile-before-retry D6, carrier half: the fingerprint
construction (raw LLM argument-shape normalisation into the four-dimension
identity the conflict query consumes), the provider-hook claim boundaries
(which tools the carrier's gate material answers for), and the fabricated
feedback texts (hold / batch-held / probe outcomes). The generic state
machine that consumes this material through the registry seam is tested in
``tests/test_agent/nodes/test_reconcile_gate.py``.
"""

import pytest

from chaos_agent.agent.providers.chaosblade.provider import ChaosbladeProvider
from chaos_agent.agent.providers.chaosblade.reconcile import (
    fingerprint_from_tool_call_args,
    format_batch_held_feedback,
    format_gate_feedback,
)
from chaos_agent.tools.markers import GATE_RECONCILE_BLOCKED_MARKER


_RETRY_ARGS = {
    "scope": "pod",
    "target": "network",
    "action": "delay",
    "namespace": "cms-demo",
    "names": ["accounting-7dc7b44956-krtm6"],
    "labels": {"app": "accounting"},
}


# ------------------------------------------------------- fingerprint identity

class TestFingerprintNormalization:
    """Registration and retry both derive from LLM tool_call args, whose
    shapes vary — the fingerprint must be shape- and order-insensitive."""

    def test_list_and_csv_shapes_same_fingerprint(self):
        csv_args = dict(
            _RETRY_ARGS,
            names="accounting-7dc7b44956-krtm6",
            labels="app=accounting",
        )
        assert fingerprint_from_tool_call_args(_RETRY_ARGS).matches(
            fingerprint_from_tool_call_args(csv_args)
        )

    def test_reordered_labels_match(self):
        a = fingerprint_from_tool_call_args({
            "scope": "pod", "target": "network", "action": "delay",
            "labels": "app=a,tier=b", "names": "p1",
        })
        b = fingerprint_from_tool_call_args({
            "scope": "pod", "target": "network", "action": "delay",
            "labels": "tier=b,app=a", "names": "p1",
        })
        assert a.matches(b)

    def test_scope_target_action_case_insensitive(self):
        a = fingerprint_from_tool_call_args({
            "scope": "Pod", "target": "Network", "action": "Delay",
        })
        b = fingerprint_from_tool_call_args({
            "scope": "pod", "target": "network", "action": "delay",
        })
        assert a.matches(b)

    def test_unified_key_face_only(self):
        # Phase-10 key-face uniformity: the generic layer reads the
        # unified scope/target/action keys; the ``blade_*`` legacy
        # aliases are provider-layer vocabulary and yield an EMPTY
        # scope-target-action dimension here (never a partial match).
        aliased = {
            "blade_scope": "pod", "blade_target": "network",
            "blade_action": "delay", "namespace": "ns",
        }
        fp = fingerprint_from_tool_call_args(aliased)
        assert fp.scope_target_action == ""


# --------------------------------------------------- provider-hook claim surface

class TestReconcileHookClaims:
    """The carrier's judgment material claims exactly its declared create
    tool and nothing else — the registry scan (first non-None wins) relies
    on the ``None`` pass-through for every other tool."""

    def test_declarations_name_the_create_and_reads(self):
        prov = ChaosbladeProvider()
        assert prov.reconcile_create_tool_names == frozenset({"blade_create"})
        assert prov.reconcile_read_tool_names == frozenset(
            {"blade_status", "blade_query_k8s", "kubectl_read"}
        )

    def test_fingerprint_hook_claims_only_blade_create(self):
        prov = ChaosbladeProvider()
        fp = prov.build_reconcile_fingerprint(
            "blade_create", dict(_RETRY_ARGS)
        )
        assert fp is not None
        assert fp.namespace == "cms-demo"
        assert fp.scope_target_action == "pod-network-delay"
        # Every other tool passes through (registry scan continues).
        assert prov.build_reconcile_fingerprint("kubectl", {"subcommand": "get"}) is None
        assert prov.build_reconcile_fingerprint("blade_destroy", {"uid": "x"}) is None

    @pytest.mark.asyncio
    async def test_hold_feedback_hook_claims_only_blade_create(self):
        prov = ChaosbladeProvider()
        fp = fingerprint_from_tool_call_args(_RETRY_ARGS)
        # blade_destroy / unknown tools: None (no probe side effects).
        assert await prov.reconcile_hold_feedback(
            "blade_destroy", fp, 1, 2,
        ) is None
        assert await prov.reconcile_hold_feedback(
            "kubectl", fp, 1, 2,
        ) is None

    def test_batch_held_hook_claims_only_blade_create(self):
        prov = ChaosbladeProvider()
        assert prov.reconcile_batch_held_feedback(
            "kubectl", "kubectl_read",
        ) is None
        text = prov.reconcile_batch_held_feedback("blade_create", "kubectl_read")
        assert text is not None
        assert "NOT executed" in text
        assert "blade_create" in text


# ------------------------------------------------------- fabricated feedback texts

class TestFeedbackTexts:
    """The fabricated texts carry the load-bearing wordings the generic
    three-state scan keys on: the GATE marker heads the hold feedback, and
    the batch-held notice claims non-execution."""

    def test_gate_feedback_headed_by_marker_and_carries_fix(self):
        fp = fingerprint_from_tool_call_args(_RETRY_ARGS)
        text = format_gate_feedback(fp, new_count=1, block_limit=2)
        assert text.startswith(GATE_RECONCILE_BLOCKED_MARKER)
        assert "UNKNOWN outcome" in text
        assert "DUPLICATE" in text
        assert "namespace=cms-demo" in text
        assert "is_hard_floor=False" in text
        assert "blade_status" in text
        assert "hold #1" in text

    def test_gate_feedback_splices_probe_section_before_fix(self):
        fp = fingerprint_from_tool_call_args(_RETRY_ARGS)
        probe = "Probe result: NO active experiment matches."
        text = format_gate_feedback(fp, 1, 2, probe)
        fix_idx = text.index("Fix (")
        assert text.index(probe) < fix_idx

    def test_batch_held_feedback_names_the_held_call(self):
        text = format_batch_held_feedback("kubectl_read")
        assert "kubectl_read" in text
        assert "NOT executed" in text
        assert format_batch_held_feedback("") != ""  # unknown-tool wording


# ------------------------------------------------------ promoted identity contract

class TestReExportIdentity:
    """The identity contract lives on neutral ground
    (``tools/request_identity.py``); the conflict-check module re-exports
    it so historical import paths keep resolving to the SAME objects —
    the ``cli.py`` UNCERTAIN_OUTCOME_MARKER re-export precedent."""

    def test_historical_import_path_is_same_object(self):
        from chaos_agent.agent.nodes.side_effect import _conflict_check

        from chaos_agent.tools.request_identity import (
            RequestFingerprint as NeutralFingerprint,
        )
        from chaos_agent.tools.request_identity import (
            build_request_fingerprint as neutral_build,
        )

        assert _conflict_check.RequestFingerprint is NeutralFingerprint
        assert _conflict_check.build_request_fingerprint is neutral_build
