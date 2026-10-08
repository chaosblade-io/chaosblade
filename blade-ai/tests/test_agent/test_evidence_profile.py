from chaos_agent.agent.evidence import EvidenceProfile, host_evidence_supplements
from chaos_agent.agent.spec.fault_spec import FaultSpec


def test_k8s_evidence_profile_requires_identity_primary_and_cross_metric():
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("api-0",), fault_target="cpu"), "k8s",
    )

    incomplete = profile.coverage([
        {"description": "Pod CPU", "command": "kubectl top pod api-0 -n prod"},
    ])
    assert incomplete.missing == ("independent_cross_metric",)

    complete = profile.coverage([
        {"description": "Pod CPU", "command": "kubectl top pod api-0 -n prod"},
        {"description": "Pod conditions", "command": "kubectl describe pod api-0 -n prod"},
    ])
    assert complete.complete is True


def test_host_evidence_profile_requires_explicit_host_identity():
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="node", fault_target="mem"), "host",
    )
    coverage = profile.coverage([
        {"description": "Host memory", "command": "free -m"},
        {"description": "Host load", "command": "uptime"},
    ])

    assert "target_identity" in coverage.missing


def test_a_single_observation_cannot_satisfy_primary_and_cross_evidence():
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="host", fault_target="mem"), "host",
    )

    coverage = profile.coverage([
        {"description": "Host identity", "command": "hostname"},
        {"description": "Host memory", "command": "free -m"},
    ])

    assert coverage.missing == ("independent_cross_metric",)


def test_host_evidence_supplements_adds_identity_and_cross_for_cpu():
    supplements = host_evidence_supplements(
        "cpu", {"target_identity", "independent_cross_metric"}, "vmstat -s",
    )

    assert supplements == [
        ("Host identity", ("hostname",)),
        ("Host cross-check", ("uptime",)),
    ]


def test_host_evidence_supplements_skips_when_full_command_present():
    # The full command already appears in the evidence, so nothing is added.
    supplements = host_evidence_supplements(
        "disk", {"target_identity", "independent_cross_metric"},
        "ran hostname on host-01 then df -h /data",
    )

    assert supplements == []


def test_host_evidence_supplements_matches_full_command_not_short_token():
    # "df" appears as a substring of "pdf-report", but the FULL "df -h" command
    # does not — so the disk cross-check must still be supplemented.
    supplements = host_evidence_supplements(
        "disk", {"independent_cross_metric"}, "parsed pdf-report metrics",
    )

    assert supplements == [("Host cross-check", ("df", "-h"))]


def test_host_evidence_supplements_unknown_target_yields_no_cross():
    supplements = host_evidence_supplements(
        "gpu", {"target_identity", "independent_cross_metric"}, "",
    )

    assert supplements == [("Host identity", ("hostname",))]


def test_k8s_network_primary_metric_accepts_timing_probe_shapes():
    """Case #58 (inject-3dae7b4f): the K8s-channel primary network metric
    arrives as a timing probe, not a host socket dump.

    Pre-fix, ``curl -w connect=%{time_connect}`` matched zero entries of the
    network vocabulary (all host-side commands: ss / netstat / ip -s), so
    every k8s:*:network baseline was structurally judged incomplete — the
    capture itself was correct, only the judge could not read it.
    """
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("web-0",), fault_target="network"), "k8s",
    )

    coverage = profile.coverage([
        {
            "description": "TCP connect timing to target pod",
            "command": "kubectl exec client -- curl -s -o /dev/null "
                       "-w 'connect=%{time_connect}' http://web-0:80",
            "stdout": "connect=0.000823",
        },
    ])
    assert "primary_metric" in coverage.covered
    assert "primary_metric" not in coverage.missing


def test_k8s_network_primary_metric_accepts_qdisc_and_netem_shapes():
    """tc-side observation shapes from the netem injection family."""
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("web-0",), fault_target="network"), "k8s",
    )

    coverage = profile.coverage([
        {"command": "kubectl exec web-0 -- tc qdisc show dev eth0",
         "stdout": "qdisc netem state UNKNOWN"},
    ])
    assert "primary_metric" in coverage.covered


def test_k8s_channel_is_a_union_host_vocabulary_still_counts():
    """``kubectl exec <pod> -- ss -s`` is a legal K8s observation.

    The K8s additions must MERGE onto the base table, not replace it —
    otherwise a container-side socket dump would lose coverage it used to
    have.
    """
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("web-0",), fault_target="network"), "k8s",
    )

    coverage = profile.coverage([
        {"command": "kubectl exec web-0 -- ss -s"},
    ])
    assert "primary_metric" in coverage.covered


def test_host_channel_primary_vocabulary_is_unchanged():
    """Regression: the K8s additions must not leak into the host channel."""
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="host", fault_target="network"), "host",
    )

    # Base vocabulary still works on the host channel.
    covered = profile.coverage([{"command": "ss -s"}])
    assert "primary_metric" in covered.covered


def test_k8s_non_network_domains_still_rely_on_base_vocabulary():
    """Only network needed K8s additions — cpu/mem/disk/process K8s baselines
    run kubectl top / describe / df / ps, whose vocabulary is already in the
    base tables. Pinned so the "network-only additions" decision stays a
    documented fact rather than an accident.
    """
    cpu_profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("api-0",), fault_target="cpu"), "k8s",
    )
    coverage = cpu_profile.coverage([
        {"command": "kubectl top pod api-0 -n prod", "stdout": "api-0 12m 128Mi"},
    ])
    assert "primary_metric" in coverage.covered


def test_unknown_target_on_k8s_falls_back_to_target_word():
    """Unknown fault targets keep the (target,) fallback on either channel."""
    profile = EvidenceProfile.for_fault(
        FaultSpec(scope="pod", names=("worker-0",), fault_target="gpu"), "k8s",
    )

    coverage = profile.coverage([
        # Pod name deliberately avoids the target word: the fallback pattern
        # is a bare substring ("gpu"), which would match a pod named gpu-0.
        {"command": "kubectl top pod worker-0 -n prod", "stdout": "worker-0 12m 128Mi"},
    ])
    # No additions exist for unknown targets; the fallback ("gpu",) does not
    # match this observation, so primary stays missing — unchanged behavior.
    assert "primary_metric" in coverage.missing


def test_execution_location_suffix_cannot_fake_coverage():
    """The displayed location must not count as evidence.

    ``display_command`` now appends WHERE a command ran (``uptime  [kubewiz_host
    -> network-node…]``) and baseline observations store that string in their
    ``command`` field. Coverage matching is substring-based over generic
    vocabulary, so a host named ``network-node-1`` would otherwise mark the
    network primary metric as covered on EVERY observation — silently
    suppressing the deterministic supplement probes.
    """
    from chaos_agent.agent.evidence import EvidenceProfile
    from chaos_agent.agent.spec.fault_spec import FaultSpec
    from chaos_agent.transports import PROFILE_HOST
    from chaos_agent.transports.base import TransportTarget
    from chaos_agent.transports.channels import KubewizHostChannel

    spec = FaultSpec(scope="host", fault_target="network")
    profile = EvidenceProfile.for_fault(spec, PROFILE_HOST)

    target = TransportTarget(
        scope="host", host_name="network-node-1", kubewiz_profile="p",
        channel_override="kubewiz_host",
    )
    shown = KubewizHostChannel().display_command(["uptime"], target)
    assert "network-node" in shown, "premise: the suffix carries the host name"

    coverage = profile.coverage([{"description": "load check", "command": shown}])
    assert "primary_metric" not in coverage.covered, (
        "the location suffix must be stripped before coverage matching"
    )
    # A real network observation still counts.
    coverage = profile.coverage([{"description": "socket stats", "command": "ss -s"}])
    assert "primary_metric" in coverage.covered


def test_strip_execution_location_leaves_ordinary_text_alone():
    from chaos_agent.transports import strip_execution_location

    assert strip_execution_location("kubectl get pods  [kubeconfig]") == "kubectl get pods"
    # Only a trailing, double-space-separated bracket group is a location.
    assert strip_execution_location("jsonpath={.items[0].name}") == "jsonpath={.items[0].name}"
    assert strip_execution_location("iostat -xd 1 2") == "iostat -xd 1 2"


def test_location_suffix_stripped_for_every_record_shape():
    """``_record_text`` handles dict / str / other — dict and str must strip.

    The dict shape is what production passes today; the str branch is defensive.
    Leaving it unstripped means the guard silently stops holding the day a caller
    passes a plain string.

    The third branch (``str(record)`` on anything else) is best-effort only: the
    suffix pattern is END-ANCHORED on purpose — a non-anchored one would eat
    legitimate bracketed content such as ``jsonpath={.items[0].name}`` — so a
    suffix buried inside a repr is out of reach. Pinned here so the limit is a
    known decision rather than a surprise.
    """
    from chaos_agent.agent.evidence import _record_text

    suffix = "  [kubewiz_host -> network-node-1]"
    assert "network-node" not in _record_text(f"uptime{suffix}")
    assert "network-node" not in _record_text({"command": f"uptime{suffix}"})
    # The semantic command itself is untouched.
    assert "uptime" in _record_text(f"uptime{suffix}")
    # Documented limit: a repr-wrapped suffix is no longer at end of string.
    assert "network-node" in _record_text(("uptime" + suffix,))
