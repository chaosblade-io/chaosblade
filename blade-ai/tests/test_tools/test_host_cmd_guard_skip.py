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

from chaos_agent.agent.providers.host_shell.provider import _classify_host_inject
from chaos_agent.agent.target_guard.types import SCOPE_READONLY
from chaos_agent.tools.host_cmd import _HostInjectArgs, host_inject, host_read
from chaos_agent.tools.kubectl_cli import kubectl


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


class TestHostInjectObserveContract:
    """B2 design-root-cause fix (cited incident: CPU#2 code4002 false-negative).

    A host CPU fullload (``blade ... cpu fullload --timeout 360``) physically
    injected — out-of-band SSH confirmed the burn — yet the framework ruled
    ``fail``: the primary evidence (CPU%) was never sampled INSIDE the fault
    window, so ``primary_evidence_observed=false`` capped the verdict at
    ``partial`` (→ CLI fail). Root cause was a CONTRACT LIE, not a guard/scope
    defect: host_inject is the SUPERSET of host_read (a clean read-only probe is
    admitted with ``skip_guard``) and the ONLY tool bound in EXECUTE — the one
    phase guaranteed to run while a self-recovering fault is still live — but its
    contract declared fault-injection ONLY, so the model quoted that line and
    never attempted an in-window ``top -bn1``. The isomorphic k8s EXECUTE phase
    has no such gap because ``kubectl``'s contract honestly declares it covers
    reads. The fix mirrors kubectl: (①) the contract now names BOTH command
    kinds; (③) a pipe-blocked read-only probe gets phase-correct guidance
    instead of ToolGuard's dead-end pointer to the unbound host_read.
    """

    # ① Contract honesty — the LLM-visible surfaces must declare the read-only
    #    observation channel, and the old fault-only lie must not come back.
    def test_contract_declares_readonly_observe_channel(self):
        desc = host_inject.description
        assert "read-only diagnostic" in desc
        assert "OBSERVE" in desc
        # the in-window fact that makes the channel load-bearing for a
        # self-recovering fault: host_read is NOT bound in this phase.
        assert "host_read is NOT bound" in desc

    def test_schema_field_declares_two_channels(self):
        field = _HostInjectArgs.model_fields["command"].description
        assert "read-only" in field
        # reverse-pin the exact old fault-only wording (the lie) — it must stay
        # gone, not merely be joined by new text.
        assert "One host fault-injection command." not in field

    def test_superset_contract_consistent_with_kubectl(self):
        # Both EXECUTE-phase superset tools honestly advertise read coverage —
        # the isomorphism that makes the k8s side immune to this gap.
        assert "inspection" in kubectl.description
        assert "read-only" in host_inject.description

    # ② The capability the honest contract now advertises: the exact incident
    #    probe is admitted read-only in-phase (skip_guard=True), so the model CAN
    #    sample the primary effect while the fault is live.
    @pytest.mark.asyncio
    async def test_execute_phase_top_probe_skips_guard(self, spy):
        await host_inject.ainvoke({"command": "top -bn1"})
        assert spy.last["skip_guard"] is True

    # ③ Dead-end elimination: a pipe-blocked read-only probe returns actionable
    #    in-phase guidance, NOT the guard's pointer to the unbound host_read, and
    #    is never executed (the fast path stays a non-gate).
    @pytest.mark.asyncio
    async def test_piped_readonly_probe_gets_phase_correct_guidance(self, spy):
        out = await host_inject.ainvoke({"command": "top -bn1 | head -5"})
        assert out.startswith("Error: host_inject")
        assert "top -bn1" in out               # concrete single-command fix
        assert "do NOT need host_read" in out  # reachable in-phase alternative
        # the guard's dead-end wording must NOT leak into this phase's feedback
        assert "reach them through the host_read tool" not in out
        # guidance only — nothing was dispatched to the transport
        assert spy.calls == []

    # ③b The gate fires ONLY for a pure every-stage-read-only pipe; a mutating
    #     composition still falls through to ToolGuard (never re-cast as an
    #     observation, never whitewashing a mutating tail).
    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "cat /proc/loadavg | rm -rf /tmp/x",  # mutating tail behind a pipe
        "ps aux | tee /tmp/out",              # mutating stage (file write)
        "df -h; rm -rf /data",                # chain, not a pure pipe
        "cat /etc/passwd > /tmp/copy",        # file redirect
    ])
    async def test_mutating_composition_falls_through_to_guard(self, spy, command):
        await host_inject.ainvoke({"command": command})
        assert spy.last["skip_guard"] is False, command


class TestSkipGuardClassifierCannotDiverge:
    """Pin the provider docstring's single-source claim through BOTH real entry
    points.

    ``_classify_host_inject`` (provider.py L107-111) asserts its SCOPE_READONLY
    verdict is granted EXACTLY when ``host_cmd`` dispatches with ``skip_guard``
    — "the two layers cannot diverge". They reach that verdict by DIFFERENT
    predicates: host_cmd uses ``is_readonly_argv(argv) and not
    contains_shell_metachar(command)``; the classifier uses
    ``host_command_rejection_reason(command) is None``. The two are aligned by
    construction (``contains_shell_metachar`` is the same screen the rejection
    reason applies, and both reuse ``_classify_argv`` for binary/flag
    semantics), but nothing previously pinned them AGAINST EACH OTHER — a
    future one-sided tightening/loosening would silently split the dispatch-time
    guard skip from the target_guard READONLY pass. This nails them together at
    the two real call sites (host_inject dispatch + the classifier), so any
    divergence turns red.

    Corpus deliberately spans the read-only / mutating / metachar-composition
    classes already proven on each side independently. Pure read-only pipes
    (``top -bn1 | head``) are EXCLUDED: host_inject early-returns phase-correct
    guidance for those and never dispatches (covered by
    TestHostInjectObserveContract), so there is no skip_guard decision to
    compare.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        # read-only probes — both sides must agree: skip_guard=True / READONLY
        "df -h",
        "ps aux",
        "iptables -L -n",
        "find /etc -maxdepth 1",
        "curl -sI http://svc/health",
        "top -bn1",
        "free -m",
        "cat /proc/loadavg",
        "ss -ltn",
        "dd if=/dev/zero of=/dev/null",
        # mutating faults — both sides: skip_guard=False / scope=host
        "stress-ng --cpu 4 --timeout 60s",
        "kill -9 1234",
        "systemctl stop kubelet",
        "iptables -A INPUT -p tcp --dport 80 -j DROP",
        "tc qdisc add dev eth0 root netem delay 100ms",
        # metachar / mutating compositions — fall through to guard, never READONLY
        "df -h; rm -rf /data",
        "ps aux | tee /tmp/out",
        "cat /etc/passwd > /tmp/copy",
        "uptime && reboot",
        "echo $(cat /etc/shadow)",
    ])
    async def test_skip_guard_equals_classifier_readonly_verdict(self, spy, command):
        classifier_readonly = (
            _classify_host_inject({"command": command}, f"host_inject {command}").scope
            == SCOPE_READONLY
        )
        await host_inject.ainvoke({"command": command})
        # Every command here dispatches (no pure read-only pipe), so spy.last is
        # host_cmd's real skip_guard decision.
        assert spy.calls, command
        assert spy.last["skip_guard"] == classifier_readonly, (
            f"host_cmd skip_guard ({spy.last['skip_guard']}) diverges from the "
            f"target_guard READONLY verdict ({classifier_readonly}) for {command!r}"
        )

