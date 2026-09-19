"""R44 pins: the no-``--`` exec command shape and glued pipes (tools face).

Two refusals the read-only judge owes its five consumers, both measured red
on the pre-R44 code (probe: ``_r44_probe.py``):

  1. ``kubectl exec POD COMMAND`` (no ``--``) — kubectl v1.34.1 refuses the
     separator-less command form outright ("exec [POD] [COMMAND] is not
     supported anymore. Use exec [POD] -- [COMMAND] instead"), but that
     client-side refusal is not this judge's guarantee: the entry-only
     reading blessed the shape as a read-only probe while the command rode
     unclassified (the facts face returned ``None`` and every consumer read
     that as "bare entry"). The shared walker
     ``_readonly_facts.exec_command_without_double_dash`` now names the
     trailing command — flags skipped with their values, pflag's
     interspersed model, short clusters resolved through
     ``KUBECTL_VALUE_SHORTHANDS`` — and the judge refuses it with the ``--``
     fix path. A bare entry / entry plus flags stays read-only: attach runs
     nothing at all.

  2. Glued pipes (``ps aux|rm``) at the token layer — shlex glues the pipe
     to the adjacent word, the entry test ``"|" in tokens`` is a membership
     test that a glued token never satisfies, and the tail stage then rode
     as an ARGUMENT of the head stage (``ps`` with an odd argument) and was
     admitted — while the facts engine refused the identical payload. The
     branch now enters on the JOINED text and expands glued pipes into
     separator tokens (after the control-operator scan, which must still
     see ``||`` as a substring); a pipe with no command on one side
     (``cat /f |``) is refused as the shell syntax error the facts engine
     reports.

The facts face (raw text exists) is the exact judge; the token face is the
fallback for synthetic arg shapes with no raw text. Both are pinned here,
plus the controls that must stay admitted (read-only pipelines, escape
probes, attach shapes) and the pinned token-layer over-refusal (a quoted
``a|b`` literal is indistinguishable from a glued operator once quotes are
gone — the split over-denies there by design, fail-closed).
"""

import pytest

from chaos_agent.tools.readonly import (
    kubectl_exec_rejection_reason,
    readonly_inner_tokens_reason,
)

SEP = "kubectl exec drill-pod -n default -- "


class TestFactsFaceNoSeparatorCommandRefused:
    """``POD [flags] COMMAND`` without ``--`` carries a command -> refused."""

    @pytest.mark.parametrize(
        "v_args",
        [
            "kubectl exec drill-pod rm -rf /tmp/px",
            "kubectl exec drill-pod -n default rm -rf /tmp/px",
            "kubectl exec drill-pod sh -c 'ps aux|rm'",
            "kubectl exec drill-pod cat /etc/x",
            "kubectl exec drill-pod -c mycontainer rm -rf /tmp/px",
            "kubectl exec drill-pod ls",
            "kubectl debug drill-pod --image=busybox rm /tmp/px",
            # short-flag cluster: the ``c`` of ``-qc`` takes the next token
            "kubectl exec drill-pod -qc mycontainer rm /tmp/px",
            # global-flag prefixes on either side of the subcommand
            "kubectl -n default exec drill-pod rm /tmp/px",
            "kubectl exec -n default drill-pod rm /tmp/px",
            "kubectl exec drill-pod --context=prod rm /tmp/px",
            # valueless flags do not swallow the command
            "kubectl exec drill-pod -it rm /tmp/px",
            "kubectl exec drill-pod /bin/rm /tmp/x",
            "kubectl debug drill-pod --env FOO=bar rm /tmp/x",
            # ``-`` is an operand, not a flag (getopt) — a command it is
            "kubectl exec drill-pod -",
        ],
    )
    def test_refused(self, v_args):
        assert kubectl_exec_rejection_reason(v_args) is not None

    def test_reason_names_the_command_and_the_fix_path(self):
        reason = kubectl_exec_rejection_reason("kubectl exec drill-pod ls")
        assert reason is not None
        assert "'ls'" in reason
        assert "--" in reason

    def test_reason_caps_a_long_command(self):
        reason = kubectl_exec_rejection_reason(
            "kubectl exec drill-pod rm a b c d e f g h i j"
        )
        assert reason is not None
        assert " ..." in reason


class TestFactsFaceBareEntryKept:
    """Entry-only shapes run nothing -> stay read-only (no over-refusal)."""

    @pytest.mark.parametrize(
        "v_args",
        [
            "kubectl exec drill-pod",
            "kubectl exec drill-pod -n default",
            "kubectl exec drill-pod -it",
            "kubectl exec drill-pod -c mycontainer -n default",
            "kubectl exec drill-pod -i -t",
            "kubectl exec drill-pod -qc mycontainer",
            "kubectl exec drill-pod --container mycontainer",
            # an unknown long option reads as valueless (fail-closed walk);
            # with no positional after the pod slot there is no command
            "kubectl exec drill-pod --foo",
            "kubectl debug drill-pod --image=busybox",
            "kubectl debug drill-pod --image busybox -n default",
            "kubectl debug drill-pod --image busybox --profile sysadmin",
            "kubectl -n default exec drill-pod",
            "kubectl --context prod exec drill-pod -n default",
            # the separator with nothing after it is still a bare entry
            "kubectl exec drill-pod --",
            # explicit-separator command shapes keep their verdicts
            "kubectl exec drill-pod -- cat /f",
            "kubectl exec drill-pod -- ls -l /f",
            "kubectl exec drill-pod -- ps aux | grep x",
        ],
    )
    def test_admitted(self, v_args):
        assert kubectl_exec_rejection_reason(v_args) is None


class TestTokenFaceGluedAndDegeneratePipesRefused:
    """Token fallback: glued / nested / degenerate pipes must fail closed."""

    @pytest.mark.parametrize(
        "inner",
        [
            ["sh", "-c", "ps aux|rm"],
            ["sh", "-c", "cat /etc/hostname|rm -rf /tmp/px"],
            ["sh", "-c", "cat /etc/hostname |rm"],
            ["sh", "-c", "cat /etc/hostname| rm"],
            ["sh", "-c", "ps aux|sh"],
            ["sh", "-c", "id|rm"],
            # glued pipes behind wrappers / escape primitives
            ["sh", "-c", "chroot /host cat /f|rm"],
            ["sh", "-c", "timeout 5 cat /f|rm"],
            ["sh", "-c", "nsenter -t 1 -m cat /f|rm"],
            ["chroot", "/host", "cat", "/f|rm"],
            ["timeout", "5", "cat", "/f|rm"],
            ["env", "cat", "/f|rm"],
            # degenerate stages: the facts engine reports a shell syntax
            # error for these; the token layer must not skip the empty side
            ["cat", "/f", "|"],
            ["|", "df"],
            ["cat", "|", "|", "df"],
            ["|"],
            ["sh", "-c", "cat /f |"],
            ["sh", "-c", "|df"],
        ],
    )
    def test_refused(self, inner):
        assert readonly_inner_tokens_reason(inner) is not None

    def test_reason_names_the_bare_pipe(self):
        reason = readonly_inner_tokens_reason(["cat", "/f", "|"])
        assert reason is not None
        assert "'|'" in reason


class TestTokenFaceReadonlyControlsKept:
    """Read-only pipelines / wrappers / escape probes keep their verdicts."""

    @pytest.mark.parametrize(
        "inner",
        [
            ["sh", "-c", "ps aux|grep nginx"],
            ["sh", "-c", "ps aux | grep nginx"],
            ["sh", "-c", "cat /f"],
            ["cat", "/etc/hostname"],
            ["ps", "aux"],
            ["chroot", "/host", "cat", "/etc/os-release"],
            ["sh", "-c", "chroot /host cat /etc/os-release"],
            ["sh", "-c", "ps aux | grep nginx | head -5"],
            ["sh", "-c", "cat /proc/diskstats | grep vda"],
            ["timeout", "5", "df", "-h"],
            ["nsenter", "-t", "1", "-m", "--", "df", "-h"],
        ],
    )
    def test_admitted(self, inner):
        assert readonly_inner_tokens_reason(inner) is None


class TestTokenLayerPinnedBoundaries:
    """Order dependencies and the by-design over-refusal, pinned."""

    def test_quoted_pipe_literal_over_denies_at_token_layer(self):
        # Quote information is gone at this layer, so ``a|b`` (an awk/grep
        # program literal in the real argv) is indistinguishable from a
        # glued operator. Splitting can only ADD stages, each of which must
        # pass the read-only judge — the denial is fail-closed, by design.
        assert readonly_inner_tokens_reason(["grep", "-E", "a|b", "/f"]) is not None

    def test_facts_face_is_the_exact_judge_for_the_same_literal(self):
        assert kubectl_exec_rejection_reason(SEP + "grep -E 'a|b' /f") is None

    def test_control_operator_scan_wins_over_the_pipe_split(self):
        # ``||`` must be caught as a control operator BEFORE the glued-pipe
        # expansion runs (expanding first would rewrite it to ``| |`` and
        # hide it from the substring scan).
        reason = readonly_inner_tokens_reason(["sh", "-c", "echo a||b"])
        assert reason is not None
        assert "control operator" in reason

    @pytest.mark.parametrize(
        "inner",
        [
            ["ps", "aux", "||", "rm", "x"],
            ["sh", "-c", "echo a&&rm b"],
        ],
    )
    def test_chain_separators_still_refused(self, inner):
        assert readonly_inner_tokens_reason(inner) is not None
