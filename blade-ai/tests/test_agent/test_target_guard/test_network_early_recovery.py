"""W-67-8 network early-recovery lane: a standalone network reclaim finally has a gate.

The network family's window-internal pure inverse (``iptables -D`` /
``tc qdisc del``) had NO legal channel: ``iptables -D`` maps to no fault
family of its own (a delete is not an insert) and pairs with no ``-I``
inside the same command, so the ordinary gates (empty family →
no-bounded-recovery) refused it exactly when it was the right move,
leaving the timer as the only channel. recoverability already recognised
the network inverse (``_network_inverse`` / ``_iptables_rules_are_reversed``
/ ``_tc_rules_are_reversed``), but the carriers early-recovery lane had no
network branch — the judgement layer and the admission layer were not
wired together.

These tests lock the lane's four sides: the reclaim-rule parser's closed
shape, the gate that matches a standalone reclaim against the ledger's
armed network rules (network family only), the shared
``network_inserted_rules`` / ``network_deleted_rules`` extraction the
writer and the matcher both ride, and the ledger writer that records the
armed rules at arming time.
"""

from __future__ import annotations

from chaos_agent.agent.target_guard.carriers import (
    _pure_network_reclaim_rule,
    effective_target_from_registered_carrier,
)
from chaos_agent.agent.target_guard.recoverability import (
    network_deleted_rules,
    network_inserted_rules,
)
from chaos_agent.agent.target_guard.types import ApprovedTarget


# ---------------------------------------------------------------------------
# network_inserted_rules / network_deleted_rules: the shared extraction
# ---------------------------------------------------------------------------


class TestNetworkInsertedRules:
    """The writer face: what an arming command inserted."""

    def test_iptables_insert_short_form(self):
        rules = network_inserted_rules(
            "iptables -I INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]

    def test_iptables_append_long_form(self):
        rules = network_inserted_rules(
            "iptables --append FORWARD -d 10.0.0.2 -j REJECT"
        )
        assert rules == [("iptables", "forward -d 10.0.0.2 -j reject")]

    def test_ip6tables_is_a_distinct_binary(self):
        rules = network_inserted_rules(
            "ip6tables -I INPUT -s ::1 -j DROP"
        )
        assert rules == [("ip6tables", "input -s ::1 -j drop")]

    def test_tc_qdisc_add_extracts_device(self):
        rules = network_inserted_rules(
            "tc qdisc add dev eth0 root netem delay 100ms"
        )
        assert rules == [("tc", "eth0")]

    def test_multiple_rules_deduplicated(self):
        rules = network_inserted_rules(
            "iptables -I INPUT -s 10.0.0.1 -j DROP && "
            "iptables -I INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]

    def test_multiple_distinct_rules_kept_in_order(self):
        rules = network_inserted_rules(
            "iptables -I INPUT -s 10.0.0.1 -j DROP && "
            "iptables -I INPUT -s 10.0.0.2 -j DROP"
        )
        assert rules == [
            ("iptables", "input -s 10.0.0.1 -j drop"),
            ("iptables", "input -s 10.0.0.2 -j drop"),
        ]

    def test_delete_is_not_an_insert(self):
        rules = network_inserted_rules(
            "iptables -D INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == []

    def test_nft_is_excluded(self):
        # nft handles are runtime-only; static text cannot fingerprint them.
        rules = network_inserted_rules(
            "nft add rule inet filter input ip saddr 10.0.0.1 drop"
        )
        assert rules == []

    def test_no_network_binary_yields_empty(self):
        rules = network_inserted_rules("truncate -s 0 /var/tmp/fill.bin")
        assert rules == []

    def test_wait_flag_is_tolerated(self):
        rules = network_inserted_rules(
            "iptables --wait=5 -I INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]

    def test_redirection_tail_is_split_off(self):
        # The rule spec stops at a redirection (same split as
        # _iptables_rules_are_reversed).
        rules = network_inserted_rules(
            "iptables -I INPUT -s 10.0.0.1 -j DROP 2>/dev/null"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]


class TestNetworkDeletedRules:
    """The matcher face: what a standalone reclaim deletes."""

    def test_iptables_delete_short_form(self):
        rules = network_deleted_rules(
            "iptables -D INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]

    def test_iptables_delete_long_form(self):
        rules = network_deleted_rules(
            "iptables --delete INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == [("iptables", "input -s 10.0.0.1 -j drop")]

    def test_tc_qdisc_del_extracts_device(self):
        rules = network_deleted_rules(
            "tc qdisc del dev eth0 root"
        )
        assert rules == [("tc", "eth0")]

    def test_insert_is_not_a_delete(self):
        rules = network_deleted_rules(
            "iptables -I INPUT -s 10.0.0.1 -j DROP"
        )
        assert rules == []

    def test_write_read_single_source_byte_match(self):
        """The writer and matcher normalizations agree byte-for-byte.

        This is the #59 lesson applied to the network face: a rule指纹
        recorded at arming time must match the same rule extracted from a
        standalone reclaim, or the lane silently refuses the right move.
        """
        arming = "iptables -I INPUT -s 10.0.0.1 -j DROP"
        reclaim = "iptables -D INPUT -s 10.0.0.1 -j DROP"
        inserted = network_inserted_rules(arming)
        deleted = network_deleted_rules(reclaim)
        assert inserted == deleted
        assert inserted[0][1] == deleted[0][1]
        # Both are lowercase-normalized (the shared discipline).
        assert inserted[0] == ("iptables", "input -s 10.0.0.1 -j drop")


# ---------------------------------------------------------------------------
# _pure_network_reclaim_rule: the closed shape parser
# ---------------------------------------------------------------------------


class TestPureNetworkReclaimRule:
    """Exactly the standalone inverse shapes parse; all else fails closed."""

    def test_standalone_iptables_delete(self):
        assert _pure_network_reclaim_rule(
            "iptables -D INPUT -s 10.0.0.1 -j DROP"
        ) == ("iptables", "input -s 10.0.0.1 -j drop")

    def test_standalone_tc_qdisc_del(self):
        assert _pure_network_reclaim_rule(
            "tc qdisc del dev eth0 root"
        ) == ("tc", "eth0")

    def test_chroot_entry_pair_is_tolerated(self):
        assert _pure_network_reclaim_rule(
            "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
        ) == ("iptables", "input -s 10.0.0.1 -j drop")

    def test_host_binary_prefix_is_tolerated(self):
        assert _pure_network_reclaim_rule(
            "/host/iptables -D INPUT -s 10.0.0.1 -j DROP"
        ) == ("iptables", "input -s 10.0.0.1 -j drop")

    def test_single_sh_wrapper_is_unwrapped(self):
        assert _pure_network_reclaim_rule(
            "sh -c 'iptables -D INPUT -s 10.0.0.1 -j DROP'"
        ) == ("iptables", "input -s 10.0.0.1 -j drop")

    def test_insert_plus_delete_is_not_a_pure_reclaim(self):
        # A command that both inserts and deletes is a re-arm (second
        # mutation), not a reclaim.
        assert _pure_network_reclaim_rule(
            "iptables -I INPUT -s 10.0.0.2 -j DROP && "
            "iptables -D INPUT -s 10.0.0.1 -j DROP"
        ) == ("", "")

    def test_two_deletes_is_not_a_pure_reclaim(self):
        # "Exactly one" inverse — two deletes is not a standalone reclaim.
        assert _pure_network_reclaim_rule(
            "iptables -D INPUT -s 10.0.0.1 -j DROP && "
            "iptables -D INPUT -s 10.0.0.2 -j DROP"
        ) == ("", "")

    def test_timer_wrapped_inverse_is_not_a_pure_reclaim(self):
        # Arming a NEW timer is a mutation-shaped command, not a reclaim.
        assert _pure_network_reclaim_rule(
            "systemd-run --on-active=60s iptables -D INPUT -s 10.0.0.1 -j DROP"
        ) == ("", "")

    def test_composite_command_is_not_a_reclaim(self):
        assert _pure_network_reclaim_rule(
            "iptables -D INPUT -s 10.0.0.1 -j DROP && echo done"
        ) == ("", "")

    def test_non_network_command_fails_closed(self):
        assert _pure_network_reclaim_rule(
            "truncate -s 0 /var/tmp/fill.bin"
        ) == ("", "")

    def test_bare_iptables_list_is_not_a_reclaim(self):
        # A read-only list has no delete rule.
        assert _pure_network_reclaim_rule("iptables -L INPUT") == ("", "")


# ---------------------------------------------------------------------------
# The early-recovery gate: network lane
# ---------------------------------------------------------------------------


def _network_fixtures(
    *,
    rules: list[tuple[str, str]] | None = None,
    node: str = "node-a",
):
    """A network-approved target plus one armed debug-pod carrier artifact."""
    if rules is None:
        rules = [("iptables", "input -s 10.0.0.1 -j drop")]
    approved = ApprovedTarget(
        scope="node", namespace="", names=(node,), fault_target="network",
    )
    artifacts = [{
        "type": "debug_pod",
        "name": "node-debugger-n1-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "privileged": True,
        "status": "recovery_armed",
        "target": {"scope": "node", "name": node},
        "recovery_network_rules": rules,
        "recovery_deadline_epoch": 9_999_999_999.0,
    }]
    return approved, artifacts


class TestNetworkEarlyRecoveryGate:
    """The carrier gate resolves a standalone network reclaim against the armed rules."""

    def test_standalone_iptables_delete_on_armed_rule_is_allowed(self):
        approved, artifacts = _network_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.fault_target == "network"
        assert resolution.effective.scope == "node"
        assert resolution.effective.names == ("node-a",)
        assert "iptables -D INPUT" in resolution.effective.raw_command

    def test_standalone_tc_del_on_armed_device_is_allowed(self):
        approved, artifacts = _network_fixtures(
            rules=[("tc", "eth0")],
        )
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host tc qdisc del dev eth0 root"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.fault_target == "network"

    def test_reclaim_of_a_rule_this_task_did_not_arm_is_rejected(self):
        # Constrained to rules THIS TASK armed: a delete of an arbitrary
        # rule (another task's, a system rule) stays illegal.
        approved, artifacts = _network_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.99 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_non_network_family_never_uses_the_lane(self):
        # A disk approval with an armed-looking network record is a
        # mismatched-family situation; the lane is network-only by design.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",), fault_target="disk",
        )
        _, artifacts = _network_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_cross_node_reclaim_is_rejected(self):
        # The armed (node, binary, fingerprint) triple is the match unit:
        # a multi-carrier task that armed a rule on node-a must not let a
        # carrier bound to node-b delete that same rule on node-b.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a", "node-b"),
            fault_target="network",
        )
        artifacts = [
            {   # Armed on node-a through its own carrier.
                "type": "debug_pod",
                "name": "node-debugger-a-abc12",
                "namespace": "kubewiz",
                "uid": "uid-a",
                "privileged": True,
                "status": "recovery_armed",
                "target": {"scope": "node", "name": "node-a"},
                "recovery_network_rules": [
                    ("iptables", "input -s 10.0.0.1 -j drop"),
                ],
                "recovery_deadline_epoch": 9_999_999_999.0,
            },
            {   # A second carrier registered on node-b (active, no armed
                # rules of its own) carrying the pod name the exec targets.
                "type": "debug_pod",
                "name": "node-debugger-b-def34",
                "namespace": "kubewiz",
                "uid": "uid-b",
                "privileged": True,
                "status": "active",
                "target": {"scope": "node", "name": "node-b"},
            },
        ]
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-b-def34 -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_cleaned_void_record_still_matches(self):
        # The void aftermath of a dead carrier whose reversal died with it:
        # the lane is the only legal channel for that active recovery. The
        # exec rides a FRESH active carrier (the cleaned pod is gone and
        # cannot be exec'd into), but the armed record from the cleaned+void
        # artifact is still in the lane's match set.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",), fault_target="network",
        )
        artifacts = [
            {   # The dead carrier's void aftermath — holds the armed rules.
                "type": "debug_pod",
                "name": "node-debugger-n1-dead",
                "namespace": "kubewiz",
                "uid": "uid-dead",
                "privileged": True,
                "status": "cleaned",
                "recovery_void": True,
                "target": {"scope": "node", "name": "node-a"},
                "recovery_network_rules": [
                    ("iptables", "input -s 10.0.0.1 -j drop"),
                ],
            },
            {   # A fresh active carrier on the SAME node — the exec target.
                "type": "debug_pod",
                "name": "node-debugger-n1-fresh",
                "namespace": "kubewiz",
                "uid": "uid-fresh",
                "privileged": True,
                "status": "active",
                "target": {"scope": "node", "name": "node-a"},
            },
        ]
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-fresh -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved

    def test_same_node_different_carrier_still_matches(self):
        # The node triple survives carrier rotation: the armed carrier died
        # and a FRESH carrier was created on the SAME node — the reclaim
        # through the new carrier matches, because the rule lives on the
        # node, not the pod.
        approved, artifacts = _network_fixtures()
        fresh = dict(artifacts[0])
        fresh.update({
            "name": "node-debugger-n1-fresh",
            "uid": "uid-fresh",
            "status": "active",
        })
        fresh.pop("recovery_network_rules", None)
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-fresh -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            [artifacts[0], fresh],
            approved,
        )
        assert resolution.resolved

    def test_no_armed_rules_lane_does_not_fire(self):
        # An active carrier with no armed network rules: the lane has
        # nothing to match against, so the command falls through to the
        # ordinary gates (empty family for a bare -D).
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",), fault_target="network",
        )
        artifacts = [{
            "type": "debug_pod",
            "name": "node-debugger-n1-abc12",
            "namespace": "kubewiz",
            "uid": "uid-1",
            "privileged": True,
            "status": "active",
            "target": {"scope": "node", "name": "node-a"},
        }]
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host iptables -D INPUT -s 10.0.0.1 -j DROP"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved
