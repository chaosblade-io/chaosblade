"""Case #59 early-recovery lane: a standalone disk reclaim finally has a gate.

Pod_被驱逐重建_DiskPressure (case #59) armed a ``systemd-run`` rollback timer
whose payload was ``truncate -s 0 <path>``, then needed to recover EARLY when
the operator judged the drill sufficient. The pure inverse had NO legal
channel: ``truncate`` maps to no fault family (empty-family rejection) and a
bare ``fallocate -d`` pairs with no fill in the same command (no-bounded-
recovery rejection). The case doc prescribed a recovery the legislation made
structurally unreachable.

These tests lock the lane's three sides: the reclaim-path parser's closed
shape, the gate that matches a standalone reclaim against the ledger's armed
fill path (disk family only), and the shared ``disk_fill_path`` extraction the
writer and the matcher both ride.
"""

from __future__ import annotations

from chaos_agent.agent.target_guard import carriers
from chaos_agent.agent.target_guard.carriers import (
    _pure_disk_reclaim_path,
    effective_target_from_registered_carrier,
)
from chaos_agent.agent.target_guard.recoverability import (
    disk_fill_path,
    dm_mapping_name,
)
from chaos_agent.agent.target_guard.types import ApprovedTarget


class TestPureDiskReclaimPath:
    """Exactly the four standalone inverse shapes parse; all else fails closed."""

    def test_truncate_short_form(self):
        assert _pure_disk_reclaim_path(
            "truncate -s 0 /var/lib/kubelet/fill.bin"
        ) == "/var/lib/kubelet/fill.bin"

    def test_truncate_long_equals_form(self):
        assert _pure_disk_reclaim_path(
            "truncate --size=0 /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_truncate_long_spaced_form(self):
        assert _pure_disk_reclaim_path(
            "truncate --size 0 /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_fallocate_punch_form(self):
        assert _pure_disk_reclaim_path(
            "fallocate -d /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_chroot_entry_pair_is_tolerated(self):
        # The exec channel's carrier shape: ``chroot /host <cmd>``.
        assert _pure_disk_reclaim_path(
            "chroot /host truncate -s 0 /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"
        assert _pure_disk_reclaim_path(
            "chroot /host fallocate -d /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_host_binary_prefix_is_tolerated(self):
        assert _pure_disk_reclaim_path(
            "/host/truncate -s 0 /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_single_sh_wrapper_is_unwrapped(self):
        assert _pure_disk_reclaim_path(
            "sh -c 'truncate -s 0 /var/tmp/fill.bin'"
        ) == "/var/tmp/fill.bin"

    def test_quoted_path_is_stripped(self):
        assert _pure_disk_reclaim_path(
            'truncate -s 0 "/var/tmp/fill with space"'
        ) == "/var/tmp/fill with space"

    def test_nonzero_truncate_is_not_a_reclaim(self):
        # Growing or resizing to 1M is a fill, not the family inverse.
        assert _pure_disk_reclaim_path("truncate -s 1M /var/tmp/fill.bin") == ""

    def test_fallocate_allocate_is_not_a_reclaim(self):
        assert _pure_disk_reclaim_path("fallocate -l 1G /var/tmp/fill.bin") == ""

    def test_rm_is_not_a_reclaim(self):
        assert _pure_disk_reclaim_path("rm -f /var/tmp/fill.bin") == ""

    def test_composite_command_is_not_a_reclaim(self):
        # A second statement means something else may mutate; fail closed.
        assert _pure_disk_reclaim_path(
            "truncate -s 0 /var/tmp/fill.bin && echo done"
        ) == ""

    def test_timer_wrapped_inverse_is_not_a_pure_reclaim(self):
        # Arming a NEW timer is a mutation-shaped command, not a reclaim.
        assert _pure_disk_reclaim_path(
            "systemd-run --on-active=60s truncate -s 0 /var/tmp/fill.bin"
        ) == ""

    def test_multi_path_truncate_fails_closed(self):
        assert _pure_disk_reclaim_path(
            "truncate -s 0 /var/tmp/a.bin /var/tmp/b.bin"
        ) == ""


def _disk_fixtures(*, fill_path: str = "/var/tmp/fill.bin"):
    """A disk-approved target plus one armed debug-pod carrier artifact."""
    approved = ApprovedTarget(
        scope="node", namespace="", names=("node-a",), fault_target="disk",
    )
    artifacts = [{
        "type": "debug_pod",
        "name": "node-debugger-n1-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "privileged": True,
        "status": "recovery_armed",
        "target": {"scope": "node", "name": "node-a"},
        "recovery_fill_path": fill_path,
        "recovery_deadline_epoch": 9_999_999_999.0,
    }]
    return approved, artifacts


class TestEarlyRecoveryGate:
    """The carrier gate resolves a standalone reclaim against the armed fill."""

    def test_standalone_truncate_on_armed_path_is_allowed(self):
        approved, artifacts = _disk_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.fault_target == "disk"
        assert resolution.effective.scope == "node"
        assert resolution.effective.names == ("node-a",)
        # The raw command keeps the exec line verbatim for the audit trail.
        assert "truncate -s 0 /var/tmp/fill.bin" in resolution.effective.raw_command

    def test_standalone_fallocate_on_armed_path_is_allowed(self):
        approved, artifacts = _disk_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host fallocate -d /var/tmp/fill.bin"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved

    def test_reclaim_of_a_path_this_task_did_not_fill_is_rejected(self):
        # Constrained to paths THIS TASK armed: a truncate of an arbitrary
        # file (another task's fill, a system file) stays illegal.
        approved, artifacts = _disk_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host truncate -s 0 /etc/hostname"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved
        # Falls through to the armed-carrier rejection, which now names the
        # early-recovery alternative (the path mismatch is the point).
        assert "recovery_armed" in resolution.detail
        assert "truncate -s 0" in resolution.detail

    def test_non_disk_family_never_uses_the_lane(self):
        # A network approval with an armed-looking fill record is a
        # mismatched-family situation; the lane is disk-only by design.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",), fault_target="network",
        )
        _, artifacts = _disk_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_cross_node_reclaim_is_rejected(self):
        # The armed (node, path) pair is the match unit, not the bare path:
        # a multi-carrier task that armed a fill on node-a must not let a
        # carrier bound to node-b truncate that same path on node-b — the
        # reclaim would miss the armed node's fault entirely while clobbering
        # the other node's file. Adversarial self-review of the #59 fix: the
        # path-only set opened exactly this cross-node lane.
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a", "node-b"),
            fault_target="disk",
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
                "recovery_fill_path": "/var/tmp/fill.bin",
                "recovery_deadline_epoch": 9_999_999_999.0,
            },
        ]
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-b-def34 -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            [
                artifacts[0],
                {   # A second carrier registered on node-b (active, no armed
                    # fill of its own) carrying the same pod name the exec
                    # targets.
                    "type": "debug_pod",
                    "name": "node-debugger-b-def34",
                    "namespace": "kubewiz",
                    "uid": "uid-b",
                    "privileged": True,
                    "status": "active",
                    "target": {"scope": "node", "name": "node-b"},
                },
            ],
            approved,
        )
        assert not resolution.resolved
        # Falls through to the empty-family wording: a bare truncate maps to
        # no fault family, and no armed (node-b, path) pair rescues it.
        assert "does not map to any fault family" in resolution.detail

    def test_same_node_different_carrier_still_matches(self):
        # The node pair survives carrier rotation: the armed carrier died
        # (#59's DiskPressure eviction) and a FRESH carrier was created on
        # the SAME node — the early-recovery reclaim through the new carrier
        # matches, because the fill lives on the node, not the pod.
        approved, artifacts = _disk_fixtures()
        fresh = dict(artifacts[0])
        fresh.update({
            "name": "node-debugger-n1-fresh",
            "uid": "uid-fresh",
            "status": "active",
        })
        fresh.pop("recovery_fill_path", None)
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-fresh -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            [artifacts[0], fresh],
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.names == ("node-a",)

    def test_void_cleaned_record_keeps_the_lane_open(self):
        # The full #59 aftermath: the armed carrier DIED before finalize's
        # cleanup, which stamped the carrier-resident reversal void and set
        # the record cleaned (recovery_fill_path stays on the record). The
        # void stamp's meaning is "self-recovery is gone, ACTIVE recovery is
        # required" — so a fresh carrier on the same node executing the pure
        # inverse must still resolve. Without this, the recover graph's
        # re-issued truncate hits the empty-family rejection at exactly the
        # moment the void stamp says to go recover (adversarial self-review
        # cascade: writer stamps void, matcher ignores it).
        approved, artifacts = _disk_fixtures()
        dead = dict(artifacts[0])
        dead.update({"status": "cleaned", "recovery_void": True})
        fresh = dict(artifacts[0])
        fresh.update({
            "name": "node-debugger-n1-fresh",
            "uid": "uid-fresh",
            "status": "active",
        })
        fresh.pop("recovery_fill_path", None)
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-fresh -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            [dead, fresh],
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.names == ("node-a",)

    def test_normally_cleaned_record_stays_closed(self):
        # A record cleaned WITHOUT the void stamp is a fill already reclaimed
        # (timer fired, or a manual delete of a live carrier whose host-managed
        # timer may still be counting): nothing to actively recover, so the
        # lane must stay shut — fail-closed for anything unproven.
        approved, artifacts = _disk_fixtures()
        cleaned = dict(artifacts[0])
        cleaned.update({"status": "cleaned"})
        fresh = dict(artifacts[0])
        fresh.update({
            "name": "node-debugger-n1-fresh",
            "uid": "uid-fresh",
            "status": "active",
        })
        fresh.pop("recovery_fill_path", None)
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-fresh -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            [cleaned, fresh],
            approved,
        )
        assert not resolution.resolved
        assert "does not map to any fault family" in resolution.detail

    def test_active_carrier_without_armed_fill_stays_rejected(self):
        # No armed rollback record → no fill path in the ledger → the pure
        # inverse keeps its pre-#59 fate (empty family, fail closed).
        approved, artifacts = _disk_fixtures()
        artifacts[0]["status"] = "active"
        artifacts[0].pop("recovery_fill_path")
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host truncate -s 0 /var/tmp/fill.bin"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved
        # The empty-family wording, not the armed-carrier one.
        assert "does not map to any fault family" in resolution.detail


class TestDiskFillPathSingleSource:
    """Writer and matcher share the same extraction rules (public face)."""

    def test_dd_of_form(self):
        assert disk_fill_path(
            "dd if=/dev/zero of=/var/tmp/fill.bin bs=1M count=512"
        ) == "/var/tmp/fill.bin"

    def test_fallocate_length_form(self):
        assert disk_fill_path(
            "fallocate -l 2G /var/tmp/fill.bin"
        ) == "/var/tmp/fill.bin"

    def test_no_fill_yields_empty(self):
        assert disk_fill_path("truncate -s 0 /var/tmp/fill.bin") == ""
        assert disk_fill_path("echo hi") == ""

    def test_matches_the_carrier_classifier_family(self):
        # The armed command shape the ledger records from (a timer whose
        # payload is the family inverse): extraction must see the fill even
        # when the inverse rides a systemd-run payload in the same string.
        armed = (
            "chroot /host sh -c 'dd if=/dev/zero of=/var/tmp/fill.bin "
            "bs=1M count=4096 && systemd-run --on-active=600s "
            "--unit=drill-reclaim-x truncate -s 0 /var/tmp/fill.bin'"
        )
        assert disk_fill_path(armed) == "/var/tmp/fill.bin"
        # And the gate's parser reads the payload's inverse as the standalone
        # reclaim the operator would re-issue for early recovery.
        assert carriers._pure_disk_reclaim_path(
            "truncate -s 0 /var/tmp/fill.bin"
        ) == disk_fill_path(armed)


def _dm_fixtures(*, dm_name: str = "error-device"):
    """A disk-approved target plus one armed carrier whose rollback is a
    device-mapper mapping — the IO-error case's arm shape."""
    approved = ApprovedTarget(
        scope="node", namespace="", names=("node-a",), fault_target="disk",
    )
    artifacts = [{
        "type": "debug_pod",
        "name": "node-debugger-n1-abc12",
        "namespace": "kubewiz",
        "uid": "uid-1",
        "privileged": True,
        "status": "recovery_armed",
        "target": {"scope": "node", "name": "node-a"},
        "recovery_dm_name": dm_name,
        "recovery_deadline_epoch": 9_999_999_999.0,
    }]
    return approved, artifacts


class TestPureDmReclaimName:
    """Exactly a standalone ``dmsetup remove <name>`` parses; all else fails."""

    def test_bare_form(self):
        assert carriers._pure_dm_reclaim_name(
            "dmsetup remove error-device"
        ) == "error-device"

    def test_chroot_entry_pair_is_tolerated(self):
        assert carriers._pure_dm_reclaim_name(
            "chroot /host dmsetup remove error-device"
        ) == "error-device"

    def test_host_binary_prefix_is_tolerated(self):
        assert carriers._pure_dm_reclaim_name(
            "/host/dmsetup remove error-device"
        ) == "error-device"

    def test_single_sh_wrapper_is_unwrapped(self):
        assert carriers._pure_dm_reclaim_name(
            "sh -c 'dmsetup remove error-device'"
        ) == "error-device"

    def test_force_flag_is_skipped(self):
        assert carriers._pure_dm_reclaim_name(
            "dmsetup remove -f error-device"
        ) == "error-device"

    def test_create_is_not_a_reclaim(self):
        assert carriers._pure_dm_reclaim_name(
            "echo '0 1024 error' | dmsetup create error-device"
        ) == ""

    def test_second_statement_is_not_a_reclaim(self):
        assert carriers._pure_dm_reclaim_name(
            "dmsetup remove error-device; echo done"
        ) == ""

    def test_two_names_fail_closed(self):
        assert carriers._pure_dm_reclaim_name("dmsetup remove a b") == ""

    def test_other_binary_is_not_a_reclaim(self):
        assert carriers._pure_dm_reclaim_name(
            "truncate -s 0 /var/tmp/fill.bin"
        ) == ""


class TestDmEarlyRecoveryGate:
    """The carrier gate resolves a standalone ``dmsetup remove`` against the
    armed mapping — the device-mapper face of the #59 lane."""

    def test_standalone_remove_on_armed_name_is_allowed(self):
        approved, artifacts = _dm_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host dmsetup remove error-device"
                ),
            },
            artifacts,
            approved,
        )
        assert resolution.resolved
        assert resolution.effective is not None
        assert resolution.effective.scope == "node"
        assert resolution.effective.names == ("node-a",)
        assert resolution.effective.fault_target == "disk"
        assert "dmsetup remove error-device" in resolution.effective.raw_command

    def test_remove_of_an_unarmed_name_is_rejected(self):
        # The mapping name is the match unit: removing something this task
        # never created stays illegal.
        approved, artifacts = _dm_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host dmsetup remove other-device"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_create_is_not_an_early_recovery(self):
        approved, artifacts = _dm_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host sh -c \"echo '0 1024 error' | "
                    "dmsetup create error-device\""
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_non_disk_family_never_uses_the_dm_lane(self):
        approved = ApprovedTarget(
            scope="node", namespace="", names=("node-a",), fault_target="network",
        )
        _, artifacts = _dm_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host dmsetup remove error-device"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved

    def test_ledger_without_a_dm_record_never_uses_the_lane(self):
        # A fill-only ledger (the #59 shape) must not admit a dm reclaim:
        # the two faces key on different record fields.
        approved, artifacts = _disk_fixtures()
        resolution = effective_target_from_registered_carrier(
            "kubectl",
            {
                "subcommand": "exec",
                "v_args": (
                    "node-debugger-n1-abc12 -n kubewiz -- "
                    "chroot /host dmsetup remove error-device"
                ),
            },
            artifacts,
            approved,
        )
        assert not resolution.resolved


class TestDmMappingNameSingleSource:
    """Writer and matcher share the same extraction rules (public face)."""

    def test_pipe_form(self):
        assert dm_mapping_name(
            "echo '0 1024 error' | dmsetup create error-device"
        ) == "error-device"

    def test_table_flag_form(self):
        assert dm_mapping_name(
            "dmsetup create errdev --table '0 100 error'"
        ) == "errdev"

    def test_name_case_is_preserved(self):
        # Mapping names are case-sensitive; the ledger and the matcher must
        # agree exactly.
        assert dm_mapping_name("dmsetup create Error-Dev") == "Error-Dev"

    def test_no_create_yields_empty(self):
        assert dm_mapping_name("dmsetup remove errdev") == ""
        assert dm_mapping_name("echo hi") == ""

    def test_arm_shape_with_timer(self):
        # The case's full arm command: create plus the host-managed removal
        # timer in one string — extraction must still see the created name.
        armed = (
            "echo '0 1024 error' | dmsetup create error-device && "
            "systemd-run --on-active=600s --unit=blade-restore-dmerr "
            "sh -c 'dmsetup remove error-device'"
        )
        assert dm_mapping_name(armed) == "error-device"
        assert carriers._pure_dm_reclaim_name(
            "dmsetup remove error-device"
        ) == dm_mapping_name(armed)
