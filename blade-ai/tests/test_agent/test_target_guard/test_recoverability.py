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

from chaos_agent.agent.target_guard.recoverability import Recoverability, assess


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
