"""Guard-skip and audit-trail wiring for the two host tools.

Two properties, both invisible in the tool's return value:

1. ``skip_guard`` — a read-only verdict from the shared classifier lets a call
   bypass ToolGuard entirely (the diag binaries sit outside
   ``ALLOWED_COMMANDS`` on purpose). ``host_inject`` decides that at ARGV
   level, which cannot see a write hidden INSIDE a program string
   (``awk '{print > "/tmp/x"}'``), so it also applies the raw-string metachar
   screen ``host_read`` applies. Failing that screen must NOT reject the call —
   it must fall through to the guard, where a genuine fault binary still runs
   and everything else is refused.

2. ``audit`` — skipping the guard used to skip the audit log too, because the
   executor fused the two. Both host tools are LLM-facing, not internal probes:
   their commands belong on the record either way.
"""

import pytest

from chaos_agent.tools.host_cmd import host_inject, host_read


class _Recorder:
    """Capture the kwargs ``execute_via_transport`` is called with."""

    def __init__(self):
        self.calls = []

    async def __call__(self, cmd, target, **kwargs):
        from chaos_agent.models.command_result import CommandResult

        self.calls.append({"cmd": cmd, **kwargs})
        return CommandResult(exit_code=0, stdout="out", stderr="")

    @property
    def last(self):
        return self.calls[-1]


@pytest.fixture
def spy(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr("chaos_agent.tools.host_cmd.execute_via_transport", rec)
    return rec


class TestHostInjectGuardSkip:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "df -h",
        "ps aux",
        "iptables -L -n",
        "find /etc -maxdepth 1",
        "curl -sI http://svc/health",
    ])
    async def test_readonly_command_skips_guard(self, spy, command):
        await host_inject.ainvoke({"command": command})
        assert spy.last["skip_guard"] is True, command

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        # Writes hidden inside a program string — invisible at argv level.
        'awk \'{print > "/etc/cron.d/evil"}\' /tmp/x',
        'awk \'{print | "sh"}\' /tmp/x',
        # Chaining / substitution appended to an otherwise read-only command.
        "df -h; rm -rf /data",
        "ps aux | tee /tmp/out",
        "cat /etc/passwd > /tmp/copy",
        "uptime && reboot",
        "echo $(cat /etc/shadow)",
    ])
    async def test_metachar_command_does_not_skip_guard(self, spy, command):
        """The read-only fast path must not admit a command with a metachar.

        The call is NOT rejected here — it falls through to ToolGuard, which is
        the component that decides. That keeps the fast path a pure
        optimisation rather than a second, weaker gate.
        """
        await host_inject.ainvoke({"command": command})
        assert spy.last["skip_guard"] is False, command

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "stress-ng --cpu 4 --timeout 60s",
        "iptables -A INPUT -p tcp --dport 80 -j DROP",
        "tc qdisc add dev eth0 root netem delay 100ms",
    ])
    async def test_fault_command_goes_through_guard(self, spy, command):
        await host_inject.ainvoke({"command": command})
        assert spy.last["skip_guard"] is False, command


class TestHostToolsStayOnTheAuditTrail:
    @pytest.mark.asyncio
    async def test_host_read_audits_despite_skipping_guard(self, spy):
        await host_read.ainvoke({"command": "df -h"})
        assert spy.last["skip_guard"] is True
        assert spy.last["audit"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "df -h",                              # read-only → guard skipped
        "stress-ng --cpu 4 --timeout 60s",    # fault → guard applied
    ])
    async def test_host_inject_audits_either_way(self, spy, command):
        await host_inject.ainvoke({"command": command})
        assert spy.last["audit"] is True, command
