"""Recover-graph screener boundary lock (§4.6 of write-set-approval-contract).

design.md Non-goals pin the boundary: the recover screener is NOT
drift-guarded. A mechanism-namespace recovery write — deleting the
kube-system ConfigMap the injection created, removing the taint from the
node the mechanism touched — must never be blocked by an
``approved_target`` identity check, or recovery could fail while the
fault stays installed (the #5 topology-deadlock lesson from the other
direction).

This fixture locks that boundary behaviorally: the recover screener
(kubectl bound at RECOVER_VERIFY, mutations allowed for Layer 1) passes a
kube-system delete even when the run's approved target is a pod in
``default`` — a write the inject-phase guard would reject as secondary
namespace drift.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes._phase_screener import make_phase_screener
from chaos_agent.agent.providers import FaultProviderRegistry
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.target_guard.freeze import freeze_approved_target_from_spec


def _kubectl_call(call_id: str = "rc-1"):
    return {
        "name": "kubectl",
        "args": {
            "command": (
                "delete configmap drill-nxdomain-tmp -n kube-system "
                "--ignore-not-found"
            ),
        },
        "id": call_id,
        "type": "tool_call",
    }


def _recover_state(*calls) -> dict:
    return {
        "fault_spec": {"scope": "pod", "namespace": "default",
                       "names": ["victim-pod"]},
        # Layer 1 is the repair phase; read-only discipline gates only
        # Layer 2 (graph.py wiring).
        "recover_phase": "layer1_recovery",
        # The inject run's approval: pod in default. The mechanism's
        # kube-system writes live in its manifest — but recovery must not
        # depend on that manifest being present at all.
        "approved_target": freeze_approved_target_from_spec(
            FaultSpec(scope="pod", namespace="default", names=["victim-pod"],
                      fault_target="network", fault_action="loss"),
        ),
        "messages": [AIMessage(content="", tool_calls=list(calls))],
    }


def _recover_screener():
    # Same wiring as graph.py: mutations allowed for Layer 1 recovery,
    # read-only for Layer 2 verification.
    node, route = make_phase_screener(
        capability_phase="recover_verify",
        readonly=lambda s: s.get("recover_phase", "layer1_recovery")
        == "layer2_verification",
    )
    return node, route


class TestRecoverScreenerNoDriftGuard:
    @pytest.mark.asyncio
    async def test_mechanism_namespace_recovery_write_passes(self):
        FaultProviderRegistry.register_builtins()
        node, route = _recover_screener()

        out = await node(_recover_state(_kubectl_call()))

        # The recovery write to kube-system passes the recover screener:
        # no identity drift check exists on this path, by design. The
        # same write through the inject-phase guard would be a
        # REJECT_DRIFT — recovery must not inherit that gate.
        assert out["screener_route"] == "pass"
        assert route({**_recover_state(), **out}) == "pass"

    @pytest.mark.asyncio
    async def test_node_mechanism_recovery_write_passes(self):
        # Cluster-scoped mechanism shape (#4 family): removing a taint /
        # label from the mechanism node during recovery.
        FaultProviderRegistry.register_builtins()
        node, _ = _recover_screener()

        call = {
            "name": "kubectl",
            "args": {"command": "taint node drill-worker-1 "
                                "node.ops/pending-reboot=true:NoSchedule-"},
            "id": "rc-2",
            "type": "tool_call",
        }
        out = await node(_recover_state(call))
        assert out["screener_route"] == "pass"
