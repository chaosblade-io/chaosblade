"""Read-only judgement for host-escape probes and dual-use diagnostics.

Background (task-9560ee48): Phase 1 rejected
``kubectl exec <debug-pod> -- chroot /host sh -c "which iptables systemd-run"``
with ``phase1_readonly_violation``. The classifier keyed on the FIRST token
being ``chroot`` and never looked at the command actually being run, so a pure
capability probe was treated as a mutation. The planner could then not verify
host preconditions (iptables / systemd present?) and had to finalize the plan on
an assumption — while the far riskier step that created the privileged debug pod
was allowed.

The judgement is context-dependent, and both halves are asserted here:

  - kubectl-exec context: escape primitives are UNWRAPPED and the wrapped
    command decides. This is the only way to inspect a node from a debug pod.
  - bare host command: escape primitives stay rejected. ``is_readonly_argv``
    feeds ``host_inject``'s ``skip_guard``, and chroot/nsenter are NOT in
    ToolGuard's allow-list — admitting them there would open a guard bypass on
    a path that never needs them.
"""

import pytest

from chaos_agent.tools.readonly import (
    host_command_rejection_reason,
    is_readonly_kubectl_exec,
    kubectl_exec_rejection_reason,
)


def _exec(inner: str) -> str:
    return f"probe-pod -n default -- {inner}"


class TestEscapeProbeInExecContext:
    """Read-only probes through chroot/nsenter/unshare are allowed."""

    @pytest.mark.parametrize("inner", [
        'chroot /host sh -c "which iptables systemd-run"',  # the reported case
        "chroot /host cat /etc/os-release",
        "chroot /host df -h",
        "chroot /host systemctl status kubelet",
        "chroot /host iptables -L INPUT -n",
        "chroot /host journalctl -u kubelet -n 50",
        "nsenter -t 1 -m -u -n -i -- cat /etc/os-release",
        "nsenter -t 1 -n -- ss -tlnp",
        "nsenter -t1 -m ls /host",           # attached flag value
        "unshare -m df -h",
        "timeout 5 chroot /host df -h",      # wrapper in front of the escape
        'sh -c "chroot /host df -h"',        # escape hidden inside sh -c
    ])
    def test_readonly_escape_probe_allowed(self, inner):
        assert is_readonly_kubectl_exec(_exec(inner)), (
            kubectl_exec_rejection_reason(_exec(inner))
        )

    @pytest.mark.parametrize("inner", [
        'chroot /host sh -c "iptables -A INPUT -p tcp --dport 6443 -j DROP"',
        "chroot /host iptables -F",
        "chroot /host systemctl stop kubelet",
        "chroot /host dd if=/dev/zero of=/tmp/f bs=1M count=100",
        "chroot /host stress-ng --cpu 8",
        "chroot /host sysctl -w net.ipv4.ip_forward=0",
        "chroot /host journalctl --vacuum-time=1s",
        "nsenter -t 1 -m -- tc qdisc add dev eth0 root netem delay 100ms",
        "nsenter -t 1 -m -- systemctl restart docker",
        "timeout 5 chroot /host iptables -F",
        'sh -c "chroot /host iptables -F"',
    ])
    def test_mutating_escape_still_blocked(self, inner):
        """Unwrapping must not weaken the verdict on the real command."""
        assert not is_readonly_kubectl_exec(_exec(inner))

    @pytest.mark.parametrize("inner", [
        "chroot",                                  # no NEWROOT, no command
        "chroot /host",                            # NEWROOT only, no command
        "nsenter -t 1 -m",                         # flags only, no command
        "chroot /host chroot /host chroot /host df -h",  # nesting past the cap
    ])
    def test_unparseable_or_deep_nesting_fails_closed(self, inner):
        assert not is_readonly_kubectl_exec(_exec(inner))

    @pytest.mark.parametrize("inner", [
        "chroot --skip-chdir /host iptables -F",
        "chroot --userspec=root:root /host dd if=/dev/zero of=/tmp/x",
        "chroot --groups grp /host iptables -F",
    ])
    def test_option_before_newroot_does_not_bypass(self, inner):
        """``chroot [OPTION]... NEWROOT CMD`` — options may precede NEWROOT.

        Regression: the first parser assumed ``rest[0]`` was NEWROOT, so with an
        option present it returned ``['/host', 'iptables', '-F']`` as the
        "command". ``/host`` then basenamed to ``host`` — which is the read-only
        DNS lookup binary — and the real ``iptables -F`` was never inspected,
        yielding a READ-ONLY verdict for a host-wide firewall flush.
        """
        assert not is_readonly_kubectl_exec(_exec(inner))

    @pytest.mark.parametrize("inner", [
        "chroot --skip-chdir /host df -h",
        "chroot --userspec=root:root /host cat /etc/os-release",
    ])
    def test_option_before_newroot_still_allows_readonly(self, inner):
        assert is_readonly_kubectl_exec(_exec(inner)), (
            kubectl_exec_rejection_reason(_exec(inner))
        )


class TestEscapeStillBlockedOnBareHost:
    """The host path must NOT gain an escape route.

    ``is_readonly_argv`` decides ``host_inject``'s ``skip_guard``; a read-only
    verdict bypasses ToolGuard entirely. chroot/nsenter/unshare are absent from
    ToolGuard's allow-list, so they must keep failing here even when the wrapped
    command is read-only.
    """

    @pytest.mark.parametrize("command", [
        "chroot /host cat /etc/os-release",
        "nsenter -t 1 -m -- df -h",
        "unshare -m df -h",
        "timeout 5 chroot /host df -h",
    ])
    def test_host_escape_rejected(self, command):
        assert host_command_rejection_reason(command) is not None


class TestDualUseDiagnostics:
    """Newly recognised diagnostics, and the mutating forms they must not admit."""

    @pytest.mark.parametrize("command", [
        "lsof -i",
        "lsmod",
        "sysctl -a",
        "sysctl -n net.ipv4.ip_forward",
        "sysctl net.ipv4.ip_forward",
        "journalctl -u kubelet -n 50",
        "journalctl --since -1h",
        "crictl ps",
        "crictl inspect abc123",
        "docker ps",
        "timeout 5 df -h",
        "env",
    ])
    def test_readonly_forms_allowed(self, command):
        assert host_command_rejection_reason(command) is None, (
            host_command_rejection_reason(command)
        )

    @pytest.mark.parametrize("command", [
        "sysctl -w net.ipv4.ip_forward=0",
        "sysctl net.ipv4.ip_forward=0",
        "sysctl -p /etc/sysctl.conf",
        "sysctl --system",               # applies every config file
        "journalctl --rotate",
        "journalctl --vacuum-size=1M",
        "journalctl --flush",
        "crictl rm abc123",
        "crictl stopp abc123",
        "docker kill abc123",
        "timeout 5 stress --cpu 4",
        "timeout",                       # wrapper with nothing wrapped
    ])
    def test_mutating_forms_rejected(self, command):
        assert host_command_rejection_reason(command) is not None

    @pytest.mark.parametrize("command", [
        "docker image rm abc123",
        "docker image prune -f",
        "crictl image rm abc123",
        "docker config rm cfg",
        "crictl config --set x=y",
    ])
    def test_grouping_verb_with_mutating_subverb_rejected(self, command):
        """Grouping verbs must not be on the read-only allow-list.

        Regression: ``image`` / ``config`` were listed, and the verdict looked at
        the FIRST non-flag token only — so ``docker image rm X`` matched
        ``image`` and was judged read-only. On the host path that verdict also
        sets ``skip_guard``, i.e. it would have bypassed ToolGuard and escaped
        injection attribution.
        """
        assert host_command_rejection_reason(command) is not None

    @pytest.mark.parametrize("command", [
        "crictl --runtime-endpoint unix:///run/containerd/containerd.sock ps",
        "crictl -r unix:///x pods",
        "docker -H tcp://localhost:2375 ps",
        "ionice -c 2 -n 5 df -h",
        "nice -n 5 df -h",
        "timeout 5s df -h",
        "stdbuf -o L ps aux",
    ])
    def test_flag_values_are_not_mistaken_for_the_command(self, command):
        """A flag's VALUE must not be read as the verb / wrapped command.

        Regression: ``crictl --runtime-endpoint <url> ps`` took ``<url>`` as the
        verb and rejected a plain ``ps``; ``ionice -c 2 -n 5 df -h`` stopped
        stripping at ``-n`` and classified ``-n`` as the binary.
        """
        assert host_command_rejection_reason(command) is None, (
            host_command_rejection_reason(command)
        )

    def test_crictl_exec_is_not_readonly(self):
        """``exec`` is excluded on purpose: its inner command is unbounded, so
        admitting it would let any mutation ride in behind a read-only verb."""
        assert host_command_rejection_reason("crictl exec -it abc sh") is not None
        assert not is_readonly_kubectl_exec(_exec("crictl exec -it abc sh"))


class TestClassifierScopeForEscapeProbe:
    """The guard SCOPE must follow the same split, since the Phase 1 screener
    rejects anything that is not ``SCOPE_READONLY``."""

    def _scope(self, inner: str) -> str:
        from chaos_agent.agent.target_guard.classifier import infer_effective_target

        return infer_effective_target(
            "kubectl_read", {"subcommand": "exec", "v_args": _exec(inner)},
        ).scope

    @pytest.mark.parametrize("inner", [
        'chroot /host sh -c "which iptables systemd-run"',
        "chroot /host df -h",
        "nsenter -t 1 -n -- ss -tlnp",
    ])
    def test_readonly_probe_scopes_readonly(self, inner):
        from chaos_agent.agent.target_guard.classifier import SCOPE_READONLY

        assert self._scope(inner) == SCOPE_READONLY

    @pytest.mark.parametrize("inner", [
        'chroot /host sh -c "iptables -A INPUT -j DROP"',
        "chroot /host systemctl stop kubelet",
        "chroot /host",
    ])
    def test_mutating_probe_scopes_escape(self, inner):
        from chaos_agent.agent.target_guard.classifier import SCOPE_ESCAPE

        assert self._scope(inner) == SCOPE_ESCAPE
