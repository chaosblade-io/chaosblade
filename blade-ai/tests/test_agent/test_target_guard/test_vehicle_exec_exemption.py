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
    "chaos_agent.tools.pod_discovery"
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
            "fault_target": "network",
            "fault_action": "corrupt",
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
                fault_scope="pod", fault_target="network",
                fault_action="corrupt",
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
                "labels": {}, "fault_target": "network",
                "fault_action": "corrupt",
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


class TestDriftCorrectionOwnerAnchorStaleness:
    """owner_names is dual-sourced (labels-matched owners AND the
    names→ownerReferences chain discovered for the generation anchor):
    a human-approved correction that changes the NAMES identity drops it —
    the anchor must never outlive the identity it was discovered against
    (case #39 follow-up)."""

    def _state(self, owner_names, resolved_names=()) -> dict:
        return {
            "fault_spec": {
                "namespace": "default", "scope": "pod",
                "names": ["web-abc-111"],
                "labels": {}, "fault_target": "cpu",
                "fault_action": "fullload",
                "params": {}, "params_flags": [], "duration_seconds": 0,
                "source": "test", "user_description": "",
            },
            "approved_target": {
                "scope": "pod", "namespace": "default",
                "names": ["web-abc-111"],
                "owner_names": list(owner_names),
                "resolved_names": list(resolved_names),
            },
        }

    def test_names_correction_drops_owner_names(self):
        state = self._state(("web", "web-abc"), ("web-abc-111",))
        eff = EffectiveTarget(
            scope="pod", namespace="default",
            names=("web-abc-222",),
        )
        delta = _apply_drift_correction(state, eff)
        at = delta["approved_target"]
        assert at["names"] == ["web-abc-222"]
        # Generation anchor dropped — it was discovered against the OLD
        # pod identity; the guard falls back to namespace-only anchoring
        # until the next approval re-discovers.
        assert at["owner_names"] == []
        # resolved_names is purely label-derived: a names-only
        # correction leaves the label identity (hence it) intact.
        assert at["resolved_names"] == ["web-abc-111"]

    def test_no_identity_change_keeps_owner_names(self):
        state = self._state(("web", "web-abc"))
        eff = EffectiveTarget(
            scope="pod", namespace="default",
            names=("web-abc-111",),
        )
        delta = _apply_drift_correction(state, eff)
        assert delta["approved_target"]["owner_names"] == ["web", "web-abc"]


class TestFaultBinaryMutationSegmentCoverage:
    """R25/G-9: the identity-review marker must survive COMPOUND payloads.

    The fault-binary branch keyed on the (single-layer-peeked) head
    token missed every fault binary riding past a ``;`` separator inside
    ``sh -c`` — a ``blade destroy x; stress-ng`` compound lost the
    marker, so the vehicle exemption (this file's screener face) AND the
    machinery≠mutation faces (execution_artifacts) would swallow a real
    fault binary. Segment-level detection (the shared syntax parser +
    the shared ``_FAULT_BINARIES`` set) closes the head-only blind spot;
    a pure-machinery compound (destroy + readonly companion) still earns
    no marker.
    """

    @pytest.mark.parametrize(
        "v_args,expected",
        [
            # fault binary past a ";" inside sh -c — head-only peek missed it
            (
                "tp -n ns -- sh -c "
                "'blade destroy aa11bb22cc33dd44; stress-ng --cpu 4'",
                True,
            ),
            (
                "tp -n ns -- sh -c "
                "'blade destroy aa11bb22cc33dd44; "
                "tc qdisc add dev eth0 root netem loss 10%'",
                True,
            ),
            # readonly head + mutating tail — the same blind spot
            (
                "tp -n ns -- sh -c 'cat /etc/hosts; iptables -A INPUT -j DROP'",
                True,
            ),
            # wrapper-prefixed compound (timeout resolves to the script)
            (
                "tp -n ns -- timeout 30 sh -c "
                "'blade destroy aa11bb22cc33dd44; stress-ng --cpu 4'",
                True,
            ),
            # pure machinery compound: destroy + readonly companion
            (
                "tp -n ns -- sh -c "
                "'blade destroy aa11bb22cc33dd44; cat /tmp/destroy.log'",
                False,
            ),
        ],
        ids=[
            "destroy-plus-stress-ng",
            "destroy-plus-tc",
            "readonly-head-plus-iptables",
            "timeout-wrapped-compound",
            "pure-machinery-compound",
        ],
    )
    def test_compound_fault_binary_marker(self, v_args, expected):
        eff = infer_effective_target(
            "kubectl", {"subcommand": "exec", "v_args": v_args},
        )
        assert eff.fault_binary_mutation is expected


class TestEscapePrimitiveSegmentCoverage:
    """R26/G-10: escape primitives must be caught in COMPOUND payloads.

    Same function, same head-only root, same fix shape as G-9
    (TestFaultBinaryMutationSegmentCoverage): the escape branch's
    single-layer peek reads only the FIRST command's head, so a payload
    with an innocent head and an escape primitive riding past a ``;``
    (``cat /etc/hosts; nsenter -t 1 -m sh``) never reached the
    SCOPE_ESCAPE legislation — it classified as a plain pod mutation,
    and when the exec target IS the approved pod the identity match
    passes the whole chain end-to-end (screener-level probe: route=pass
    for the compound while every direct/wrapped form is REJECT_BANNED).
    The escape branch's own comment promises "a single ``sh -c`` wrapper
    must not hide the escape primitive" — the compound form hid it
    anyway.

    Segment-level detection (the shared parser, same as G-9) routes any
    payload whose ANY segment head is nsenter/chroot/unshare into the
    escape branch, where the shared readonly judge (already
    segment-level, B46) rules the compound: an escape stage that is not
    read-only lands in SCOPE_ESCAPE; an all-readonly compound stays
    SCOPE_READONLY (the readonly-escape exemption keeps its width).
    """

    @pytest.mark.parametrize(
        "v_args",
        [
            "tp -n ns -- sh -c 'cat /etc/hosts; nsenter -t 1 -m sh'",
            "tp -n ns -- sh -c 'cat /etc/hosts; chroot /host bash'",
            "tp -n ns -- sh -c 'ls /tmp; unshare -m sh'",
            "tp -n ns -- sh -c 'cat /etc/os-release && nsenter -t 1 -m sh'",
            "tp -n ns -- timeout 30 sh -c 'cat f; nsenter -t 1 -m sh'",
        ],
        ids=[
            "cat-then-nsenter",
            "cat-then-chroot",
            "ls-then-unshare",
            "and-then-nsenter",
            "timeout-wrapped-compound",
        ],
    )
    def test_compound_escape_is_scope_escape(self, v_args):
        """复合逃逸形态必须落 SCOPE_ESCAPE（现行落 pod——红侧牙）。"""
        eff = infer_effective_target(
            "kubectl", {"subcommand": "exec", "v_args": v_args},
        )
        assert eff.scope == "__escape__", (
            f"{v_args!r}: an escape primitive riding past a ';' must hit "
            "the SCOPE_ESCAPE legislation, not classify as a pod mutation"
        )

    @pytest.mark.parametrize(
        "v_args",
        [
            # all-readonly compound WITHOUT an escape segment: unchanged
            "tp -n ns -- sh -c 'cat /etc/hosts; cat /proc/uptime'",
            # readonly compound WITH a readonly escape tail: the
            # readonly-escape exemption keeps its width (head-position
            # chroot was already exempt; the tail-position form must not
            # narrow it)
            "tp -n ns -- sh -c 'cat /etc/hosts; chroot /host cat /etc/os-release'",
        ],
        ids=[
            "readonly-compound-no-escape",
            "readonly-compound-with-readonly-escape-tail",
        ],
    )
    def test_readonly_compound_stays_readonly(self, v_args):
        """阴性对照：全 readonly 复合（含 readonly 逃逸尾）仍 READONLY。"""
        eff = infer_effective_target(
            "kubectl", {"subcommand": "exec", "v_args": v_args},
        )
        assert eff.scope == "__readonly__"


class TestEscapeRejectDetailNamesTheRealPrimitive:
    """R27/G-11c: the reject message must name the primitive that
    TRIGGERED the escape branch — the model's repair loop reads it.

    G-10 made the branch trigger segment-level, but the reject_detail
    still quotes the head-only ``escape_probe[0]`` — for a compound
    payload the message names the innocent head ('cat'/'blade'),
    misleading the model's self-repair direction. The message is the
    guidance surface; the named primitive must be the one the
    legislation actually caught.
    """

    @pytest.mark.parametrize(
        "v_args,expected_primitive",
        [
            # segment-level trigger: the message must name the RIDING
            # primitive, not the innocent head 'cat'
            (
                "tp -n ns -- sh -c 'cat /etc/hosts; nsenter -t 1 -m sh'",
                "nsenter",
            ),
            (
                "tp -n ns -- sh -c 'cat /etc/hosts; chroot /host bash'",
                "chroot",
            ),
            # head-position trigger: unchanged — the head IS the trigger
            (
                "tp -n ns -- nsenter -t 1 -m sh",
                "nsenter",
            ),
        ],
        ids=[
            "compound-names-riding-primitive",
            "compound-names-chroot",
            "direct-head-unchanged",
        ],
    )
    def test_reject_detail_names_trigger_primitive(self, v_args, expected_primitive):
        eff = infer_effective_target(
            "kubectl", {"subcommand": "exec", "v_args": v_args},
        )
        assert eff.scope == "__escape__"
        assert f"'{expected_primitive}'" in eff.reject_detail, (
            f"reject_detail must name the primitive the escape branch "
            f"actually caught ({expected_primitive!r}), got: "
            f"{eff.reject_detail!r}"
        )


class TestHiddenEscapeFormsAreScoped:
    """R33/G-12: escape primitives riding shell STRUCTURES.

    Command substitution (``$()``), backticks, subshells and command
    groups execute the primitive just as ``;`` does — the escape scope
    legislation must see them. Pre-fix probe (live): all forms below
    classified scope=pod and the screener passed them end-to-end with
    an approved-pod identity match.
    """

    @pytest.mark.parametrize(
        "v_args",
        [
            "tp -n ns -- sh -c 'cat f; $(nsenter -t 1 -m sh)'",
            "tp -n ns -- sh -c 'cat f; `nsenter -t 1 -m sh`'",
            "tp -n ns -- sh -c 'cat f; (nsenter -t 1 -m sh)'",
            "tp -n ns -- sh -c 'cat f; { nsenter -t 1 -m sh; }'",
            "tp -n ns -- sh -c 'cat f; \"$(nsenter -t 1 -m sh)\"'",
            "tp -n ns -- sh -c 'cat $(nsenter -t 1 -m cat /etc/shadow)'",
            "tp -n ns -- sh -c 'cat f; $(echo $(nsenter -t 1 -m sh))'",
        ],
        ids=[
            "cmdsub", "backtick", "subshell", "brace-group",
            "dq-wrapped", "arg-position", "nested",
        ],
    )
    def test_structure_riding_escape_is_scoped_and_named(self, v_args):
        eff = infer_effective_target(
            "kubectl", {"subcommand": "exec", "v_args": v_args},
        )
        assert eff.scope == "__escape__", (
            f"an escape primitive riding a shell structure executes just "
            f"as one past ';' — scope must be __escape__: {v_args!r}"
        )
        assert "'nsenter'" in eff.reject_detail, (
            f"reject_detail must name the caught primitive: "
            f"{eff.reject_detail!r}"
        )

    def test_chroot_riding_structure_is_scoped(self):
        eff = infer_effective_target(
            "kubectl",
            {"subcommand": "exec",
             "v_args": "tp -n ns -- sh -c 'echo done `chroot /host bash`'"},
        )
        assert eff.scope == "__escape__"

    def test_case_branch_escape_is_scoped(self):
        # R33/G-12b: the case pattern terminator `)` opens a branch
        # command list — an escape primitive in the branch executes.
        # Pre-fix probe (live): scope=pod and the screener passed it
        # end-to-end with an approved-pod identity match, even in the
        # destroy+escape compound form.
        for v_args in (
            "tp -n ns -- sh -c 'case x in a) nsenter -t 1 -m sh;; esac'",
            "tp -n ns -- sh -c 'case x in a) blade destroy aa11bb22cc33dd44;"
            "; b) nsenter -t 1 -m sh;; esac'",
        ):
            eff = infer_effective_target(
                "kubectl", {"subcommand": "exec", "v_args": v_args},
            )
            assert eff.scope == "__escape__", (
                f"a case-branch escape executes — scope must be "
                f"__escape__: {v_args!r}"
            )

    def test_escape_in_argument_tail_is_not_scoped(self):
        # R35/G-13: the closer's PARAMETER TAIL is the host command's
        # argument text — the shell never executes it as a command, so
        # it must not trigger the escape scope. Pre-fix probe (live):
        # the bare form classified scope=__escape__ and the screener
        # rejected it end-to-end (a legal form killed).
        for v_args in (
            "tp -n ns -- sh -c 'echo done $(date) nsenter -t 1 -m sh'",
            "tp -n ns -- sh -c 'echo done \"$(date) nsenter -t 1 -m sh\"'",
        ):
            eff = infer_effective_target(
                "kubectl", {"subcommand": "exec", "v_args": v_args},
            )
            assert eff.scope == "pod", (
                f"an escape primitive in an argument tail is parameter "
                f"text, not a command — scope must stay pod: {v_args!r}"
            )

    def test_escape_in_heredoc_body_is_not_scoped(self):
        # R36/G-14: a heredoc body is stdin text — a restore script
        # WRITTEN via the carrier-blessed quoted-heredoc form that merely
        # MENTIONS nsenter must not be classified as a host escape.
        # Pre-fix probe (live): scope=__escape__ → the screener rejected
        # it end-to-end with reject_banned naming 'nsenter'.
        for v_args in (
            # double-quoted delimiter — the exact carrier-blessed form
            "tp -n ns -- sh -c 'cat > /tmp/restore.sh <<\"EOF\"\n"
            "nsenter -t 1 -m sh -c \"umount /tmp/stale-mount\"\n"
            "EOF\necho done'",
            # bare delimiter variant
            "tp -n ns -- sh -c 'cat > /tmp/restore.sh <<EOF\n"
            "nsenter -t 1 -m sh\n"
            "EOF\necho done'",
        ):
            eff = infer_effective_target(
                "kubectl", {"subcommand": "exec", "v_args": v_args},
            )
            assert eff.scope == "pod", (
                f"a heredoc body is stdin text — an escape word inside "
                f"it must not scope the exec: {v_args!r}"
            )
