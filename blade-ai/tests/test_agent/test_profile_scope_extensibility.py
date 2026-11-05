"""Readiness tests: the profile / scope axis accepts a third environment.

Structural-preparation guard. These tests register a *fake* third profile and
scope (``"vm"``) into every axis registry and assert the N-ary seams honour it
without any binary ``k8s``/``host`` branch:

  1. scope -> profile resolution (``profile_of_scope`` / ``profile_for_spec``)
  2. guard cross-profile drift (a third profile is coarse drift vs. k8s)
  3. side-effect node capture gate driven by ``observer.can_capture(spec)``
  4. network feasibility fail-open for a non-k8s profile

Nothing here ships a real third environment — the fake registrations are torn
down by the autouse fixture, so the production registries are untouched.
"""
from __future__ import annotations

import pytest

from chaos_agent.agent.nodes.side_effect import _side_effect_detectors as sed
from chaos_agent.agent.nodes.side_effect._side_effect_detectors import (
    PostInjectState,
    SideEffectSnapshot,
)
from chaos_agent.agent.nodes.side_effect.se_snapshot import se_snapshot_node
from chaos_agent.agent.spec import fault_registry as fr
from chaos_agent.agent.spec.fault_registry import (
    FaultFamily,
    profile_of_scope,
)
from chaos_agent.agent.spec.fault_spec import FaultSpec
from chaos_agent.agent.spec.feasibility import profile_for_spec
from chaos_agent.agent.target_guard.guard import target_drift_guard
from chaos_agent.agent.target_guard.types import (
    ApprovedTarget,
    ConfidenceLevel,
    EffectiveTarget,
    GuardVerdict,
)

VM_PROFILE = "vm"
VM_SCOPE = "vm"


@pytest.fixture(autouse=True)
def _register_fake_vm_environment():
    """Register a throwaway third profile/scope and restore every registry."""
    fam_snapshot = dict(fr._REGISTRY)
    obs_snapshot = dict(sed._OBSERVERS)

    fr.register_family(
        FaultFamily(
            family_id="vm_fake",
            scopes=(VM_SCOPE,),
            carrier_types=("vm_carrier",),
            cluster_scoped=(VM_SCOPE,),
            profile=VM_PROFILE,
        )
    )
    try:
        yield
    finally:
        fr._REGISTRY.clear()
        fr._REGISTRY.update(fam_snapshot)
        sed._OBSERVERS.clear()
        sed._OBSERVERS.update(obs_snapshot)


def test_profile_resolution_honours_third_family():
    """A family declaring ``profile="vm"`` makes its scope resolve to that
    profile — the axis is N-ary, not host-vs-everything-else."""
    assert profile_of_scope(VM_SCOPE) == VM_PROFILE
    assert profile_for_spec(FaultSpec(scope=VM_SCOPE)) == VM_PROFILE
    # Built-ins are unchanged.
    assert profile_of_scope("pod") == "k8s"
    assert profile_of_scope("host") == "host"


def test_guard_flags_third_profile_as_cross_profile_drift():
    """Approving a k8s pod then acting on a ``vm`` scope is coarse drift —
    the guard compares profiles, not a host boolean."""
    approved = ApprovedTarget(scope="pod", namespace="default")
    effective = EffectiveTarget(
        scope=VM_SCOPE,
        namespace="",
        confidence=ConfidenceLevel.HIGH,
        raw_command="blade create vm-cpu",
    )
    decision = target_drift_guard(effective, approved)
    assert decision.verdict == GuardVerdict.REJECT_DRIFT


async def test_se_snapshot_not_skipped_when_namespace_not_required():
    """An observer whose ``can_capture`` returns True is never skipped for
    lacking a namespace — the node consults the observer, not ``== k8s``."""

    class _VmObserver:
        profile = VM_PROFILE

        def can_capture(self, spec):
            return True

        async def capture_base_snapshot(self, spec, kubeconfig, task_id="", *, xport_target=None):
            return SideEffectSnapshot(captured_at="t", namespace="")

        async def fetch_post_inject_state(self, spec, kubeconfig, injection_start_time, task_id="", *, xport_target=None):
            return PostInjectState(captured_at="t")

        def summarize(self, snapshot):
            return ("vm state captured", {"vm": 1})

    sed.register_observer(_VmObserver())

    state = {
        "fault_spec": FaultSpec(scope=VM_SCOPE, namespace="").to_dict(),
        "kubeconfig": "",
        "task_id": "",
    }
    result = await se_snapshot_node(state)
    assert "se_snapshot" in result


async def test_network_feasibility_fail_open_for_third_profile():
    """Network feasibility is only assessed on k8s; a third profile fails open
    (returns None) rather than being treated as host by a literal check."""
    from chaos_agent.agent.spec._feasibility_checkers import NetworkFeasibilityChecker

    checker = NetworkFeasibilityChecker()
    spec = FaultSpec(scope=VM_SCOPE, namespace="", fault_target="network")
    assert await checker.assess(spec, "") is None
