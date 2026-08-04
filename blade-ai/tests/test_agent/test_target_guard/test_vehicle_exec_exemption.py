"""Regression tests for the injection-vehicle exec exemption (task-5193538b).

A ``kubectl exec`` into an injection vehicle is access to the injection
MACHINERY, not an operation on the fault target. The incident task
diagnosed a failed injection inside the cluster's ChaosBlade tool pod;
each diagnostic exec was read as identity drift, AUTO mode approved every
interrupt, and every approval rewrote fault_spec toward whatever pod the
LAST exec had entered.

Vehicle identity is DATA-driven, never name-based:

  - the classifier stays stateless — it never guesses vehicles from pod
    names; it only marks ``fault_binary_mutation`` shapes that keep
    identity review,
  - the screener exempts execs whose pod is a task-registered vehicle
    (``is_vehicle_name``: debug_pod artifact, ``kubectl_exec_pod_name``,
    debug-pod-meta tags) or one confirmed by LIVE label-selector discovery
    against the cluster (``discover_tool_pods_cluster_wide``), cached in
    ``known_vehicle_pods`` / ``vehicle_probe_misses``,
  - ``K8sDriftPolicy.check_identity_drift`` short-circuits on the flag,
  - ``_apply_drift_correction`` never rewrites fault_spec toward a vehicle
    even when a drift verdict survives on a residual path.

The fault-binary mutation branch deliberately keeps identity review: a
fault binary inside a privileged / hostNetwork tool pod shapes the host.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from chaos_agent.agent.nodes.planning.tool_screener import (
    SCREENER_ROUTE_PASS,
    SCREENER_ROUTE_RETRY,
    _apply_drift_correction,
    tool_screener,
)
from chaos_agent.agent.spec.fault_spec import read_fault_spec
from chaos_agent.agent.target_guard import approved_from_dict, freeze_approved_target
from chaos_agent.agent.target_guard.classifier import (
    SCOPE_READONLY,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.drift_policy import K8sDriftPolicy
from chaos_agent.agent.target_guard.types import EffectiveTarget
from chaos_agent.config.settings import settings

_DISCOVERY = (
    "chaos_agent.agent.nodes.execute._injection_detection"
    ".discover_tool_pods_cluster_wide"
)


def _exec_command(*tokens: str) -> dict:
    return {"command": ["exec", *tokens]}


class TestClassifierStaysStateless:
    """The classifier never infers vehicle identity from pod names."""

    def test_incident_diagnostic_exec_is_not_flagged_by_name(self):
        # The incident shape: modprobe inside the tool pod. The classifier
        # classifies the inner command (pod-scope mutation) and leaves
        # vehicle identity to the stateful screener.
        eff = infer_effective_target(
            "kubectl",
            _exec_command(
                "chaosblade-tool-jlc95", "-n", "default", "--",
                "modprobe", "sch_netem",
            ),
        )
        assert eff.scope == "pod"
        assert eff.names == ("chaosblade-tool-jlc95",)
        assert eff.is_vehicle_exec is False
        assert eff.fault_binary_mutation is False

    def test_pure_stdio_attach_is_not_flagged_by_name(self):
        eff = infer_effective_target(
            "kubectl",
            _exec_command("chaosblade-tool-jlc95", "-n", "default"),
        )
        assert eff.scope == "pod"
        assert eff.is_vehicle_exec is False

    def test_regular_pod_exec_keeps_identity_review(self):
        eff = infer_effective_target(
            "kubectl",
            _exec_command("pod-a", "-n", "ns", "--", "rm", "/tmp/x"),
        )
        assert eff.scope == "pod"
        assert eff.names == ("pod-a",)
        assert eff.is_vehicle_exec is False
        assert eff.fault_binary_mutation is False

    def test_fault_binary_mutation_is_marked(self):
        # A fault binary inside a privileged/hostNetwork tool pod shapes the
        # host — the marker keeps identity review even for known vehicles.
        eff = infer_effective_target(
            "kubectl",
            _exec_command(
                "chaosblade-tool-jlc95", "-n", "default", "--",
                "tc", "qdisc", "add", "dev", "eth0", "root", "netem",
                "loss", "10%",
            ),
        )
        assert eff.scope == "pod"
        assert eff.is_vehicle_exec is False
        assert eff.fault_binary_mutation is True

    def test_readonly_inner_command_never_reaches_pod_scope(self):
        eff = infer_effective_target(
            "kubectl",
            _exec_command(
                "chaosblade-tool-jlc95", "-n", "default", "--",
                "find", "/lib/modules", "-name", "sch_netem*",
            ),
        )
        assert eff.scope == SCOPE_READONLY


class TestDriftPolicyVehicleExemption:
    def _approved_ark_system_pod(self):
        # The policy takes a hydrated ApprovedTarget, not the state dict.
        return approved_from_dict({
            "scope": "pod",
            "namespace": "ark-system",
            "names": ["kone-runtime-5b69b7b8bd-6swrx"],
            "labels": {},
            "blade_target": "network",
            "blade_action": "corrupt",
        })

    def test_vehicle_exec_skips_identity_drift(self):
        approved = self._approved_ark_system_pod()
        effective = EffectiveTarget(
            scope="pod", namespace="default",
            names=("chaosblade-tool-jlc95",),
            is_vehicle_exec=True,
        )
        assert K8sDriftPolicy().check_identity_drift(approved, effective) is None

    def test_same_shape_without_flag_still_drifts(self):
        # The exemption must be FLAG-driven: the identical target without the
        # flag is still a namespace+name drift (the pre-fix behaviour).
        approved = self._approved_ark_system_pod()
        effective = EffectiveTarget(
            scope="pod", namespace="default",
            names=("chaosblade-tool-jlc95",),
        )
        decision = K8sDriftPolicy().check_identity_drift(approved, effective)
        assert decision is not None


class TestScreenerVehicleExemption:
    @pytest.fixture(autouse=True)
    def _enforcing(self):
        orig = settings.target_guard_enforcing
        settings.target_guard_enforcing = True
        yield
        settings.target_guard_enforcing = orig

    @staticmethod
    def _state_for_exec(v_args: str, **extra) -> dict:
        state = {
            "messages": [AIMessage(
                content="",
                tool_calls=[{
                    "name": "kubectl",
                    "args": {"subcommand": "exec", "v_args": v_args},
                    "id": "tc-vehicle",
                }],
            )],
            "approved_target": freeze_approved_target(
                target={
                    "namespace": "ark-system",
                    "names": ["kone-runtime-5b69b7b8bd-6swrx"],
                },
                params={"scope": "pod"},
                blade_scope="pod", blade_target="network",
                blade_action="corrupt",
            ),
        }
        state.update(extra)
        return state

    @pytest.mark.asyncio
    async def test_registered_exec_tool_pod_is_exempt_without_probe(self):
        # A vehicle registered by THIS task (kubectl_exec_pod_name) is a
        # state fact — exempt without any cluster probe.
        state = self._state_for_exec(
            "custom-tool-pod-x1 -n default -- echo keepalive",
            kubectl_exec_pod_name="custom-tool-pod-x1",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_cluster_discovered_tool_pod_is_exempt(self):
        # The incident shape with NO task-side registration: the tool pod
        # belongs to the cluster's ChaosBlade DaemonSet. Live label-selector
        # discovery (a cluster fact, not a naming convention) exempts it and
        # persists the positive in ``known_vehicle_pods``.
        state = self._state_for_exec(
            "chaosblade-tool-jlc95 -n default -- modprobe sch_netem",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(
                _DISCOVERY,
                new_callable=AsyncMock,
                return_value=[("chaosblade-tool-jlc95", "default")],
            ) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_awaited_once()
        assert "chaosblade-tool-jlc95" in delta["known_vehicle_pods"]

    @pytest.mark.asyncio
    async def test_known_vehicle_cache_prevents_reprobe(self):
        # A positive from an earlier round must be honoured WITHOUT another
        # in-band cluster probe (self-poisoning under an active fault).
        state = self._state_for_exec(
            "chaosblade-tool-jlc95 -n default -- modprobe sch_netem",
            known_vehicle_pods=("chaosblade-tool-jlc95",),
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_miss_keeps_drift_review_and_caches(self):
        # An exec'd pod that discovery does NOT recognise is a genuine drift
        # candidate: the interrupt fires, and the negative is cached so the
        # cluster is never re-probed for the same name.
        state = self._state_for_exec(
            "some-other-pod -n default -- rm -rf /data",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ) as mock_interrupt,
            patch(
                _DISCOVERY, new_callable=AsyncMock, return_value=[],
            ) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        mock_interrupt.assert_called_once()
        mock_discover.assert_awaited_once()
        assert "some-other-pod" in delta["vehicle_probe_misses"]

    @pytest.mark.asyncio
    async def test_probe_miss_cache_prevents_reprobe(self):
        state = self._state_for_exec(
            "some-other-pod -n default -- rm -rf /data",
            vehicle_probe_misses=("some-other-pod",),
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ),
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            await tool_screener(state)
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_fault_binary_into_known_vehicle_keeps_review(self):
        # A fault binary inside a privileged/hostNetwork tool pod can shape
        # the HOST — the vehicle exemption must NOT swallow this shape even
        # when the pod is a proven vehicle.
        state = self._state_for_exec(
            "chaosblade-tool-jlc95 -n default -- "
            "tc qdisc add dev eth0 root netem loss 10%",
            kubectl_exec_pod_name="chaosblade-tool-jlc95",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        mock_interrupt.assert_called_once()
        mock_discover.assert_not_called()

    @pytest.mark.asyncio
    async def test_fault_binary_into_unregistered_pod_still_probes(self):
        # The probe must run on the fault-binary branch too: if the drift
        # verdict that survives there is human-approved, ``_apply_drift_
        # correction`` needs the discovered identity to refuse rewriting
        # fault_spec toward the machinery. Review is kept, but identity is
        # established and persisted.
        state = self._state_for_exec(
            "chaosblade-tool-jlc95 -n default -- "
            "tc qdisc add dev eth0 root netem loss 10%",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
                return_value="rejected",
            ) as mock_interrupt,
            patch(
                _DISCOVERY,
                new_callable=AsyncMock,
                return_value=[("chaosblade-tool-jlc95", "default")],
            ) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_RETRY
        mock_interrupt.assert_called_once()
        mock_discover.assert_awaited_once()
        assert "chaosblade-tool-jlc95" in delta["known_vehicle_pods"]

    @pytest.mark.asyncio
    async def test_exec_into_approved_target_never_probes(self):
        # An exec into the approved pod itself cannot drift — probing the
        # cluster for it would spend an in-band query (and under an active
        # network fault hit the severed API path) for nothing.
        state = self._state_for_exec(
            "kone-runtime-5b69b7b8bd-6swrx -n ark-system -- "
            "sysctl -w net.core.somaxconn=1024",
        )
        with (
            patch(
                "chaos_agent.agent.nodes.planning.tool_screener.interrupt",
            ) as mock_interrupt,
            patch(_DISCOVERY, new_callable=AsyncMock) as mock_discover,
        ):
            delta = await tool_screener(state)
        assert delta["screener_route"] == SCREENER_ROUTE_PASS
        mock_interrupt.assert_not_called()
        mock_discover.assert_not_called()


class TestDriftCorrectionNeverRewritesTowardVehicle:
    def _state(self, **extra) -> dict:
        state = {
            "fault_spec": {
                "namespace": "ark-system", "scope": "pod",
                "names": ["kone-runtime-5b69b7b8bd-6swrx"],
                "labels": {}, "blade_target": "network",
                "blade_action": "corrupt",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
        }
        state.update(extra)
        return state

    def test_correction_toward_registered_vehicle_is_skipped(self):
        state = self._state(kubectl_exec_pod_name="chaosblade-tool-jlc95")
        eff = EffectiveTarget(
            scope="pod", namespace="default",
            names=("chaosblade-tool-jlc95",),
        )
        assert _apply_drift_correction(state, eff) == {}
        # The spec must be untouched.
        spec = read_fault_spec(state)
        assert spec.names == ("kone-runtime-5b69b7b8bd-6swrx",)
        assert spec.namespace == "ark-system"

    def test_correction_toward_discovered_vehicle_is_skipped(self):
        state = self._state(known_vehicle_pods=("chaosblade-tool-jlc95",))
        eff = EffectiveTarget(
            scope="pod", namespace="default",
            names=("chaosblade-tool-jlc95",),
        )
        assert _apply_drift_correction(state, eff) == {}
        spec = read_fault_spec(state)
        assert spec.names == ("kone-runtime-5b69b7b8bd-6swrx",)

    def test_correction_toward_round_discovered_vehicle_is_skipped(self):
        # Vehicles discovered in the SAME screening round are not in state
        # yet when an interrupt resumes — the caller passes them in.
        state = self._state()
        eff = EffectiveTarget(
            scope="pod", namespace="default",
            names=("chaosblade-tool-jlc95",),
        )
        assert _apply_drift_correction(
            state, eff, frozenset({"chaosblade-tool-jlc95"}),
        ) == {}
        spec = read_fault_spec(state)
        assert spec.names == ("kone-runtime-5b69b7b8bd-6swrx",)

    def test_correction_toward_real_target_still_applies(self):
        state = self._state()
        eff = EffectiveTarget(
            scope="pod", namespace="ark-system",
            names=("kone-runtime-5b69b7b8bd-OTHER",),
        )
        delta = _apply_drift_correction(state, eff)
        assert delta["fault_spec"]["names"] == ["kone-runtime-5b69b7b8bd-OTHER"]
