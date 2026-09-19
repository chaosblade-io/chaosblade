"""R45 pins: the ``--`` POSITION model at the declaration faces + carrier gate.

R44 made the separator-less ``POD COMMAND`` form refuse at every face. R45
asks WHERE the separator sits and what the stretch before it is worth:

  1. The classifier's legacy slice (``args.index("--")``) was blind to a
     ``--`` that a value-taking flag swallowed as its VALUE (pflag is
     value-FIRST: ``-c --`` / ``-n --`` / ``--container --``) and never asked
     what else sits before a TRUE boundary. The branch now walks with
     ``tools._readonly_facts.exec_separator_shape`` — the same single source
     the tool-layer judge and the host-carrier gate ride — so the faces cannot
     drift: a swallowed ``--`` routes into the R44 separator-less refusal, and
     extras before a true separator refuse as the R45 stray class
     (``SCOPE_UNKNOWN`` so the reason survives verbatim,
     ``_FIX_EXEC_ONE_ENTRY`` naming the one-entry shape).

  2. ``kubectl debug`` resolves EVERY positional as a separate target (v1.34:
     ``o.TargetNames = args[:argsLen]`` then ``ResourceNames("pods", ...)`` +
     a per-info Visit that creates a privileged pod per node / patches an
     ephemeral container per pod). Reading only the FIRST positional let
     extra names ride the approved one into the cluster — refused with
     ``_FIX_DEBUG_ONE_TARGET``, while a flag's value (``--image ubuntu``)
     never counts as a positional.

  3. The host carrier gate ``_parse_host_exec`` (whose outer scan took the
     first non-flag token as the pod and silently ignored the rest) refuses
     the extra-positional shape BEFORE the registered-pod / approved-node
     walk can bless an operation whose real target set is larger.

  4. R45-4 cascade (measured red on the pre-fix code, probe G): with a
     ``--`` in a flag's VALUE slot and a LATER true separator
     (``-c -- -- chroot /host iptables -F``), the content views sliced at
     the first standalone ``--`` — the classifier's escape peek, its
     segment scans and the carrier gate all started inside the flag value,
     so a real host escape was ruled ``scope=pod`` (the R26/G-10 failure
     mode: an identity match on the approved pod passes the whole chain)
     and a fault binary lost its ``fault_binary_mutation`` marker. All of
     them now ride ``exec_separator_index`` (pflag's value-FIRST locator),
     and the host-carrier detector keeps carrier treatment (fail closed)
     when every dash was swallowed.

Controls pin the no-over-refusal side across all three faces: single entry
plus flag values (glued AND space-separated), the live node-debug carrier
shape, and the R44 verdicts.
"""

import pytest

from chaos_agent.agent.providers.k8s_native.classifier import (
    _FIX_DEBUG_ONE_TARGET,
    _FIX_EXEC_ONE_ENTRY,
    _classify_kubectl_debug,
    _classify_kubectl_exec,
)
from chaos_agent.agent.target_guard.carriers import (
    _parse_host_exec,
    is_host_carrier_call,
)
from chaos_agent.agent.target_guard.types import (
    SCOPE_ESCAPE,
    SCOPE_READONLY,
    SCOPE_UNKNOWN,
)
from chaos_agent.tools._readonly_facts import exec_separator_shape


def _exec(args, cmdline):
    return _classify_kubectl_exec(list(args), "kubectl exec", _cmdline_raw=cmdline)


def _debug(args):
    return _classify_kubectl_debug(list(args), "kubectl debug")


class TestExecClassifierStrayTokensRefused:
    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            # raw text present: the strays are the v1.11 command head
            (
                ["drill-pod", "rm", "/data/x", "--", "cat", "/f"],
                "drill-pod rm /data/x -- cat /f",
            ),
            # synthetic arg shape, no raw text: the token walk still names them
            (["drill-pod", "ghost", "--", "cat", "/f"], None),
            (["drill-pod", "-n", "default", "ghost", "--", "cat", "/f"], None),
            # value-slot ``--`` (``-c`` eats it): no true separator, so this is
            # the R44 separator-less form whose command the deprecated path runs
            (
                ["drill-pod", "rm", "/data/x", "-c", "--", "cat"],
                "drill-pod rm /data/x -c -- cat",
            ),
        ],
    )
    def test_refused_with_a_reason(self, args, cmdline):
        target = _exec(args, cmdline)
        assert target.scope == SCOPE_UNKNOWN
        assert target.reject_detail
        assert target.reject_suggestion

    def test_strays_are_named_and_the_fix_shape_is_quoted(self):
        target = _exec(
            ["drill-pod", "rm", "/data/x", "--", "cat", "/f"],
            "drill-pod rm /data/x -- cat /f",
        )
        assert "'rm /data/x'" in target.reject_detail
        assert target.reject_suggestion == _FIX_EXEC_ONE_ENTRY


class TestExecClassifierAttachAndReadonlyKept:
    """Single entry / entry plus flag values -> unchanged verdicts (R44 parity)."""

    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            (["drill-pod"], "drill-pod"),
            (["drill-pod", "--"], "drill-pod --"),
            (["drill-pod", "-qc", "mycontainer"], "drill-pod -qc mycontainer"),
        ],
    )
    def test_attach(self, args, cmdline):
        assert _exec(args, cmdline).scope == "pod"

    @pytest.mark.parametrize(
        ("args", "cmdline"),
        [
            (["drill-pod", "--", "cat", "/f"], "drill-pod -- cat /f"),
            (["drill-pod", "-c", "c1", "--", "cat", "/f"], "drill-pod -c c1 -- cat /f"),
            (
                ["drill-pod", "-n", "default", "--", "ls", "-l", "/f"],
                "drill-pod -n default -- ls -l /f",
            ),
        ],
    )
    def test_readonly(self, args, cmdline):
        assert _exec(args, cmdline).scope == SCOPE_READONLY


class TestDebugClassifierExtraTargetsRefused:
    @pytest.mark.parametrize(
        "args",
        [
            ["drill-pod", "rogue-pod", "--", "cat", "/f"],
            # no separator: still two targets (the second is what runs)
            ["drill-pod", "rogue-pod"],
            # a flag's space-separated VALUE is not a positional
            ["drill-pod", "--image", "busybox", "rogue"],
            # node targets count the same way
            ["node/n1", "node/n2", "-it", "--image=ubuntu", "--", "cat", "/f"],
        ],
    )
    def test_refused_with_a_reason(self, args):
        target = _debug(args)
        assert target.scope == SCOPE_UNKNOWN
        assert target.reject_detail
        assert target.reject_suggestion == _FIX_DEBUG_ONE_TARGET

    def test_reason_names_the_extra_targets(self):
        target = _debug(["drill-pod", "rogue-pod", "--", "cat", "/f"])
        assert "'rogue-pod'" in target.reject_detail

    def test_reason_caps_a_long_target_list(self):
        target = _debug(["drill-pod"] + [f"p{i}" for i in range(10)] + ["--", "cat"])
        assert " ..." in target.reject_detail


class TestDebugClassifierSingleTargetKept:
    @pytest.mark.parametrize(
        ("args", "scope", "names"),
        [
            # the live node-debug carrier shape: space-separated flag values
            (
                ["node/n1", "-it", "--image=ubuntu", "--", "chroot", "/host", "bash"],
                "node",
                ("n1",),
            ),
            (["drill-pod", "--image=busybox"], "pod", ("drill-pod",)),
            (
                ["drill-pod", "--image", "busybox", "--profile", "sysadmin"],
                "pod",
                ("drill-pod",),
            ),
        ],
    )
    def test_single_target(self, args, scope, names):
        target = _debug(args)
        assert target.scope == scope
        assert tuple(target.names) == names


class TestHostCarrierGateRefusesExtraPositionals:
    @pytest.mark.parametrize(
        "v_args",
        [
            "debug-pod rogue-pod -n chaosblade -- chroot /host bash",
            "debug-pod rogue1 rogue2 -- nsenter -t 1 -m df",
            # the extra rides even when the approved entry form (registered
            # pod + host entry) is otherwise complete
            "debug-pod rogue-pod -- chroot /host bash",
            # plain exec form through a tool pod: same gate
            "tool-pod rogue-pod -n chaosblade -- chroot /host bash",
        ],
    )
    def test_not_a_host_exec(self, v_args):
        assert _parse_host_exec(v_args) is None

    @pytest.mark.parametrize(
        "v_args",
        [
            "debug-pod -n chaosblade -- chroot /host bash",
            "debug-pod -it --image ubuntu -n chaosblade -- chroot /host bash",
            "debug-pod -c c1 -n chaosblade -- nsenter -t 1 -m df",
        ],
    )
    def test_host_exec_kept(self, v_args):
        got = _parse_host_exec(v_args)
        assert got is not None
        pod, ns, _inner = got
        assert pod == "debug-pod"
        assert ns == "chaosblade"


class TestTheWalkIsTheSingleSourceAcrossFaces:
    """The faces read one answer — pinned so they cannot drift apart."""

    def test_debug_stray_line_refused_at_both_faces(self):
        from chaos_agent.tools.readonly import kubectl_exec_rejection_reason

        v_args = "kubectl debug drill-pod rogue-pod --image=busybox -- cat /f"
        assert kubectl_exec_rejection_reason(v_args) is not None
        target = _debug(["drill-pod", "rogue-pod", "--image=busybox", "--", "cat", "/f"])
        assert target.scope == SCOPE_UNKNOWN

    def test_shape_reports_the_strays_the_faces_read(self):
        before, after = exec_separator_shape(
            ["drill-pod", "rogue-pod", "--image=busybox", "--", "cat", "/f"]
        )
        assert before == ["drill-pod", "rogue-pod"]
        assert after == ["cat", "/f"]

    def test_swallowed_separator_reports_no_separator(self):
        before, after = exec_separator_shape(
            ["drill-pod", "rm", "/data/x", "-c", "--", "cat"]
        )
        assert before == ["drill-pod", "rm", "/data/x", "cat"]
        assert after is None


class TestValueSlotDashThenTrueSeparator:
    """R45-4: the value-slot ``--`` must not blind the content faces."""

    @pytest.mark.parametrize(
        "inner",
        [
            ["chroot", "/host", "iptables", "-F"],
            ["nsenter", "-t", "1", "-m", "rm", "-rf", "/x"],
        ],
    )
    def test_host_escape_seen_through_case_a(self, inner):
        target = _exec(
            ["drill-pod", "-c", "--", "--", *inner],
            "drill-pod -c -- -- " + " ".join(inner),
        )
        assert target.scope == SCOPE_ESCAPE

    def test_wrapped_escape_seen(self):
        target = _exec(
            ["drill-pod", "-c", "--", "--", "sh", "-c", "chroot /host iptables -F"],
            "drill-pod -c -- -- sh -c 'chroot /host iptables -F'",
        )
        assert target.scope == SCOPE_ESCAPE

    def test_fault_binary_still_marked(self):
        target = _exec(
            ["drill-pod", "-c", "--", "--", "iptables", "-A", "INPUT", "-j", "DROP"],
            "drill-pod -c -- -- iptables -A INPUT -j DROP",
        )
        assert target.fault_binary_mutation is True

    def test_readonly_payload_stays_readonly(self):
        target = _exec(
            ["drill-pod", "-c", "--", "--", "cat", "/f"],
            "drill-pod -c -- -- cat /f",
        )
        assert target.scope == SCOPE_READONLY


class TestCarrierGateValueAware:
    """R45-4 at the carrier gates: the TRUE separator, not the first dash."""

    def test_parse_sees_the_true_separator(self):
        assert _parse_host_exec("drill-pod -c -- -- chroot /host bash") == (
            "drill-pod",
            "",
            "chroot /host bash",
        )

    def test_parse_none_when_every_dash_was_swallowed(self):
        # No true separator -> the R44/R45-3 refusal owns the line; the gate
        # must not bless it as a host operation.
        assert _parse_host_exec("drill-pod -c -- chroot /host bash") is None

    @pytest.mark.parametrize(
        "v_args",
        [
            "drill-pod -c -- -- chroot /host bash",
            "drill-pod -n ns -- chroot /host bash",
            # swallowed-only: the deprecated path would run the entry's tail
            "drill-pod -c -- chroot /host bash",
        ],
    )
    def test_carrier_call_seen(self, v_args):
        assert is_host_carrier_call(
            "kubectl", {"subcommand": "exec", "v_args": v_args}
        ) is True

    @pytest.mark.parametrize(
        "v_args",
        ["drill-pod", "drill-pod rm /data/x -c -- cat"],
    )
    def test_not_a_carrier_call(self, v_args):
        assert is_host_carrier_call(
            "kubectl", {"subcommand": "exec", "v_args": v_args}
        ) is False


class TestSegmentViewStartsAtTheTrueSeparator:
    """``exec_command_segments`` — the segment view the classifier's escape /
    fault-binary scans and the blade receipt scans all ride (R45-4)."""

    def test_case_a_view_is_the_real_payload(self):
        from chaos_agent.agent.providers.message_scanning import exec_command_segments

        # The primitive-KEEPING projection pins the view's start: the value
        # slot ``--`` must not begin it (pre-fix the head was ``--``).
        segments = exec_command_segments(
            "kubectl exec drill-pod -c -- -- chroot /host cat /f",
            chroot_delegation=False,
        )
        assert segments == [["chroot", "/host", "cat", "/f"]]

    def test_swallowed_only_view_is_fail_closed_empty(self):
        from chaos_agent.agent.providers.message_scanning import exec_command_segments

        # No true separator: pre-fix this view read ``['cat']`` (the tail of
        # the swallowed dash), naming a command the line does not carry.
        assert exec_command_segments("kubectl exec drill-pod rm /data/x -c -- cat") == []
