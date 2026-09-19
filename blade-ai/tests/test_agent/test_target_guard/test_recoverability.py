"""Tests for ``chaos_agent.agent.target_guard.recoverability``.

The refactor's core bug fix: recoverability is judged by STRUCTURE (any
bounded-timer form + a paired inverse, or a registered rollback), not by the
one literal ``systemd-run --on-active=`` the old ``_SYSTEMD_TIMER`` demanded.
The regression anchor is the real drill that failed (task-be05d1ad): a correct
``systemd-run --on-create=600s`` reversal was killed only because the literal
did not recognise ``--on-create``.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.target_guard.carriers import classify_host_operation
from chaos_agent.agent.target_guard.recoverability import Recoverability, assess

# The EXACT stoploop command task inject-e47de3e8 burned six minutes being
# rejected for (skill case Pod_进程被杀死 path B): a rounds-capped crictl-stop
# loop armed with a systemd-run timer whose payload pkills the loop. Both the
# recoverability gate and the family classifier must clear it verbatim.
_DRILL_STOPLOOP_CMD = (
    "chroot /host sh -c 'systemd-run --on-active=60s "
    "--unit=blade-stoploop-mysql sh -c \"pkill -f \\\"crictl sto\\$((0+0))p "
    "-t 0\\\"; pkill -x crictl; true\" && sh -c \"for i in 1 2 3 4; do "
    "SBX=$(crictl pods --name mysql -q | head -1); CID=$(crictl ps -q --pod "
    "$SBX | head -1); [ -n \\\"$CID\\\" ] && crictl stop -t 0 $CID; sleep 15; "
    "done\"'"
)


class TestBoundedTimerForms:
    """ANY --on-* systemd timer form is a bound — not just --on-active."""

    @pytest.mark.parametrize("timer", [
        "--on-active=600s",
        "--on-create=600s",   # the exact form the failed drill emitted
        "--on-boot=600s",
        "--on-startup=300s",
        "--on-calendar=*:0/10",  # no numeric? has 0/10 -> contains a nonzero digit
        "--on-unit-active=120s",
    ])
    def test_network_timer_variants_are_bounded(self, timer):
        cmd = (
            "chroot /host sh -c 'iptables -I OUTPUT -j DROP && "
            f"systemd-run {timer} sh -c \"iptables -D OUTPUT -j DROP\"'"
        )
        assert assess(cmd, "network").recoverable is True

    def test_on_create_regression_against_old_literal(self):
        """Direct regression for task-be05d1ad: --on-create must be honoured."""
        cmd = (
            "iptables -I OUTPUT -j DROP && iptables -I INPUT -j DROP && "
            "systemd-run --on-create=600s sh -c "
            "'iptables -D OUTPUT -j DROP && iptables -D INPUT -j DROP'"
        )
        assert assess(cmd, "network").recoverable is True

    def test_zero_delay_timer_is_not_a_bound(self):
        cmd = (
            "iptables -I OUTPUT -j DROP && systemd-run --on-active=0s sh -c "
            "'iptables -D OUTPUT -j DROP'"
        )
        # No positive delay → no real bound → not recoverable.
        assert assess(cmd, "network").recoverable is False


class TestFamilyContractsPreserved:
    """The family-specific inverse semantics are unchanged by the refactor."""

    def test_cpu_self_terminating_timeout(self):
        assert assess("stress-ng --cpu 2 --timeout 60", "cpu").recoverable is True

    def test_cpu_without_bound_is_not_recoverable(self):
        assert assess("stress-ng --cpu 2", "cpu").recoverable is False

    def test_process_suspend_resume_bounded(self):
        cmd = "kill -STOP 1234 && sleep 300 && kill -CONT 1234"
        assert assess(cmd, "process").recoverable is True

    def test_process_terminate_not_bounded(self):
        assert assess("kill -9 1234", "process").recoverable is False

    def test_disk_reclaim_same_path_bounded(self):
        cmd = (
            "dd if=/dev/zero of=/host/tmp/fill bs=1M count=1024 && "
            "sleep 600 && truncate -s 0 /host/tmp/fill"
        )
        assert assess(cmd, "disk").recoverable is True

    def test_disk_reclaim_other_path_not_bounded(self):
        cmd = (
            "dd if=/dev/zero of=/host/tmp/fill bs=1M count=1024 && "
            "sleep 600 && truncate -s 0 /host/tmp/other"
        )
        assert assess(cmd, "disk").recoverable is False


class TestRegisteredRollbackEscape:
    """A registered rollback handle makes an inline timer unnecessary."""

    def test_registered_rollback_short_circuits(self):
        # No inline timer at all, but a rollback is on record.
        r = assess("iptables -I OUTPUT -j DROP", "network",
                   has_registered_rollback=True)
        assert r.recoverable is True


class TestTransparentMissingReasons:
    """When NOT recoverable, name exactly what is missing — never a silent no."""

    def test_missing_names_the_absent_inverse(self):
        cmd = (
            "iptables -I OUTPUT -j DROP && systemd-run --on-active=600s sh -c "
            "'echo done'"  # timer present, inverse absent
        )
        r = assess(cmd, "network")
        assert r.recoverable is False
        assert any("inverse" in m for m in r.missing)

    def test_missing_names_the_absent_bound(self):
        # inverse present (matched -I/-D) but no timer at all
        cmd = "iptables -I OUTPUT -j DROP && iptables -D OUTPUT -j DROP"
        r = assess(cmd, "network")
        assert r.recoverable is False
        assert any("time bound" in m or "bound" in m for m in r.missing)

    def test_missing_is_empty_when_recoverable(self):
        r = assess("stress-ng --cpu 2 --timeout 60", "cpu")
        assert r == Recoverability(True)


class TestVocabularyExtensions:
    """Equivalent syntax spellings a shell may emit must not be false-rejected."""

    def test_kill_signal_name_spellings_bounded(self):
        cmd = "kill -s STOP 1234 && sleep 300 && kill -s CONT 1234"
        assert assess(cmd, "process").recoverable is True

    def test_kill_sigstop_spelling_bounded(self):
        cmd = "kill -SIGSTOP 1234 && sleep 300 && kill -SIGCONT 1234"
        assert assess(cmd, "process").recoverable is True

    def test_iptables_long_wait_flag_pairs(self):
        cmd = (
            "iptables --wait -A OUTPUT -j DROP && sleep 300 && "
            "iptables --wait -D OUTPUT -j DROP"
        )
        assert assess(cmd, "network").recoverable is True

    def test_iptables_wait_equals_form_pairs(self):
        cmd = (
            "iptables --wait=5 -A OUTPUT -j DROP && sleep 300 && "
            "iptables --wait=5 -D OUTPUT -j DROP"
        )
        assert assess(cmd, "network").recoverable is True


class TestTcDevicePairing:
    """tc reversals pair by device — a del on another dev leaves the fault."""

    def test_same_dev_del_bounded(self):
        cmd = (
            "tc qdisc add dev eth0 root netem loss 100% && sleep 300 && "
            "tc qdisc del dev eth0 root"
        )
        assert assess(cmd, "network").recoverable is True

    def test_other_dev_del_not_bounded(self):
        cmd = (
            "tc qdisc add dev eth0 root netem loss 100% && sleep 300 && "
            "tc qdisc del dev eth1 root"
        )
        assert assess(cmd, "network").recoverable is False

    def test_multi_dev_needs_every_dev_reversed(self):
        cmd = (
            "tc qdisc add dev eth0 root netem loss 100% && "
            "tc qdisc add dev eth1 root netem loss 100% && sleep 300 && "
            "tc qdisc del dev eth0 root"
        )
        assert assess(cmd, "network").recoverable is False

    def test_device_less_add_falls_back_to_existence(self):
        cmd = "tc qdisc add root netem loss 100% && sleep 300 && tc qdisc del root"
        assert assess(cmd, "network").recoverable is True


class TestDegenerateBounds:
    """A formally present but absurd timer is no bound at all."""

    def test_sleep_beyond_cap_not_bounded(self):
        cmd = (
            "iptables -I OUTPUT -j DROP && sleep 999999999 && "
            "iptables -D OUTPUT -j DROP"
        )
        assert assess(cmd, "network").recoverable is False

    def test_stress_timeout_beyond_cap_not_bounded(self):
        assert (
            assess("stress-ng --cpu 2 --timeout 999999999", "cpu").recoverable
            is False
        )

    def test_sleep_at_the_cap_is_bounded(self):
        cmd = (
            "iptables -I OUTPUT -j DROP && sleep 2592000 && "
            "iptables -D OUTPUT -j DROP"
        )
        assert assess(cmd, "network").recoverable is True

    def test_systemd_calendar_value_not_value_capped(self):
        # Calendar specs are recurrence, not durations — never value-capped.
        cmd = (
            "iptables -I OUTPUT -j DROP && "
            "systemd-run --on-calendar=*:0/10 sh -c 'iptables -D OUTPUT -j DROP'"
        )
        assert assess(cmd, "network").recoverable is True


class TestBoundedContainerStopLoop:
    """Terminate-style process faults expressed through ``crictl stop``.

    Regression anchor: task inject-e47de3e8. The skill's documented
    kubectl-native process kill was rejected at BOTH carrier gates — the
    family classifier had no ``crictl stop`` mapping and recoverability
    knew only suspend/resume — so the model spent six minutes detouring to
    another carrier. Two forms are cleared: the sustained mode (a
    double-bounded loop — rounds cap + timer terminator) and the discrete
    mode (a one-shot stop the kubelet self-heals). Loop-shaped variants
    without both bounds keep failing closed: a host-side loop outlives the
    agent, while one-shots are agent-paced. Arbitrary host kills (kill -9)
    stay rejected — no kubelet rebuilds a killed host process.
    """

    def test_drill_stoploop_command_is_recoverable(self):
        assert assess(_DRILL_STOPLOOP_CMD, "process").recoverable is True

    def test_drill_stoploop_command_classifies_as_process(self):
        assert classify_host_operation(_DRILL_STOPLOOP_CMD) == "process"

    def test_seq_form_is_bounded(self):
        cmd = (
            "systemd-run --on-active=120s --unit=stoploop sh -c 'pkill -f "
            "crictl-stoploop' && for i in $(seq 1 4); do crictl stop "
            "-t 0 abc123; sleep 15; done"
        )
        assert assess(cmd, "process").recoverable is True

    def test_bare_loop_without_timer_not_bounded(self):
        cmd = (
            "for i in 1 2 3 4; do crictl stop -t 0 abc123; sleep 15; done"
        )
        assert assess(cmd, "process").recoverable is False

    def test_one_shot_container_stop_is_bounded_discrete(self):
        # Discrete mode: a single instantaneous event the kubelet self-heals
        # — no persistent state, no window, nothing to undo (skill case
        # Pod_进程被杀死 path B discrete form).
        assert assess("crictl stop -t 0 abc123", "process").recoverable is True

    def test_one_shot_stop_sequence_is_bounded_discrete(self):
        # Several one-shots in one command are still agent-paced and
        # instantaneous — no loop means no durable mechanism.
        assert assess(
            "crictl stop -t 0 aaa; crictl stop -t 0 bbb", "process",
        ).recoverable is True

    def test_until_loop_around_stop_not_discrete(self):
        assert assess(
            "until false; do crictl stop -t 0 abc123; sleep 5; done",
            "process",
        ).recoverable is False

    def test_watch_around_stop_not_discrete(self):
        assert assess(
            "watch -n 5 crictl stop -t 0 abc123", "process",
        ).recoverable is False

    def test_uncapped_while_loop_not_bounded(self):
        cmd = (
            "systemd-run --on-active=60s --unit=x sh -c 'pkill -f crictl' && "
            "while true; do crictl stop -t 0 abc123; sleep 15; done"
        )
        assert assess(cmd, "process").recoverable is False

    def test_loop_without_interval_not_bounded(self):
        cmd = (
            "systemd-run --on-active=60s --unit=x sh -c 'pkill -f crictl' && "
            "for i in 1 2 3 4; do crictl stop -t 0 abc123; done"
        )
        assert assess(cmd, "process").recoverable is False

    def test_timer_with_unrelated_payload_not_bounded(self):
        # Timer present, but its payload does not terminate the loop — it
        # bounds nothing, so the loop could outlive the drill window.
        cmd = (
            "systemd-run --on-active=60s --unit=x sh -c 'echo done' && "
            "for i in 1 2 3 4; do crictl stop -t 0 abc123; sleep 15; done"
        )
        assert assess(cmd, "process").recoverable is False

    def test_inspection_crictl_verbs_do_not_classify(self):
        # ps/pods/inspect are read-only probes — never a process fault.
        for verb in ("crictl ps", "crictl pods", "crictl inspect abc"):
            assert classify_host_operation(verb) == ""

    def test_one_shot_stop_classifies_and_discrete_gate_accepts(self):
        # Family mapping is one-shot-agnostic; the recoverability gate
        # accepts the one-shot form as the discrete mode.
        assert classify_host_operation("crictl stop -t 0 abc123") == "process"
        assert assess("crictl stop -t 0 abc123", "process").recoverable is True

    def test_arbitrary_host_kill_still_not_bounded(self):
        # A killed host process has no kubelet to rebuild it — only
        # container stops are self-healing.
        assert assess("kill -9 1234", "process").recoverable is False


class TestHostTimerPayloadFamily:
    """Host-channel ``systemd-run`` timer family extraction.

    The host skill 降级方案 timers (进程假死挂起 et al.) carry their inverse
    command inside a quoted ``sh -c 'kill -CONT $(…)'`` script. The
    host-entry unwrap only knows chroot/nsenter/unshare, so without surfacing
    the quoted text the family resolved EMPTY and the fault-type lock lost
    its pin (verified pre-fix: family '' for the exact skill command).
    """

    def test_quoted_sigCONT_timer_classifies_as_process(self):
        cmd = (
            "systemd-run --on-active=300s --unit=blade-cont-nginx "
            "sh -c 'kill -CONT $(pgrep -f nginx)'"
        )
        assert classify_host_operation(cmd) == "process"

    def test_direct_argv_timer_classifies_as_network(self):
        cmd = (
            "systemd-run --on-active=600s --unit=blade-restore-drop "
            "iptables -D OUTPUT -d 10.0.0.5 -j DROP"
        )
        assert classify_host_operation(cmd) == "network"

    def test_non_quoted_timer_family_unchanged(self):
        # No quotes to surface — the plain-argv family regexes already see
        # iptables; the quoted-text join must not disturb them.
        cmd = "systemd-run --on-active=600s --unit=x systemctl restart nginx"
        assert classify_host_operation(cmd) == ""


class TestTimeoutBoundedListener:
    """Port occupation via a timeout(1)-bounded ``nc -l`` listener.

    Regression anchor: skill case Node_网络故障_节点端口占用. The port is
    held only while the listener runs, so ending the process IS the
    recovery — no inverse rule exists to pair. A listener without a sane
    bound, and a client-mode nc (no ``-l``), keep failing closed.
    """

    def test_timeout_wrapped_listener_is_recoverable(self):
        cmd = "timeout 300 nc -l -p 8080 -k"
        assert assess(cmd, "network").recoverable is True

    def test_listener_classifies_as_network(self):
        assert classify_host_operation("timeout 300 nc -l -p 8080 -k") == "network"

    def test_self_timeout_flag_form_is_recoverable(self):
        cmd = "nc -l -p 8080 --timeout=300"
        assert assess(cmd, "network").recoverable is True

    def test_unbounded_listener_not_recoverable(self):
        assert assess("nc -l -p 8080 -k", "network").recoverable is False

    def test_degenerate_timeout_not_recoverable(self):
        assert (
            assess("timeout 999999999 nc -l -p 8080 -k", "network").recoverable
            is False
        )

    def test_client_mode_nc_does_not_classify(self):
        # No ``-l``: a connect attempt occupies nothing — not a fault.
        assert classify_host_operation("nc 10.0.0.1 80 < /dev/null") == ""


class TestTimeoutBoundedIoBurn:
    """IO pressure via a timeout(1)-bounded burner (dd/fio).

    Regression anchor: skill cases Node_磁盘IO过高 /
    Pod_Terminating_Volume卸载失败 used ``timeout 300 sh -c "while true;
    do dd ...; done"`` — the burn loop is uncapped but the wrapping timeout
    kills it, so the pressure self-ends. Disk FILLS do NOT qualify: killing
    a fallocate leaves the bytes on disk, so fills keep needing a paired
    reclaim.
    """

    def test_timeout_wrapped_burn_loop_is_recoverable(self):
        cmd = (
            "timeout 300 sh -c 'while true; do dd if=/dev/zero of=/data/burn "
            "bs=1M count=512 oflag=direct; done'"
        )
        assert assess(cmd, "disk").recoverable is True

    def test_timeout_wrapped_fio_is_recoverable(self):
        cmd = "timeout 600 fio --name=burn --rw=randwrite --size=1G"
        assert assess(cmd, "disk").recoverable is True

    def test_unbounded_burn_loop_not_recoverable(self):
        cmd = "while true; do dd if=/dev/zero of=/data/burn bs=1M; done"
        assert assess(cmd, "disk").recoverable is False

    def test_timeout_wrapped_fill_still_needs_reclaim(self):
        # Killing the filler does not reclaim the bytes — fill semantics
        # are deliberately excluded from the self-terminating bound.
        cmd = "timeout 60 fallocate -l 10G /data/fill"
        assert assess(cmd, "disk").recoverable is False

    def test_fill_with_timer_and_reclaim_still_recoverable(self):
        # The original paired-reclaim form is untouched by the burn bound.
        cmd = (
            "fallocate -l 10G /data/fill && systemd-run --on-active=300s "
            "fallocate -d /data/fill"
        )
        assert assess(cmd, "disk").recoverable is True


class TestTimerArmedFreezerSuspend:
    """A cgroup-freezer suspend armed with a THAW timer BEFORE the freeze.

    Regression anchor: skill cases Pod_进程异常_进程被挂起 /
    Container_进程异常_Sidecar进程被挂起. The documented discipline is
    arm-then-freeze: a frozen container cannot register its own rescue and
    the carrier pod may be cleaned before the thaw is due, so the timer
    whose payload writes THAWED to the SAME freezer.state must be armed
    first. Every violation keeps failing closed.
    """

    _FREEZER_PATH = "/sys/fs/cgroup/freezer/kubepods/abc123/freezer.state"

    def _armed_cmd(self, payload: str = None, arm_first: bool = True) -> str:
        payload = payload or f"echo THAWED > {self._FREEZER_PATH}"
        arm = f"systemd-run --on-active=120s --unit=blade-thaw sh -c '{payload}'"
        freeze = f"echo FROZEN > {self._FREEZER_PATH}"
        parts = (arm, freeze) if arm_first else (freeze, arm)
        return "; sleep 1; ".join(parts)

    def test_arm_then_freeze_is_recoverable(self):
        assert assess(self._armed_cmd(), "process").recoverable is True

    def test_freezer_write_classifies_as_process(self):
        assert (
            classify_host_operation(f"echo FROZEN > {self._FREEZER_PATH}")
            == "process"
        )
        assert (
            classify_host_operation(f"echo THAWED > {self._FREEZER_PATH}")
            == "process"
        )

    def test_freeze_before_arm_not_recoverable(self):
        # A rescue registered after the freeze may never run — the frozen
        # container cannot exec and the carrier may already be gone.
        assert (
            assess(self._armed_cmd(arm_first=False), "process").recoverable
            is False
        )

    def test_timer_payload_without_thaw_not_recoverable(self):
        assert (
            assess(self._armed_cmd(payload="echo done"), "process").recoverable
            is False
        )

    def test_thaw_to_different_state_file_not_recoverable(self):
        other = f"echo THAWED > {self._FREEZER_PATH}.other"
        assert assess(self._armed_cmd(payload=other), "process").recoverable is False

    def test_bare_freeze_without_timer_not_recoverable(self):
        cmd = f"echo FROZEN > {self._FREEZER_PATH}"
        assert assess(cmd, "process").recoverable is False


class TestNetworkZeroMutationRouting:
    """B34: a network-family command with NO mutation verb is not "missing
    a paired inverse" — there is nothing to invert. The rejection keeps
    failing closed, but its guidance points at the FORM (split into
    single-statement probes), never at a phantom ``iptables -D``."""

    def test_zero_mutation_compound_is_rejected_as_unproven(self):
        # The B34 shape: compound read-only inspection that fell through
        # the probe face (redirect here). Fail-closed, but honest cause.
        cmd = "chroot /host sh -c 'iptables -S INPUT > /tmp/out'"
        verdict = assess(cmd, "network")
        assert verdict.recoverable is False
        assert verdict.readonly_unproven is True
        # The phantom-inverse guidance is gone.
        assert not any("paired inverse" in m for m in verdict.missing)
        assert any("single-statement" in m for m in verdict.missing)

    def test_zero_mutation_even_with_a_timer_still_unproven(self):
        # A timer cannot fix a form problem — the guidance must not
        # collapse into "only the missing reversal blocks it".
        cmd = "chroot /host sh -c 'sleep 60; iptables -S INPUT'"
        verdict = assess(cmd, "network")
        assert verdict.recoverable is False
        assert verdict.readonly_unproven is True

    def test_real_mutation_keeps_the_reversal_frame(self):
        cmd = "chroot /host sh -c 'iptables -I INPUT -s 10.0.0.1 -j DROP'"
        verdict = assess(cmd, "network")
        assert verdict.recoverable is False
        assert verdict.readonly_unproven is False
        assert any("paired inverse" in m for m in verdict.missing)

    def test_delete_only_cleanup_keeps_the_reversal_frame(self):
        # ``-D`` is a mutation verb even though nothing was inserted —
        # the reversal frame (timer + paired re-insert) is the honest one.
        cmd = "chroot /host sh -c 'iptables -D INPUT 1'"
        verdict = assess(cmd, "network")
        assert verdict.readonly_unproven is False

    def test_long_option_mutation_keeps_the_reversal_frame(self):
        # ``--insert`` joined the detectable verbs at the B34 fix —
        # previously invisible, it read as "zero mutations".
        cmd = "chroot /host sh -c 'iptables --insert INPUT 1 -s 10.0.0.1 -j DROP'"
        verdict = assess(cmd, "network")
        assert verdict.recoverable is False
        assert verdict.readonly_unproven is False

    def test_long_option_insert_delete_pairing_is_recoverable(self):
        cmd = (
            "iptables --insert INPUT 1 -s 10.0.0.1 -j DROP && "
            "systemd-run --on-active=60s sh -c "
            "'iptables --delete INPUT 1 -s 10.0.0.1 -j DROP'"
        )
        assert assess(cmd, "network").recoverable is True

    def test_mutation_verb_in_next_segment_does_not_pair_across_the_pipe(self):
        # The detector's gap cannot cross a separator: ``grep -i`` in the
        # next stage must not read as an ``-i`` insert verb next to the
        # ``iptables`` in this stage (B34's exact compound shape).
        from chaos_agent.agent.target_guard.recoverability import (
            _network_has_mutation_verb,
        )

        assert not _network_has_mutation_verb("iptables -s input | grep -i drop")
        assert _network_has_mutation_verb("iptables -i input 1 -j drop")

    def test_readonly_listing_forms_are_not_mutations(self):
        from chaos_agent.agent.target_guard.recoverability import (
            _network_has_mutation_verb,
        )

        assert not _network_has_mutation_verb("iptables -s input")
        assert not _network_has_mutation_verb("iptables -t nat -l -n")
        assert not _network_has_mutation_verb("tc qdisc show dev eth0")
        assert not _network_has_mutation_verb("nft list ruleset")


class TestSocatPortListener:
    """socat TCP-LISTEN as the port-occupation fault vocabulary (B32 guard gap).

    Regression anchor: case-32 (Node_网络故障_节点端口占用). The skill case's
    law settled on ``socat TCP-LISTEN`` because nc is absent on the cluster's
    hosts — but the guard's family whitelist and the bounded-listener
    recovery rule only knew ``nc -l``, so the documented shape was rejected
    (family_mismatch) and the model paid a detour tax to find an alternate
    form. Both layers now match the case law, with the shape boundary
    fail-closed: EXEC:/SYSTEM:/SHELL: addresses turn a listener into
    arbitrary command execution (the classic bind-shell) and stay
    unclassified, and an unbounded listener stays unrecoverable.
    """

    def test_socat_listen_classifies_as_network(self):
        # The exact case-32 carrier payload (minus the kill sibling):
        assert classify_host_operation(
            "nohup timeout 180 socat TCP-LISTEN:9100,reuseaddr,fork "
            "OPEN:/dev/null"
        ) == "network"

    def test_kill_plus_socat_compound_classifies_as_network(self):
        # The full case-32 payload: kill (process) + socat (network) —
        # TWO families, so classify still fails closed by design (the
        # compound must be split); a kill in ANOTHER segment does not
        # void the socat family.
        assert classify_host_operation("kill 2351338") == "process"
        assert classify_host_operation(
            "kill 2351338; sleep 1; nohup timeout 180 socat "
            "TCP-LISTEN:9100,reuseaddr,fork OPEN:/dev/null"
        ) == ""

    def test_socat_exec_address_stays_unclassified(self):
        # Bind-shell form: listener wired to a command-exec address —
        # the port-occupation family must NOT adopt it.
        assert classify_host_operation(
            "timeout 180 socat TCP-LISTEN:9100 EXEC:/bin/sh"
        ) == ""

    def test_socat_system_and_shell_addresses_stay_unclassified(self):
        assert classify_host_operation(
            "timeout 180 socat TCP-LISTEN:9100 SYSTEM:'echo pwned'"
        ) == ""
        assert classify_host_operation(
            "timeout 180 socat TCP-LISTEN:9100 SHELL"
        ) == ""

    def test_socat_danger_scan_has_no_character_cap(self):
        # Regression anchor for the de-windowed danger scan: the former
        # {0,200} window was an implementation seam — 200+ chars of
        # option padding pushed EXEC:/SYSTEM:/SHELL past the scan while
        # the LISTEN form still matched, so a timeout-bounded bind-shell
        # sailed through BOTH the family gate and the recovery gate
        # (2026-09-10 self-audit of the #32 socat extension). The
        # segment bound — not a character count — is the boundary.
        pad = "nodelay,keepalive,reuseaddr,linger=1," * 6  # 216 > 200
        # Smuggled comma-address form: EXEC rides the LISTEN option run.
        assert classify_host_operation(
            f"nohup timeout 180 socat TCP-LISTEN:9100,{pad}EXEC:/bin/sh"
        ) == ""
        # Legal-address form: a second socat address after the padding.
        assert classify_host_operation(
            f"nohup timeout 180 socat TCP-LISTEN:9100,reuseaddr,fork,{pad} "
            "EXEC:/bin/sh"
        ) == ""
        # The SYSTEM:/SHELL vocabularies ride the same seam.
        assert classify_host_operation(
            f"nohup timeout 180 socat TCP-LISTEN:9100,{pad}SYSTEM:/bin/sh"
        ) == ""
        assert classify_host_operation(
            f"nohup timeout 180 socat TCP-LISTEN:9100,{pad}SHELL"
        ) == ""

    def test_socat_danger_scan_still_segment_bounded(self):
        # De-windowing must NOT widen the scan across segments: EXEC: in
        # a different ``;``-delimited segment does not void the socat
        # family — the danger has to ride the socat segment itself.
        assert classify_host_operation(
            "echo EXEC:; nohup timeout 180 socat TCP-LISTEN:9100,"
            "reuseaddr,fork OPEN:/dev/null"
        ) == "network"

    def test_client_mode_socat_is_not_a_fault(self):
        # No LISTEN address → plain client, not an occupation.
        assert classify_host_operation(
            "timeout 10 socat - TCP:127.0.0.1:9100"
        ) == ""

    def test_bounded_socat_listener_is_recoverable(self):
        assert assess(
            "nohup timeout 180 socat TCP-LISTEN:9100,reuseaddr,fork "
            "OPEN:/dev/null",
            "network",
        ).recoverable is True

    def test_unbounded_socat_listener_not_recoverable(self):
        verdict = assess(
            "nohup socat TCP-LISTEN:9100,reuseaddr,fork OPEN:/dev/null",
            "network",
        )
        assert verdict.recoverable is False

    def test_bounded_nc_listener_still_recoverable(self):
        # The nc vocabulary this rule generalises keeps working.
        assert assess("timeout 180 nc -l -p 9100", "network").recoverable is True
