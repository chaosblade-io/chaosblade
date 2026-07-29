"""A fault binary inside ``kubectl exec`` is pod-scoped unless it escapes.

``kubectl exec <pod> -- tc qdisc add dev eth0 ...`` runs in the target pod's own
network namespace, and that containment is a kernel property. Measured on the
test cluster: two pods of one Deployment reported ``/proc/self/ns/net`` as
``net:[4026532579]`` and ``net:[4026532741]`` — distinct from each other and from
the host's — and each saw only ``lo`` plus its own ``eth0@ifN`` veth end. Shaping
``eth0`` there cannot reach the node.

The classifier used to reject it anyway, keying on the binary name alone
(task-866648cc: ``kubectl exec <pod> -- tc qdisc add dev eth0 root netem loss
100%`` → ``REJECT_BANNED``, "mutates the host directly"). The code comment had
always scoped that rule to hostNetwork pods; the check never implemented the
condition, so eight skill cases whose documented injection step is
``kubectl exec ... tc netem ...`` could not execute as written.

The escape branch runs FIRST and still owns everything that reaches the host, so
these tests come in pairs: the pod-level form is allowed, and the same binary
behind ``chroot`` / ``nsenter`` / ``unshare`` (including one ``sh -c`` wrapper)
is still classified as an escape.
"""

import pytest

from chaos_agent.agent.target_guard.classifier import (
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    infer_effective_target,
)
from chaos_agent.agent.target_guard.types import ConfidenceLevel


def classify(v_args: str):
    return infer_effective_target(
        "kubectl", {"subcommand": "exec", "v_args": v_args}
    )


POD_LEVEL_MUTATIONS = [
    "tc qdisc add dev eth0 root netem duplicate 30%",
    "tc qdisc add dev eth0 root netem loss 80%",
    "tc qdisc add dev eth0 root netem delay 1000ms",
    "tc qdisc del dev eth0 root",
    "iptables -A OUTPUT -j DROP",
    "iptables -D OUTPUT -j DROP",
    "stress-ng --cpu 2",
    "dd if=/dev/zero of=/data/fill bs=1M count=100",
    "fallocate -l 1G /data/big",
]


class TestPodScopedFaultBinaries:
    @pytest.mark.parametrize("inner", POD_LEVEL_MUTATIONS)
    def test_classified_as_pod_not_escape(self, inner):
        eff = classify(f"demo-pod-0 -n arms-prom -- {inner}")
        assert eff.scope == "pod", f"{inner} must stay pod-scoped"
        assert eff.scope != SCOPE_ESCAPE

    def test_identity_is_preserved_for_blast_radius_comparison(self):
        """A pod-scoped verdict is only useful if the target is carried with it."""
        eff = classify(
            "demo-pod-0 -n arms-prom -- tc qdisc add dev eth0 root netem loss 80%"
        )
        assert eff.namespace == "arms-prom"
        assert eff.names == ("demo-pod-0",)
        assert eff.confidence is ConfidenceLevel.HIGH

    def test_the_exact_command_from_the_reported_failure(self):
        """task-866648cc, verbatim."""
        eff = classify(
            "arms-prometheus-ack-arms-prometheus-6686c9f99b-4ph4z -n arms-prom "
            "-- tc qdisc add dev eth0 root netem loss 100%"
        )
        assert eff.scope == "pod"
        assert eff.names == ("arms-prometheus-ack-arms-prometheus-6686c9f99b-4ph4z",)


class TestEscapeStillBlocked:
    """The security floor: anything that reaches the host stays rejected."""

    @pytest.mark.parametrize("primitive", [
        "chroot /host tc qdisc add dev eth0 root netem loss 50%",
        "nsenter -t 1 -n tc qdisc add dev eth0 root netem loss 50%",
        "unshare -n tc qdisc add dev eth0 root netem loss 50%",
        "chroot /host iptables -A OUTPUT -j DROP",
        "nsenter -t 1 -m -u -n -i dd if=/dev/zero of=/host/fill bs=1M count=1",
    ])
    def test_host_entry_primitives_are_escapes(self, primitive):
        eff = classify(f"dbg-pod -n default -- {primitive}")
        assert eff.scope == SCOPE_ESCAPE, f"{primitive} must remain an escape"

    @pytest.mark.parametrize("wrapped", [
        "sh -c 'chroot /host iptables -A OUTPUT -j DROP'",
        "bash -c 'nsenter -t 1 -n tc qdisc add dev eth0 root netem loss 50%'",
    ])
    def test_one_shell_wrapper_does_not_hide_the_escape(self, wrapped):
        """The wrapper-unwrapping must keep working after this change."""
        eff = classify(f"dbg-pod -n default -- {wrapped}")
        assert eff.scope == SCOPE_ESCAPE


class TestReadOnlyExemptionsIntact:
    @pytest.mark.parametrize("inner", [
        "tc --version",
        "tc -Version",
        "iptables -L",
        "iptables -S",
        "nft list",
        "cat /proc/net/dev",
    ])
    def test_probes_stay_readonly(self, inner):
        eff = classify(f"demo-pod-0 -n arms-prom -- {inner}")
        assert eff.scope == SCOPE_READONLY, f"{inner} is a read, not a mutation"

    def test_host_readonly_probe_through_chroot_still_allowed(self):
        """Phase 1 must be able to verify host preconditions."""
        eff = classify("dbg-pod -n default -- chroot /host cat /etc/os-release")
        assert eff.scope == SCOPE_READONLY


class TestDocumentedSkillCaseCommandsAreExecutable:
    """The eight cases this unblocked all inject through ``kubectl exec ... tc``."""

    @pytest.mark.parametrize(("case", "inner"), [
        ("Pod_网络故障_网络包重复", "tc qdisc add dev eth0 root netem duplicate 30%"),
        ("Pod_网络故障_网络包损坏", "tc qdisc add dev eth0 root netem corrupt 30%"),
        ("Pod_网络延迟_网络请求延迟", "tc qdisc add dev eth0 root netem delay 1000ms"),
    ])
    def test_injection_step_is_not_rejected(self, case, inner):
        eff = classify(f"demo-pod-0 -n arms-prom -- {inner}")
        assert eff.scope == "pod", f"{case} injection step must be executable"

    def test_recovery_step_is_not_rejected(self):
        """Every one of those cases recovers with the same ``tc qdisc del``."""
        eff = classify("demo-pod-0 -n arms-prom -- tc qdisc del dev eth0 root")
        assert eff.scope == "pod"
