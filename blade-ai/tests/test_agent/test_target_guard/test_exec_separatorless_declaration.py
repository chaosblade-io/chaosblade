"""R44 pins: the separator-less exec DECLARATION + attribution consumers.

The classifier's exec branch used to read every no-``--`` shape as "pure
stdio attach" (``scope=pod``, nothing rejected): the command written after
the pod name was never handed to any judge, so the declaration said "just
entering the pod" while the payload carried a command — and the read-only
phase screen's exemption for ``kubectl_read debug`` means the declaration is
the only guard some entry forms ever meet. The branch now consults the
shared walker (``tools._readonly_facts.exec_command_without_double_dash`` —
the same single source the tool-layer judge rides) and refuses with the
``--`` fix path (``SCOPE_UNKNOWN`` so the reason survives verbatim); a bare
entry keeps the attach scope, because vehicle identity is decided DATA-side
by the screener (state + live discovery), never from the pod name here.

Pinned alongside: the attribution proxy ``exec_inner_command_mutates``
(issue-time hook and verifier-side reverse scans — the shape must attribute
as MUTATING, not slip through as "not an injection") and the baseline
profile gate ``validate_command`` (its own ``--`` requirement plus the facts
judge behind it). All three consume the same facts judge, so a regression
in any one of them fails here.
"""

import pytest

from chaos_agent.agent.nodes.baseline._baseline_profiles import validate_command
from chaos_agent.agent.providers.k8s_native.classifier import _classify_kubectl_exec
from chaos_agent.agent.providers.message_scanning import exec_inner_command_mutates
from chaos_agent.agent.target_guard.types import SCOPE_READONLY, SCOPE_UNKNOWN
from chaos_agent.transports import PROFILE_HOST, PROFILE_K8S


def _classify(args, cmdline):
    return _classify_kubectl_exec(list(args), "kubectl exec", _cmdline_raw=cmdline)


class TestClassifierSeparatorlessCommandRefused:
    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            # raw text present: the facts face names the trailing command
            (["drill-pod", "rm", "-rf", "/tmp/px"], "drill-pod rm -rf /tmp/px"),
            (["drill-pod", "ls"], "drill-pod ls"),
            # synthetic arg shape, no raw text: the token walk still names it
            (["drill-pod", "cmd"], None),
        ],
    )
    def test_refused_with_a_reason(self, args, cmdline):
        target = _classify(args, cmdline)
        assert target.scope != "pod"
        assert target.reject_detail
        assert target.reject_suggestion

    def test_scope_is_unknown_so_the_reason_survives(self):
        # The declaration must not claim the pod: the command is what the
        # call would do, and it was never classified.
        target = _classify(
            ["drill-pod", "rm", "-rf", "/tmp/px"], "drill-pod rm -rf /tmp/px"
        )
        assert target.scope == SCOPE_UNKNOWN

    def test_reason_names_the_command(self):
        target = _classify(["drill-pod", "ls"], "drill-pod ls")
        assert "'ls'" in target.reject_detail


class TestClassifierAttachKept:
    """Bare entry / entry plus flags / empty payload stay ``scope=pod``."""

    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            (["drill-pod"], "drill-pod"),
            (["drill-pod", "-n", "default"], "drill-pod -n default"),
            (["-n", "default", "drill-pod"], "-n default drill-pod"),
            (["drill-pod", "-qc", "mycontainer"], "drill-pod -qc mycontainer"),
            (["drill-pod", "--"], "drill-pod --"),
        ],
    )
    def test_attach(self, args, cmdline):
        assert _classify(args, cmdline).scope == "pod"


class TestClassifierReadonlyProbesKept:
    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            # explicit separator, raw text: the facts judge rules
            (["drill-pod", "--", "cat", "/f"], "drill-pod -- cat /f"),
            # no raw text (token fallback): the escape probe stays admitted
            (
                ["drill-pod", "--", "sh", "-c", "chroot /host cat /etc/os-release"],
                None,
            ),
        ],
    )
    def test_readonly(self, args, cmdline):
        assert _classify(args, cmdline).scope == SCOPE_READONLY


class TestClassifierTokenPathGluedPipes:
    """The token fallback in its real consumer: glued pipes fail closed."""

    @pytest.mark.parametrize(
        "script",
        [
            "ps aux|rm",
            "chroot /host cat /f|rm",
        ],
    )
    def test_not_readonly(self, script):
        target = _classify(["drill-pod", "--", "sh", "-c", script], None)
        assert target.scope != SCOPE_READONLY


class TestAttributionProxy:
    """``exec_inner_command_mutates`` (issue-time + verifier reverse scans)."""

    def test_separatorless_command_attributes_as_mutating(self):
        # Pre-R44 this returned False: the entry-only reading said "read-only
        # probe", so a separator-less injection escaped attribution.
        assert exec_inner_command_mutates("kubectl exec pod cat /f") is True

    def test_bare_entry_is_not_an_injection(self):
        assert exec_inner_command_mutates("kubectl exec pod") is False

    def test_explicit_separator_probe_is_not_an_injection(self):
        assert exec_inner_command_mutates("kubectl exec pod -- cat /f") is False


class TestBaselineProfileGate:
    """``validate_command`` — its own ``--`` requirement + the facts judge."""

    def test_separatorless_command_refused_by_the_gate(self):
        assert validate_command("kubectl exec pod rm -rf /", PROFILE_K8S) is False

    def test_separatorless_entry_refused_by_the_gate(self):
        # A bare ``kubectl exec pod`` (no ``--``) is not a baseline probe:
        # every baseline exec needs the explicit separator.
        assert validate_command("kubectl exec pod", PROFILE_K8S) is False

    def test_explicit_separator_mutation_refused_by_the_judge(self):
        assert validate_command("kubectl exec pod -- rm -rf /", PROFILE_K8S) is False

    def test_explicit_separator_probe_admitted(self):
        assert validate_command("kubectl exec pod -- cat /etc/x", PROFILE_K8S) is True

    def test_host_profile_untouched(self):
        assert validate_command("cat /proc/cpuinfo", PROFILE_HOST) is True
